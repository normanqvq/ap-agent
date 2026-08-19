"""LLM client abstraction.

This module owns provider-specific details so the rest of the code stays clean.
The agent loop should never have to check which provider is running.

Providers (set LLM_PROVIDER):
    anthropic - native Anthropic SDK (also serves DeepSeek's Anthropic endpoint)
    deepseek  - DeepSeek's native OpenAI-compatible endpoint
    groq      - Groq's OpenAI-compatible endpoint
    openai    - OpenAI itself, or any other OpenAI-compatible endpoint
    bedrock   - Claude on AWS Bedrock via the anthropic SDK (AnthropicBedrock)

deepseek and groq are presets over the same OpenAI-compatible code path: they
differ only in which env var holds the key, the base URL, and the default model.
We make them named providers anyway, so switching is one env var
(LLM_PROVIDER=groq) instead of three (key + base URL + model). Each provider
reads its own key env var (DEEPSEEK_API_KEY, GROQ_API_KEY) so several keys can
sit in .env at once and switching never means renaming keys.

Why we normalize responses:
If each provider returns a different shape, every caller has to branch on provider.
That means provider logic leaks into the loop, the matching engine, everywhere.
By normalizing here, the rest of the codebase can just work with one shape.

Message format (internal, provider-neutral):
    {"role": "user", "content": str}
    {"role": "assistant", "content": str | None,
     "tool_calls": [{"id": str, "name": str, "args": dict}]}
    {"role": "tool_results", "results": [{"id": str, "name": str, "result": str}]}

The loop builds messages in this shape and this module converts to each
provider's wire format. Earlier we sent tool results as plain user text, which
worked, but off-protocol: the model loses the structured link between a call and
its result, and providers can't apply their tool-use finetuning. Now we speak
each provider's real tool protocol (tool_use/tool_result blocks for Anthropic,
role="tool" messages for OpenAI-compatible APIs).

Why prompt caching:
System prompt and tool definitions are the same every round. Anthropic charges less
for cached tokens. On a 5-round conversation that's 4x cache hits on the heavy parts.
Anthropic allows at most 4 cache breakpoints per request, so we mark only the
LAST tool (a breakpoint covers everything before it) plus the system prompt.
Marking every tool worked with 3 test tools but would blow the limit on the
first real registry.

Why we load dotenv here:
This module is the first place that reads env vars (API keys, provider config).
Loading dotenv at import time means .env files work automatically without manual
export commands.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from the repo root
# Why we walk up to find the repo root: this file is nested in src/apagent/llm/.
# The .env file lives at the repo root (same level as pyproject.toml).
_current_file = Path(__file__).resolve()
_repo_root = _current_file.parent
while _repo_root != _repo_root.parent:
    if (_repo_root / "pyproject.toml").exists():
        break
    _repo_root = _repo_root.parent

_env_file = _repo_root / ".env"
if _env_file.exists():
    load_dotenv(_env_file)


# One row per OpenAI-compatible provider: (key env var, base URL, model env var,
# default model). A dict instead of if/elif so adding a provider is one line and
# the test suite can assert the table directly.
_OPENAI_COMPAT_PROVIDERS = {
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "DEEPSEEK_MODEL", "deepseek-chat"),
    "groq": (
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
        "GROQ_MODEL",
        "llama-3.3-70b-versatile",
    ),
    "openai": ("OPENAI_API_KEY", None, "LLM_MODEL", "gpt-4o"),
}


def call_model(
    messages: list[dict],
    tools: list[dict],
    system: str,
    provider: str | None = None,
) -> dict:
    """Call the LLM and return a normalized response.

    Args:
        messages: conversation history in the internal format (see module docstring)
        tools: list of tool definitions from the registry (provider-neutral format)
        system: system prompt
        provider: provider name. Defaults to LLM_PROVIDER env var, then "anthropic"

    Returns:
        Normalized dict with the same shape regardless of provider:
        {
            "text": str | None,
            "tool_calls": [{"id": str, "name": str, "args": dict}],
            "stop_reason": str
        }
    """
    provider = provider or os.getenv("LLM_PROVIDER", "anthropic")

    if provider == "anthropic":
        return _call_anthropic(messages, tools, system)
    elif provider in _OPENAI_COMPAT_PROVIDERS:
        return _call_openai_compat(messages, tools, system, provider)
    elif provider == "bedrock":
        return _call_bedrock(messages, tools, system)
    else:
        known = ", ".join(["anthropic", *_OPENAI_COMPAT_PROVIDERS, "bedrock"])
        raise ValueError(f"Unknown provider: {provider}. Known providers: {known}")


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Convert internal messages to Anthropic wire format.

    Assistant tool calls become tool_use content blocks; tool results become a
    user message of tool_result blocks referencing the tool_use ids. That id link
    is the whole point of the protocol: the model knows exactly which call each
    result answers, even when it made several calls in one round.
    """
    out = []
    for msg in messages:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            blocks = []
            # Anthropic rejects empty text blocks, so only add one if there is text
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                blocks.append(
                    {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]}
                )
            out.append({"role": "assistant", "content": blocks})
        elif msg["role"] == "tool_results":
            blocks = [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["result"]}
                for r in msg["results"]
            ]
            out.append({"role": "user", "content": blocks})
        else:
            out.append({"role": msg["role"], "content": msg["content"]})
    return out


def _to_openai_messages(messages: list[dict], system: str) -> list[dict]:
    """Convert internal messages to OpenAI wire format.

    OpenAI carries the system prompt as the first message, assistant tool calls
    as a tool_calls array (arguments JSON-encoded as a string), and each tool
    result as its own role="tool" message referencing tool_call_id.
    """
    out = [{"role": "system", "content": system}]
    for msg in messages:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                        }
                        for tc in msg["tool_calls"]
                    ],
                }
            )
        elif msg["role"] == "tool_results":
            for r in msg["results"]:
                out.append({"role": "tool", "tool_call_id": r["id"], "content": r["result"]})
        else:
            out.append({"role": msg["role"], "content": msg["content"]})
    return out


def _anthropic_style_request(client, model: str, messages, tools, system) -> dict:
    """The shared messages.create call for every Anthropic-surface client.

    The first-party Anthropic client and the Bedrock client (AnthropicBedrock)
    expose the identical messages.create API, so the tool prep, prompt-cache
    markers, request, and response parsing live here once. Only the client
    construction and the model id differ between providers.
    """
    import anthropic

    # Mark only the LAST tool as a cache breakpoint. A breakpoint caches
    # everything before it in the request, so one marker at the end covers the
    # whole tool list. One marker per tool would hit Anthropic's limit of 4
    # breakpoints as soon as the registry holds more than 3 tools. Bedrock
    # honors cache_control for Claude models too, so this carries over.
    anthropic_tools = [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in tools
    ]
    if anthropic_tools:
        anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}

    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_blocks,
        messages=_to_anthropic_messages(messages),
        tools=anthropic_tools if anthropic_tools else anthropic.NOT_GIVEN,
    )

    text_parts = []
    tool_calls = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "args": block.input})

    usage = getattr(response, "usage", None)
    return {
        "text": "\n".join(text_parts) if text_parts else None,
        "tool_calls": tool_calls,
        "stop_reason": response.stop_reason,
        # Token usage per call — the one cost figure available without leaving
        # the process (see slides: log it to measure cost per invoice instead
        # of discovering it at month end). Callers may ignore it.
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        if usage
        else None,
    }


def _call_anthropic(messages: list[dict], tools: list[dict], system: str) -> dict:
    """Call Anthropic API with prompt caching enabled.

    Why we support ANTHROPIC_BASE_URL:
    DeepSeek offers an Anthropic-compatible endpoint at
    https://api.deepseek.com/anthropic. By honoring base_url, the same code path
    can target either provider. DeepSeek ignores cache_control markers (it has
    its own automatic disk cache), so leaving them on is harmless there.
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

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    return _anthropic_style_request(client, model, messages, tools, system)


def _parse_tool_args(raw: str) -> dict:
    """OpenAI-compatible APIs send tool arguments as a JSON string; models
    occasionally emit broken JSON there. Raising here would crash the whole
    agent loop over one bad call, so we degrade: hand the tool a dict that
    says what happened. The tool then complains about its missing required
    args, the message flows back to the model, and the model retries — the
    same recovery path as any other tool error."""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_malformed_args": raw}
    except json.JSONDecodeError:
        return {"_malformed_args": raw}


def _call_openai_compat(
    messages: list[dict], tools: list[dict], system: str, provider: str
) -> dict:
    """Call an OpenAI-compatible API (OpenAI, DeepSeek, Groq).

    All three speak the same wire format; only the key, base URL and default
    model differ, and those come from _OPENAI_COMPAT_PROVIDERS. For "openai"
    the base URL stays None (official endpoint) unless LLM_BASE_URL overrides
    it, which keeps the old escape hatch for any other compatible endpoint.
    """
    from openai import OpenAI

    key_env, default_base_url, model_env, default_model = _OPENAI_COMPAT_PROVIDERS[provider]

    api_key = os.getenv(key_env)
    if not api_key:
        raise ValueError(f"{key_env} environment variable not set (required for {provider})")

    base_url = os.getenv("LLM_BASE_URL") or default_base_url
    model = os.getenv(model_env, default_model)

    client = OpenAI(api_key=api_key, base_url=base_url)

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

    response = client.chat.completions.create(
        model=model,
        messages=_to_openai_messages(messages, system),
        tools=openai_tools if openai_tools else None,
    )

    message = response.choices[0].message

    tool_calls = []
    if message.tool_calls:
        for tc in message.tool_calls:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": _parse_tool_args(tc.function.arguments),
                }
            )

    return {
        "text": message.content,
        "tool_calls": tool_calls,
        "stop_reason": response.choices[0].finish_reason,
    }


def _call_bedrock(messages: list[dict], tools: list[dict], system: str) -> dict:
    """Call Claude on AWS Bedrock via the anthropic SDK's Bedrock client.

    We use AnthropicBedrock, NOT boto3. It exposes the identical
    messages.create surface as the first-party client, so the whole message
    conversion and tool protocol (_anthropic_style_request) is reused
    verbatim — only the client construction and the model id change. Going
    through boto3's Converse API instead would mean hand-writing a third
    tool-calling format (toolUse/toolResult blocks) for no gain.

    NOT AnthropicBedrockMantle: that variant carries skip_auth and targets an
    internal gateway, not real AWS Bedrock. AnthropicBedrock signs requests
    with AWS SigV4 from the standard credential chain (env vars
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, a shared profile, or an
    instance role) — so no key is ever passed as an argument or read by this
    code. Requires: pip install "anthropic[aws]".

    Model id: Bedrock ids differ from first-party ones and carry a routing
    prefix, e.g. global.anthropic.claude-haiku-4-5-20251001-v1:0 (the
    workshop default in ap-southeast-1). Override with BEDROCK_MODEL.

    Caveat: Bedrock does not serve every first-party feature. Prompt caching
    and tool use are supported (kept on); the server-side web-search / code-
    execution tools and the Files/Batches endpoints are not.
    """
    try:
        from anthropic import AnthropicBedrock
    except ImportError as exc:
        raise ValueError(
            "Bedrock support needs the AWS extra: pip install 'anthropic[aws]'."
        ) from exc

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if not region:
        raise ValueError(
            "Bedrock needs a region. Set AWS_REGION (or AWS_DEFAULT_REGION), e.g. ap-southeast-1."
        )

    # Credentials come from the standard AWS chain; we pass only the region.
    client = AnthropicBedrock(aws_region=region)
    model = os.getenv("BEDROCK_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    return _anthropic_style_request(client, model, messages, tools, system)
