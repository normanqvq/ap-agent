"""One invoice, end to end: match -> rules -> agent -> guardrails -> decision.

This is the assembly line the demo script and the API both call, so the
two can never drift apart. Everything deterministic happens BEFORE the
model is invoked: by the time run_agent starts, the facts are computed,
tolerance-checked and frozen into the task message. The model adds
judgment and tool-gathered evidence on top — it cannot change the facts.

And everything with AUTHORITY happens AFTER the model returns: the code
guardrails below re-check the model's action against the computed facts
and override it when they disagree. The prompt states the same policies so
the model's reasoning makes sense, but telling is not enforcing — a model
that ignores the policy (or is talked out of it by injected document text)
answers into a layer that does not negotiate. Every override keeps the
model's original reasoning in the trail, because "the model was wrong and
code caught it" is audit gold, not something to hide.
"""

from apagent.agent.ap_tools import hard_duplicates
from apagent.agent.loop import run_agent
from apagent.agent.prompts import AP_SYSTEM_PROMPT, build_task_message
from apagent.agent.registry import ToolRegistry
from apagent.matching.engine import match_invoice
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.schemas import Action, AgentDecision, Document, HoldReason, ToleranceConfig
from apagent.store import DocumentStore


def decide_invoice(
    invoice: Document,
    store: DocumentStore,
    registry: ToolRegistry,
    base_config: ToleranceConfig | None = None,
    max_rounds: int = 5,
) -> AgentDecision:
    """Run the full pipeline for one invoice and return the decision.

    base_config defaults to the stock ToleranceConfig — vendor overrides are
    deliberately NOT preloaded. When a contract grants a bigger allowance,
    the agent finds it live via recheck_against_contract, whose verdict is
    computed in code from the clause text.
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

    decision = _apply_guardrails(decision, invoice, checked, review_gate, duplicates)

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


def _apply_guardrails(
    decision: AgentDecision,
    invoice: Document,
    checked,
    review_gate: bool,
    duplicates: list[Document],
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

    # 2. The duplicate gate. Blocked only when a hard duplicate exists with
    # an earlier (or equal) issue date — i.e. this invoice is not the first
    # of the pair. The original itself stays approvable; on a date tie both
    # are blocked, which costs one escalation and never a double payment.
    prior = [d for d in duplicates if d.issue_date <= invoice.issue_date]
    if prior:
        names = ", ".join(d.doc_id for d in prior)
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"This invoice hard-duplicates {names} (same vendor, same PO "
            "reference, same total), so code overrides APPROVE to ESCALATE.",
        )

    # 3. The proof-of-delivery gate. No goods receipt means nothing confirms
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


def _render_outbound_message(
    decision: AgentDecision, invoice: Document, checked, store: DocumentStore
) -> str | None:
    """The message a human will act on, rendered by CODE from templates.

    Every slot comes from our own records: the vendor name is the canonical
    one from the vendor directory (NOT the name printed on the invoice) and
    the PO id comes from the match. The model never authors a word of this —
    the action enum constrains what the agent does, and this constrains what
    anyone outside the audit trail reads. A poisoned invoice description can
    still appear inside `reasoning`, which is why reasoning is labelled
    internal audit text and is never sent anywhere.
    """
    vendor_name = store.vendors().get(invoice.vendor_id, invoice.vendor_id)
    po_ref = checked.po_id or "no PO on record"
    if invoice.total_cents is not None:
        amount = f"{invoice.currency or 'SGD'} {invoice.total_cents / 100:,.2f}"
    else:
        amount = "amount not printed"

    if decision.action == Action.HOLD and decision.hold_reason == HoldReason.AWAITING_GRN:
        return (
            f"To operations: please confirm whether the goods for {po_ref} "
            f"({vendor_name}) have arrived, and record the goods receipt so "
            f"invoice {invoice.doc_id} ({amount}) can be matched and paid."
        )
    if decision.action == Action.HOLD and decision.hold_reason == HoldReason.AWAITING_DELIVERY:
        return (
            f"To operations: invoice {invoice.doc_id} from {vendor_name} bills "
            f"more than the goods receipt for {po_ref} records as received. "
            "Please confirm the outstanding delivery before this invoice is released."
        )
    if decision.action == Action.EMAIL:
        return (
            f"To {vendor_name}: our records show invoice {invoice.doc_id} "
            f"({amount}) does not match purchase order {po_ref}. Please send a "
            "corrected invoice, or the agreed basis for the difference, quoting "
            "the PO number."
        )
    return None
