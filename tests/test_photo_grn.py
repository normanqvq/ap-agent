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
from apagent.schemas import Action, AgentDecision


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
