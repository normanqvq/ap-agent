"""A corrected invoice, attached to a vendor's reply, matched again by code.

Offline: extraction is stubbed everywhere. These tests are about what WE do
with a document a counterparty sent us, not about reading PDFs.
"""

from pathlib import Path

import pytest

from apagent.agent.ap_tools import hard_duplicates
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
