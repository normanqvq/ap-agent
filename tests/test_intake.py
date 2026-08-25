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
