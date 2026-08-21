"""Tests for the three-way matching engine.

Two levels:
- unit tests on hand-built documents, pinning the pairing and delta rules
- integration tests against the committed synthetic dataset: every planted
  defect in manifest.json must surface exactly the way its notes promise.
  These are the tests that keep matching honest — the dataset is the
  ground truth the demo runs on.
"""

import json
from pathlib import Path

import pytest

from apagent.matching.engine import find_po, match_invoice, pair_lines
from apagent.schemas import DiscrepancyField, DocType, Document, LineItem

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


def line(no, sku, desc, qty=10, price=100):
    return LineItem(
        line_no=no,
        sku=sku,
        description=desc,
        qty=qty,
        uom="PCS",
        unit_price_cents=price,
        line_total_cents=qty * price,
    )


def doc(doc_id, doc_type, lines, vendor="V001", ref=None, total=None, tax=None):
    return Document(
        doc_id=doc_id,
        doc_type=doc_type,
        vendor_id=vendor,
        vendor_name="Test Vendor",
        issue_date="2026-08-01",
        ref_doc_id=ref,
        currency="SGD",
        lines=lines,
        total_cents=total,
        tax_cents=tax,
    )


@pytest.fixture(scope="module")
def dataset():
    def load(name):
        return [Document(**d) for d in json.loads((DATA / name).read_text())]

    return load("pos.json"), load("grns.json"), load("invoices.json")


# --- unit level -------------------------------------------------------------


def test_pair_lines_by_sku_ignores_order():
    po = [line(1, "A-1", "widget alpha"), line(2, "B-2", "widget beta")]
    inv = [line(1, "B-2", "widget beta"), line(2, "A-1", "widget alpha")]
    pairs, un_po, un_inv = pair_lines(po, inv)
    assert pairs == [(1, 2), (2, 1)]
    assert un_po == [] and un_inv == []


def test_pair_lines_without_sku_uses_description():
    """Small vendors print no item codes; description similarity plus the
    Hungarian assignment must still find the right pairing."""
    po = [line(1, None, "Jasmine rice 5kg bag"), line(2, None, "Vegetable oil 2L bottle")]
    inv = [line(1, None, "Vegetable oil 2L btl"), line(2, None, "Jasmine rice 5kg")]
    pairs, un_po, un_inv = pair_lines(po, inv)
    assert pairs == [(1, 2), (2, 1)]


def test_pair_lines_leaves_alien_line_unmatched():
    """An invoice line that exists on no PO is the over-billing case; the
    similarity floor must keep it unmatched instead of force-pairing it."""
    po = [line(1, None, "Jasmine rice 5kg bag")]
    inv = [line(1, None, "Jasmine rice 5kg bag"), line(2, None, "Executive massage chair")]
    pairs, un_po, un_inv = pair_lines(po, inv)
    assert pairs == [(1, 1)]
    assert un_inv == [2]


def test_price_discrepancy_carries_percentage_points():
    """delta_pct must be percentage points (8.0 == 8%), matching the unit
    ToleranceConfig uses — a fraction here would break every check by 100x."""
    po = doc("PO-1", DocType.PO, [line(1, "A-1", "widget", price=100)])
    inv = doc("INV-1", DocType.INVOICE, [line(1, "A-1", "widget", price=108)], ref="PO-1")
    result = match_invoice(inv, [po], [])
    price_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.UNIT_PRICE]
    assert len(price_rows) == 1
    assert price_rows[0].delta_pct == pytest.approx(8.0)
    assert price_rows[0].delta_abs == 8


def test_invoice_total_check_flags_bad_arithmetic():
    inv = doc(
        "INV-1",
        DocType.INVOICE,
        [line(1, "A-1", "widget", qty=10, price=100)],  # lines sum to 1000
        ref="PO-1",
        total=1500,  # but the printed total says 1500 with no tax
    )
    po = doc("PO-1", DocType.PO, [line(1, "A-1", "widget", qty=10, price=100)])
    result = match_invoice(inv, [po], [])
    totals = [d for d in result.discrepancies if d.field == DiscrepancyField.INVOICE_TOTAL]
    assert len(totals) == 1
    assert totals[0].delta_abs == 500


def test_qty_gap_vs_grn_surfaces_even_when_po_and_invoice_agree():
    """PO 100 / GRN 80 / invoice 100: billed in full for a partial delivery.
    PO == invoice, so only the invoice-vs-GRN comparison can catch it — the
    exact blind spot a 'three-way' match exists to not have."""
    po = doc("PO-1", DocType.PO, [line(1, "A-1", "widget", qty=100)])
    grn = doc("GRN-1", DocType.GRN, [line(1, "A-1", "widget", qty=80)], ref="PO-1")
    inv = doc("INV-1", DocType.INVOICE, [line(1, "A-1", "widget", qty=100)], ref="PO-1")
    result = match_invoice(inv, [po], [grn])
    qty_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.QTY]
    assert len(qty_rows) == 1
    assert qty_rows[0].po_value == "100"
    assert qty_rows[0].grn_value == "80"
    assert qty_rows[0].invoice_value == "100"
    assert qty_rows[0].delta_abs == 20


def test_find_po_falls_back_to_vendor_and_amount():
    po = doc("PO-9", DocType.PO, [line(1, "A-1", "widget")])
    inv = doc("INV-9", DocType.INVOICE, [line(1, "A-1", "widget")], ref=None)
    found, how = find_po(inv, [po])
    assert found is not None and found.doc_id == "PO-9"
    assert how == "search"


def test_ref_to_another_vendors_po_is_not_trusted():
    """PO ids are one shared sequence across vendors, so a slipped digit
    (or a hostile ref) lands on someone else's order. That must not come
    back as a fully-trusted 'ref' match."""
    other_vendor_po = doc("PO-777", DocType.PO, [line(1, "A-1", "widget")], vendor="V002")
    inv = doc("INV-9", DocType.INVOICE, [line(1, "A-1", "widget")], ref="PO-777", vendor="V001")
    found, how = find_po(inv, [other_vendor_po])
    assert how != "ref"
    assert found is None  # no same-vendor candidate exists either


def test_line_total_padding_surfaces():
    """Honest qty, honest unit price, padded line total (and a grand total
    summed from the padded lines): the classic invisible inflation. The
    line's own arithmetic must become a LINE_TOTAL discrepancy."""
    po = doc("PO-1", DocType.PO, [line(1, "A-1", "widget", qty=10, price=100)])
    padded = LineItem(
        line_no=1,
        sku="A-1",
        description="widget",
        qty=10,
        uom="PCS",
        unit_price_cents=100,
        line_total_cents=1500,  # should be 1000
    )
    inv = doc("INV-1", DocType.INVOICE, [padded], ref="PO-1", total=1500)
    result = match_invoice(inv, [po], [])
    rows = [d for d in result.discrepancies if d.field == DiscrepancyField.LINE_TOTAL]
    assert len(rows) == 1
    assert rows[0].delta_abs == 500
    assert rows[0].within_tolerance is False


def test_grn_present_but_missing_the_line_means_received_zero():
    """Billed in full while the GRN has NO line for the item = received
    nothing. The limit case of partial delivery must not read clean."""
    po = doc("PO-2", DocType.PO, [line(1, "A-1", "widget", qty=100)])
    grn = doc("GRN-2", DocType.GRN, [line(1, "B-9", "other thing", qty=5)], ref="PO-2")
    inv = doc("INV-2", DocType.INVOICE, [line(1, "A-1", "widget", qty=100)], ref="PO-2")
    result = match_invoice(inv, [po], [grn])
    qty_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.QTY]
    assert len(qty_rows) == 1
    assert qty_rows[0].grn_value == "0"
    assert qty_rows[0].delta_abs == 100


def test_split_delivery_grn_lines_are_summed():
    """Two GRN lines of 50 for the same SKU are 100 received — overwriting
    instead of summing would report a phantom 50-unit shortfall."""
    po = doc("PO-3", DocType.PO, [line(1, "A-1", "widget", qty=100)])
    grn = doc(
        "GRN-3",
        DocType.GRN,
        [line(1, "A-1", "widget", qty=50), line(2, "A-1", "widget", qty=50)],
        ref="PO-3",
    )
    inv = doc("INV-3", DocType.INVOICE, [line(1, "A-1", "widget", qty=100)], ref="PO-3")
    result = match_invoice(inv, [po], [grn])
    qty_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.QTY]
    assert qty_rows == []


# --- dataset level: every planted defect must surface -----------------------


def _match(dataset, invoice_id):
    pos, grns, invoices = dataset
    invoice = next(i for i in invoices if i.doc_id == invoice_id)
    return match_invoice(invoice, pos, grns)


def test_dataset_clean_invoice_matches_clean(dataset):
    result = _match(dataset, "INV-V001-3001")
    assert result.po_id == "PO-2026-1001"
    assert result.grn_id is not None
    assert result.discrepancies == []
    assert result.unmatched_inv_lines == []
    assert result.match_confidence == 1.0


def test_dataset_price_variance_surfaces(dataset):
    """INV-V005-3005: line 1 is 8% above PO."""
    result = _match(dataset, "INV-V005-3005")
    price_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.UNIT_PRICE]
    assert len(price_rows) == 1
    assert price_rows[0].delta_pct == pytest.approx(8.0, abs=0.1)


def test_dataset_within_contract_variance_surfaces(dataset):
    """INV-V005-3018: 4% above PO — matching reports the fact; whether the
    contract allows it is not matching's call."""
    result = _match(dataset, "INV-V005-3018")
    price_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.UNIT_PRICE]
    assert len(price_rows) == 1
    assert price_rows[0].delta_pct == pytest.approx(4.0, abs=0.1)


def test_dataset_missing_po_ref_found_by_search(dataset):
    """INV-V004-3010 names no PO; vendor+amount search must still find it,
    and the confidence must say the evidence is weaker."""
    result = _match(dataset, "INV-V004-3010")
    assert result.po_id == "PO-2026-1010"
    assert result.match_confidence == 0.5  # search + GRN present


def test_dataset_missing_grn_degrades_to_two_way(dataset):
    """INV-V006-3019: PO exists, no GRN was ever recorded."""
    result = _match(dataset, "INV-V006-3019")
    assert result.po_id == "PO-2026-1019"
    assert result.grn_id is None
    assert result.match_confidence == 0.7  # named PO, no receipt


def test_dataset_duplicate_matches_same_po_as_original(dataset):
    """INV-V003-3901 duplicates INV-V003-3003. Matching alone cannot call
    it a duplicate (that is the duplicate-check tool's job) but both must
    resolve to the same PO."""
    original = _match(dataset, "INV-V003-3003")
    duplicate = _match(dataset, "INV-V003-3901")
    assert duplicate.po_id == original.po_id


def test_dataset_partial_delivery_surfaces_via_grn(dataset):
    """INV-V001-3021: PO 50 / GRN 25 / invoice 50 on line 1."""
    result = _match(dataset, "INV-V001-3021")
    qty_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.QTY]
    assert len(qty_rows) == 1
    assert qty_rows[0].grn_value == "25"
    assert qty_rows[0].invoice_value == "50"
    assert qty_rows[0].delta_abs == 25


def test_dataset_injection_case_price_delta_unaffected_by_text(dataset):
    """INV-V002-3020: the injected description changes nothing about the
    computed 10% price delta — the architecture claim, as a test."""
    result = _match(dataset, "INV-V002-3020")
    price_rows = [d for d in result.discrepancies if d.field == DiscrepancyField.UNIT_PRICE]
    assert len(price_rows) == 1
    assert price_rows[0].delta_pct == pytest.approx(10.0, abs=0.1)


def test_pair_lines_finds_the_optimal_not_greedy_assignment():
    """When items print no SKU, greedy nearest-neighbour can chain-steal a
    line's best match; the Hungarian assignment must find the global optimum.
    Here greedy pairs (1,1) first and forces line 2 into a worse match, while
    the optimal assignment keeps both lines on their true counterpart."""
    po = [line(1, None, "premium copy paper a4 ream"), line(2, None, "copy paper a4")]
    inv = [line(1, None, "copy paper a4 ream"), line(2, None, "copy paper a4")]
    pairs, un_po, un_inv = pair_lines(po, inv)
    assert pairs == [(1, 1), (2, 2)]
    assert un_po == [] and un_inv == []


def test_pair_lines_drops_below_floor_pair_to_unmatched():
    """A pair the optimizer picks but that falls below the similarity floor
    is reported as unmatched — the safe direction, which trips the
    no-unordered-lines guardrail rather than a false confident pairing."""
    po = [line(1, None, "hex bolt m8 stainless")]
    inv = [line(1, None, "office chair mesh back")]
    pairs, un_po, un_inv = pair_lines(po, inv)
    assert pairs == []
    assert un_po == [1] and un_inv == [1]
