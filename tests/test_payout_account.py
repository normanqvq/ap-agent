from pathlib import Path

from apagent.api.service import Service, _reason_label
from apagent.pipeline import decide_invoice_rules_only
from apagent.schemas import Action, DocType, Document, LineItem, Vendor
from apagent.store import DocumentStore


def _line():
    return LineItem(
        line_no=1,
        sku="SKU-1",
        description="widget",
        qty=10,
        uom="PCS",
        unit_price_cents=100,
        line_total_cents=1000,
    )


def test_document_carries_payout_account_defaulting_none():
    doc = Document(
        doc_id="INV1",
        doc_type=DocType.INVOICE,
        vendor_id="V1",
        vendor_name="Acme",
        issue_date="2026-01-01",
        ref_doc_id="PO1",
        currency="SGD",
        lines=[_line()],
    )
    assert doc.payout_account is None
    doc2 = doc.model_copy(update={"payout_account": "SG12 3456"})
    assert doc2.payout_account == "SG12 3456"


def test_vendor_master_model():
    v = Vendor(vendor_id="V1", vendor_name="Acme", payout_account="SG12 3456")
    assert v.vendor_id == "V1"
    assert v.payout_account == "SG12 3456"
    assert Vendor(vendor_id="V2", vendor_name="B").payout_account is None


def _po():
    return Document(
        doc_id="PO1",
        doc_type=DocType.PO,
        vendor_id="V1",
        vendor_name="Acme",
        issue_date="2026-01-01",
        ref_doc_id=None,
        currency="SGD",
        lines=[_line()],
    )


def test_store_returns_vendor_account_and_none_when_absent():
    store = DocumentStore([_po()], [], [], {"V1": "SG12 3456"})
    assert store.vendor_account("V1") == "SG12 3456"
    assert store.vendor_account("V-unknown") is None


def test_store_without_accounts_returns_none():
    store = DocumentStore([_po()], [], [])
    assert store.vendor_account("V1") is None


def _triple(invoice_account, master_account):
    """A clean-matching PO/GRN/invoice and a store; only the account varies."""
    line = _line()
    po = Document(
        doc_id="PO1",
        doc_type=DocType.PO,
        vendor_id="V1",
        vendor_name="Acme",
        issue_date="2026-01-01",
        ref_doc_id=None,
        currency="SGD",
        lines=[line],
    )
    grn = Document(
        doc_id="GRN1",
        doc_type=DocType.GRN,
        vendor_id="V1",
        vendor_name="Acme",
        issue_date="2026-01-02",
        ref_doc_id="PO1",
        currency="SGD",
        lines=[line],
    )
    inv = Document(
        doc_id="INV1",
        doc_type=DocType.INVOICE,
        vendor_id="V1",
        vendor_name="Acme",
        issue_date="2026-01-02",
        ref_doc_id="PO1",
        currency="SGD",
        lines=[line],
        total_cents=1000,
        payout_account=invoice_account,
    )
    accounts = {"V1": master_account} if master_account is not None else {}
    return DocumentStore([po], [grn], [inv], accounts), inv


def test_mismatch_escalates():
    store, inv = _triple("SG99 8888 7777 6666", "SG12 3456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.ESCALATE
    assert "payout account" in dec.reasoning.lower()


def test_match_stays_approve():
    store, inv = _triple("SG12 3456 7890 1234", "SG12 3456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE


def test_spacing_and_case_ignored():
    store, inv = _triple("sg12 34567890 1234", "SG123456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE


def test_invoice_without_account_passes():
    store, inv = _triple(None, "SG12 3456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE


def test_no_master_account_passes():
    store, inv = _triple("SG99 8888 7777 6666", None)
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE


def test_demo_bankswap_escalates_from_committed_dataset():
    data_dir = Path(__file__).resolve().parents[1] / "data" / "synthetic"
    store = DocumentStore.from_dir(data_dir)
    inv = store.get_invoice("INV-DEMO-BANKSWAP")
    assert inv is not None
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.ESCALATE
    assert "payout account" in dec.reasoning.lower()


def test_view_is_silent_when_only_one_side_is_known():
    """An invoice that prints no account is not a CHANGED account. Before
    this pin, every live-extracted PDF (no payout field yet) rendered the red
    "Payout account changed" card against a vendor with an account on file."""
    svc = Service()
    inv = svc.store.get_invoice("INV-V001-3001")
    assert svc._payout_account_view(inv.model_copy(update={"payout_account": None})) is None
    stranger = inv.model_copy(update={"vendor_id": "V-nobody", "payout_account": "SG99 9999"})
    assert svc._payout_account_view(stranger) is None


def test_view_uses_the_gate_normaliser():
    svc = Service()
    inv = svc.store.get_invoice("INV-V001-3001")
    on_file = svc.store.vendor_account(inv.vendor_id)
    respaced = inv.model_copy(update={"payout_account": on_file.replace(" ", "").lower()})
    assert svc._payout_account_view(respaced)["matches"] is True


def test_ninth_gate_is_a_chip_and_names_itself_in_the_handoff():
    """The strip must not read "all passed" beside an ESCALATE, and the
    hand-off email must name the gate that failed."""
    svc = Service()
    swap = svc.get_case("INV-DEMO-BANKSWAP")
    chips = {g["key"]: g["passed"] for g in swap["guardrails"]}
    assert len(chips) == 9
    assert chips["payout"] is False
    assert "Payout account on file" in swap["handoff_draft"]["body"]
    assert "Failed gates: none" not in swap["handoff_draft"]["body"]
    clean = svc.get_case("INV-V001-3001")
    assert {g["key"]: g["passed"] for g in clean["guardrails"]}["payout"] is True


def test_queue_reason_label_knows_the_payout_gate():
    dec = {
        "action": "ESCALATE",
        "hold_reason": None,
        "reasoning": "[code guardrail] The invoice's payout account (…6666) differs from the "
        "account on file for this vendor (…1234), so code overrides APPROVE to ESCALATE.",
    }
    assert _reason_label(dec) == "Payout account changed"
