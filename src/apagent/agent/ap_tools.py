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
from apagent.retrieval.search import register_contract_tools
from apagent.store import DocumentStore


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

        # The comparison lives here, in code. The agent never eyeballs two
        # invoices and guesses — it gets a computed verdict with evidence.
        # Same vendor + same PO reference + same total is a hard duplicate
        # signal; same vendor + same total alone is soft (two orders can
        # legitimately cost the same), so we report the two separately.
        hard, soft = [], []
        for other in store.invoices_for_vendor(invoice.vendor_id):
            if other.doc_id == invoice.doc_id:
                continue
            same_total = other.total_cents is not None and other.total_cents == invoice.total_cents
            same_ref = other.ref_doc_id is not None and other.ref_doc_id == invoice.ref_doc_id
            if same_total and same_ref:
                hard.append(other)
            elif same_total:
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


def build_registry(store: DocumentStore, contracts_dir: Path) -> ToolRegistry:
    """The full toolbelt for the AP agent: lookups + contract search.

    One assembly point so the demo script, the API and the tests all run
    the agent with the same tools — a demo that quietly used an extra tool
    would be a demo of a different system.
    """
    registry = ToolRegistry()
    register_ap_tools(registry, store)
    register_contract_tools(registry, contracts_dir)
    return registry
