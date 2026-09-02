"""One invoice, end to end: match -> rules -> agent -> guardrails -> decision.

This is the assembly line the demo script and the API both call, so the
two can never drift apart. Everything deterministic happens BEFORE the
model is invoked: by the time run_agent starts, the facts are computed,
tolerance-checked and frozen into the task message. The model adds
judgment and tool-gathered evidence on top — it cannot change the facts.

And everything with AUTHORITY happens AFTER the model returns: the code
guardrails below re-check an APPROVE against every computed fact — whether
a later correction has superseded this document, the money threshold, hard
duplicates, the tolerance verdicts (contract-aware), unmatched lines, the
currency the order was placed in, and proof of delivery — and override it when they
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

from apagent.agent.ap_tools import (
    billed_elsewhere,
    hard_duplicates,
    recheck_with_contract,
    superseded_by,
)
from apagent.agent.loop import MAX_ROUNDS, run_agent
from apagent.agent.prompts import AP_SYSTEM_PROMPT, build_task_message
from apagent.agent.registry import ToolRegistry
from apagent.matching.engine import match_invoice
from apagent.retrieval.search import Chunk, load_contracts
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.schemas import (
    Action,
    AgentDecision,
    ChatGrnPolicy,
    Discrepancy,
    DiscrepancyField,
    Document,
    EvidenceSource,
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
    max_rounds: int = MAX_ROUNDS,
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
    duplicates = hard_duplicates(invoice, store, config)
    # Whether a later document has replaced this one. Deterministic and
    # code-owned like the duplicate set, and computed here for the same
    # reason: the guardrail must not depend on the model asking.
    superseded = superseded_by(invoice, store)
    billed = billed_elsewhere(invoice, store, config)

    decision = run_agent(
        system_prompt=AP_SYSTEM_PROMPT,
        user_message=build_task_message(invoice, checked, review_gate, duplicates),
        registry=registry,
        invoice_id=invoice.doc_id,
        max_rounds=max_rounds,
    )

    chunks = _contract_chunks(str(contracts_dir)) if contracts_dir else ()
    # The GRN document itself, not just checked.grn_id: gate 6 now tiers on
    # WHERE the receipt came from, and a bare id cannot answer that.
    grn = store.get_grn_for_po(checked.po_id) if checked.po_id else None
    po = store.get_po(checked.po_id) if checked.po_id else None
    decision = _apply_guardrails(
        decision,
        invoice,
        checked,
        review_gate,
        duplicates,
        config,
        chunks,
        grn,
        po,
        superseded,
        vendor_account=store.vendor_account(invoice.vendor_id),
        billed_elsewhere=billed,
    )

    outbound = _render_outbound_message(decision, invoice, checked, store)
    if outbound is not None:
        decision = decision.model_copy(update={"outbound_message": outbound})
    return decision


def decide_invoice_rules_only(
    invoice: Document,
    store: DocumentStore,
    base_config: ToleranceConfig | None = None,
) -> AgentDecision:
    """The same pipeline WITHOUT the agent: assume payable and let the
    deterministic gates decide — and, the point of the A/B, never look at a
    contract.

    This is the honest baseline the agent is measured against. It reuses the
    real _apply_guardrails (no weakened straw-man), so the only thing missing
    is the agent's judgement: the plan-act-adapt loop that reads a vendor's
    contract and recovers a price variance the default tolerance would hold.
    chunks=() makes the price gate see no contract allowance, so
    INV-V005-3018's 4% stays out of tolerance here and holds — exactly the
    friction the agent removes. Everything else (supersession, duplicate, money gate,
    proof of delivery, payout account) is identical to decide_invoice, so the baseline is as SAFE as
    the full pipeline; it just leaves approvals on the table. Both columns
    score false-approves = 0; the difference is STP, not risk.
    """
    config = resolve_config(invoice.vendor_id, base_config or ToleranceConfig())
    match = match_invoice(invoice, store.all_pos(), store.all_grns())
    checked = apply_tolerances(match, config)
    review_gate = requires_manual_review(invoice.total_cents, config)
    duplicates = hard_duplicates(invoice, store, config)
    reason = "[rules-only baseline] deterministic gates only, no contract lookup"
    baseline = AgentDecision(
        invoice_id=invoice.doc_id,
        action=Action.APPROVE,
        hold_reason=None,
        confidence=1.0,
        reasoning=reason,
        tool_calls=[],
        rounds_used=0,
    )
    grn = store.get_grn_for_po(checked.po_id) if checked.po_id else None
    po = store.get_po(checked.po_id) if checked.po_id else None
    return _apply_guardrails(
        baseline,
        invoice,
        checked,
        review_gate,
        duplicates,
        config,
        (),
        grn,
        po,
        superseded=superseded_by(invoice, store),
        vendor_account=store.vendor_account(invoice.vendor_id),
        billed_elsewhere=billed_elsewhere(invoice, store, config),
    )


def _norm_account(account: str) -> str:
    """Compare accounts ignoring spacing and case, so '1234 5678' and
    '12345678' are the same account. Deterministic, no cleverness."""
    return "".join(account.split()).upper()


def _override(decision: AgentDecision, action: Action, hold_reason, why: str) -> AgentDecision:
    return decision.model_copy(
        update={
            "action": action,
            "hold_reason": hold_reason,
            "reasoning": f"[code guardrail] {why} Model reasoning was: {decision.reasoning}",
        }
    )


def supersede(decision: AgentDecision, successor_id: str) -> AgentDecision:
    """Withdraw an APPROVE because a later document replaced this invoice.

    Public, and separate from the gate that calls it, because supersession
    is the one guardrail whose trigger can arrive AFTER the decision was
    made: R1 is decided while it is still the newest document in the chain,
    and R2 arriving is what withdraws it. The service applies this to R1's
    cached decision at that moment rather than re-running the pipeline --
    the fact is deterministic, so asking the model again would only risk a
    different answer to a question code already settled.

    A non-APPROVE is returned untouched by the caller, not here: the rule is
    the same one the whole guardrail layer obeys -- only APPROVE moves money,
    so only APPROVE is ever overridden.
    """
    return _override(
        decision,
        Action.ESCALATE,
        None,
        f"This document has been superseded by {successor_id}; only "
        "the latest document in a correction chain is payable, so code "
        "overrides APPROVE to ESCALATE.",
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


def _chat_grn_reconciles(
    po: Document, grn: Document, invoice: Document, match: MatchResult
) -> bool:
    """Every billed line has an explicit confirmed quantity that covers it.

    Gate 5 already BLOCKS a short chat receipt in the common case, so this
    looks redundant — it is not. matching.build_discrepancies only compares
    invoice against GRN when the PO line carries a sku (engine.py:185); a PO
    line with sku=None makes grn_qty None, the comparison silently vanishes,
    and gate 5 goes quiet. Every committed PO happens to print SKUs, so that
    hole is invisible today, but LineItem.sku is str | None precisely because
    SME documents lack item codes — and the invisible version of this bug is
    "one Telegram message approves anything under the ceiling".

    So a chat receipt clears a POSITIVE bar (we FOUND a confirmed quantity
    that covers every billed line) rather than merely surviving the absence
    of a negative finding. Weaker evidence, higher burden of proof.

    Matched by line_no carried over from the PO, deliberately NOT by sku, so
    this check does not inherit the very indexing weakness it exists to cover.
    Rejected alternative: relying on gate 5 alone.
    """
    confirmed: dict[int, int] = {}
    for line in grn.lines:
        confirmed[line.line_no] = confirmed.get(line.line_no, 0) + line.qty

    po_by_no = {line.line_no: line for line in po.lines}
    inv_by_no = {line.line_no: line for line in invoice.lines}

    for po_no, inv_no in match.line_pairs:
        inv_line = inv_by_no.get(inv_no)
        if inv_line is None or po_no not in po_by_no:
            return False  # a pairing we cannot re-derive: refuse rather than guess
        received = confirmed.get(po_no)
        if received is None or received < inv_line.qty:
            return False
    # An invoice line that paired with nothing is gate 3's business, but it also
    # means we have no confirmed quantity for money being billed.
    return not match.unmatched_inv_lines


def money_gate(
    invoice: Document, review_gate: bool, config: ToleranceConfig
) -> tuple[bool, str | None]:
    """Gate 2 as a pure function: (passed, refusal_reason). Enforced by
    _apply_guardrails, displayed by the API's gate strip -- one definition.

    Three facts about the AMOUNT, none of which line matching can see: the
    manual-review threshold; a tax line out of proportion to the goods
    (matching checks every line against the order and the total against
    lines-plus-tax, but nothing bounds the tax itself, so a tax at 50x the
    goods clears every comparison and rides into the payment run); and a
    negative tax or total, which is a credit note, not a bill to pay.
    """
    if review_gate:
        return False, (
            "The invoice total is at or above the manual-review threshold, "
            "so code overrides APPROVE to ESCALATE."
        )
    tax = invoice.tax_cents or 0
    if tax < 0 or (invoice.total_cents is not None and invoice.total_cents < 0):
        return False, (
            "The invoice carries a negative tax or total, which is a credit "
            "and not a bill to pay, so code overrides APPROVE to ESCALATE."
        )
    # The base is what the goods are worth: positive lines only, so a
    # discount line cannot shrink it into a false "tax out of policy", and
    # qty x unit price when the extractor found no line total.
    goods = 0
    for line in invoice.lines:
        value = line.line_total_cents
        if value is None and line.unit_price_cents is not None:
            value = line.qty * line.unit_price_cents
        if value and value > 0:
            goods += value
    if tax * 100 > goods * config.max_tax_pct:
        return False, (
            f"The invoice's tax ({tax / 100:,.2f}) is more than {config.max_tax_pct:g}% "
            f"of its goods value ({goods / 100:,.2f}), so code overrides APPROVE "
            "to ESCALATE."
        )
    return True, None


def grn_gate(
    checked: MatchResult,
    grn: Document | None,
    po: Document | None,
    invoice: Document,
    config: ToleranceConfig,
    billed_elsewhere: dict[str, int] | None = None,
) -> tuple[bool, str | None]:
    """Gate 6 as a pure function: (passed, refusal_reason).

    Called by _apply_guardrails, which ENFORCES it, and by the API's gate
    strip, which DISPLAYS it. One definition so the two cannot disagree —
    before this existed the UI kept its own hand-written copy of the six
    gates, and a UI that claims a gate passed while code refuses it is worse
    than no UI at all.

    The tiering: an ERP receipt is a record made against a process, a CHAT
    one is a colleague saying it arrived. The second is real evidence and
    genuinely unblocks the SME case this feature exists for, but it buys
    automation only for small money, only from someone we authorised in
    advance, and only when the quantities actually add up.

    Which of those apply is set by config.chat_grn_policy: OFF refuses chat
    proof outright, EVIDENCE_ONLY always defers to a reviewer, TIERED is the
    roster-plus-ceiling rule above, TRUSTED drops the ceiling. No setting
    waives the quantity check -- that is arithmetic, not policy.

    A chat receipt a reviewer has endorsed clears the first two of those --
    a signed-in human vouching for the evidence is exactly what the roster
    and the ceiling exist to demand, so satisfying it directly is the
    intended path, not a bypass. The quantity check still applies: endorsing
    a receipt that says 80 arrived does not endorse an invoice billing 100.
    Nothing here ever asks for a formal receipt to be typed up instead --
    an SME that had that habit would not need this feature.
    """
    if grn is None:
        return False, (
            "No goods receipt is recorded for this invoice's PO, so code "
            "overrides APPROVE to HOLD until receipt is confirmed."
        )
    # Whatever the receipt's source, it covers the ORDER, not this document:
    # an invoice for 4 on an order of 10 fully received is fine on its own and
    # over-billed once another live invoice has already claimed 10 of the
    # same goods. Only the sum can see that, so the sum is what is compared.
    if billed_elsewhere and po is not None:
        received: dict[str, int] = {}
        for line in grn.lines:
            if line.sku:
                received[line.sku] = received.get(line.sku, 0) + line.qty
        for line in invoice.lines:
            if not line.sku or line.sku not in billed_elsewhere:
                continue
            together = line.qty + billed_elsewhere[line.sku]
            have = received.get(line.sku, 0)
            if together > have:
                return False, (
                    f"Other invoices on {po.doc_id} already bill "
                    f"{billed_elsewhere[line.sku]} of {line.sku}; with this one that is "
                    f"{together} against {have} received, so code overrides APPROVE "
                    "to HOLD."
                )
    if grn.source != EvidenceSource.CHAT:
        return True, None

    # From here down: a chat-sourced receipt, judged under the company's
    # chosen policy. How much a colleague's word is worth genuinely differs
    # between businesses, so it is configured rather than decided here.
    policy = config.chat_grn_policy
    if policy == ChatGrnPolicy.OFF:
        return False, (
            f"Goods receipt {grn.doc_id} was confirmed in chat, and this company does "
            "not accept chat confirmations as proof of delivery, so code overrides "
            "APPROVE to HOLD."
        )

    if not grn.endorsed_by:
        if policy == ChatGrnPolicy.EVIDENCE_ONLY:
            return False, (
                f"Goods receipt {grn.doc_id} was confirmed in chat, and this company "
                "treats chat confirmations as evidence for a reviewer rather than "
                "grounds to pay, so code overrides APPROVE to HOLD."
            )
        if not grn.confirmed_by:
            return False, (
                f"Goods receipt {grn.doc_id} came from a chat message whose sender is "
                "not an authorised receiver, so it is evidence for a reviewer but not "
                "grounds to pay; code overrides APPROVE to HOLD."
            )
        # TRUSTED takes the roster's word whatever the amount. The
        # manual-review threshold still applies above it — that gate is a
        # promise about large payments, not about proof of delivery, and one
        # setting must not quietly relax the other.
        if policy == ChatGrnPolicy.TIERED:
            # None fails closed. It cannot reach here today (gate 1 escalates
            # a None total first) but this function is also called by the UI,
            # which evaluates every gate unconditionally — and None < int is
            # a TypeError.
            if (
                invoice.total_cents is None
                or invoice.total_cents >= config.informal_grn_ceiling_cents
            ):
                return False, (
                    f"Goods receipt {grn.doc_id} was confirmed in chat, and this invoice "
                    f"is at or above the {config.informal_grn_ceiling_cents / 100:,.2f} "
                    "ceiling for paying on an informal receipt alone, so code overrides "
                    "APPROVE to HOLD for a reviewer to accept the confirmation."
                )
    if po is None or not _chat_grn_reconciles(po, grn, invoice, checked):
        return False, (
            f"Goods receipt {grn.doc_id} was confirmed in chat but does not record a "
            "confirmed quantity covering every billed line, so code overrides "
            "APPROVE to HOLD."
        )
    return True, None


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
    grn: Document | None = None,
    po: Document | None = None,
    superseded: Document | None = None,
    vendor_account: str | None = None,
    billed_elsewhere: dict[str, int] | None = None,
) -> AgentDecision:
    """The authority layer: an APPROVE must survive every code check.

    Only APPROVE is ever overridden — HOLD/EMAIL/ESCALATE move no money, and
    second-guessing the model toward MORE automation would be the one
    direction a guardrail must never push.
    """
    if decision.action != Action.APPROVE:
        return decision

    # 1. The supersession gate. A document another document replaces has
    # been withdrawn by its own issuer -- the vendor sent a correction, and
    # the correction is what we owe. Paying both is not a duplicate in the
    # gate-5 sense (hard_duplicates deliberately skips inside a correction
    # chain, so the corrected invoice is not reported against the one it
    # corrects), so without this rule every copy of one correction clears on
    # its own merits and each schedules its own payment. Exactly one document
    # in a chain can be live, and it is the last one.
    if superseded is not None:
        return supersede(decision, superseded.doc_id)

    # 2. The money gate. Above the manual-review threshold a human signs
    # off even on a perfectly clean match.
    passed, why = money_gate(invoice, review_gate, config)
    if not passed:
        return _override(decision, Action.ESCALATE, None, why)

    # 3. No purchase order at all: there is nothing to have matched against,
    # so an APPROVE has no factual basis whatever the model says.
    if checked.po_id is None:
        return _override(
            decision,
            Action.ESCALATE,
            None,
            "No purchase order could be matched to this invoice, so code "
            "overrides APPROVE to ESCALATE.",
        )

    # 4. The currency gate. Everything below this line compares integers of
    # cents, and not one of those comparisons looks at the unit: an invoice
    # denominated in another currency than the order matches line for line,
    # clears every tolerance, and asks for a different amount of money.
    # USD billed as GBP is a silent ~30% overpayment that no later gate can
    # see. The currency on an invoice is the vendor's text; the currency on
    # the purchase order is ours, and they must agree. A currency we could
    # not read is not a match either -- including the case where NEITHER is
    # readable, which compares equal and would otherwise pass. Strict is the
    # only direction a guardrail is allowed to fail in.
    if po is None or not invoice.currency or invoice.currency != po.currency:
        billed = invoice.currency or "a currency we could not read"
        ordered = po.currency if po is not None else "one we cannot look up"
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"The invoice is billed in {billed} and purchase order "
            f"{checked.po_id} was placed in {ordered}, so code overrides "
            "APPROVE to ESCALATE.",
        )

    # 5. Invoice lines that exist on no PO: the over-billing / wrong-customer
    # case, and money attached to goods we never ordered.
    if checked.unmatched_inv_lines:
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"Invoice lines {checked.unmatched_inv_lines} match no purchase "
            "order line, so code overrides APPROVE to ESCALATE.",
        )

    # 6. The duplicate gate. If ANY hard duplicate exists, neither invoice
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

    # 7. The facts gate. Re-check every tolerance verdict — with the
    # vendor's contractual price allowance re-derived IN CODE from the
    # clause text (the same computation the recheck tool showed the model).
    # An APPROVE survives only if code agrees the rows are covered. This is
    # the gate that makes the injection defense an architecture property:
    # the injected 10% overcharge is blocked here even if the model is
    # fully fooled.
    allowance, rechecked = recheck_with_contract(checked, invoice.vendor_id, chunks, config)
    if allowance is not None and allowance[0] > config.max_contract_allowance_pct:
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"The contract grants a {allowance[0]:g}% price allowance, above the "
            f"{config.max_contract_allowance_pct:g}% cap code will apply on its own; "
            "a human must read that clause, so code overrides APPROVE to ESCALATE.",
        )
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

    # 8. The proof-of-delivery gate. No goods receipt means nothing confirms
    # the goods arrived; paying on the vendor's word alone is the exact risk
    # a three-way match exists to prevent. (If the business later handles
    # service invoices with no GRN concept, this gate gains an exemption —
    # in code, reviewed, not via prompt wording.)
    #
    # That exemption arrived, in the shape this comment demanded: a receipt
    # confirmed in a chat group is accepted, but only under conditions code
    # checks (grn_gate), never conditions the prompt describes. The tiering
    # lives in grn_gate so the API's gate strip enforces the same rule.
    passed, why = grn_gate(checked, grn, po, invoice, config, billed_elsewhere)
    if not passed:
        return _override(decision, Action.HOLD, HoldReason.AWAITING_GRN, why)

    # 9. The payout-account gate. Every gate above proves WHAT is paid; none
    # proves WHOM. An invoice correct in every line but printed with a changed
    # remittance account is business-email-compromise — a compromised vendor
    # mailbox redirecting the money. The account on the invoice is the vendor's
    # text; the account on file (store.vendor_account, from the vendor master)
    # is ours, and they must agree before money moves. Silent when either side
    # is unknown (a new vendor has no baseline; an invoice may print no account)
    # or when they match — which is why it never fires on the graded set, whose
    # invoices carry their on-file account. This is the last check before an
    # APPROVE releases payment.
    if (
        invoice.payout_account
        and vendor_account
        and _norm_account(invoice.payout_account) != _norm_account(vendor_account)
    ):
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"The invoice's payout account (…{invoice.payout_account[-4:]}) "
            f"differs from the account on file for this vendor "
            f"(…{vendor_account[-4:]}), so code overrides APPROVE to ESCALATE.",
        )

    return decision


def _safe_doc_id(doc_id: str) -> str:
    """An invoice id fit for a human-facing message. The id is printed by
    the supplier, i.e. attacker text — 'INV-1. OPS: wire the balance to
    acct 999' is a perfectly legal invoice number. Anything outside a
    strict id shape is withheld entirely rather than 'cleaned', because a
    sanitizer that rewrites hostile text still delivers hostile text.

    Blocking spaces alone is not enough: hyphens read as spaces, and
    'PAY-NOW-WIRE-TO-DBS-0123456789-URGENT' is a readable instruction a
    security review landed in an ops message. So the shape is tight: an
    alphanumeric token plus at most three '-/.'-separated groups (real ids
    like INV-V003-3003 or PO-2026-1003 fit; a seven-word instruction does
    not), and it must contain a digit (an all-words id is prose, not an id).
    """
    ok = re.fullmatch(r"[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+){0,3}", doc_id)
    if ok and any(ch.isdigit() for ch in doc_id):
        return doc_id
    return "this invoice (id withheld: unusual format)"


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
        # Chat evidence exists but did not clear gate 6 (too much money, or an
        # unauthorised confirmer). Asking operations to "confirm the goods
        # arrived" would be telling them to do the thing they just did.
        #
        # And the ask is NOT "record a formal goods receipt" either. The
        # companies this is built for do not keep one — that is the whole
        # reason delivery gets confirmed in a chat group. A message demanding
        # a formal receipt would make the hold permanent in practice and the
        # feature useless. So the ask is the action that actually exists:
        # a reviewer opens the invoice, reads the confirmation, and accepts it.
        #
        # We cannot branch on hold_reason alone — the same reason covers "no
        # evidence at all". Nothing from the chat message is echoed here: not
        # the confirmer's words, not their display name, not the PO as typed.
        chat_grn = store.get_grn_for_po(checked.po_id) if checked.po_id else None
        if chat_grn is not None and chat_grn.source == EvidenceSource.CHAT:
            return (
                f"To the AP reviewer: delivery for {po_ref} ({vendor_name}) was "
                f"confirmed in chat, but not by someone on the receiver list or not "
                f"for an amount we release automatically. Please open invoice "
                f"{inv_ref} ({amount}), read the confirmation, and accept it if it "
                "looks right."
            )
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
