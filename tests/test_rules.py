"""Tests for the tolerance rules layer.

The one behavior that must never regress: the same 4% price variance is
out of tolerance under the default config and WITHIN tolerance under
V005's contractual override. That flip is the demo's headline case, and
rules is the layer that makes it happen.
"""

import json
from pathlib import Path

from apagent.matching.engine import match_invoice
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.schemas import (
    Discrepancy,
    DiscrepancyField,
    Document,
    MatchResult,
    ToleranceConfig,
)

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"

DEFAULT = ToleranceConfig()  # 2% price, $5/1% total, qty exact, $5k review gate

# What the system looks like once V005's contract clause (5% price
# variance) has been recorded as an override.
WITH_V005_OVERRIDE = ToleranceConfig(
    per_vendor_overrides={"V005": ToleranceConfig(unit_price_pct=5.0)}
)


def _match(invoice_id):
    def load(name):
        return [Document(**d) for d in json.loads((DATA / name).read_text(encoding="utf-8"))]

    pos, grns, invoices = load("pos.json"), load("grns.json"), load("invoices.json")
    invoice = next(i for i in invoices if i.doc_id == invoice_id)
    return match_invoice(invoice, pos, grns)


def _price_rows(match):
    return [d for d in match.discrepancies if d.field == DiscrepancyField.UNIT_PRICE]


def test_resolve_config_falls_back_to_base():
    assert resolve_config("V001", WITH_V005_OVERRIDE).unit_price_pct == 2.0
    assert resolve_config("V005", WITH_V005_OVERRIDE).unit_price_pct == 5.0


def test_headline_case_flips_with_the_contract_override():
    """INV-V005-3018 (4% variance): out of tolerance by default, within
    tolerance once the contract's 5% is applied."""
    match = _match("INV-V005-3018")

    by_default = apply_tolerances(match, resolve_config("V005", DEFAULT))
    assert _price_rows(by_default)[0].within_tolerance is False

    by_contract = apply_tolerances(match, resolve_config("V005", WITH_V005_OVERRIDE))
    assert _price_rows(by_contract)[0].within_tolerance is True


def test_8pct_variance_is_out_even_under_the_override():
    """INV-V005-3005 (8%): beyond even the contractual 5% — the override
    must not turn into a blank cheque."""
    match = _match("INV-V005-3005")
    checked = apply_tolerances(match, resolve_config("V005", WITH_V005_OVERRIDE))
    assert _price_rows(checked)[0].within_tolerance is False


def test_apply_tolerances_does_not_mutate_the_original():
    """The raw match is evidence; apply_tolerances must return a copy."""
    match = _match("INV-V005-3018")
    apply_tolerances(match, resolve_config("V005", WITH_V005_OVERRIDE))
    assert all(d.within_tolerance is False for d in match.discrepancies)


def test_manual_review_gate():
    assert requires_manual_review(500_000, DEFAULT) is True  # at threshold: review
    assert requires_manual_review(499_999, DEFAULT) is False
    assert requires_manual_review(None, DEFAULT) is True  # unbounded exposure


def test_clean_invoice_stays_clean():
    match = _match("INV-V001-3001")
    checked = apply_tolerances(match, resolve_config("V001", DEFAULT))
    assert checked.discrepancies == []


# --- _check, field by field ----------------------------------------------
# A review mutation experiment showed the old suite passed with the QTY
# check fully inverted and every <= flipped to <. These pin each branch,
# including the exact boundary, so that can never happen silently again.


def _row(field, delta_abs=None, delta_pct=None):
    return Discrepancy(
        line_pair=(1, 1),
        field=field,
        po_value="100",
        grn_value=None,
        invoice_value="104",
        delta_abs=delta_abs,
        delta_pct=delta_pct,
        within_tolerance=False,
    )


def _checked(row, config=DEFAULT):
    match = MatchResult(
        invoice_id="INV-X",
        po_id="PO-X",
        grn_id=None,
        line_pairs=[(1, 1)],
        unmatched_po_lines=[],
        unmatched_inv_lines=[],
        discrepancies=[row],
        match_confidence=1.0,
    )
    return apply_tolerances(match, config).discrepancies[0].within_tolerance


def test_check_qty_is_never_within_tolerance_when_exact():
    assert _checked(_row(DiscrepancyField.QTY, delta_abs=1, delta_pct=1.0)) is False


def test_check_unit_price_boundary_is_inclusive():
    """Exactly at the limit is WITHIN (<=, not <): 2.0% under a 2.0% limit."""
    assert _checked(_row(DiscrepancyField.UNIT_PRICE, delta_abs=2, delta_pct=2.0)) is True
    assert _checked(_row(DiscrepancyField.UNIT_PRICE, delta_abs=2, delta_pct=2.01)) is False
    # delta_pct None (zero base) must fail conservative, not pass
    assert _checked(_row(DiscrepancyField.UNIT_PRICE, delta_abs=2, delta_pct=None)) is False


def test_check_invoice_total_requires_both_bounds():
    """abs within + pct within -> in; either alone -> out."""
    both = _row(DiscrepancyField.INVOICE_TOTAL, delta_abs=400, delta_pct=0.5)
    abs_only = _row(DiscrepancyField.INVOICE_TOTAL, delta_abs=400, delta_pct=1.5)
    pct_only = _row(DiscrepancyField.INVOICE_TOTAL, delta_abs=600, delta_pct=0.5)
    assert _checked(both) is True
    assert _checked(abs_only) is False
    assert _checked(pct_only) is False


def test_check_uom_and_line_total_have_no_tolerance():
    assert _checked(_row(DiscrepancyField.UOM)) is False
    assert _checked(_row(DiscrepancyField.LINE_TOTAL, delta_abs=1, delta_pct=0.1)) is False
