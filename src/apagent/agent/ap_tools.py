"""The agent's evidence tools over the document store.

Every tool here RETRIEVES facts; none of them judge. The duplicate check is
the closest to a judgment call, and even there the comparison (same vendor,
same PO reference, same amount) is plain code — the tool reports what it
found and the agent decides what it means. This is the division of labour
the whole system is built on: 代码算事实，模型判含义.

Handlers return strings and never raise (see registry.py: a lookup miss is
evidence, not an error). "PO not found" is exactly the answer the agent
needs when an invoice bills against an order we never placed.
"""

import json
from pathlib import Path

from apagent.agent.registry import Tool, ToolRegistry
from apagent.matching.engine import find_po, match_invoice
from apagent.retrieval.search import (
    ContractIndex,
    price_variance_allowance,
    register_contract_tools,
)
from apagent.rules.tolerance import apply_tolerances
from apagent.schemas import DiscrepancyField, Document, ToleranceConfig
from apagent.store import DocumentStore


def _totals_duplicate(a: int | None, b: int | None, config: ToleranceConfig) -> bool:
    """Two invoice totals close enough to be the same bill. A tolerance, not
    exact equality, because a resubmission nudged by a cent is still a
    resubmission — and a nudge small enough to sit here is also small enough
    to slip the INVOICE_TOTAL check, so the two gates together leave no gap.
    Missing totals never match: a None total already forces manual review."""
    if a is None or b is None:
        return False
    return abs(a - b) <= config.total_abs_cents


def _revision_chain(invoice: Document, store: DocumentStore) -> set[str]:
    """Every doc_id in this invoice's correction chain, both directions.

    A corrected invoice is indistinguishable from a resubmission by the
    duplicate key alone -- same vendor, same purchase order, near-identical
    total is the definition of both. What separates them is the `replaces`
    link, which only code sets, and only when the document arrived as an
    answer to a query we ourselves sent.

    Walked in both directions, and transitively, because which end of the
    chain is being decided is an accident of timing: the revision is scored
    when it arrives, and the original is re-scored whenever anything else
    about it changes.

    A `replaces` naming a document we do not hold is ignored rather than
    trusted -- it buys an attacker nothing, which is the point of checking
    against the store instead of against the field alone.
    """
    chain = {invoice.doc_id}
    frontier = [invoice]
    while frontier:
        current = frontier.pop()
        candidates = []
        if current.replaces:
            older = store.get_invoice(current.replaces)
            if older is not None:
                candidates.append(older)
        candidates.extend(
            other
            for other in store.invoices_for_vendor(invoice.vendor_id)
            if other.replaces == current.doc_id
        )
        for doc in candidates:
            if doc.doc_id not in chain:
                chain.add(doc.doc_id)
                frontier.append(doc)
    return chain


def superseded_by(invoice: Document, store: DocumentStore) -> Document | None:
    """The document that replaces this one, or None if it is still the live one.

    The other half of _revision_chain, and the reason that helper is safe.
    Skipping the duplicate check inside a correction chain is only sound if
    exactly one document in the chain can be paid; without this, R1, R2 and
    R3 are three invoices that duplicate-check clean against each other and
    each APPROVE on their own merits. A vendor re-sending the same correction
    twice is enough to trigger that -- no attacker required.

    Only the direct successor is looked for, not the whole chain: a document
    with any successor at all cannot be paid, so the first one found settles
    it. `replaces` is set by code alone (see make_revision), so this cannot
    be induced by anything a vendor sends.
    """
    for other in store.invoices_for_vendor(invoice.vendor_id):
        if other.replaces == invoice.doc_id:
            return other
    return None


def hard_duplicates(
    invoice: Document, store: DocumentStore, config: ToleranceConfig | None = None
) -> list[Document]:
    """Ledger invoices that hard-duplicate this one.

    Keyed on the RESOLVED purchase order (find_po) plus a near-equal total —
    NOT on the printed ref_doc_id and exact total, which are both supplier
    text. A security review verified the old key was defeated four ways:
    drop the ref, alter it, resubmit a natively ref-less invoice, or nudge
    the total by a cent — each evaded detection while the engine re-attached
    the very same PO, so the resubmission read clean and was paid again.
    Resolving the PO ourselves closes all four: strip or forge the ref and
    the vendor+amount search lands on the same order anyway.

    Module-level (not buried in a tool handler) because the pipeline's code
    guardrail needs the same verdict — duplicate detection is a
    deterministic fact, and facts must not depend on whether the model
    remembered to call a tool.

    A document in the invoice's own revision chain (see _revision_chain) is
    never reported against it: a corrected invoice and the invoice it
    corrects share vendor, PO and near-identical total by construction, and
    without the chain check that is exactly a hard-duplicate match. The
    chain is populated only by the code-set `replaces` link, verified
    against the store, so this does NOT make an unlinked copy safe — an
    invoice with no `replaces` (or a forged one naming a document we do not
    hold) still hits the ordinary comparison below and is still caught.

    Residual (documented, not closed here): a resubmission that changes the
    amount via a WITHIN-tolerance price bump, kept internally consistent, is
    indistinguishable from a genuine slightly-different invoice without a
    record of which invoices were already PAID. That payment-status ledger
    is the real fix; until it exists this catches the cheap evasions, and
    the money/facts gates catch the rest.
    """
    base = config or ToleranceConfig()
    inv_po, _ = find_po(invoice, store.all_pos())
    if inv_po is None:
        # No resolvable PO. The no-PO guardrail already escalates these, and
        # with nothing to key on we cannot claim a duplicate.
        return []
    chain = _revision_chain(invoice, store)
    out = []
    for other in store.invoices_for_vendor(invoice.vendor_id):
        if other.doc_id in chain:
            continue
        other_po, _ = find_po(other, store.all_pos())
        if other_po is None or other_po.doc_id != inv_po.doc_id:
            continue
        if _totals_duplicate(invoice.total_cents, other.total_cents, base):
            out.append(other)
    return out


def billed_elsewhere(invoice: Document, store: DocumentStore) -> dict[str, int]:
    """Quantity per SKU that OTHER live invoices from this vendor already bill
    against this invoice's purchase order -- the instalment attack the
    duplicate gate cannot see. Three invoices at 100%, 90% and 80% of one
    order are three different totals and three different documents, each
    inside the order on its own, together nearly triple it. Summed here and
    compared against what was received in grn_gate.

    Same population as hard_duplicates -- same vendor, same resolved PO,
    outside this invoice's own correction chain -- minus documents a later
    correction has withdrawn, which owe nothing.
    """
    inv_po, _ = find_po(invoice, store.all_pos())
    if inv_po is None:
        return {}
    chain = _revision_chain(invoice, store)
    out: dict[str, int] = {}
    for other in store.invoices_for_vendor(invoice.vendor_id):
        if other.doc_id in chain or superseded_by(other, store) is not None:
            continue
        other_po, _ = find_po(other, store.all_pos())
        if other_po is None or other_po.doc_id != inv_po.doc_id:
            continue
        for line in other.lines:
            if line.sku:
                out[line.sku] = out.get(line.sku, 0) + line.qty
    return out


def recheck_with_contract(
    match, vendor_id: str, chunks, base_config: ToleranceConfig | None = None
):
    """Re-run the tolerance check under the vendor's contractual price
    allowance. Returns (allowance, rechecked_match); allowance is
    (pct, source_chunk) or None when the contract is silent.

    This is THE one computation for "does the contract cover this price?" —
    the agent-facing tool and the pipeline's code guardrail both call it, so
    the verdict the model is shown and the verdict code enforces can never
    disagree. The percentage is parsed from the clause text by code
    (price_variance_allowance); it never passes through the model.
    """
    base = base_config or ToleranceConfig()
    allowance = price_variance_allowance(list(chunks), vendor_id) if chunks else None
    if allowance is None:
        return None, apply_tolerances(match, base)
    pct, _chunk = allowance
    return allowance, apply_tolerances(match, base.model_copy(update={"unit_price_pct": pct}))


def _doc_summary(doc) -> dict:
    """A document as the agent should see it: full lines, cents as cents.

    We dump the whole document rather than a trimmed view. The documents
    are small, and deciding which fields the agent 'does not need' is how
    you end up debugging a wrong decision caused by a missing field.
    """
    return doc.model_dump()


def register_ap_tools(registry: ToolRegistry, store: DocumentStore) -> None:
    """Register the four AP lookup tools against a store."""

    def lookup_po(args: dict) -> str:
        po_id = args.get("po_id", "")
        po = store.get_po(po_id)
        if po is None:
            return (
                f"PO '{po_id}' not found. Either the invoice references an "
                "order we never placed, or the reference is mistyped."
            )
        return json.dumps(_doc_summary(po))

    def lookup_grn(args: dict) -> str:
        po_id = args.get("po_id", "")
        grn = store.get_grn_for_po(po_id)
        if grn is None:
            return (
                f"No goods receipt recorded for PO '{po_id}'. The goods may "
                "not have arrived, or warehouse has not entered the GRN yet. "
                "Without a receipt there is no proof of delivery."
            )
        return json.dumps(_doc_summary(grn))

    def get_vendor_history(args: dict) -> str:
        vendor_id = args.get("vendor_id", "")
        history = store.invoices_for_vendor(vendor_id)
        if not history:
            return f"No invoices on record for vendor '{vendor_id}'."
        return json.dumps(
            {
                "vendor_id": vendor_id,
                "invoice_count": len(history),
                "invoices": [
                    {
                        "doc_id": d.doc_id,
                        "issue_date": d.issue_date,
                        "po_reference": d.ref_doc_id,
                        "total_cents": d.total_cents,
                        "currency": d.currency,
                    }
                    for d in history
                ],
            }
        )

    def check_duplicate_invoice(args: dict) -> str:
        invoice_id = args.get("invoice_id", "")
        invoice = store.get_invoice(invoice_id)
        if invoice is None:
            return f"Invoice '{invoice_id}' is not in the ledger; cannot check."

        # The comparison lives in code (hard_duplicates — shared with the
        # pipeline's guardrail). The agent never eyeballs two invoices and
        # guesses — it gets a computed verdict with evidence. Same vendor +
        # same PO reference + same total is a hard duplicate signal; same
        # vendor + same total alone is soft (two orders can legitimately
        # cost the same), so we report the two separately.
        hard = hard_duplicates(invoice, store)
        soft = []
        for other in store.invoices_for_vendor(invoice.vendor_id):
            if other.doc_id == invoice.doc_id or other in hard:
                continue
            if other.total_cents is not None and other.total_cents == invoice.total_cents:
                soft.append(other)

        return json.dumps(
            {
                "invoice_id": invoice_id,
                "likely_duplicates": [
                    {
                        "doc_id": d.doc_id,
                        "issue_date": d.issue_date,
                        "po_reference": d.ref_doc_id,
                        "total_cents": d.total_cents,
                        "evidence": "same vendor, same PO reference, same total amount",
                    }
                    for d in hard
                ],
                "same_amount_only": [
                    {"doc_id": d.doc_id, "issue_date": d.issue_date, "total_cents": d.total_cents}
                    for d in soft
                ],
            }
        )

    registry.register(
        Tool(
            name="lookup_po",
            description=(
                "Fetch a purchase order by its id (e.g. 'PO-2026-1005'). "
                "Returns the full PO with all line items, prices in integer "
                "cents. Use it to see the original ordered quantities and "
                "prices behind a discrepancy."
            ),
            input_schema={
                "type": "object",
                "properties": {"po_id": {"type": "string", "description": "The PO id"}},
                "required": ["po_id"],
            },
            handler=lookup_po,
        )
    )
    registry.register(
        Tool(
            name="lookup_grn",
            description=(
                "Fetch the goods receipt (GRN) recorded against a PO, by PO id. "
                "Returns the received quantities, or a clear message when no "
                "receipt exists — which means there is no proof of delivery."
            ),
            input_schema={
                "type": "object",
                "properties": {"po_id": {"type": "string", "description": "The PO id"}},
                "required": ["po_id"],
            },
            handler=lookup_grn,
        )
    )
    registry.register(
        Tool(
            name="get_vendor_history",
            description=(
                "List a vendor's invoices on record (dates, PO references, "
                "totals). Use it to judge whether this vendor is a regular "
                "supplier and whether the current invoice looks out of line."
            ),
            input_schema={
                "type": "object",
                "properties": {"vendor_id": {"type": "string", "description": "e.g. 'V005'"}},
                "required": ["vendor_id"],
            },
            handler=get_vendor_history,
        )
    )
    registry.register(
        Tool(
            name="check_duplicate_invoice",
            description=(
                "Check whether an invoice duplicates one already in the "
                "ledger. The comparison (same vendor, same PO reference, same "
                "total) is computed in code; you get the verdict with "
                "evidence. ALWAYS call this before approving any invoice."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string", "description": "The invoice to check"}
                },
                "required": ["invoice_id"],
            },
            handler=check_duplicate_invoice,
        )
    )


def register_recheck_tool(
    registry: ToolRegistry, store: DocumentStore, index: ContractIndex
) -> None:
    """Register recheck_against_contract: the tool that closes the authority
    loop on the contract-flip case.

    Without it, the agent would read a clause ("up to 5%") and personally
    decide that 4% is therefore fine — the model relaxing a code-set limit,
    which breaks the 'code owns authority' principle at its most visible
    moment. With it, the flow is: the MODEL decides to consult the contract;
    CODE parses the allowance out of the clause text with a regex, rebuilds
    the tolerance config, and re-runs the rules layer. The percentage never
    passes through the model — not as tool input, not as its arithmetic.
    The model's contribution is judgment (go check) and citation (quote the
    clause the tool returns), never the verdict.
    """

    def recheck_against_contract(args: dict) -> str:
        invoice_id = args.get("invoice_id", "")
        invoice = store.get_invoice(invoice_id)
        if invoice is None:
            return f"Invoice '{invoice_id}' is not in the ledger; cannot recheck."

        match = match_invoice(invoice, store.all_pos(), store.all_grns())
        allowance, rechecked = recheck_with_contract(match, invoice.vendor_id, index.chunks)
        if allowance is None:
            base = ToleranceConfig()
            return (
                f"The {invoice.vendor_id} contract grants no price variance "
                f"allowance (no 'up to N%' clause in its pricing section). The "
                f"default tolerance of {base.unit_price_pct}% stands; the "
                "out-of-tolerance verdicts in the match result are final."
            )
        pct, chunk = allowance
        price_rows = [
            {
                "line_pair": d.line_pair,
                "po_price_cents": d.po_value,
                "invoice_price_cents": d.invoice_value,
                "delta_pct": d.delta_pct,
                "within_contract_tolerance": d.within_tolerance,
            }
            for d in rechecked.discrepancies
            if d.field == DiscrepancyField.UNIT_PRICE
        ]
        return json.dumps(
            {
                "vendor_id": invoice.vendor_id,
                "contract_allowance_pct": pct,
                "parsed_by": "code (regex over the clause text), not the model",
                "source": chunk.source,
                "section": chunk.heading,
                "clause_text": chunk.text,
                "price_rows_rechecked": price_rows,
            }
        )

    registry.register(
        Tool(
            name="recheck_against_contract",
            description=(
                "Re-run the tolerance check for an invoice using the price "
                "variance allowance parsed from the vendor's contract BY CODE. "
                "Call this whenever a UNIT_PRICE discrepancy is out of "
                "tolerance: if the contract grants an allowance, this tool "
                "recomputes within_tolerance under it and returns the clause "
                "to cite. Its verdict is authoritative — do not apply a "
                "contract percentage yourself."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string", "description": "The invoice to recheck"}
                },
                "required": ["invoice_id"],
            },
            handler=recheck_against_contract,
        )
    )


def build_registry(store: DocumentStore, contracts_dir: Path) -> ToolRegistry:
    """The full toolbelt for the AP agent: lookups + contract search + recheck.

    One assembly point so the demo script, the API and the tests all run
    the agent with the same tools — a demo that quietly used an extra tool
    would be a demo of a different system.
    """
    registry = ToolRegistry()
    register_ap_tools(registry, store)
    index = register_contract_tools(registry, contracts_dir)
    register_recheck_tool(registry, store, index)
    return registry
