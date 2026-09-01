"""Rules-only baseline vs the agent — the A/B that quantifies the judgement.

The baseline (pipeline.decide_invoice_rules_only) runs the real code
guardrails with no agent and no contract lookup. The claim these tests pin:
the agent lifts STP by recovering the contract-covered price variance, and
neither column ever false-approves — more straight-through, no more risk.
"""

import json
from pathlib import Path

from apagent.api.service import Service
from apagent.eval import evaluate
from apagent.pipeline import decide_invoice_rules_only
from apagent.schemas import Action, DocType, Document, HoldReason, LineItem
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


def test_rules_only_baseline_holds_the_contract_flip():
    """Rules alone (no contract) hold INV-V005-3018's 4% price variance — the
    exact friction the agent removes by reading the 5% clause."""
    store = DocumentStore.from_dir(DATA)
    decision = decide_invoice_rules_only(store.get_invoice("INV-V005-3018"), store)
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.PRICE_VARIANCE


def test_rules_only_baseline_never_false_approves():
    """The baseline is stricter than the agent, never looser: scored over the
    manifest it has zero false approvals, so the A/B's 'no added risk' holds."""
    store = DocumentStore.from_dir(DATA)
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    decisions = {
        e["invoice_id"]: decide_invoice_rules_only(
            store.get_invoice(e["invoice_id"]), store
        ).model_dump()
        for e in manifest
    }
    report = evaluate(manifest, decisions)
    assert report["metrics"]["false_approve_count"] == 0


def test_baseline_comparison_recovers_the_contract_case():
    """The service panel: rules-only holds INV-V005-3018, the agent approves
    it, and both columns keep false approvals at zero."""
    ab = Service().baseline_comparison()
    assert ab["baseline"]["false_approve_count"] == 0
    assert ab["agent"]["false_approve_count"] == 0
    assert ab["agent"]["stp_pct"] >= ab["baseline"]["stp_pct"]
    assert "INV-V005-3018" in {r["invoice_id"] for r in ab["recovered"]}


def test_roi_reports_manual_cost_and_is_honest_about_tokens():
    """ROI cites the manual cost and does not fabricate a token cost the
    committed cache never measured."""
    roi = Service().roi()
    assert roi["manual_cost_cents"] == 940
    assert roi["manual_batch_cents"] == 940 * roi["invoices"]
    # False because the committed cache carries no token usage. If a real
    # provider run is ever committed (_save_cache keeps non-None token fields),
    # this flips to True — the red would mean the data got more honest, not a bug.
    assert roi["agent_cost_measured"] is False


def test_rules_only_baseline_refuses_a_superseded_original():
    """Gate 1 in the baseline too. The A/B is only honest if the rules-only
    column runs every gate the agent column runs; this one was skipped, so a
    withdrawn original still scored APPROVE in the baseline."""
    line = LineItem(
        line_no=1,
        sku="SKU-1",
        description="widget",
        qty=10,
        uom="PCS",
        unit_price_cents=100,
        line_total_cents=1000,
    )

    def _doc(doc_id, doc_type, ref, **kw):
        return Document(
            doc_id=doc_id,
            doc_type=doc_type,
            vendor_id="V1",
            vendor_name="Acme",
            issue_date="2026-01-02",
            ref_doc_id=ref,
            currency="SGD",
            lines=[line],
            **kw,
        )

    po = _doc("PO1", DocType.PO, None)
    grn = _doc("GRN1", DocType.GRN, "PO1")
    original = _doc("INV1", DocType.INVOICE, "PO1", total_cents=1000)
    revision = _doc("INV1-R1", DocType.INVOICE, "PO1", total_cents=1000, replaces="INV1")
    store = DocumentStore([po], [grn], [original, revision])
    dec = decide_invoice_rules_only(original, store)
    assert dec.action != Action.APPROVE
    assert "superseded" in dec.reasoning.lower()
