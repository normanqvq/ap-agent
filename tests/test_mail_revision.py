"""A corrected invoice, attached to a vendor's reply, matched again by code.

Offline: extraction is stubbed everywhere. These tests are about what WE do
with a document a counterparty sent us, not about reading PDFs.
"""

import base64
from pathlib import Path

import pytest

from apagent.agent.ap_tools import hard_duplicates
from apagent.mail.attach import MAX_ATTACHMENT_BYTES, pdf_attachments
from apagent.schemas import DocType, Document
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


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


@pytest.fixture
def store():
    return DocumentStore.from_dir(DATA)


def test_a_revision_is_not_a_duplicate_of_what_it_replaces(store):
    original = store.get_invoice("INV-V005-3005")
    revision = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    store.add_invoice(revision)
    assert hard_duplicates(revision, store) == []


def test_the_original_is_not_a_duplicate_of_its_revision(store):
    """The link is symmetric in effect, whichever side is being decided."""
    original = store.get_invoice("INV-V005-3005")
    revision = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    store.add_invoice(revision)
    assert hard_duplicates(original, store) == []


def test_a_second_revision_is_not_a_duplicate_of_the_first(store):
    """R2 replaces R1 replaces the original: the whole chain is one invoice."""
    original = store.get_invoice("INV-V005-3005")
    first = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    second = original.model_copy(
        update={"doc_id": "INV-V005-3005-R2", "replaces": "INV-V005-3005-R1"}
    )
    store.add_invoice(first)
    store.add_invoice(second)
    assert hard_duplicates(second, store) == []


def test_a_genuine_duplicate_is_still_caught(store):
    """The guard must not become a way to launder a resubmission: an
    unlinked copy of the same invoice is still a duplicate."""
    original = store.get_invoice("INV-V005-3005")
    copy = original.model_copy(update={"doc_id": "INV-V005-3005-COPY"})
    store.add_invoice(copy)
    assert [d.doc_id for d in hard_duplicates(copy, store)] == ["INV-V005-3005"]


def test_a_forged_replaces_pointing_at_nothing_is_still_a_duplicate(store):
    """`replaces` is set by code, but if a document ever arrives carrying one
    that names no document we hold, it buys nothing."""
    original = store.get_invoice("INV-V005-3005")
    copy = original.model_copy(
        update={"doc_id": "INV-V005-3005-COPY", "replaces": "INV-NOT-A-DOCUMENT"}
    )
    store.add_invoice(copy)
    assert [d.doc_id for d in hard_duplicates(copy, store)] == ["INV-V005-3005"]


_PDF = b"%PDF-1.4\n... not a real pdf, but it starts like one ...\n%%EOF\n"


def _with_attachment(payload: bytes, filename: str = "corrected.pdf") -> bytes:
    encoded = base64.b64encode(payload).decode()
    return (
        "From: AR Dept <ar-dept@pacific.example>\n"
        "To: ap@example.test\n"
        "Subject: corrected\n"
        'Content-Type: multipart/mixed; boundary="B"\n'
        "\n"
        "--B\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\n"
        "Corrected invoice attached.\n"
        "--B\n"
        "Content-Type: application/pdf\n"
        "Content-Transfer-Encoding: base64\n"
        f'Content-Disposition: attachment; filename="{filename}"\n'
        "\n"
        f"{encoded}\n"
        "--B--\n"
    ).encode()


def test_a_pdf_attachment_comes_back_with_its_bytes():
    found = pdf_attachments(_with_attachment(_PDF))
    assert [name for name, _ in found] == ["corrected.pdf"]
    assert found[0][1].startswith(b"%PDF")


def test_a_message_with_no_attachment_yields_nothing():
    assert pdf_attachments(b"From: a@b.test\n\njust text\n") == []


def test_something_merely_named_pdf_is_refused():
    """The filename is the sender's choice; the magic bytes are not."""
    assert pdf_attachments(_with_attachment(b"MZ\x90\x00 this is a windows binary")) == []


def test_an_oversized_attachment_is_refused():
    assert pdf_attachments(_with_attachment(b"%PDF-" + b"x" * MAX_ATTACHMENT_BYTES)) == []


def test_only_the_first_few_attachments_are_considered():
    """A reply with forty attachments is not a correction, and extracting
    each one costs a model call."""
    parts = "".join(
        "--B\nContent-Type: application/pdf\n"
        "Content-Transfer-Encoding: base64\n"
        f'Content-Disposition: attachment; filename="a{i}.pdf"\n\n'
        f"{base64.b64encode(_PDF).decode()}\n"
        for i in range(10)
    )
    raw = (
        "From: a@b.test\n"
        'Content-Type: multipart/mixed; boundary="B"\n\n' + parts + "--B--\n"
    ).encode()
    assert len(pdf_attachments(raw)) == 3


def test_a_malformed_message_yields_nothing_instead_of_raising():
    assert pdf_attachments(b"\xff\xfe not a message at all") == []
