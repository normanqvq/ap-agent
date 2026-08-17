"""Agent Runtime

This module implements the core agent runtime for the AP automation system.

## Architecture

The runtime is split into three clean layers:

1. **LLM Client** (`llm/client.py`) - Provider abstraction
   - Normalizes responses from Anthropic and OpenAI-compatible APIs
   - Handles tool schema format conversion
   - Enables prompt caching for repeated elements

2. **Tool Registry** (`agent/registry.py`) - Plugin system
   - Tools register themselves without touching the loop
   - Error-safe execution (never crashes on tool failures)
   - Provider-agnostic tool definitions

3. **Agent Loop** (`agent/loop.py`) - Control flow
   - Hand-written tool-calling loop (no frameworks)
   - Full reasoning trail preserved for auditing
   - Graceful degradation (always returns a decision)

## Usage

```python
from apagent.agent.loop import run_agent
from apagent.agent.registry import ToolRegistry, Tool

# Create registry and register tools
registry = ToolRegistry()


def lookup_po_handler(args: dict) -> str:
    po_id = args["po_id"]
    # ... database lookup
    return f"PO {po_id}: vendor ABC, total $1000"


registry.register(
    Tool(
        name="lookup_po",
        description="Look up a purchase order by ID",
        input_schema={
            "type": "object",
            "properties": {"po_id": {"type": "string", "description": "The PO number"}},
            "required": ["po_id"],
        },
        handler=lookup_po_handler,
    )
)

# Run the agent
decision = run_agent(
    system_prompt="You are an AP agent. Decide whether to approve invoices.",
    user_message="Invoice INV-123 has a total of $1000. Should I approve it?",
    registry=registry,
    invoice_id="INV-123",
    max_rounds=5,
)

print(f"Action: {decision.action}")
print(f"Reasoning: {decision.reasoning}")
print(f"Tool calls: {len(decision.tool_calls)}")
```

## Environment Variables

### Provider selection
- `LLM_PROVIDER` - one of `anthropic` (default), `deepseek`, `groq`, `openai`,
  `bedrock` (placeholder until hackathon AWS credits arrive)

### API keys (each provider reads its own; several can coexist in .env)
- `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `GROQ_API_KEY` / `OPENAI_API_KEY`

### Optional
- `ANTHROPIC_MODEL` / `DEEPSEEK_MODEL` / `GROQ_MODEL` / `LLM_MODEL` - per-provider
  model overrides (sensible defaults if unset)
- `ANTHROPIC_BASE_URL` - e.g. DeepSeek's Anthropic-compatible endpoint
- `LLM_BASE_URL` - override for `openai` provider only; deepseek/groq presets
  set their own base URLs

See `.env.example` at the repo root for the full annotated list.

## Design Decisions

### Why hand-written instead of LangChain?
For the hackathon demo, judges need to see every step of the decision process.
Framework-wrapped logic is a black box. Hand-written code can be walked through
line by line to show exactly how the agent reached its conclusion.

### Why the registry never raises?
Tool failures are data, not bugs. "Invoice not found" or "vendor not in system"
are legitimate answers the agent needs to reason about. Crashing on lookup misses
treats data problems as code problems, which breaks the agent's ability to handle
real-world messiness.

### Why normalize LLM responses?
If each provider returns a different shape, every caller has to branch on provider.
That leaks provider logic into the loop, matching engine, everywhere. By normalizing
at the boundary, the rest of the codebase stays clean and provider-agnostic.

### Why force a decision at max_rounds?
An agent can get stuck in a loop (calling the same tool repeatedly). Without a limit,
the process hangs forever. With a limit, we return ESCALATE with the tool call history
so a human can see what went wrong. Escalations are handleable; hung processes are not.

## Testing

All tests run offline with no API keys:

```bash
pytest tests/test_registry.py tests/test_loop.py -v
```

Tests use monkeypatching to replace `call_model` with fake responses, so no network
calls are made. This makes tests fast, deterministic, and runnable in CI without secrets.

## Next Steps

1. Install `openai` package when network is available: `pip install openai`
2. Write real AP tools (lookup_po, lookup_grn, etc) and register them
3. Build the system prompt with tolerance rules and decision guidelines
4. Wire this into the API layer for end-to-end flow
"""
