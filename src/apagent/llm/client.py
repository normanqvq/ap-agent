"""LLM client abstraction.

This module owns provider-specific details so the rest of the code stays clean.
The agent loop should never have to check which provider is running.

Why we normalize responses:
If each provider returns a different shape, every caller has to branch on provider.
That means provider logic leaks into the loop, the matching engine, everywhere.
By normalizing here, the rest of the codebase can just work with one shape.

Why prompt caching:
System prompt and tool definitions are the same every round. Anthropic charges less
for cached tokens. On a 5-round conversation that's 4x cache hits on the heavy parts.
Free speedup, just annotate the blocks.

Why tool schema conversion lives here:
Anthropic puts input_schema at the top level. OpenAI nests it under function.parameters.
The registry stays provider-agnostic (JSON Schema only). We convert on the way out.

Why we load dotenv here:
This module is the first place that reads env vars (API keys, provider config).
Loading dotenv at import time means .env files work automatically without manual
export commands. The load happens once when Python imports this module, then every
os.getenv() call sees the values.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from the repo root
# Why we walk up to find the repo root: this file is nested in src/apagent/llm/.
# The .env file lives at the repo root (same level as pyproject.toml).
# We search upward for pyproject.toml to find the root, then load .env from there.
_current_file = Path(__file__).resolve()
_repo_root = _current_file.parent
while _repo_root != _repo_root.parent:
    if (_repo_root / "pyproject.toml").exists():
        break
    _repo_root = _repo_root.parent

_env_file = _repo_root / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    # No .env file found, environment variables must come from the shell or CI
    pass


def call_model(
    messages: list[dict],
    tools: list[dict],
    system: str,
    provider: str | None = None,
) -> dict:
    """Call the LLM and return a normalized response.

    Args:
        messages: conversation history in standard format [{"role": "user", "content": "..."}]
        tools: list of tool definitions from the registry (provider-neutral format)
        system: system prompt
        provider: "anthropic" or "openai". Defaults to LLM_PROVIDER env var, then "anthropic"

    Returns:
        Normalized dict with the same shape regardless of provider:
        {
            "text": str | None,
            "tool_calls": [{"id": str, "name": str, "args": dict}],
            "stop_reason": str
        }

    Why we normalize:
    The loop should never branch on provider. If we return provider-specific shapes,
    every caller has to know about Anthropic vs OpenAI internals. That breaks the
    whole point of an abstraction layer.
    """
    provider = provider or os.getenv("LLM_PROVIDER", "anthropic")

    if provider == "anthropic":
        return _call_anthropic(messages, tools, system)
    elif provider == "openai":
        return _call_openai(messages, tools, system)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _call_anthropic(messages: list[dict], tools: list[dict], system: str) -> dict:
    """Call Anthropic API with prompt caching enabled.

    Why prompt caching:
    System prompt and tool definitions repeat every round. Marking them as
    cache_control lets Anthropic reuse the processed tokens. On a 5-round agent
    conversation, that's 4x cache hits on the heavy parts (system + tools is often
    several thousand tokens). Cache writes cost the same as normal tokens, cache
    reads cost 90% less. After round 1, every round is faster and cheaper.

    Why we return a normalized shape:
    Anthropic returns content as a list of blocks. Some are text, some are tool_use.
    We flatten that into {text, tool_calls} so the loop doesn't have to iterate blocks.

    Why we support ANTHROPIC_BASE_URL:
    DeepSeek now offers an Anthropic-compatible endpoint at
    https://api.deepseek.com/anthropic. By honoring base_url, the same code path
    can target either provider. That means we can compare models without changing
    SDKs — the only variable is the model itself.
    """
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    base_url = os.getenv("ANTHROPIC_BASE_URL")
    if base_url:
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    else:
        client = anthropic.Anthropic(api_key=api_key)

    # Convert tools to Anthropic format
    # The registry gives us provider-neutral format (name, description, input_schema).
    # Anthropic wants exactly that. No conversion needed.
    anthropic_tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["input_schema"],
            # Mark tool definitions for caching. They don't change between rounds.
            "cache_control": {"type": "ephemeral"},
        }
        for t in tools
    ]

    # Build system blocks with caching
    # Why we use a list: Anthropic lets you attach cache_control to each block.
    # System prompt is the same every round, so we mark it cacheable.
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    response = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
        max_tokens=4096,
        system=system_blocks,
        messages=messages,
        tools=anthropic_tools if anthropic_tools else anthropic.NOT_GIVEN,
    )

    # Normalize response
    text_parts = []
    tool_calls = []

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "args": block.input})

    return {
        "text": "\n".join(text_parts) if text_parts else None,
        "tool_calls": tool_calls,
        "stop_reason": response.stop_reason,
    }


def _call_openai(messages: list[dict], tools: list[dict], system: str) -> dict:
    """Call OpenAI-compatible API (OpenAI, DeepSeek, etc).

    Why this covers DeepSeek:
    DeepSeek uses the OpenAI API format. Just point LLM_BASE_URL at their endpoint
    and this code works unchanged. Same for any other OpenAI-compatible provider.

    Why we convert tool schemas:
    OpenAI nests input_schema under function.parameters. Anthropic puts it at the
    top level. The registry uses the Anthropic shape (flatter, cleaner). We convert
    here so the registry stays provider-agnostic.
    """
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL", "gpt-4")

    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable not set")

    client = OpenAI(api_key=api_key, base_url=base_url)

    # Convert tools to OpenAI format
    # Registry gives: {name, description, input_schema}
    # OpenAI wants: {type: "function", function: {name, description, parameters}}
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]

    # OpenAI puts system message in the messages list
    openai_messages = [{"role": "system", "content": system}] + messages

    response = client.chat.completions.create(
        model=model,
        messages=openai_messages,
        tools=openai_tools if openai_tools else None,
    )

    message = response.choices[0].message

    # Normalize response
    text = message.content
    tool_calls = []

    if message.tool_calls:
        for tc in message.tool_calls:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    # OpenAI gives arguments as a JSON string. Parse it.
                    "args": json.loads(tc.function.arguments),
                }
            )

    return {
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": response.choices[0].finish_reason,
    }
