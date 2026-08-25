"""A corrected invoice, attached to a vendor's reply, matched again by code.

Offline: extraction is stubbed everywhere. These tests are about what WE do
with a document a counterparty sent us, not about reading PDFs.
"""

from apagent.schemas import DocType, Document


def _doc(doc_id, **kw):
    base = dict(
        doc_id=doc_id,
        doc_type=DocType.INVOICE,
        vendor_id="V005",
        vendor_name="Pacific Circuit Components Inc",
        issue_date="2026-08-14",
        ref_doc_id="PO-2026-1005",
        currency="USD",
        lines=[],
        total_cents=53040,
    )
    base.update(kw)
    return Document(**base)


def test_a_document_records_what_it_supersedes():
    revision = _doc("INV-V005-3005-R1", replaces="INV-V005-3005")
    assert revision.replaces == "INV-V005-3005"


def test_an_ordinary_document_supersedes_nothing():
    assert _doc("INV-V005-3005").replaces is None
