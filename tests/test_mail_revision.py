"""A corrected invoice, attached to a vendor's reply, matched again by code.

Offline: extraction is stubbed everywhere. These tests are about what WE do
with a document a counterparty sent us, not about reading PDFs.
"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

import apagent.api.service as service_module
from apagent.agent.ap_tools import hard_duplicates
from apagent.api.service import Service
from apagent.mail.attach import MAX_ATTACHMENT_BYTES, pdf_attachments
from apagent.mail.directory import VendorDirectory
from apagent.mail.inbound import parse_mail
from apagent.mail.revise import make_revision
from apagent.schemas import DocType, Document, EvidenceSource
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"
DECISIONS_CACHE = DATA / "decisions.json"


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


def test_the_revision_keeps_our_identity_not_the_vendors(store):
    original = store.get_invoice("INV-V005-3005")
    # Everything a hostile "correction" might try to redirect:
    extracted = original.model_copy(
        update={
            "doc_id": "TOTALLY-DIFFERENT-NUMBER",
            "vendor_id": "V001",
            "vendor_name": "Someone Else Pte Ltd",
            "ref_doc_id": "PO-2026-1001",
            "total_cents": 49000,
        }
    )
    revision = make_revision(original, extracted, sequence=1)
    assert revision.doc_id == "INV-V005-3005-R1"
    assert revision.vendor_id == "V005"
    assert revision.vendor_name == "Pacific Circuit Components Inc."
    assert revision.ref_doc_id == "PO-2026-1005"
    assert revision.replaces == "INV-V005-3005"
    # ... while the figures it IS allowed to correct come from the paper:
    assert revision.total_cents == 49000


def test_a_second_revision_numbers_itself(store):
    original = store.get_invoice("INV-V005-3005")
    assert make_revision(original, original, sequence=2).doc_id == "INV-V005-3005-R2"


def test_the_revision_is_flagged_as_having_arrived_by_email(store):
    original = store.get_invoice("INV-V005-3005")
    revision = make_revision(original, original, sequence=1, evidence_id="MAIL-EV-0001")
    assert revision.source == EvidenceSource.EMAIL
    assert revision.source_ref == "MAIL-EV-0001"


def test_the_corrected_line_prices_are_the_vendors(store):
    """The point of the whole exercise: the figures really do come from the
    corrected document, or there would be nothing to re-match."""
    original = store.get_invoice("INV-V005-3005")
    cheaper = [line.model_copy(update={"unit_price_cents": 100}) for line in original.lines]
    extracted = original.model_copy(update={"lines": cheaper})
    revision = make_revision(original, extracted, sequence=1)
    assert [line.unit_price_cents for line in revision.lines] == [100] * len(original.lines)


# --- the service joins the pieces: a reply that raises a revision ---------


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def _reply_with_pdf(reply_to, message_id, sender="ar-dept@pacific.example"):
    """A reply that correlates AND carries a corrected invoice."""
    encoded = base64.b64encode(_PDF).decode()
    return (
        f"From: AR Dept <{sender}>\n"
        f"To: {reply_to}\n"
        "Subject: corrected invoice\n"
        f"In-Reply-To: {message_id}\n"
        f"References: {message_id}\n"
        "Date: Mon, 25 Aug 2026 10:00:00 +0800\n"
        'Content-Type: multipart/mixed; boundary="B"\n'
        "\n"
        "--B\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\n"
        "You are right, here is the corrected invoice.\n"
        "--B\n"
        "Content-Type: application/pdf\n"
        "Content-Transfer-Encoding: base64\n"
        'Content-Disposition: attachment; filename="corrected.pdf"\n'
        "\n"
        f"{encoded}\n"
        "--B--\n"
    ).encode()


def _reply_text_only(reply_to, message_id, sender="ar-dept@pacific.example"):
    return (
        f"From: AR Dept <{sender}>\n"
        f"To: {reply_to}\n"
        "Subject: re: query\n"
        f"In-Reply-To: {message_id}\n"
        f"References: {message_id}\n"
        "Date: Mon, 25 Aug 2026 10:00:00 +0800\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        "We will look into it, no attachment yet.\n"
    ).encode()


def _bounce_with_pdf(reply_to, message_id, sender="mailer-daemon@example.test"):
    encoded = base64.b64encode(_PDF).decode()
    return (
        f"From: Mail Delivery System <{sender}>\n"
        f"To: {reply_to}\n"
        "Subject: Undelivered Mail Returned to Sender\n"
        f"In-Reply-To: {message_id}\n"
        f"References: {message_id}\n"
        "Auto-Submitted: auto-replied\n"
        "Date: Mon, 25 Aug 2026 10:00:00 +0800\n"
        'Content-Type: multipart/mixed; boundary="B"\n'
        "\n"
        "--B\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\n"
        "This is an automatically generated Delivery Status Notification.\n"
        "--B\n"
        "Content-Type: application/pdf\n"
        "Content-Transfer-Encoding: base64\n"
        'Content-Disposition: attachment; filename="original.pdf"\n'
        "\n"
        f"{encoded}\n"
        "--B--\n"
    ).encode()


def _wired(monkeypatch, corrected_total_cents=49000, extraction_raises=False):
    """A service with the mail side attached and extraction stubbed."""
    # The revision runs the whole pipeline, which calls the agent. Stubbed
    # the way test_chat stubs it: this suite is about what code does with a
    # corrected document, not about the model. _save_cache is stubbed too --
    # a test must never rewrite the committed benchmark.
    monkeypatch.setattr(
        "apagent.agent.loop.call_model",
        lambda messages, tools, system, provider=None: {
            "text": json.dumps(
                {"action": "APPROVE", "hold_reason": None, "confidence": 0.9, "reasoning": "ok"}
            ),
            "tool_calls": [],
        },
    )
    svc = Service()
    monkeypatch.setattr(svc, "_save_cache", lambda: None)
    svc.attach_mail(
        VendorDirectory({"V005": {"email": "billing@pacific.example"}}),
        FakeSender(),
        "ap@example.test",
    )
    original = svc.store.get_invoice("INV-V005-3005")
    corrected = original.model_copy(update={"total_cents": corrected_total_cents})

    def fake_extract(path, vendors, **kw):
        if extraction_raises:
            raise service_module.ExtractionError("unreadable")
        return corrected

    monkeypatch.setattr(service_module, "extract_invoice", fake_extract)
    return svc


def _deliver(svc, raw_builder, sender="ar-dept@pacific.example"):
    """Register a query, build the reply, and run it through the service."""
    registry = svc.mail_harvester().registry
    query = registry.register("INV-V005-3005", "ap@example.test")
    raw = raw_builder(query.reply_to, query.message_id, sender)
    evidence = svc.mail_harvester().on_mail(parse_mail(raw))
    svc.on_vendor_reply(evidence, raw)
    return evidence


def test_a_reply_with_a_pdf_raises_a_revision_in_the_store(monkeypatch):
    svc = _wired(monkeypatch)
    evidence = _deliver(svc, _reply_with_pdf)
    assert evidence is not None
    assert evidence.from_registered_sender is True
    revision = svc.store.get_invoice("INV-V005-3005-R1")
    assert revision is not None
    assert revision.total_cents == 49000
    case = svc.get_case("INV-V005-3005-R1")
    assert case["decision"] is not None
    original_case = svc.get_case("INV-V005-3005")
    assert "INV-V005-3005-R1" in original_case["revisions"]


def test_an_unregistered_sender_produces_no_revision(monkeypatch):
    svc = _wired(monkeypatch)
    evidence = _deliver(svc, _reply_with_pdf, sender="someone@gmail.com")
    assert evidence is not None
    assert evidence.from_registered_sender is False
    assert svc.store.get_invoice("INV-V005-3005-R1") is None


def test_a_bounce_produces_no_revision(monkeypatch):
    svc = _wired(monkeypatch)
    evidence = _deliver(svc, _bounce_with_pdf)
    assert evidence is not None
    assert evidence.is_non_delivery is True
    assert svc.store.get_invoice("INV-V005-3005-R1") is None


def test_a_text_only_reply_produces_no_revision_and_no_error(monkeypatch):
    svc = _wired(monkeypatch)
    evidence = _deliver(svc, _reply_text_only)
    assert evidence is not None
    assert svc.store.get_invoice("INV-V005-3005-R1") is None


def test_extraction_failure_leaves_evidence_but_no_revision_and_does_not_raise(monkeypatch):
    svc = _wired(monkeypatch, extraction_raises=True)
    evidence = _deliver(svc, _reply_with_pdf)  # must not raise
    assert evidence is not None
    assert svc.store.get_invoice("INV-V005-3005-R1") is None
    replies = svc._vendor_replies.get("INV-V005-3005", [])
    assert any(r["evidence_id"] == evidence.evidence_id for r in replies)


def test_a_revision_does_not_move_the_headline_metrics(monkeypatch):
    svc = _wired(monkeypatch)
    before_analytics = svc.analytics()["metrics"]
    before_metrics = svc.metrics()["false_approve"]
    _deliver(svc, _reply_with_pdf)
    after_analytics = svc.analytics()["metrics"]
    after_metrics = svc.metrics()["false_approve"]
    assert after_analytics == before_analytics
    assert after_metrics == before_metrics


def test_the_committed_decision_and_cache_file_are_untouched(monkeypatch):
    before_hash = hashlib.sha256(DECISIONS_CACHE.read_bytes()).hexdigest()
    svc = _wired(monkeypatch)
    _deliver(svc, _reply_with_pdf)
    on_disk = json.loads(DECISIONS_CACHE.read_text(encoding="utf-8"))
    assert on_disk["INV-V005-3005"]["action"] == "EMAIL"
    after_hash = hashlib.sha256(DECISIONS_CACHE.read_bytes()).hexdigest()
    assert after_hash == before_hash
