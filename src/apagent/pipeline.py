"""One invoice, end to end: match -> rules -> agent -> decision.

This is the assembly line the demo script and the API both call, so the
two can never drift apart. Everything deterministic happens BEFORE the
model is invoked: by the time run_agent starts, the facts are computed,
tolerance-checked and frozen into the task message. The model adds
judgment and tool-gathered evidence on top — it cannot change the facts.
"""

from apagent.agent.loop import run_agent
from apagent.agent.prompts import AP_SYSTEM_PROMPT, build_task_message
from apagent.agent.registry import ToolRegistry
from apagent.matching.engine import match_invoice
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.schemas import AgentDecision, Document, ToleranceConfig
from apagent.store import DocumentStore


def decide_invoice(
    invoice: Document,
    store: DocumentStore,
    registry: ToolRegistry,
    base_config: ToleranceConfig | None = None,
    max_rounds: int = 5,
) -> AgentDecision:
    """Run the full pipeline for one invoice and return the decision.

    base_config defaults to the stock ToleranceConfig — in the demo we
    deliberately do NOT preload vendor overrides, so the contract-flip case
    is the agent's find (search the contract, cite the clause), not a
    config entry doing the work off-screen.
    """
    config = resolve_config(invoice.vendor_id, base_config or ToleranceConfig())

    match = match_invoice(invoice, store.all_pos(), store.all_grns())
    checked = apply_tolerances(match, config)
    review_gate = requires_manual_review(invoice.total_cents, config)

    return run_agent(
        system_prompt=AP_SYSTEM_PROMPT,
        user_message=build_task_message(invoice, checked, review_gate),
        registry=registry,
        invoice_id=invoice.doc_id,
        max_rounds=max_rounds,
    )
