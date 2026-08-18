"""Tests for the end-to-end pipeline, with the LLM monkeypatched.

What these prove: the task message handed to the model carries the right
facts for each planted defect. The model's judgment is tested live (demo
script); the facts it judges from are pinned here, offline.
"""

import json
from pathlib import Path

import pytest

from apagent.agent.ap_tools import build_registry
from apagent.pipeline import decide_invoice
from apagent.schemas import Action
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


@pytest.fixture(scope="module")
def store():
    return DocumentStore.from_dir(DATA)


@pytest.fixture(scope="module")
def registry(store):
    return build_registry(store, DATA / "contracts")


def run_capturing_task(monkeypatch, store, registry, invoice_id):
    """Run the pipeline with a stub model and capture the task message."""
    captured = {}

    def fake_call_model(messages, tools, system, provider=None):
        captured["task"] = messages[0]["content"]
        captured["system"] = system
        return {
            "text": json.dumps(
                {"action": "ESCALATE", "hold_reason": None, "confidence": 1.0, "reasoning": "stub"}
            ),
            "tool_calls": [],
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", fake_call_model)
    invoice = store.get_invoice(invoice_id)
    decision = decide_invoice(invoice, store, registry)
    captured["decision"] = decision
    return captured


def test_clean_invoice_task_shows_clean_facts(monkeypatch, store, registry):
    captured = run_capturing_task(monkeypatch, store, registry, "INV-V001-3001")
    payload = json.loads(captured["task"].split("\n\n", 1)[1])
    assert payload["match_result"]["discrepancies"] == []
    assert payload["match_result"]["match_confidence"] == 1.0
    assert payload["manual_review_required"] is False


def test_headline_case_arrives_out_of_tolerance_by_default(monkeypatch, store, registry):
    """INV-V005-3018: with no preloaded override, the 4% variance must reach
    the agent flagged out-of-tolerance — finding the contract's 5% clause is
    the agent's job, live."""
    captured = run_capturing_task(monkeypatch, store, registry, "INV-V005-3018")
    payload = json.loads(captured["task"].split("\n\n", 1)[1])
    price_rows = [d for d in payload["match_result"]["discrepancies"] if d["field"] == "UNIT_PRICE"]
    assert len(price_rows) == 1
    assert price_rows[0]["within_tolerance"] is False


def test_missing_grn_case_arrives_with_null_grn(monkeypatch, store, registry):
    captured = run_capturing_task(monkeypatch, store, registry, "INV-V006-3019")
    payload = json.loads(captured["task"].split("\n\n", 1)[1])
    assert payload["match_result"]["grn_id"] is None
    assert payload["match_result"]["match_confidence"] == 0.7


def test_large_invoice_trips_the_review_gate(monkeypatch, store, registry):
    """INV-V002-3008 totals SGD 5,853.30 — at or above the SGD 5,000 gate it
    must arrive flagged. The flag is computed in code, before the model runs."""
    assert store.get_invoice("INV-V002-3008").total_cents >= 500_000
    captured = run_capturing_task(monkeypatch, store, registry, "INV-V002-3008")
    payload = json.loads(captured["task"].split("\n\n", 1)[1])
    assert payload["manual_review_required"] is True


def test_stub_decision_round_trips(monkeypatch, store, registry):
    captured = run_capturing_task(monkeypatch, store, registry, "INV-V001-3001")
    assert captured["decision"].action == Action.ESCALATE
    assert captured["decision"].invoice_id == "INV-V001-3001"


def _defiant_approve(monkeypatch):
    """A model that approves everything — the adversary every guardrail
    test runs against."""

    def defiant_model(messages, tools, system, provider=None):
        return {
            "text": json.dumps(
                {
                    "action": "APPROVE",
                    "hold_reason": None,
                    "confidence": 0.99,
                    "reasoning": "looks fine to me",
                }
            ),
            "tool_calls": [],
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", defiant_model)


def test_code_guardrail_overrides_approve_above_the_gate(monkeypatch, store, registry):
    """The architecture claim, as a test: a model that answers APPROVE on an
    invoice above the manual-review threshold gets overridden IN CODE. The
    prompt asks for compliance; this is what happens when it doesn't get it."""
    _defiant_approve(monkeypatch)
    invoice = store.get_invoice("INV-V002-3008")  # SGD 5,853.30, above the gate
    decision = decide_invoice(invoice, store, registry)

    assert decision.action == Action.ESCALATE
    assert "[code guardrail]" in decision.reasoning
    assert "looks fine to me" in decision.reasoning  # model reasoning preserved for audit


def test_code_guardrail_blocks_approving_a_duplicate(monkeypatch, store, registry):
    """INV-V003-3901 hard-duplicates the earlier INV-V003-3003. Approving it
    must not depend on the model calling any tool — code blocks it."""
    _defiant_approve(monkeypatch)
    decision = decide_invoice(store.get_invoice("INV-V003-3901"), store, registry)
    assert decision.action == Action.ESCALATE
    assert "INV-V003-3003" in decision.reasoning


def test_duplicate_gate_leaves_the_original_approvable(monkeypatch, store, registry):
    """The ORIGINAL (earlier-dated) invoice of the pair is not blocked —
    blocking both forever would mean the vendor never gets paid at all."""
    _defiant_approve(monkeypatch)
    decision = decide_invoice(store.get_invoice("INV-V003-3003"), store, registry)
    assert decision.action == Action.APPROVE


def test_code_guardrail_blocks_approving_without_grn(monkeypatch, store, registry):
    """INV-V006-3019 has no goods receipt; an APPROVE becomes HOLD in code,
    and the ops chase message is rendered from the template."""
    _defiant_approve(monkeypatch)
    decision = decide_invoice(store.get_invoice("INV-V006-3019"), store, registry)
    assert decision.action == Action.HOLD
    assert decision.hold_reason is not None
    assert decision.hold_reason.value == "AWAITING_GRN"
    assert decision.outbound_message is not None
    assert "PO-2026-1019" in decision.outbound_message


def test_outbound_message_is_code_templated_not_model_written(monkeypatch, store, registry):
    """A cooperative model that HOLDs for a missing GRN gets the outbound
    message filled by code — with the CANONICAL vendor name from our vendor
    directory, not whatever the invoice printed."""

    def holding_model(messages, tools, system, provider=None):
        return {
            "text": json.dumps(
                {
                    "action": "HOLD",
                    "hold_reason": "AWAITING_GRN",
                    "confidence": 0.9,
                    "reasoning": "no receipt",
                }
            ),
            "tool_calls": [],
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", holding_model)
    decision = decide_invoice(store.get_invoice("INV-V006-3019"), store, registry)
    assert decision.outbound_message is not None
    assert store.vendors()["V006"] in decision.outbound_message
    assert "INV-V006-3019" in decision.outbound_message


def test_task_message_carries_code_computed_duplicates(monkeypatch, store, registry):
    """The duplicate facts ride in the task message — the model does not
    need to remember to ask."""
    captured = run_capturing_task(monkeypatch, store, registry, "INV-V003-3901")
    payload = json.loads(captured["task"].split("\n\n", 1)[1])
    assert [d["doc_id"] for d in payload["known_duplicates"]] == ["INV-V003-3003"]
