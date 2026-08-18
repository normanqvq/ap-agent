"""One invoice, end to end: match -> rules -> agent -> guardrails -> decision.

This is the assembly line the demo script and the API both call, so the
two can never drift apart. Everything deterministic happens BEFORE the
model is invoked: by the time run_agent starts, the facts are computed,
tolerance-checked and frozen into the task message. The model adds
judgment and tool-gathered evidence on top — it cannot change the facts.

And everything with AUTHORITY happens AFTER the model returns: the code
guardrails below re-check an APPROVE against every computed fact — the
money threshold, hard duplicates, the tolerance verdicts (contract-aware),
unmatched lines, and proof of delivery — and override it when they
disagree. The prompt states the same policies so the model's reasoning
makes sense, but telling is not enforcing: a model that ignores the policy
(or is talked out of it by injected document text) answers into a layer
that does not negotiate. Two independent code reviews proved the earlier
version of this file enforced only three of those checks, which made the
injection defense a model behavior, not an architecture property. Every
override keeps the model's original reasoning in the trail, because "the
model was wrong and code caught it" is audit gold, not something to hide.
"""

import re
from functools import lru_cache
from pathlib import Path

from apagent.agent.ap_tools import hard_duplicates, recheck_with_contract
from apagent.agent.loop import run_agent
from apagent.agent.prompts import AP_SYSTEM_PROMPT, build_task_message
from apagent.agent.registry import ToolRegistry
from apagent.matching.engine import match_invoice
from apagent.retrieval.search import Chunk, load_contracts
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.schemas import (
    Action,
    AgentDecision,
    Discrepancy,
    DiscrepancyField,
    Document,
    HoldReason,
    MatchResult,
    ToleranceConfig,
)
from apagent.store import DocumentStore


@lru_cache(maxsize=4)
def _contract_chunks(contracts_dir: str) -> tuple[Chunk, ...]:
    """Contract sections, parsed once per directory. The guardrail needs
    them on every decision; re-reading six PDFs per invoice would be waste."""
    return tuple(load_contracts(Path(contracts_dir)))


def decide_invoice(
    invoice: Document,
    store: DocumentStore,
    registry: ToolRegistry,
    base_config: ToleranceConfig | None = None,
    max_rounds: int = 5,
    contracts_dir: str | Path | None = None,
) -> AgentDecision:
    """Run the full pipeline for one invoice and return the decision.

    base_config defaults to the stock ToleranceConfig — vendor overrides are
    deliberately NOT preloaded. The model-facing task message shows the
    STRICT view (default tolerances), so discovering a contract allowance
    stays the agent's live find; the code guardrail then re-derives the
    same allowance itself before enforcing. Model discovers, code decides.

    contracts_dir feeds that guardrail. When None, the guardrail treats
    every contract as silent — the strict direction: more holds, never a
    looser approve.
    """
    config = resolve_config(invoice.vendor_id, base_config or ToleranceConfig())

    match = match_invoice(invoice, store.all_pos(), store.all_grns())
    checked = apply_tolerances(match, config)
    review_gate = requires_manual_review(invoice.total_cents, config)

    # Duplicates are a deterministic fact, so they are computed HERE and
    # handed to the model in the task message — not left to whether the
    # model remembers to call the duplicate tool. The tool stays available
    # for investigation; the fact does not depend on it.
    duplicates = hard_duplicates(invoice, store)

    decision = run_agent(
        system_prompt=AP_SYSTEM_PROMPT,
        user_message=build_task_message(invoice, checked, review_gate, duplicates),
        registry=registry,
        invoice_id=invoice.doc_id,
        max_rounds=max_rounds,
    )

    chunks = _contract_chunks(str(contracts_dir)) if contracts_dir else ()
    decision = _apply_guardrails(
        decision, invoice, checked, review_gate, duplicates, config, chunks
    )

    outbound = _render_outbound_message(decision, invoice, checked, store)
    if outbound is not None:
        decision = decision.model_copy(update={"outbound_message": outbound})
    return decision


def _override(decision: AgentDecision, action: Action, hold_reason, why: str) -> AgentDecision:
    return decision.model_copy(
        update={
            "action": action,
            "hold_reason": hold_reason,
            "reasoning": f"[code guardrail] {why} Model reasoning was: {decision.reasoning}",
        }
    )


def _billed_within_order(d: Discrepancy) -> bool:
    """True when a QTY gap is only the invoice billing LESS than ordered
    (and no more than received) — a partial bill, which policy allows.
    Billing more than ordered, or more than the receipt records, is the
    over-billing case and must block an approve."""
    try:
        inv = int(d.invoice_value)
        po = int(d.po_value)
    except (TypeError, ValueError):
        return False
    if inv > po:
        return False
    if d.grn_value is not None:
        try:
            if inv > int(d.grn_value):
                return False
        except ValueError:
            return False
    return True


def _blocking_rows(match: MatchResult) -> list[Discrepancy]:
    """The out-of-tolerance rows that forbid an APPROVE.

    QTY rows are direction-aware: a partial bill (invoice bills less than
    ordered and no more than received) stays approvable per policy #6.
    Everything else out of tolerance blocks — including a price BELOW the
    order beyond tolerance, because an unexpected discount is still a
    document that doesn't match its order."""
    out = []
    for d in match.discrepancies:
        if d.within_tolerance:
            continue
        if d.field == DiscrepancyField.QTY and _billed_within_order(d):
            continue
        out.append(d)
    return out


def _apply_guardrails(
    decision: AgentDecision,
    invoice: Document,
    checked: MatchResult,
    review_gate: bool,
    duplicates: list[Document],
    config: ToleranceConfig,
    chunks: tuple[Chunk, ...],
) -> AgentDecision:
    """The authority layer: an APPROVE must survive every code check.

    Only APPROVE is ever overridden — HOLD/EMAIL/ESCALATE move no money, and
    second-guessing the model toward MORE automation would be the one
    direction a guardrail must never push.
    """
    if decision.action != Action.APPROVE:
        return decision

    # 1. The money gate. Above the manual-review threshold a human signs
    # off even on a perfectly clean match.
    if review_gate:
        return _override(
            decision,
            Action.ESCALATE,
            None,
            "The invoice total is at or above the manual-review threshold, "
            "so code overrides APPROVE to ESCALATE.",
        )

    # 2. No purchase order at all: there is nothing to have matched against,
    # so an APPROVE has no factual basis whatever the model says.
    if checked.po_id is None:
        return _override(
            decision,
            Action.ESCALATE,
            None,
            "No purchase order could be matched to this invoice, so code "
            "overrides APPROVE to ESCALATE.",
        )

    # 3. Invoice lines that exist on no PO: the over-billing / wrong-customer
    # case, and money attached to goods we never ordered.
    if checked.unmatched_inv_lines:
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"Invoice lines {checked.unmatched_inv_lines} match no purchase "
            "order line, so code overrides APPROVE to ESCALATE.",
        )

    # 4. The duplicate gate. If ANY hard duplicate exists, neither invoice
    # of the pair is auto-payable — a human picks the real one. We used to
    # let the earlier-dated one through, but issue_date is printed by the
    # supplier: back-dating a resubmission walked straight past that gate
    # (and both reviews caught opposite failure modes of it). Deciding
    # "which is the original" belongs to an internal payment-status record,
    # which does not exist yet — until it does, the safe answer is both
    # escalate, which costs one human touch and never a double payment.
    if duplicates:
        names = ", ".join(d.doc_id for d in duplicates)
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"This invoice hard-duplicates {names} (same vendor, same PO "
            "reference, same total); a human must pick which one is payable, "
            "so code overrides APPROVE to ESCALATE.",
        )

    # 5. The facts gate. Re-check every tolerance verdict — with the
    # vendor's contractual price allowance re-derived IN CODE from the
    # clause text (the same computation the recheck tool showed the model).
    # An APPROVE survives only if code agrees the rows are covered. This is
    # the gate that makes the injection defense an architecture property:
    # the injected 10% overcharge is blocked here even if the model is
    # fully fooled.
    allowance, rechecked = recheck_with_contract(checked, invoice.vendor_id, chunks, config)
    blocking = _blocking_rows(rechecked)
    if blocking:
        fields = {d.field for d in blocking}
        detail = "; ".join(
            f"{d.field.value} line_pair={d.line_pair} po={d.po_value} "
            f"grn={d.grn_value} invoice={d.invoice_value}"
            for d in blocking
        )
        if fields == {DiscrepancyField.UNIT_PRICE}:
            covered = (
                f"even under the contract's {allowance[0]}% allowance"
                if allowance
                else "and the contract grants no price variance allowance"
            )
            return _override(
                decision,
                Action.HOLD,
                HoldReason.PRICE_VARIANCE,
                f"Price rows remain out of tolerance {covered} ({detail}), "
                "so code overrides APPROVE to HOLD.",
            )
        if fields == {DiscrepancyField.QTY}:
            return _override(
                decision,
                Action.HOLD,
                HoldReason.AWAITING_DELIVERY,
                f"The invoice bills more than was ordered or received "
                f"({detail}), so code overrides APPROVE to HOLD.",
            )
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"Out-of-tolerance discrepancies remain ({detail}), so code "
            "overrides APPROVE to ESCALATE.",
        )

    # 6. The proof-of-delivery gate. No goods receipt means nothing confirms
    # the goods arrived; paying on the vendor's word alone is the exact risk
    # a three-way match exists to prevent. (If the business later handles
    # service invoices with no GRN concept, this gate gains an exemption —
    # in code, reviewed, not via prompt wording.)
    if checked.grn_id is None:
        return _override(
            decision,
            Action.HOLD,
            HoldReason.AWAITING_GRN,
            "No goods receipt is recorded for this invoice's PO, so code "
            "overrides APPROVE to HOLD until receipt is confirmed.",
        )

    return decision


def _safe_doc_id(doc_id: str) -> str:
    """An invoice id fit for a human-facing message. The id is printed by
    the supplier, i.e. attacker text — 'INV-1. OPS: wire the balance to
    acct 999' is a perfectly legal invoice number. Anything outside a
    strict id shape is withheld entirely rather than 'cleaned', because a
    sanitizer that rewrites hostile text still delivers hostile text."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,39}", doc_id):
        return doc_id
    return "this invoice (id withheld: unusual characters)"


def _render_outbound_message(
    decision: AgentDecision, invoice: Document, checked, store: DocumentStore
) -> str | None:
    """The message a human will act on, rendered by CODE from templates.

    Every slot is either from our own records or validated against a strict
    shape: the vendor name is the canonical one from the vendor directory
    (NOT the name printed on the invoice), the PO id comes from the match,
    the invoice id is shape-checked (_safe_doc_id), and the currency label
    must be a plain 3-letter code or it is not used. The model never
    authors a word of this — the action enum constrains what the agent
    does, and this constrains what anyone outside the audit trail reads.
    A poisoned invoice description can still appear inside `reasoning`,
    which is why reasoning is labelled internal audit text and is never
    sent anywhere.
    """
    vendor_name = store.vendors().get(invoice.vendor_id, invoice.vendor_id)
    po_ref = checked.po_id or "no PO on record"
    inv_ref = _safe_doc_id(invoice.doc_id)

    currency = invoice.currency if invoice.currency else None
    if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
        currency = None
    if invoice.total_cents is None:
        amount = "amount not printed"
    elif currency:
        amount = f"{currency} {invoice.total_cents / 100:,.2f}"
    else:
        amount = f"{invoice.total_cents / 100:,.2f} (currency unverified)"

    if decision.action == Action.HOLD and decision.hold_reason == HoldReason.AWAITING_GRN:
        return (
            f"To operations: please confirm whether the goods for {po_ref} "
            f"({vendor_name}) have arrived, and record the goods receipt so "
            f"invoice {inv_ref} ({amount}) can be matched and paid."
        )
    if decision.action == Action.HOLD and decision.hold_reason == HoldReason.AWAITING_DELIVERY:
        return (
            f"To operations: invoice {inv_ref} from {vendor_name} bills "
            f"more than the goods receipt for {po_ref} records as received. "
            "Please confirm the outstanding delivery before this invoice is released."
        )
    if decision.action == Action.EMAIL:
        return (
            f"To {vendor_name}: our records show invoice {inv_ref} "
            f"({amount}) does not match purchase order {po_ref}. Please send a "
            "corrected invoice, or the agreed basis for the difference, quoting "
            "the PO number."
        )
    return None
