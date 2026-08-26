"""An extracted correction plus the invoice it answers -> a revision.

The split is the whole point. A vendor's corrected invoice is allowed to
change what it is entitled to change -- prices, quantities, dates, the total
-- and nothing else. Identity comes from OUR records:

    doc_id      derived here, INV-...-R1, never the number printed on the paper
    vendor_id   carried from the invoice under query
    ref_doc_id  carried from the invoice under query
    replaces    the document this one withdraws, named by the caller

Without that, a "correction" is a way to re-point an invoice at a different
purchase order, or to bill under a different vendor's terms -- both cheaper
than any attack the correlation layer already refuses, and both invisible
afterwards, because the resulting document looks entirely ordinary.

Marked EvidenceSource.EMAIL with the evidence id that carried it, so the
provenance of every figure on it is one lookup away. A revision is an
ordinary document after this: it goes through the same pipeline, the same
gates and the same tolerances as an invoice that arrived by post.
"""

from apagent.schemas import Document, EvidenceSource


def make_revision(
    original: Document,
    extracted: Document,
    sequence: int,
    evidence_id: str | None = None,
    supersedes: str | None = None,
) -> Document:
    """The revision document, with identity owned by code.

    supersedes is the document this correction withdraws, which is the
    NEWEST document in the chain, not necessarily `original`. Pointing every
    revision at the original instead would leave R1, R2 and R3 as three
    siblings that all replace something already withdrawn and none of which
    replaces the others -- three payable invoices for one obligation. The
    pipeline's supersession gate reads this link, so getting it wrong here
    is a payment, not a display bug. Defaults to the original, which is what
    the first revision in a chain supersedes.
    """
    return extracted.model_copy(
        update={
            "doc_id": f"{original.doc_id}-R{sequence}",
            "doc_type": original.doc_type,
            "vendor_id": original.vendor_id,
            "vendor_name": original.vendor_name,
            "ref_doc_id": original.ref_doc_id,
            "replaces": supersedes or original.doc_id,
            "source": EvidenceSource.EMAIL,
            "source_ref": evidence_id,
        }
    )
