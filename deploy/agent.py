"""The AP agent as a Bedrock AgentCore entrypoint.

This is the §6 deployment shape from the training: the same pipeline wrapped
in one decorator. AgentCore supplies the HTTP server, the POST /invocations
and GET /ping routes, the container build, and the serverless runtime -- so
nothing about the decision logic changes. The handler just runs
decide_invoice on the requested invoice and returns the decision.

Run it two ways, from the SAME file:
  - locally:  `python deploy/agent.py`  -> a plain HTTP server on :8080
              (free, no AWS -- this is the "Local first" path the rubric
              accepts)
  - deployed: `agentcore launch` packages this and runs it serverless behind
              an HTTPS endpoint (see deploy/02_deploy.py)

The dataset ships in the container (data/synthetic is part of the source), so
a deployed agent decides the same invoices as the local one. Set
LLM_PROVIDER=bedrock so the deployed agent uses Claude on Bedrock.

bedrock-agentcore is an optional dependency: pip install -e ".[deploy]".
"""

from pathlib import Path

from bedrock_agentcore import BedrockAgentCoreApp

from apagent.agent.ap_tools import build_registry
from apagent.matching.engine import match_invoice  # noqa: F401 — warms imports at cold start
from apagent.pipeline import decide_invoice
from apagent.store import DocumentStore

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "synthetic"
CONTRACTS = DATA / "contracts"

# Built once at cold start and reused across invocations -- the store and the
# tool registry are read-only, so one microVM can serve many invoices.
_store = DocumentStore.from_dir(DATA)
_registry = build_registry(_store, CONTRACTS)

app = BedrockAgentCoreApp()


@app.entrypoint
def handler(payload: dict) -> dict:
    """Decide one invoice. Payload: {"invoice_id": "INV-V005-3018"}.

    Returns the full AgentDecision -- action, confidence, the numbered
    reasoning, and the complete tool-call trail -- as a plain dict.
    """
    invoice_id = (payload or {}).get("invoice_id")
    invoice = _store.get_invoice(invoice_id) if invoice_id else None
    if invoice is None:
        return {
            "error": f"unknown invoice_id {invoice_id!r}",
            "available": sorted(_store._invoices)[:5] + ["..."],  # noqa: SLF001
        }
    decision = decide_invoice(invoice, _store, _registry, contracts_dir=CONTRACTS)
    return decision.model_dump()


if __name__ == "__main__":
    # Local mode: AgentCore's SDK serves the same POST /invocations and
    # GET /ping on localhost:8080 -- no AWS resources involved.
    app.run()
