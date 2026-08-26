"""Photo goods-receipt: a photographed delivery note becomes a chat-tier GRN.

The image path is the text path with a different reader: extract a claim, then
run the EXACT chat resolve + grn_gate. These tests pin that the seam is real
(the photo releases the missing-GRN case through the true guardrail, not a
mock), that unreadable photos refuse, and that the capability cleanly does not
exist on a provider without image support.

Offline: the multimodal call and the agent are both stubbed. What runs for real
is resolve_grn and the six guardrails — the parts that decide whether a photo
may release money.
"""

import pytest

import apagent.pipeline as pipeline
from apagent.api.service import Service
from apagent.chat import vision
from apagent.llm.client import call_model_vision
from apagent.schemas import Action, AgentDecision, EvidenceSource


def _approve_agent(**kwargs):
    """A stand-in agent that always approves, so the code guardrails — not the
    model — decide whether the photo receipt releases the invoice."""
    return AgentDecision(
        invoice_id=kwargs["invoice_id"],
        action=Action.APPROVE,
        hold_reason=None,
        confidence=1.0,
        reasoning="stub agent approve",
        tool_calls=[],
        rounds_used=0,
    )


def test_call_model_vision_refuses_a_provider_without_image_support():
    """DeepSeek/Groq/OpenAI have no image path here; the capability is absent
    by construction and fails loudly rather than mis-reading a receipt."""
    with pytest.raises(ValueError, match="image"):
        call_model_vision(b"x", "image/jpeg", "prompt", "system", provider="deepseek")


def test_extract_from_image_parses_the_same_claim_schema(monkeypatch):
    """The image reader emits the identical claim dict the text reader does, so
    resolve_grn consumes it unchanged."""
    monkeypatch.setattr(
        vision,
        "call_model_vision",
        lambda **kw: {
            "text": '{"is_delivery_confirmation": true, "po_reference": "PO-1", '
            '"items": [], "everything_arrived": true, "notes": null}',
            "usage": None,
        },
    )
    claim = vision.extract_delivery_claim_from_image(b"img", "image/jpeg")
    assert claim["is_delivery_confirmation"] is True
    assert claim["po_reference"] == "PO-1"


def test_photo_releases_the_missing_grn_case(monkeypatch):
    """INV-V006-3019 is planted missing_grn (SGD 1,270, under the SGD 2,000
    informal ceiling). A photo of the docket, uploaded by a signed-in reviewer,
    clears the proof-of-delivery gate and the invoice releases — decided by the
    real grn_gate, with the model stubbed to approve."""
    svc = Service()
    inv = svc.store.get_invoice("INV-V006-3019")
    po_id = inv.ref_doc_id
    monkeypatch.setattr(
        vision,
        "extract_delivery_claim_from_image",
        lambda image_bytes, media_type, provider=None: {
            "is_delivery_confirmation": True,
            "po_reference": po_id,
            "items": [],
            "everything_arrived": True,
            "notes": None,
        },
    )
    monkeypatch.setattr(pipeline, "run_agent", _approve_agent)
    bundle = svc.upload_delivery_photo("INV-V006-3019", b"fakejpg", "image/jpeg")
    assert bundle["decision"]["action"] == "APPROVE"
    assert bundle["chat_grn"]["source"] == "photo"


def test_photo_naming_a_different_po_is_refused(monkeypatch):
    """The photo is uploaded from ONE invoice's page, so it must confirm that
    invoice's order. A docket naming some other PO is refused outright —
    otherwise a receipt would land on an order the reviewer never meant to
    vouch for, and the invoices it actually affects would sit outside
    _chat_confirmed, where a later re-run leaks session evidence into the
    benchmark numbers."""
    svc = Service()
    other_po = svc.store.get_invoice("INV-V001-3001").ref_doc_id
    monkeypatch.setattr(
        vision,
        "extract_delivery_claim_from_image",
        lambda image_bytes, media_type, provider=None: {
            "is_delivery_confirmation": True,
            "po_reference": other_po,
            "items": [],
            "everything_arrived": True,
            "notes": None,
        },
    )
    with pytest.raises(ValueError, match="names order"):
        svc.upload_delivery_photo("INV-V006-3019", b"fakejpg", "image/jpeg")
    # Nothing was recorded: the other order keeps its ERP receipt, and no
    # invoice was marked chat-confirmed.
    assert svc.store.get_grn_for_po(other_po).source == EvidenceSource.ERP
    assert not svc._chat_confirmed


def test_unsupported_image_type_is_refused_before_the_model(monkeypatch):
    """An iPhone HEIC gets a clear refusal up front, not an opaque provider
    error after a wasted vision call."""
    svc = Service()
    monkeypatch.setattr(
        vision,
        "extract_delivery_claim_from_image",
        lambda *a, **k: pytest.fail("the extractor must not run for a rejected type"),
    )
    with pytest.raises(ValueError, match="unsupported image type"):
        svc.upload_delivery_photo("INV-V006-3019", b"heicbytes", "image/heic")


def test_photo_cannot_shadow_an_erp_receipt(monkeypatch):
    """When the order already has an ERP receipt there is nothing for a photo
    to add; refused before the model call (the store would refuse the
    ERP -> CHAT downgrade anyway — this just refuses it cheaply and clearly)."""
    svc = Service()
    monkeypatch.setattr(
        vision,
        "extract_delivery_claim_from_image",
        lambda *a, **k: pytest.fail("the extractor must not run when an ERP receipt exists"),
    )
    with pytest.raises(ValueError, match="ERP goods receipt already exists"):
        svc.upload_delivery_photo("INV-V001-3001", b"fakejpg", "image/jpeg")


def test_photo_that_confirms_nothing_is_refused(monkeypatch):
    """A blurred or irrelevant photo (no delivery confirmed) refuses before the
    agent ever runs — the safe direction, surfaced to the reviewer."""
    svc = Service()
    monkeypatch.setattr(
        vision,
        "extract_delivery_claim_from_image",
        lambda image_bytes, media_type, provider=None: {
            "is_delivery_confirmation": False,
            "po_reference": None,
            "items": [],
            "everything_arrived": False,
            "notes": None,
        },
    )
    with pytest.raises(ValueError, match="did not confirm"):
        svc.upload_delivery_photo("INV-V006-3019", b"blur", "image/jpeg")


def test_reupload_gets_a_fresh_evidence_id(monkeypatch):
    """A same-PO re-upload (a better photo of the same docket) overwrites its
    receipt entry, so a dict-size counter would stall and hand the NEXT upload
    a duplicate evidence id. The monotonic counter cannot."""
    svc = Service()
    po_id = svc.store.get_invoice("INV-V006-3019").ref_doc_id
    monkeypatch.setattr(
        vision,
        "extract_delivery_claim_from_image",
        lambda image_bytes, media_type, provider=None: {
            "is_delivery_confirmation": True,
            "po_reference": po_id,
            "items": [],
            "everything_arrived": True,
            "notes": None,
        },
    )
    monkeypatch.setattr(pipeline, "run_agent", _approve_agent)
    receipt_id = f"GRN-CHAT-{po_id.split('-')[-1]}-1"
    svc.upload_delivery_photo("INV-V006-3019", b"first", "image/jpeg")
    first = svc._chat_evidence[receipt_id].evidence_id
    svc.upload_delivery_photo("INV-V006-3019", b"better", "image/jpeg")
    second = svc._chat_evidence[receipt_id].evidence_id
    assert first == "PHOTO-EV-0001"
    assert second == "PHOTO-EV-0002"
