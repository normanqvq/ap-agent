# The tools speak MCP, and survive losing it

The agent's read-only evidence tools — purchase order, goods receipt,
vendor history, duplicate check, contract search — are exposed as an MCP
(Model Context Protocol) server, and the agent calls them over MCP with an
automatic in-process fallback. So the tool layer is a standard, reusable
service, and the demo cannot be broken by a dead tool server.

`mcp` is an optional dependency: `pip install -e ".[mcp]"`. With it off, the
agent runs the tools in-process exactly as before.

## Three layers, one client

| Layer | What it is | Speaks MCP? |
|---|---|---|
| **Raw registry** | `registry.execute()` — a direct Python call | no — the ultimate fallback, cannot fail at the protocol level |
| **In-process MCP** | a real MCP session over in-memory streams, same process | yes — shares this store, no socket, no network surface |
| **Remote MCP** | a separate `python -m apagent.mcp_server` process over stdio | yes — the decoupled microservice others can reuse |

The agent talks MCP when `AP_MCP` selects a transport (in-process or remote) —
off by default; on any transport failure it drops to the raw registry. The move that makes the
fallback safe is that every MCP tool is a one-line delegation to the same
`registry.execute`, so an MCP call returns byte-for-byte what a raw call
returns — falling back swaps the transport, never the answer. That equality
is pinned by `tests/test_mcp.py` for every published tool.

```
agent loop (unchanged)
  → ResilientToolRegistry
      ├─ primary: MCP  (in-process | remote)  ── circuit breaker + 5s timeout
      └─ fallback: raw registry  ── same answer, always available
```

## Two boundaries on purpose

- **Only read-only tools are published.** Nothing here moves money or
  mutates state, so an MCP client — ours, a teammate's agent, or Claude
  Desktop — can gather evidence but never decide. The guardrails and the
  money gate are not tools; authority stays in code.
- **Session uploads stay in-process.** A remote server has its own store and
  cannot see an invoice uploaded this session, so those decisions use the
  in-process registry (`run_case` checks `shares_store`). In-process MCP
  shares the store, so no special case is needed there.

## Resilience: the circuit breaker

The remote transport is the one that can really fail — the process can be
killed, hang, or drop the pipe. A per-call `try/except` would then pay a
fresh 5-second timeout on *every* call. The circuit breaker
(`mcp_bridge.CircuitBreaker`) opens after three consecutive failures and
sends every call straight to the fallback for a cooldown, then half-opens
and lets one probe through. `tests/test_mcp_remote.py` kills the server
mid-run and asserts the answer never changes, and unit-tests the breaker
with an injected clock.

## Running it

```bash
pip install -e ".[mcp]"

# The agent calls its tools over an in-process MCP session (shared store):
AP_MCP=inproc uvicorn apagent.api.app:app

# ...or over a separate MCP server process (decoupled microservice):
AP_MCP=remote uvicorn apagent.api.app:app

# Run the tool server on its own, for another agent or Claude Desktop:
python -m apagent.mcp_server        # stdio
```

The demo moment: run with `AP_MCP=remote`, kill the `apagent.mcp_server`
process mid-review, and watch the agent fall back to in-process with the
decision unchanged — MCP is the architecture, and it is disposable without
consequence.
