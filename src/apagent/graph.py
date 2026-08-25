"""The pipeline expressed as a real, runnable LangGraph state graph.

This is the same flow as pipeline.decide_invoice, wired with LangGraph
instead of a straight function call. It exists to show the mapping in
docs/LANGGRAPH.md is not just a diagram: the stages really are a state
graph, and `build_graph(...).invoke({"invoice": inv})` produces the same
decision as decide_invoice.

The hand-written loop stays the audited engine. Every node here calls the
SAME stage function decide_invoice calls -- match_invoice, apply_tolerances,
run_agent, _apply_guardrails -- so this is an orchestration VIEW over the
existing code, not a second implementation that could drift from it. A
test pins the two to the same output.

LangGraph is an OPTIONAL dependency (pip install -e ".[langgraph]"). The
core system never imports this module, so the graph view is a bonus and
not a new requirement.
"""

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from apagent.agent.loop import run_agent
from apagent.agent.prompts import AP_SYSTEM_PROMPT, build_task_message
from apagent.agent.registry import ToolRegistry
from apagent.matching.engine import match_invoice
from apagent.pipeline import (
    _apply_guardrails,
    _contract_chunks,
    _render_outbound_message,
)
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.schemas import Action, AgentDecision, Document, ToleranceConfig
from apagent.store import DocumentStore


class APState(TypedDict, total=False):
    """The shared memory of the graph — the case bundle, filled in stage by
    stage. `invoice` is the only required input; every other field is written
    by a node."""

    invoice: Document
    config: ToleranceConfig
    match: object
    checked: object
    review_gate: bool
    duplicates: list
    decision: AgentDecision


def build_graph(
    store: DocumentStore,
    registry: ToolRegistry,
    contracts_dir: str | Path | None = None,
    base_config: ToleranceConfig | None = None,
    max_rounds: int = 5,
):
    """Compile the AP pipeline as a LangGraph. The store, registry and config
    are captured here so each node is a plain state->update function, exactly
    the LangGraph node contract."""

    def match_node(state: APState) -> dict:
        invoice = state["invoice"]
        config = resolve_config(invoice.vendor_id, base_config or ToleranceConfig())
        match = match_invoice(invoice, store.all_pos(), store.all_grns())
        checked = apply_tolerances(match, config)
        return {"config": config, "match": match, "checked": checked}

    def rules_node(state: APState) -> dict:
        from apagent.agent.ap_tools import hard_duplicates

        invoice, config = state["invoice"], state["config"]
        return {
            "review_gate": requires_manual_review(invoice.total_cents, config),
            "duplicates": hard_duplicates(invoice, store, config),
        }

    def agent_node(state: APState) -> dict:
        decision = run_agent(
            system_prompt=AP_SYSTEM_PROMPT,
            user_message=build_task_message(
                state["invoice"], state["checked"], state["review_gate"], state["duplicates"]
            ),
            registry=registry,
            invoice_id=state["invoice"].doc_id,
            max_rounds=max_rounds,
        )
        return {"decision": decision}

    def guardrails_node(state: APState) -> dict:
        invoice, checked, config = state["invoice"], state["checked"], state["config"]
        chunks = _contract_chunks(str(contracts_dir)) if contracts_dir else ()
        grn = store.get_grn_for_po(checked.po_id) if checked.po_id else None
        po = store.get_po(checked.po_id) if checked.po_id else None
        decision = _apply_guardrails(
            state["decision"],
            invoice,
            checked,
            state["review_gate"],
            state["duplicates"],
            config,
            chunks,
            grn,
            po,
        )
        return {"decision": decision}

    def outbound_node(state: APState) -> dict:
        outbound = _render_outbound_message(
            state["decision"], state["invoice"], state["checked"], store
        )
        if outbound is None:
            return {}
        return {"decision": state["decision"].model_copy(update={"outbound_message": outbound})}

    def needs_outbound(state: APState) -> str:
        # A real conditional edge: only HOLD and EMAIL carry a code-templated
        # message; APPROVE and ESCALATE go straight to END. This mirrors what
        # _render_outbound_message returns, made explicit in the graph.
        action = state["decision"].action
        return "outbound" if action in (Action.HOLD, Action.EMAIL) else "end"

    g = StateGraph(APState)
    g.add_node("match", match_node)
    g.add_node("rules", rules_node)
    g.add_node("agent", agent_node)
    g.add_node("guardrails", guardrails_node)
    g.add_node("outbound", outbound_node)

    g.add_edge(START, "match")
    g.add_edge("match", "rules")
    g.add_edge("rules", "agent")
    g.add_edge("agent", "guardrails")
    g.add_conditional_edges("guardrails", needs_outbound, {"outbound": "outbound", "end": END})
    g.add_edge("outbound", END)
    return g.compile()


def build_demo_graph():
    """Compile the graph over the committed synthetic dataset — for the
    mermaid render and a live invoke() demo."""
    from apagent.agent.ap_tools import build_registry

    root = Path(__file__).resolve().parent.parent.parent
    data = root / "data" / "synthetic"
    store = DocumentStore.from_dir(data)
    registry = build_registry(store, data / "contracts")
    return build_graph(store, registry, contracts_dir=data / "contracts"), store


if __name__ == "__main__":
    # `python -m apagent.graph` prints LangGraph's own diagram of our pipeline.
    graph, _ = build_demo_graph()
    print(graph.get_graph().draw_mermaid())
