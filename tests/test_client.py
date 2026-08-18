"""Tests for the LLM client provider layer.

All tests run offline: they exercise provider routing and message format
conversion, never a real API. The conversion functions are pure, so we can
assert exact wire shapes without mocking an SDK.

Why we test the wire shapes so precisely:
The whole value of the client module is "the loop never needs to know the
provider". That promise only holds if the conversion to each provider's tool
protocol is exactly right — a wrong tool_use_id link or a missing role="tool"
message fails at the API with errors that are painful to debug live. Cheaper
to pin the shapes here.
"""

import pytest

from apagent.llm.client import (
    _OPENAI_COMPAT_PROVIDERS,
    _to_anthropic_messages,
    _to_openai_messages,
    call_model,
)

# A little internal-format conversation: user asks, assistant calls two tools,
# results come back. Reused by both conversion tests so the two wire formats
# are asserted against the same source.
INTERNAL_MESSAGES = [
    {"role": "user", "content": "Decide on invoice INV-1"},
    {
        "role": "assistant",
        "content": "Let me check the PO and GRN",
        "tool_calls": [
            {"id": "call_1", "name": "lookup_po", "args": {"po_id": "PO-9"}},
            {"id": "call_2", "name": "lookup_grn", "args": {"po_id": "PO-9"}},
        ],
    },
    {
        "role": "tool_results",
        "results": [
            {"id": "call_1", "name": "lookup_po", "result": "PO-9: 100 units"},
            {"id": "call_2", "name": "lookup_grn", "result": "GRN not found"},
        ],
    },
]


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider: nope"):
        call_model(messages=[], tools=[], system="s", provider="nope")


def test_bedrock_is_explicit_placeholder():
    """Bedrock must fail loudly with a pointer, not pretend to work.

    Why: a silent stub returning fake text would look like a working provider
    in a quick manual test, then explode in the demo. NotImplementedError with
    instructions is the honest failure mode.
    """
    with pytest.raises(NotImplementedError, match="AnthropicBedrockMantle"):
        call_model(messages=[], tools=[], system="s", provider="bedrock")


def test_missing_key_names_the_right_env_var(monkeypatch):
    """Each provider must ask for ITS key, not a generic one.

    The point of per-provider key env vars is that error messages tell you
    exactly what to set. DEEPSEEK_API_KEY missing should never complain about
    OPENAI_API_KEY.
    """
    for provider, (key_env, _, _, _) in _OPENAI_COMPAT_PROVIDERS.items():
        monkeypatch.delenv(key_env, raising=False)
        with pytest.raises(ValueError, match=key_env):
            call_model(messages=[], tools=[], system="s", provider=provider)


def test_provider_table_covers_expected_providers():
    assert set(_OPENAI_COMPAT_PROVIDERS) == {"deepseek", "groq", "openai"}
    # openai has no fixed base URL (official endpoint); the presets do
    assert _OPENAI_COMPAT_PROVIDERS["openai"][1] is None
    assert _OPENAI_COMPAT_PROVIDERS["deepseek"][1].startswith("https://api.deepseek.com")
    assert _OPENAI_COMPAT_PROVIDERS["groq"][1].startswith("https://api.groq.com")


def test_anthropic_message_conversion():
    wire = _to_anthropic_messages(INTERNAL_MESSAGES)

    assert wire[0] == {"role": "user", "content": "Decide on invoice INV-1"}

    # Assistant turn: text block first, then one tool_use block per call
    assistant = wire[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "text", "text": "Let me check the PO and GRN"}
    assert assistant["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "lookup_po",
        "input": {"po_id": "PO-9"},
    }
    assert assistant["content"][2]["id"] == "call_2"

    # Results: a user message of tool_result blocks, ids linking back
    results = wire[2]
    assert results["role"] == "user"
    assert results["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "PO-9: 100 units",
    }
    assert results["content"][1]["tool_use_id"] == "call_2"


def test_anthropic_conversion_skips_empty_text_block():
    """Anthropic rejects empty text blocks, so an assistant turn with tool
    calls but no text must produce tool_use blocks only."""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "name": "echo", "args": {}}],
        }
    ]
    wire = _to_anthropic_messages(messages)
    assert [b["type"] for b in wire[0]["content"]] == ["tool_use"]


def test_openai_message_conversion():
    wire = _to_openai_messages(INTERNAL_MESSAGES, system="You are an AP agent")

    # System prompt travels as the first message
    assert wire[0] == {"role": "system", "content": "You are an AP agent"}
    assert wire[1] == {"role": "user", "content": "Decide on invoice INV-1"}

    # Assistant turn: tool_calls array, arguments JSON-encoded as a string
    assistant = wire[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Let me check the PO and GRN"
    assert assistant["tool_calls"][0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup_po", "arguments": '{"po_id": "PO-9"}'},
    }

    # Each result becomes its own role="tool" message referencing tool_call_id
    assert wire[3] == {"role": "tool", "tool_call_id": "call_1", "content": "PO-9: 100 units"}
    assert wire[4] == {"role": "tool", "tool_call_id": "call_2", "content": "GRN not found"}


def test_parse_tool_args_survives_broken_json():
    """Models occasionally emit broken JSON as tool arguments. Raising there
    would kill the whole agent loop over one bad call; degrading to a
    marker dict routes it through the normal tool-error recovery path."""
    from apagent.llm.client import _parse_tool_args

    assert _parse_tool_args('{"po_id": "PO-9"}') == {"po_id": "PO-9"}
    assert _parse_tool_args('{"po_id": ') == {"_malformed_args": '{"po_id": '}
    # Valid JSON that is not an object is just as unusable as broken JSON
    assert _parse_tool_args('["PO-9"]') == {"_malformed_args": '["PO-9"]'}
