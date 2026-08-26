"""The LangGraph view produces the same decision as the pipeline.

graph.py is an orchestration view over the same stage functions
decide_invoice calls, so the two must agree. run_agent hits the LLM, so
both paths are stubbed with the same canned decision — this pins the
WIRING (match / rules / guardrails / outbound), which is what graph.py
re-expresses, without needing an API key.

Skipped when langgraph is not installed (it is an optional dependency).
"""

import pytest

pytest.importorskip("langgraph")

from pathlib import Path  # noqa: E402

from apagent.agent.ap_tools import build_registry  # noqa: E402
from apagent.pipeline import decide_invoice  # noqa: E402
from apagent.schemas import Action, AgentDecision  # noqa: E402
from apagent.store import DocumentStore  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


def _fake_run_agent(invoice_id):
    """A defiant model that always approves — so any guardrail override is
    the code's doing, and identical on both paths."""

    def _run(*args, **kwargs):
        return AgentDecision(
            invoice_id=kwargs.get("invoice_id", invoice_id),
            action=Action.APPROVE,
            hold_reason=None,
            confidence=0.99,
            reasoning="stub: approve everything",
            tool_calls=[],
            rounds_used=1,
        )

    return _run


@pytest.mark.parametrize(
    "invoice_id",
    ["INV-V005-3018", "INV-V005-3005", "INV-V006-3019", "INV-V003-3901", "INV-V001-3001"],
)
def test_graph_matches_the_pipeline(invoice_id, monkeypatch):
    from apagent import graph as graph_module

    store = DocumentStore.from_dir(DATA)
    registry = build_registry(store, DATA / "contracts")
    invoice = store.get_invoice(invoice_id)

    fake = _fake_run_agent(invoice_id)
    # Both decide_invoice and graph.py bound run_agent into their own module
    # namespace via `from ... import run_agent`, so patch both references.
    monkeypatch.setattr("apagent.pipeline.run_agent", fake)
    monkeypatch.setattr("apagent.graph.run_agent", fake)

    pipeline_decision = decide_invoice(invoice, store, registry, contracts_dir=DATA / "contracts")

    graph = graph_module.build_graph(store, registry, contracts_dir=DATA / "contracts")
    graph_state = graph.invoke({"invoice": invoice})
    graph_decision = graph_state["decision"]

    assert graph_decision.action == pipeline_decision.action
    assert graph_decision.hold_reason == pipeline_decision.hold_reason
    assert graph_decision.outbound_message == pipeline_decision.outbound_message


def test_graph_compiles_and_draws():
    from apagent import graph as graph_module

    graph, _ = graph_module.build_demo_graph()
    mermaid = graph.get_graph().draw_mermaid()
    for node in ("match", "rules", "agent", "guardrails", "outbound"):
        assert node in mermaid
