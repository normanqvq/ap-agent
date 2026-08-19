"""One live Bedrock call through ap-agent's own client — the AWS smoke test.

Mirrors the workshop's 00_check_bedrock.py but exercises OUR provider layer,
so a green run here means the whole agent can run on Bedrock, tool calls and
all. Credentials come from the standard AWS chain (env vars / shared profile);
this script never reads or prints them.

Run (creds already in your environment or in ap-agent/.env):
    LLM_PROVIDER=bedrock .venv/bin/python scripts/check_bedrock.py

Or borrow the workshop lab's credentials without copying them:
    set -a; source ../agentic_ai_hackathon_2026/lab/.env; set +a
    LLM_PROVIDER=bedrock .venv/bin/python scripts/check_bedrock.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from apagent.llm.client import call_model

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(ROOT / ".env")
    os.environ["LLM_PROVIDER"] = "bedrock"

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "(unset!)"
    model = os.getenv("BEDROCK_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    print("=" * 60)
    print("AP-AGENT BEDROCK CHECK")
    print(f"  region : {region}")
    print(f"  model  : {model}")
    print("=" * 60)

    # A tool so the check also proves tool-calling works on Bedrock, not just
    # plain text — the agent depends on it.
    tools = [
        {
            "name": "get_ok",
            "description": "Return the literal string 'ready'. Call this, then reply 'done'.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    resp = call_model(
        messages=[{"role": "user", "content": "Call get_ok, then tell me you are done."}],
        tools=tools,
        system="You are a terse assistant.",
        provider="bedrock",
    )

    print("text        :", resp.get("text"))
    print("tool_calls  :", [tc["name"] for tc in resp["tool_calls"]])
    print("stop_reason :", resp.get("stop_reason"))
    print("usage       :", resp.get("usage"))
    ok = bool(resp["tool_calls"]) or resp.get("text")
    print("\nOK — Bedrock reachable through ap-agent." if ok else "\nNo content returned.")


if __name__ == "__main__":
    main()
