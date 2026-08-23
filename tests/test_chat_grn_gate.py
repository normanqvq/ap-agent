"""The tiered proof-of-delivery gate: what a chat-confirmed receipt can and
cannot buy.

Every test here runs against a model that APPROVES EVERYTHING, because the
claim being tested is not "the model behaves" — it is "code refuses even when
the model does not". Same adversary as tests/test_pipeline.py.

The cases are chosen around the two ways this feature could be dangerous:
- it could let an unauthorised person release money (the roster tier), and
- it could accept a receipt that does not actually cover what is being billed
  (the reconciliation check).

All offline: no API key, no network, no chat platform.
"""

import json
from pathlib import Path

import pytest

from apagent.agent.ap_tools import build_registry
from apagent.agent.registry import ToolRegistry
from apagent.matching.engine import CONFIDENCE, match_invoice
from apagent.pipeline import _blocking_rows, decide_invoice, grn_gate
from apagent.rules.tolerance import apply_tolerances
from apagent.schemas import (
    Action,
    ChatGrnPolicy,
    DocType,
    Document,
    EvidenceSource,
    HoldReason,
    LineItem,
    ToleranceConfig,
)
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"
CONTRACTS = DATA / "contracts"

# The demo case: PO exists, no goods receipt was ever typed. Its manifest note
# says "the warehouse confirmed by phone" — this feature is that phone call.
DEMO_INVOICE = "INV-V006-3019"
DEMO_PO = "PO-2026-1019"


def _defiant_approve(monkeypatch):
    """A model that approves everything — the adversary every guardrail test
    runs against. Stands in for a model taken in by injected text."""

    def defiant_model(messages, tools, system, provider=None):
        return {
            "text": json.dumps(
                {
                    "action": "APPROVE",
                    "hold_reason": None,
                    "confidence": 0.99,
                    "reasoning": "looks fine to me",
                }
            ),
            "tool_calls": [],
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", defiant_model)


def _chat_grn(po: Document, confirmed_by: str | None, qty_scale: float = 1.0) -> Document:
    """A chat-sourced receipt mirroring the PO's lines — what resolve.py
    produces for "everything arrived". qty_scale < 1 is a partial delivery."""
    return Document(
        doc_id="GRN-CHAT-1019-1",
        doc_type=DocType.GRN,
        vendor_id=po.vendor_id,
        vendor_name=po.vendor_name,
        issue_date="2026-08-12",
        ref_doc_id=po.doc_id,
        currency=po.currency,
        lines=[
            LineItem(
                line_no=line.line_no,
                sku=line.sku,
                description=line.description,
                qty=int(line.qty * qty_scale),
                uom=line.uom,
                unit_price_cents=None,  # a receipt records quantities, not prices
                line_total_cents=None,
            )
            for line in po.lines
        ],
        source=EvidenceSource.CHAT,
        source_ref="CHAT-EV-0001",
        confirmed_by=confirmed_by,
        captured_at="2026-08-12T14:32:00",
    )


@pytest.fixture
def demo():
    """A fresh store each time: add_grn mutates, and a leaked chat receipt
    would silently change every later test."""
    store = DocumentStore.from_dir(DATA)
    return store, store.get_invoice(DEMO_INVOICE), store.get_po(DEMO_PO)


def _decide(store, invoice):
    return decide_invoice(invoice, store, build_registry(store, CONTRACTS), contracts_dir=CONTRACTS)


# --- the tiers ------------------------------------------------------------


def test_no_chat_evidence_still_holds(monkeypatch, demo):
    """The unchanged case. Without this passing, the feature would have
    loosened the gate for every invoice rather than the intended ones."""
    _defiant_approve(monkeypatch)
    store, invoice, _ = demo
    decision = _decide(store, invoice)
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.AWAITING_GRN


def test_unauthorised_confirmer_is_evidence_not_authority(monkeypatch, demo):
    """The security property of the whole feature: a receipt confirmed by
    someone not on the roster does NOT release money.

    This is the case where the supplier sits in the group chat — extremely
    common for an SME — and confirms their own delivery."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by=None))
    decision = _decide(store, invoice)
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.AWAITING_GRN
    assert "[code guardrail]" in decision.reasoning
    assert "not an authorised receiver" in decision.reasoning
    assert "looks fine to me" in decision.reasoning  # model reasoning kept for audit


def test_authorised_confirmer_under_ceiling_releases_the_invoice(monkeypatch, demo):
    """The point of the feature. INV-V006-3019 is the dataset's own
    'warehouse confirmed by phone' case; a roster member confirming it in
    chat is what finally lets it through."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003"))
    decision = _decide(store, invoice)
    assert decision.action == Action.APPROVE


def test_ceiling_blocks_a_large_invoice_on_an_informal_receipt(monkeypatch, demo):
    """Above the ceiling an informal receipt is not enough, however
    impeccable the confirmer. Pins the ceiling as the thing that bounds the
    blast radius of a wrong confirmation.

    The ceiling is lowered rather than the invoice inflated: editing
    total_cents would put the total out of step with its own lines and the
    PO, and gate 5 would escalate on THAT instead — testing the wrong gate.
    """
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003"))
    strict = ToleranceConfig(informal_grn_ceiling_cents=invoice.total_cents)  # >= blocks
    decision = decide_invoice(
        invoice,
        store,
        build_registry(store, CONTRACTS),
        base_config=strict,
        contracts_dir=CONTRACTS,
    )
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.AWAITING_GRN
    assert "ceiling" in decision.reasoning


def test_the_ceiling_clears_the_case_it_exists_for(demo):
    """A ceiling below the motivating invoice would make the rule dead code.
    An earlier draft set it to SGD 1,000, under INV-V006-3019's SGD 1,270.29."""
    _, invoice, _ = demo
    assert invoice.total_cents < ToleranceConfig().informal_grn_ceiling_cents


def test_short_delivery_blocks_even_when_authorised(monkeypatch, demo):
    """A roster member confirming only part of the order cannot release an
    invoice billing all of it. Here gate 5 catches it first (the receipt
    records fewer units than the invoice bills)."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003", qty_scale=0.8))
    decision = _decide(store, invoice)
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.AWAITING_DELIVERY


# --- the gap gate 5 does not cover ----------------------------------------


def _sku_less(doc_id, doc_type, qty, ref=None, **kw):
    return Document(
        doc_id=doc_id,
        doc_type=doc_type,
        vendor_id="V001",
        vendor_name="Acme Pte Ltd",
        issue_date="2026-08-01",
        ref_doc_id=ref,
        currency="SGD",
        lines=[
            LineItem(
                line_no=1,
                sku=None,  # the whole point: no item code, as SME documents often are
                description="copy paper A4 ream",
                qty=qty,
                uom="PCS",
                unit_price_cents=100,
                line_total_cents=qty * 100,
            )
        ],
        total_cents=qty * 100,
        **kw,
    )


def test_sku_less_po_line_would_slip_past_gate_five(monkeypatch):
    """The reason _chat_grn_reconciles exists rather than trusting gate 5.

    matching.build_discrepancies only compares invoice against receipt when
    the PO line carries a sku (engine.py:185). With sku=None the comparison
    silently vanishes and gate 5 has NOTHING to say — asserted below — so a
    chat receipt confirming 50 units would have released an invoice billing
    100. Every committed PO happens to print SKUs, which is exactly what
    makes this the kind of hole that ships.
    """
    _defiant_approve(monkeypatch)
    po = _sku_less("PO-T", DocType.PO, 100)
    invoice = _sku_less("INV-T", DocType.INVOICE, 100, ref="PO-T")
    grn = _sku_less(
        "GRN-T",
        DocType.GRN,
        50,  # only half was confirmed
        ref="PO-T",
        source=EvidenceSource.CHAT,
        confirmed_by="EMP-003",
        source_ref="CHAT-EV-0001",
    )
    store = DocumentStore([po], [grn], [invoice])

    # Gate 5 is genuinely silent here — this assert is the point of the test.
    checked = apply_tolerances(match_invoice(invoice, [po], [grn]), ToleranceConfig())
    assert _blocking_rows(checked) == []

    decision = decide_invoice(invoice, store, ToolRegistry())
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.AWAITING_GRN


def test_empty_chat_receipt_never_counts_as_proof(monkeypatch, demo):
    """A receipt with no lines confirms no quantity of anything."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    empty = _chat_grn(po, confirmed_by="EMP-003").model_copy(update={"lines": []})
    store.add_grn(empty)
    decision = _decide(store, invoice)
    assert decision.action == Action.HOLD


# --- the reviewer's escape hatch ------------------------------------------
#
# Everything the automatic tier refuses has to lead somewhere, and it must not
# be "record a formal goods receipt": the businesses this serves confirm
# delivery in a chat group precisely because they keep no receipt book, so
# demanding a formal record would make every hold permanent in practice.


def test_endorsement_releases_an_unauthorised_confirmation(monkeypatch, demo):
    """A reviewer reading the conversation IS the check the roster stands in
    for, so satisfying it directly is the intended path, not a bypass."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    endorsed = _chat_grn(po, confirmed_by=None).model_copy(update={"endorsed_by": "123"})
    store.add_grn(endorsed)
    assert _decide(store, invoice).action == Action.APPROVE


def test_endorsement_releases_an_invoice_over_the_ceiling(monkeypatch, demo):
    """The ceiling means "too much money to release without a human", so a
    human is exactly what clears it."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003").model_copy(update={"endorsed_by": "123"}))
    strict = ToleranceConfig(informal_grn_ceiling_cents=invoice.total_cents)
    decision = decide_invoice(
        invoice,
        store,
        build_registry(store, CONTRACTS),
        base_config=strict,
        contracts_dir=CONTRACTS,
    )
    assert decision.action == Action.APPROVE


def test_endorsement_never_waives_the_quantity_check(monkeypatch, demo):
    """The line that keeps endorsement honest: accepting a receipt that says
    80 arrived does not accept an invoice billing 100. The reviewer vouched
    for the delivery, not for the bill."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    short = _chat_grn(po, confirmed_by=None, qty_scale=0.8).model_copy(
        update={"endorsed_by": "123"}
    )
    store.add_grn(short)
    decision = _decide(store, invoice)
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.AWAITING_DELIVERY


def test_endorsement_does_not_lift_the_manual_review_threshold(monkeypatch, demo):
    """Gate 1 is a different promise from gate 6 and outranks it. Accepting a
    delivery says nothing about an amount that needs a signature."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003").model_copy(update={"endorsed_by": "123"}))
    strict = ToleranceConfig(manual_review_threshold_cents=1)
    decision = decide_invoice(
        invoice,
        store,
        build_registry(store, CONTRACTS),
        base_config=strict,
        contracts_dir=CONTRACTS,
    )
    assert decision.action == Action.ESCALATE


# --- the store's replacement rule -----------------------------------------


def test_chat_receipt_cannot_overwrite_an_erp_one(demo):
    """A downgrade attack: a chat message replacing what the warehouse
    actually recorded, potentially with smaller quantities."""
    store, _, _ = demo
    po = store.get_po("PO-2026-1001")
    assert store.get_grn_for_po(po.doc_id).source == EvidenceSource.ERP
    with pytest.raises(ValueError, match="refused to replace"):
        store.add_grn(_chat_grn(po, confirmed_by="EMP-003"))


def test_erp_receipt_may_supersede_a_chat_one(demo):
    """The warehouse catching up later is an upgrade, and must be allowed."""
    store, _, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003"))
    formal = _chat_grn(po, confirmed_by=None).model_copy(
        update={"doc_id": "GRN-2019", "source": EvidenceSource.ERP, "source_ref": None}
    )
    store.add_grn(formal)
    assert store.get_grn_for_po(po.doc_id).source == EvidenceSource.ERP


def test_add_grn_refuses_a_receipt_it_would_silently_drop(demo):
    """Receipts are indexed by the PO they confirm, so one without a
    ref_doc_id would vanish instead of failing — the worst outcome, because
    the caller would believe delivery had been recorded."""
    store, _, po = demo
    orphan = _chat_grn(po, confirmed_by="EMP-003").model_copy(update={"ref_doc_id": None})
    with pytest.raises(ValueError, match="no ref_doc_id"):
        store.add_grn(orphan)


def test_add_grn_refuses_a_document_that_is_not_a_receipt(demo):
    store, invoice, _ = demo
    with pytest.raises(ValueError, match="not a goods receipt"):
        store.add_grn(invoice)


# --- provenance and confidence --------------------------------------------


def test_committed_documents_are_all_erp_sourced(demo):
    """The provenance fields must default, or the committed dataset stops
    loading — Document(**d) is built straight from JSON that has no such keys."""
    store, _, _ = demo
    docs = store.all_pos() + store.all_grns()
    assert docs and all(d.source == EvidenceSource.ERP for d in docs)
    assert all(d.source_ref is None and d.confirmed_by is None for d in docs)


def test_a_chat_receipt_scores_below_an_erp_one():
    """match_confidence tells the agent how much to trust the match. A
    Telegram message is not worth the same as a warehouse record."""
    assert len(CONFIDENCE) == 9  # 3 lookup outcomes x 3 receipt kinds
    for how in ("ref", "search"):
        assert CONFIDENCE[(how, "chat")] < CONFIDENCE[(how, "erp")]
        assert CONFIDENCE[(how, "chat")] > CONFIDENCE[(how, "none")]


def test_gate_survives_an_invoice_with_no_printed_total(demo):
    """The pipeline escalates a None total at gate 1, so gate 6 never sees
    one — but the API's gate strip evaluates all six unconditionally, and
    `None < ceiling` is a TypeError. It must fail closed instead."""
    store, invoice, po = demo
    grn = _chat_grn(po, confirmed_by="EMP-003")
    checked = apply_tolerances(match_invoice(invoice, [po], [grn]), ToleranceConfig())
    no_total = invoice.model_copy(update={"total_cents": None})
    passed, why = grn_gate(checked, grn, po, no_total, ToleranceConfig())
    assert passed is False
    assert why


# --- the company's policy setting ------------------------------------------
#
# How much a colleague's word is worth is a judgement that differs between
# businesses, so it is configured rather than decided in the gate. It is
# configured in CODE, though: the console's policy page shows it read-only,
# the same rule manual_review_threshold_cents follows.


def _with_policy(store, invoice, policy, **kw):
    return decide_invoice(
        invoice,
        store,
        build_registry(store, CONTRACTS),
        base_config=ToleranceConfig(chat_grn_policy=policy, **kw),
        contracts_dir=CONTRACTS,
    )


def test_policy_off_refuses_chat_proof_entirely(monkeypatch, demo):
    """A company that has turned the mechanism off gets the behaviour it had
    before the feature existed — endorsement included, because a half-off
    switch is worse than either position."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003").model_copy(update={"endorsed_by": "123"}))
    decision = _with_policy(store, invoice, ChatGrnPolicy.OFF)
    assert decision.action == Action.HOLD
    assert decision.hold_reason == HoldReason.AWAITING_GRN


def test_policy_evidence_only_always_defers_to_a_reviewer(monkeypatch, demo):
    """The safest setting that still saves the chasing: a perfect
    confirmation from a roster member for a small invoice still waits."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003"))
    assert _with_policy(store, invoice, ChatGrnPolicy.EVIDENCE_ONLY).action == Action.HOLD


def test_policy_evidence_only_still_honours_an_endorsement(monkeypatch, demo):
    """EVIDENCE_ONLY means "a human decides", not "nothing works"."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003").model_copy(update={"endorsed_by": "123"}))
    assert _with_policy(store, invoice, ChatGrnPolicy.EVIDENCE_ONLY).action == Action.APPROVE


def test_policy_trusted_ignores_the_informal_ceiling(monkeypatch, demo):
    """For a company that treats its receivers' word as final regardless of
    amount. The ceiling is the thing TRUSTED drops."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003"))
    decision = _with_policy(store, invoice, ChatGrnPolicy.TRUSTED, informal_grn_ceiling_cents=1)
    assert decision.action == Action.APPROVE


def test_policy_trusted_still_requires_an_authorised_confirmer(monkeypatch, demo):
    """TRUSTED trusts the ROSTER, not the group. The supplier sitting in the
    chat is still not a receiver."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by=None))
    assert _with_policy(store, invoice, ChatGrnPolicy.TRUSTED).action == Action.HOLD


def test_policy_trusted_does_not_relax_the_manual_review_threshold(monkeypatch, demo):
    """Two different promises. One is about proof of delivery, the other
    about large payments, and neither setting may quietly relax the other."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003"))
    decision = _with_policy(store, invoice, ChatGrnPolicy.TRUSTED, manual_review_threshold_cents=1)
    assert decision.action == Action.ESCALATE


def test_no_policy_waives_the_quantity_check(monkeypatch, demo):
    """Whether a receipt covers what is billed is arithmetic, not policy."""
    _defiant_approve(monkeypatch)
    store, invoice, po = demo
    store.add_grn(_chat_grn(po, confirmed_by="EMP-003", qty_scale=0.8))
    for policy in (ChatGrnPolicy.TIERED, ChatGrnPolicy.TRUSTED):
        assert _with_policy(store, invoice, policy).action == Action.HOLD, policy


def test_the_default_policy_is_the_tiered_one():
    """An install that never thought about this gets the middle setting, not
    the permissive one."""
    assert ToleranceConfig().chat_grn_policy == ChatGrnPolicy.TIERED


def test_policy_can_be_set_per_vendor():
    """resolve_config already swaps the whole object per vendor, so a company
    can trust one supplier's deliveries and not another's."""
    from apagent.rules.tolerance import resolve_config

    base = ToleranceConfig(
        per_vendor_overrides={"V006": ToleranceConfig(chat_grn_policy=ChatGrnPolicy.OFF)}
    )
    assert resolve_config("V006", base).chat_grn_policy == ChatGrnPolicy.OFF
    assert resolve_config("V001", base).chat_grn_policy == ChatGrnPolicy.TIERED
