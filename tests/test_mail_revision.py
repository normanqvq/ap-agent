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
from apagent.agent.ap_tools import hard_duplicates, superseded_by
from apagent.api.service import Service
from apagent.mail.attach import MAX_ATTACHMENT_BYTES, pdf_attachments
from apagent.mail.directory import VendorDirectory
from apagent.mail.inbound import parse_mail
from apagent.mail.revise import make_revision
from apagent.matching.engine import match_invoice
from apagent.pipeline import _apply_guardrails
from apagent.rules.tolerance import apply_tolerances, requires_manual_review
from apagent.schemas import (
    Action,
    AgentDecision,
    DocType,
    Document,
    EvidenceSource,
    ToleranceConfig,
)
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


def _decide(invoice, store, action=Action.APPROVE):
    """Run the guardrail layer over a model that said `action`.

    The gates are what these tests are about, so the model is a constant.
    Going through _apply_guardrails rather than decide_invoice keeps them
    offline and keeps the failure message about the gate that fired.
    """
    config = ToleranceConfig()
    checked = apply_tolerances(match_invoice(invoice, store.all_pos(), store.all_grns()), config)
    decision = AgentDecision(
        invoice_id=invoice.doc_id,
        action=action,
        hold_reason=None,
        confidence=0.9,
        reasoning="looks fine",
        tool_calls=[],
        rounds_used=1,
    )
    return _apply_guardrails(
        decision,
        invoice,
        checked,
        review_gate=requires_manual_review(invoice.total_cents, config),
        duplicates=[],
        config=config,
        chunks=(),
        grn=store.get_grn_for_po(checked.po_id) if checked.po_id else None,
        po=store.get_po(checked.po_id) if checked.po_id else None,
        superseded=superseded_by(invoice, store),
    )


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
    first = original.model_copy(update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"})
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
        'From: a@b.test\nContent-Type: multipart/mixed; boundary="B"\n\n' + parts + "--B--\n"
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

    def send(self, message) -> bool:
        self.sent.append(message)
        return True


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


def _reply_with_two_pdfs(reply_to, message_id, sender="ar-dept@pacific.example"):
    """A letterhead graphic saved as a PDF, then the actual correction."""
    parts = "".join(
        f"--B\n"
        f"Content-Type: application/pdf\n"
        f"Content-Transfer-Encoding: base64\n"
        f'Content-Disposition: attachment; filename="{name}"\n'
        f"{base64.b64encode(_PDF + name.encode()).decode()}\n"
        for name in ("letterhead.pdf", "corrected.pdf")
    )
    return (
        f"From: AR Dept <{sender}>\n"
        f"To: {reply_to}\n"
        f"Subject: corrected invoice\n"
        f"In-Reply-To: {message_id}\n"
        f"References: {message_id}\n"
        "Date: Mon, 25 Aug 2026 10:00:00 +0800\n"
        'Content-Type: multipart/mixed; boundary="B"\n'
        "\n"
        "--B\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\n"
        "Our letterhead and the corrected invoice.\n" + parts + "--B--\n"
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


def _at_po_prices(svc, invoice_id="INV-V005-3005"):
    """The correction a vendor would actually send: the disputed unit price
    dropped to the ordered one, and every total re-added. It clears all the
    gates, which is what makes it the right document to test supersession
    with -- three copies of a correction that HOLDs prove nothing."""
    original = svc.store.get_invoice(invoice_id)
    ordered = {
        line.line_no: line.unit_price_cents for line in svc.store.get_po(original.ref_doc_id).lines
    }
    lines = [
        line.model_copy(
            update={
                "unit_price_cents": ordered.get(line.line_no, line.unit_price_cents),
                "line_total_cents": line.qty * ordered.get(line.line_no, line.unit_price_cents),
            }
        )
        for line in original.lines
    ]
    return original.model_copy(
        update={"lines": lines, "total_cents": sum(line.line_total_cents for line in lines)}
    )


def _wired(
    monkeypatch,
    corrected_total_cents=49000,
    extraction_raises=False,
    at_po_prices=False,
    cache_path=None,
):
    """A service with the mail side attached and extraction stubbed.

    cache_path redirects the decisions cache instead of stubbing the write.
    A test that stubs _save_cache cannot prove anything about what gets
    written -- which is what the "the cache file is untouched" test used to
    do, while claiming the opposite.
    """
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
    if cache_path is None:
        monkeypatch.setattr(svc, "_save_cache", lambda: None)
    else:
        # After construction: __init__ reads CACHE, and redirecting it first
        # would hand the service an empty benchmark instead of the real one.
        monkeypatch.setattr(service_module, "CACHE", cache_path)
    svc.attach_mail(
        VendorDirectory({"V005": {"email": "billing@pacific.example"}}),
        FakeSender(),
        "ap@example.test",
    )
    original = svc.store.get_invoice("INV-V005-3005")
    corrected = (
        _at_po_prices(svc)
        if at_po_prices
        else original.model_copy(update={"total_cents": corrected_total_cents})
    )

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


def test_a_pdf_that_only_looks_like_one_costs_the_reply_and_nothing_else(monkeypatch):
    """The old clause caught ExtractionError and ValueError. A truncated file
    that still starts with %PDF raises out of pdfminer, which is neither."""

    class PdfminerException(Exception):
        pass

    svc = _wired(monkeypatch)
    monkeypatch.setattr(
        service_module,
        "extract_invoice",
        lambda path, vendors, **kw: (_ for _ in ()).throw(PdfminerException("truncated")),
    )
    _deliver(svc, _reply_with_pdf)  # must not raise
    assert svc.store.get_invoice("INV-V005-3005-R1") is None


def test_a_reply_that_leads_with_an_unreadable_file_still_yields_the_correction(monkeypatch):
    """attach.py collects three attachments and justifies the cap by the cost
    of "extracting each one" -- a loop that did not exist. Only the first was
    tried, so a reply that led with a signature graphic lost its correction."""
    svc = _wired(monkeypatch, at_po_prices=True)
    original = svc.store.get_invoice("INV-V005-3005")
    corrected = _at_po_prices(svc)
    tried = []

    def fake_extract(path, vendors, **kw):
        tried.append(path.read_bytes())
        if len(tried) == 1:
            raise service_module.ExtractionError("this is a letterhead, not an invoice")
        return corrected

    monkeypatch.setattr(service_module, "extract_invoice", fake_extract)
    registry = svc.mail_harvester().registry
    query = registry.register("INV-V005-3005", "ap@example.test")
    raw = _reply_with_two_pdfs(query.reply_to, query.message_id)
    svc.on_vendor_reply(svc.mail_harvester().on_mail(parse_mail(raw)), raw)

    assert len(tried) == 2
    revision = svc.store.get_invoice("INV-V005-3005-R1")
    assert revision is not None
    assert revision.total_cents == corrected.total_cents
    assert original.total_cents != corrected.total_cents  # it really changed


def test_a_reply_whose_every_attachment_is_unreadable_raises_nothing(monkeypatch):
    svc = _wired(monkeypatch)
    monkeypatch.setattr(
        service_module,
        "extract_invoice",
        lambda path, vendors, **kw: (_ for _ in ()).throw(
            service_module.ExtractionError("unreadable")
        ),
    )
    registry = svc.mail_harvester().registry
    query = registry.register("INV-V005-3005", "ap@example.test")
    raw = _reply_with_two_pdfs(query.reply_to, query.message_id)
    svc.on_vendor_reply(svc.mail_harvester().on_mail(parse_mail(raw)), raw)
    assert svc.store.get_invoice("INV-V005-3005-R1") is None


def test_a_correction_we_cannot_decide_stays_in_the_store_undecided(monkeypatch):
    """An LLM failure raises RuntimeError from inside run_case, after the
    document is already in the store. The vendor really sent it, so it stays
    -- listed, undecided, and a reviewer can run it."""
    svc = _wired(monkeypatch)
    monkeypatch.setattr(
        "apagent.agent.loop.call_model",
        lambda messages, tools, system, provider=None: (_ for _ in ()).throw(
            RuntimeError("provider is down")
        ),
    )
    _deliver(svc, _reply_with_pdf)  # must not raise
    assert svc.store.get_invoice("INV-V005-3005-R1") is not None
    assert svc._cache.get("INV-V005-3005-R1") is None
    assert svc.get_case("INV-V005-3005-R1")["decision"] is None


def test_the_next_correction_supersedes_one_that_could_not_be_decided(monkeypatch):
    """R1 undecided must not become an orphan the chain walks past."""
    svc = _wired(monkeypatch, at_po_prices=True)
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(
            "apagent.agent.loop.call_model",
            lambda messages, tools, system, provider=None: (_ for _ in ()).throw(
                RuntimeError("provider is down")
            ),
        )
        _deliver(svc, _reply_with_pdf)
    _deliver(svc, _reply_with_pdf)
    assert svc.store.get_invoice("INV-V005-3005-R2").replaces == "INV-V005-3005-R1"
    assert svc._cache["INV-V005-3005-R2"]["action"] == Action.APPROVE


def test_a_revision_does_not_move_the_headline_metrics(monkeypatch):
    """Every tile on both pages, not just the one that cannot move.

    This used to compare the whole analytics metrics dict but only
    false_approve out of metrics() -- precisely the field a revision does
    not touch. total, decided, pending, stp_pct and touchless_pct all moved,
    and CI was happy.
    """
    svc = _wired(monkeypatch, at_po_prices=True)
    before_analytics = svc.analytics()
    before_metrics = svc.metrics()
    _deliver(svc, _reply_with_pdf)
    assert svc.metrics() == before_metrics
    after_analytics = svc.analytics()
    assert after_analytics["metrics"] == before_analytics["metrics"]
    assert after_analytics["distribution"] == before_analytics["distribution"]


def _v005(svc):
    return next(v for v in svc.analytics()["vendors"] if v["vendor_id"] == "V005")


def test_a_correction_is_one_obligation_in_the_vendor_rollup(monkeypatch):
    """The rollup is a live business view, so it SHOULD follow the
    correction down -- what it must not do is show both documents. It did:
    one order, billed twice on screen."""
    svc = _wired(monkeypatch, at_po_prices=True)
    before = _v005(svc)
    original = svc.store.get_invoice("INV-V005-3005").total_cents
    _deliver(svc, _reply_with_pdf)
    corrected = svc.store.get_invoice("INV-V005-3005-R1").total_cents

    after = _v005(svc)
    assert after["invoice_count"] == before["invoice_count"]
    assert after["billed_totals"]["USD"] == before["billed_totals"]["USD"] - original + corrected


def test_a_revision_is_reported_as_an_unmeasured_decision(monkeypatch):
    """The other half of holding it out. Dropping it from the scored rates is
    honest; dropping it from the harness's view of what was decided is how
    three approvals hid behind "false approvals: 0"."""
    svc = _wired(monkeypatch, at_po_prices=True)
    # INV-DEMO-BANKSWAP is always here too: a held-out demo decision with no
    # manifest entry, exactly like a revision -- see DEMO_HELD_OUT.
    assert svc.analytics()["unexpected"] == ["INV-DEMO-BANKSWAP"]
    _deliver(svc, _reply_with_pdf)
    assert svc.analytics()["unexpected"] == ["INV-DEMO-BANKSWAP", "INV-V005-3005-R1"]
    assert svc.metrics()["false_approve"] == 0


def test_the_committed_decision_and_cache_file_are_untouched(monkeypatch, tmp_path):
    """With the write LIVE and the cache redirected. Stubbing _save_cache and
    then hashing the file it never wrote proved nothing at all."""
    before_hash = hashlib.sha256(DECISIONS_CACHE.read_bytes()).hexdigest()
    written = tmp_path / "decisions.json"
    svc = _wired(monkeypatch, at_po_prices=True, cache_path=written)
    _deliver(svc, _reply_with_pdf)

    assert written.exists()  # the write really happened
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk["INV-V005-3005"]["action"] == "EMAIL"
    assert [k for k in on_disk if k.startswith("INV-V005-3005-R")] == []
    assert hashlib.sha256(DECISIONS_CACHE.read_bytes()).hexdigest() == before_hash


# --- one obligation, one payable document --------------------------------


def test_the_second_revision_supersedes_the_first_not_the_original(store):
    """R2 withdraws R1. Pointing it at the original instead would leave two
    live corrections, and hard_duplicates deliberately does not report one
    against the other."""
    original = store.get_invoice("INV-V005-3005")
    second = make_revision(original, original, sequence=2, supersedes="INV-V005-3005-R1")
    assert second.replaces == "INV-V005-3005-R1"


def test_a_document_with_a_successor_is_reported_as_superseded(store):
    original = store.get_invoice("INV-V005-3005")
    revision = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    store.add_invoice(revision)
    assert superseded_by(original, store).doc_id == "INV-V005-3005-R1"
    assert superseded_by(revision, store) is None


def test_the_gate_refuses_to_approve_a_superseded_document(store):
    """The authority half of the chain: hard_duplicates skips inside a
    correction chain, so something else must stop every copy being paid."""
    original = store.get_invoice("INV-V005-3005")
    revision = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    store.add_invoice(revision)
    approved = AgentDecision(
        invoice_id=original.doc_id,
        action=Action.APPROVE,
        hold_reason=None,
        confidence=0.9,
        reasoning="looks fine",
        tool_calls=[],
        rounds_used=1,
    )
    checked = apply_tolerances(
        match_invoice(original, store.all_pos(), store.all_grns()), ToleranceConfig()
    )
    out = _apply_guardrails(
        approved,
        original,
        checked,
        review_gate=False,
        duplicates=[],
        config=ToleranceConfig(),
        chunks=(),
        superseded=revision,
    )
    assert out.action == Action.ESCALATE
    assert "superseded by INV-V005-3005-R1" in out.reasoning


def test_a_vendor_who_sends_the_same_correction_three_times_is_paid_once(monkeypatch):
    """Finding 1 of the branch review, as a test. No attacker needed: a
    vendor chasing their own correction sends it again."""
    svc = _wired(monkeypatch, at_po_prices=True)
    for _ in range(3):
        _deliver(svc, _reply_with_pdf)
    chain = svc._revisions["INV-V005-3005"]
    assert chain == ["INV-V005-3005-R1", "INV-V005-3005-R2", "INV-V005-3005-R3"]
    # Each revision withdraws the one before it, not the original.
    assert [svc.store.get_invoice(doc_id).replaces for doc_id in chain] == [
        "INV-V005-3005",
        "INV-V005-3005-R1",
        "INV-V005-3005-R2",
    ]
    approved = [doc_id for doc_id in chain if svc._cache[doc_id]["action"] == Action.APPROVE]
    assert approved == ["INV-V005-3005-R3"]
    # And withdrawing R1 and R2 left the invoice they correct alone: it was
    # EMAIL (that is what sent the query), never an APPROVE, so nothing the
    # benchmark scores moved.
    assert svc._cache["INV-V005-3005"]["action"] == Action.EMAIL
    scheduled = [
        item["invoice_id"]
        for run in svc.schedule()["runs"]
        for payment in run["payments"]
        for item in payment["invoices"]
        if item["invoice_id"].startswith("INV-V005-3005")
    ]
    assert scheduled == ["INV-V005-3005-R3"]


def test_the_corrected_invoice_stops_the_original_being_payable(monkeypatch):
    """The original is not re-decided on its own, but the moment anything
    re-runs it — a reviewer clicking Run — code must refuse it."""
    svc = _wired(monkeypatch)
    _deliver(svc, _reply_with_pdf)
    case = svc.run_case("INV-V005-3005")
    assert case["decision"]["action"] == Action.ESCALATE
    assert "superseded by INV-V005-3005-R1" in case["decision"]["reasoning"]


def test_the_case_carries_everything_the_console_renders(monkeypatch):
    """Finding 9 of the branch review: get_case grew vendor_replies and
    revisions, app.js read neither, and a reviewer opening an invoice with
    three replies and three corrections saw no sign of any of it. The page
    reads these five keys -- this is the contract between them."""
    svc = _wired(monkeypatch, at_po_prices=True)
    _deliver(svc, _reply_with_pdf)

    case = svc.get_case("INV-V005-3005")
    assert case["superseded_by"] == "INV-V005-3005-R1"
    assert case["revisions"] == ["INV-V005-3005-R1"]
    assert case["vendor_query"]["answered"] is True
    reply = case["vendor_replies"][0]
    assert reply["from_addr"] == "ar-dept@pacific.example"
    assert reply["from_registered_sender"] is True
    assert reply["matched_by"] == "in_reply_to"
    assert reply["attachments"] == ["corrected.pdf"]

    correction = svc.get_case("INV-V005-3005-R1")
    assert correction["intake_source"] == "email"  # the provenance seam from main
    assert correction["superseded_by"] is None
    assert correction["decision"]["action"] == Action.APPROVE


def test_the_detail_view_shows_the_supersession_gate(monkeypatch):
    svc = _wired(monkeypatch)
    _deliver(svc, _reply_with_pdf)
    gate = next(g for g in svc.get_case("INV-V005-3005")["guardrails"] if g["key"] == "superseded")
    assert gate["passed"] is False
    assert gate["label"] == "Superseded by INV-V005-3005-R1"
    live = next(
        g for g in svc.get_case("INV-V005-3005-R1")["guardrails"] if g["key"] == "superseded"
    )
    assert live["passed"] is True


# --- the unit every other figure is counted in ---------------------------


def test_a_correction_cannot_change_the_currency(store):
    """Not identity, but the multiplier on every figure that is."""
    original = store.get_invoice("INV-V005-3005")
    extracted = original.model_copy(update={"currency": "EUR"})
    assert make_revision(original, extracted, sequence=1).currency == "USD"


def test_the_gate_refuses_an_invoice_billed_in_another_currency(store):
    """At the exact ordered prices, so nothing else can be what blocks it."""
    original = store.get_invoice("INV-V005-3005")
    po = store.get_po(original.ref_doc_id)
    at_po_prices = original.model_copy(
        update={"lines": po.lines, "total_cents": sum(x.line_total_cents for x in po.lines)}
    )
    assert _decide(at_po_prices, store).action == Action.APPROVE
    in_euros = at_po_prices.model_copy(update={"currency": "EUR"})
    out = _decide(in_euros, store)
    assert out.action == Action.ESCALATE
    assert "billed in EUR" in out.reasoning and "placed in USD" in out.reasoning


def test_an_unreadable_currency_is_not_a_match(store):
    """Extraction returns null when nothing is printed. A guardrail may only
    fail in the strict direction, so that is a hold, not a pass."""
    original = store.get_invoice("INV-V005-3005")
    po = store.get_po(original.ref_doc_id)
    unlabelled = original.model_copy(
        update={
            "lines": po.lines,
            "total_cents": sum(x.line_total_cents for x in po.lines),
            "currency": None,
        }
    )
    assert _decide(unlabelled, store).action == Action.ESCALATE


def test_two_unreadable_currencies_do_not_cancel_out(store):
    """None == None, so a PO and an invoice both missing a currency compared
    equal and sailed through the gate that exists to compare them."""
    original = store.get_invoice("INV-V005-3005")
    po = store.get_po(original.ref_doc_id)
    blank_po = po.model_copy(update={"currency": None})
    store._pos[blank_po.doc_id] = blank_po  # noqa: SLF001 - no setter, and none is wanted
    unlabelled = original.model_copy(
        update={
            "lines": po.lines,
            "total_cents": sum(x.line_total_cents for x in po.lines),
            "currency": None,
        }
    )
    assert _decide(unlabelled, store).action == Action.ESCALATE


def test_the_detail_view_shows_the_currency_gate(monkeypatch):
    svc = _wired(monkeypatch)
    gate = next(g for g in svc.get_case("INV-V005-3005")["guardrails"] if g["key"] == "currency")
    assert gate == {
        "key": "currency",
        "label": "Billed in the currency ordered (USD)",
        "passed": True,
    }


def test_a_withdrawn_document_loses_its_confirmed_payment(monkeypatch):
    """R1 was approved AND a reviewer confirmed its payment; then R2 arrived.
    _withdraw retired the decision but left the payment record's "Paid" row
    live, so R2 could earn a second live payment for the same bill — two
    standing payments, one delivery. The withdraw now voids the sign-off the
    same way a re-run does."""
    from apagent.api.service import Service
    from apagent.schemas import Action, AgentDecision

    svc = Service()
    monkeypatch.setattr(svc, "_save_cache", lambda: None)  # keep the repo file untouched
    doc_id = "INV-UP-R1"
    svc._uploaded.add(doc_id)  # session doc: never written to the disk cache
    svc._cache[doc_id] = AgentDecision(
        invoice_id=doc_id,
        action=Action.APPROVE,
        hold_reason=None,
        confidence=1.0,
        reasoning="stub",
        tool_calls=[],
        rounds_used=0,
    ).model_dump()
    svc._human[doc_id] = "confirmed"
    svc._payment_record.append(
        {
            "invoice_id": doc_id,
            "vendor_name": "Pacific Trading",
            "currency": "SGD",
            "total_cents": 49000,
            "confirmed_by": "reviewer",
            "confirmed_at": "2026-08-26T12:00:00",
            "voided": False,
        }
    )
    svc._withdraw(doc_id, "INV-UP-R2")
    assert svc._cache[doc_id]["action"] == Action.ESCALATE
    assert svc._payment_record[-1]["voided"] is True
    assert doc_id not in svc._human
