"""Tests for the agent loop.

All tests use a fake call_model so no network calls are made. Tests run offline
with no API key present.

Why we monkeypatch call_model:
The loop imports and calls call_model(). If we let it run, it would hit the real
API, which means tests need API keys, cost money, and are slow and non-deterministic.
By monkeypatching, we replace call_model with a fake that returns canned responses.
The loop sees the same interface (returns a dict with text/tool_calls/stop_reason),
but we control exactly what it returns.

Why these test cases:
- Model returns final answer on round 1: the happy path, no tools needed
- Model calls a tool then answers: the normal agent path
- Model never stops: proves we force-return ESCALATE at max_rounds
- Model returns unparseable output: proves the fallback works
"""

from apagent.agent.loop import run_agent
from apagent.agent.registry import ToolRegistry
from apagent.agent.test_tools import register_test_tools
from apagent.schemas import Action


def test_agent_answers_immediately(monkeypatch):
    """Test the agent returning a final answer without calling any tools.

    Why this case matters:
    Sometimes the question is so simple the agent doesn't need tools. For example,
    if the system prompt says "always approve invoices under $10" and the user
    message says "invoice total is $5", the agent can just return APPROVE without
    calling lookup_po or anything else.
    """
    registry = ToolRegistry()

    # Fake call_model that returns a final answer immediately
    def fake_call_model(messages, tools, system, provider=None):
        return {
            "text": '{"action": "APPROVE", "confidence": 1.0, "reasoning": "Invoice is valid"}',
            "tool_calls": [],
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", fake_call_model)

    decision = run_agent(
        system_prompt="You are an AP agent",
        user_message="Should I approve this invoice?",
        registry=registry,
        invoice_id="INV-001",
        max_rounds=3,
    )

    assert decision.action == Action.APPROVE
    assert decision.confidence == 1.0
    assert decision.rounds_used == 1
    assert len(decision.tool_calls) == 0


def test_agent_calls_tool_then_answers(monkeypatch):
    """Test the agent calling a tool, seeing the result, then returning a final answer.

    This is the normal flow: agent calls lookup_po or lookup_grn, sees the data,
    then makes a decision.
    """
    registry = ToolRegistry()
    register_test_tools(registry)

    call_count = 0

    def fake_call_model(messages, tools, system, provider=None):
        nonlocal call_count
        call_count += 1

        if call_count == 1:
            # First call: agent wants to use the echo tool
            return {
                "text": "Let me check something",
                "tool_calls": [{"id": "1", "name": "echo", "args": {"message": "test"}}],
                "stop_reason": "tool_use",
            }
        else:
            # Second call: agent returns a final answer
            answer = (
                '{"action": "APPROVE", "confidence": 0.9, "reasoning": "Tool result looks good"}'
            )
            return {
                "text": answer,
                "tool_calls": [],
                "stop_reason": "end_turn",
            }

    monkeypatch.setattr("apagent.agent.loop.call_model", fake_call_model)

    decision = run_agent(
        system_prompt="You are an AP agent",
        user_message="Decide on this invoice",
        registry=registry,
        invoice_id="INV-002",
        max_rounds=3,
    )

    assert decision.action == Action.APPROVE
    assert decision.rounds_used == 2
    assert len(decision.tool_calls) == 1
    assert decision.tool_calls[0].tool_name == "echo"
    assert decision.tool_calls[0].round == 1


def test_agent_hits_max_rounds(monkeypatch):
    """Test that the loop force-stops at max_rounds and returns ESCALATE.

    Why this matters:
    An agent can get stuck in a loop (calling the same tool over and over, or
    bouncing between tools). Without a limit, it hangs forever. With a limit,
    we force-stop and return ESCALATE with reasoning that says we hit the limit.

    A human can look at the tool call history and see what went wrong.
    """
    registry = ToolRegistry()
    register_test_tools(registry)

    call_count = 0

    def fake_call_model(messages, tools, system, provider=None):
        nonlocal call_count
        call_count += 1
        # Always return a tool call, never a final answer
        tool_call = {
            "id": str(call_count),
            "name": "echo",
            "args": {"message": f"round {call_count}"},
        }
        return {
            "text": f"Calling tool again (round {call_count})",
            "tool_calls": [tool_call],
            "stop_reason": "tool_use",
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", fake_call_model)

    decision = run_agent(
        system_prompt="You are an AP agent",
        user_message="Decide on this invoice",
        registry=registry,
        invoice_id="INV-003",
        max_rounds=3,
    )

    # Should force-return ESCALATE after 3 rounds
    assert decision.action == Action.ESCALATE
    assert decision.rounds_used == 3
    assert len(decision.tool_calls) == 3
    assert "did not reach a decision" in decision.reasoning.lower()


def test_agent_returns_unparseable_output(monkeypatch):
    """Test that unparseable output falls back to ESCALATE.

    Why this matters:
    LLMs sometimes ignore instructions and return free text instead of JSON.
    Or they return almost-JSON with a typo. If we crash on parse errors, the
    whole loop fails. Better to return ESCALATE with the raw text as reasoning,
    so a human can read it and figure out what the agent meant.
    """
    registry = ToolRegistry()

    def fake_call_model(messages, tools, system, provider=None):
        return {
            "text": "I think this invoice is fine, you should approve it.",
            "tool_calls": [],
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", fake_call_model)

    decision = run_agent(
        system_prompt="You are an AP agent. Return JSON.",
        user_message="Decide on this invoice",
        registry=registry,
        invoice_id="INV-004",
        max_rounds=3,
    )

    # Should fall back to ESCALATE
    assert decision.action == Action.ESCALATE
    assert decision.confidence == 0.0
    assert "Failed to parse" in decision.reasoning


def test_agent_strips_markdown_code_fences(monkeypatch):
    """Test that we strip ```json code fences before parsing.

    Why this matters:
    LLMs often wrap JSON in markdown code fences even when not asked to.
    If we don't strip them, json.loads() fails. This is a common case,
    not an edge case.
    """
    registry = ToolRegistry()

    def fake_call_model(messages, tools, system, provider=None):
        json_str = (
            '{"action": "HOLD", "hold_reason": "AWAITING_GRN", '
            '"confidence": 0.7, "reasoning": "GRN not found"}'
        )
        return {
            "text": f"```json\n{json_str}\n```",
            "tool_calls": [],
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", fake_call_model)

    decision = run_agent(
        system_prompt="You are an AP agent",
        user_message="Decide on this invoice",
        registry=registry,
        invoice_id="INV-005",
        max_rounds=3,
    )

    # Should parse successfully
    assert decision.action == Action.HOLD
    assert decision.confidence == 0.7


def test_agent_handles_empty_response(monkeypatch):
    """Test that an empty response (no text, no tool calls) returns ESCALATE.

    Why this matters:
    This should never happen, but if the API returns an empty response, we should
    handle it gracefully instead of crashing.
    """
    registry = ToolRegistry()

    def fake_call_model(messages, tools, system, provider=None):
        return {
            "text": None,
            "tool_calls": [],
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr("apagent.agent.loop.call_model", fake_call_model)

    decision = run_agent(
        system_prompt="You are an AP agent",
        user_message="Decide on this invoice",
        registry=registry,
        invoice_id="INV-006",
        max_rounds=3,
    )

    assert decision.action == Action.ESCALATE
    assert "empty response" in decision.reasoning.lower()
