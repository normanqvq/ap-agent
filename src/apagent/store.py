"""In-memory document store: the single place tools go to for documents.

Why a dict in memory and not a database: the whole dataset is ~60 small
JSON documents. SQLite would add a schema, migrations and a connection to
manage, and buy us nothing the demo needs — every query the agent's tools
make is an exact lookup or a small scan. The gap analysis said it plainly:
"内存 dict + JSON 就够，别上数据库". If the corpus ever outgrows memory,
this class is the one seam to swap; the tools only talk to DocumentStore.

Why the store is passed into tools instead of being a module-level global:
tests build a tiny store with three documents and register tools against
it. A global store would make every test depend on the committed dataset.
"""

import json
from pathlib import Path

from apagent.schemas import DocType, Document, EvidenceSource


class DocumentStore:
    """All POs, GRNs and invoices, indexed for the lookups the tools need."""

    def __init__(self, pos: list[Document], grns: list[Document], invoices: list[Document]):
        self._pos = {doc.doc_id: doc for doc in pos}
        # One GRN per PO in our world; keyed by the PO it confirms, because
        # "is there a receipt for this order?" is the question the agent asks.
        # A GRN's own id is never the lookup key.
        self._grns_by_po = {doc.ref_doc_id: doc for doc in grns if doc.ref_doc_id}
        self._invoices = {doc.doc_id: doc for doc in invoices}

    @classmethod
    def from_dir(cls, data_dir: Path) -> "DocumentStore":
        """Load the synthetic dataset directory (pos/grns/invoices .json)."""

        def load(name: str) -> list[Document]:
            return [
                Document(**d) for d in json.loads((data_dir / name).read_text(encoding="utf-8"))
            ]

        return cls(load("pos.json"), load("grns.json"), load("invoices.json"))

    # --- exact lookups ------------------------------------------------------

    def get_po(self, doc_id: str) -> Document | None:
        return self._pos.get(doc_id)

    def get_grn_for_po(self, po_id: str) -> Document | None:
        return self._grns_by_po.get(po_id)

    def get_invoice(self, doc_id: str) -> Document | None:
        return self._invoices.get(doc_id)

    # --- scans --------------------------------------------------------------

    def invoices_for_vendor(self, vendor_id: str) -> list[Document]:
        """This vendor's invoices, oldest first — the billing history."""
        docs = [d for d in self._invoices.values() if d.vendor_id == vendor_id]
        return sorted(docs, key=lambda d: d.issue_date)

    def all_pos(self) -> list[Document]:
        """For the matching engine's fallback PO search."""
        return list(self._pos.values())

    def all_grns(self) -> list[Document]:
        return list(self._grns_by_po.values())

    def vendors(self) -> dict[str, str]:
        """vendor_id -> canonical name, for extraction's name matching.

        Canonical names come from the POs — a PO is OUR document, so the
        name on it is spelled the way our records spell it. Invoice
        spellings drift (that is why match_vendor_id exists), so they are
        deliberately not the source here.
        """
        return {po.vendor_id: po.vendor_name for po in self._pos.values()}

    def add_invoice(self, doc: Document) -> None:
        """Register a newly extracted invoice (the API upload path).

        The duplicate-check tool scans the invoice ledger, so an uploaded
        invoice must be in the ledger before the agent runs on it.
        """
        if doc.doc_type != DocType.INVOICE:
            raise ValueError(f"add_invoice got a {doc.doc_type}, not an invoice")
        self._invoices[doc.doc_id] = doc

    def add_grn(self, doc: Document) -> None:
        """Register a goods receipt (the chat-confirmation path).

        Three hard rejects, all failing closed:

        - Not a GRN. Same rule as add_invoice.
        - No ref_doc_id. __init__ silently DROPS such GRNs (see above) and
          all_grns() reads the by-PO index, so a ref-less GRN would vanish
          instead of failing. A silent no-op is the worst outcome here: the
          caller would believe delivery was recorded when nothing was.
        - Replacing an ERP receipt with a CHAT one. That is a downgrade
          attack — a chat message overwriting what the warehouse actually
          recorded, potentially with smaller quantities. CHAT -> CHAT
          (re-confirmation) and CHAT -> ERP (the warehouse catches up later)
          are both fine; only ERP -> CHAT is refused.

        Note the single slot per PO: a second CHAT confirmation replaces the
        first rather than adding to it. Partial deliveries must therefore
        arrive as multiple LINES on one receipt (build_discrepancies sums per
        sku), not as multiple documents.
        """
        if doc.doc_type != DocType.GRN:
            raise ValueError(f"add_grn got a {doc.doc_type}, not a goods receipt")
        if not doc.ref_doc_id:
            raise ValueError(
                f"add_grn got {doc.doc_id} with no ref_doc_id; a goods receipt is "
                "indexed by the PO it confirms and would be silently dropped"
            )
        existing = self._grns_by_po.get(doc.ref_doc_id)
        if (
            existing is not None
            and existing.source == EvidenceSource.ERP
            and doc.source == EvidenceSource.CHAT
        ):
            raise ValueError(
                f"add_grn refused to replace ERP receipt {existing.doc_id} with "
                f"chat-sourced {doc.doc_id} for {doc.ref_doc_id}"
            )
        self._grns_by_po[doc.ref_doc_id] = doc
