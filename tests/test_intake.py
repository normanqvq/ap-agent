"""The intake seam: external channels land in the one upload pipeline.

These pin the contract the email / Telegram fetchers build against — a source
label, the same guardrails as a manual upload, session-only state — without
running the LLM extractor (that is upload_invoice's own concern). The join
under test is intake()'s thin wrapper: tag the source, reuse the upload path,
reject an unknown channel.
"""

import pytest

from apagent.api.service import VALID_INTAKE_SOURCES, Service


def test_intake_tags_the_source_and_reuses_the_upload_path(monkeypatch):
    svc = Service()
    seen = {}

    def fake_upload(filename, content):
        seen["filename"] = filename
        seen["content"] = content
        return {"invoice_id": "INV-EMAIL-1", "decision": {"action": "HOLD"}}

    monkeypatch.setattr(svc, "upload_invoice", fake_upload)
    bundle = svc.intake("email", "april.pdf", b"%PDF-1.4 fake")
    assert bundle["intake_source"] == "email"
    assert svc._intake_source["INV-EMAIL-1"] == "email"
    # It really went through the upload path, not a parallel one.
    assert seen["filename"] == "april.pdf"
    assert seen["content"] == b"%PDF-1.4 fake"


def test_intake_rejects_an_unknown_source():
    # The source is validated before the PDF is ever extracted, so a bad
    # channel is refused cheaply and never reaches the LLM.
    with pytest.raises(ValueError):
        Service().intake("carrier-pigeon", "x.pdf", b"%PDF")


def test_upload_email_and_telegram_are_the_known_sources():
    assert "upload" in VALID_INTAKE_SOURCES
    assert {"email", "telegram"} <= VALID_INTAKE_SOURCES


def test_intake_end_to_end_through_the_real_upload_path(monkeypatch):
    """The seam WITHOUT mocking upload_invoice: stub only the LLM extractor and
    the agent, and prove intake really lands a document through the true
    upload -> run_case -> get_case path, tags it, and that the bundle carries
    the invoice_id key intake depends on. Also pins C3: the tag outlives the
    intake response — a later get_case still reports the channel."""
    from apagent.api import service as service_mod
    from apagent.schemas import Action, AgentDecision, DocType, Document, LineItem

    fake = Document(
        doc_id="INV-INTAKE-9",
        doc_type=DocType.INVOICE,
        vendor_id="V001",
        vendor_name="Tan Hardware Supplies Pte Ltd",
        issue_date="2026-08-01",
        ref_doc_id=None,
        currency="SGD",
        lines=[
            LineItem(
                line_no=1,
                sku="A-1",
                description="widget",
                qty=1,
                uom="PCS",
                unit_price_cents=100,
                line_total_cents=100,
            )
        ],
        total_cents=100,
        tax_cents=0,
    )
    monkeypatch.setattr(service_mod, "extract_invoice", lambda path, vendors: fake)
    monkeypatch.setattr(
        service_mod,
        "decide_invoice",
        lambda *a, **k: AgentDecision(
            invoice_id="INV-INTAKE-9",
            action=Action.HOLD,
            hold_reason=None,
            confidence=1.0,
            reasoning="stub",
            tool_calls=[],
            rounds_used=0,
        ),
    )
    svc = Service()
    bundle = svc.intake("telegram", "msg.pdf", b"%PDF-1.4 fake")
    assert bundle["invoice_id"] == "INV-INTAKE-9"
    assert bundle["intake_source"] == "telegram"
    assert svc.get_case("INV-INTAKE-9")["intake_source"] == "telegram"
