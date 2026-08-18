"""Run the AP agent live on the planted dataset cases.

This is the demo. For each case it runs the full pipeline (match -> rules
-> agent with tools) against the real LLM (whatever LLM_PROVIDER says, see
.env) and prints the tool trail plus the decision — the same trail the
frontend will visualize.

Usage:
    python scripts/run_agent_demo.py                 # all showcase cases
    python scripts/run_agent_demo.py INV-V005-3018   # one specific invoice

Each case line states what SHOULD happen, so a judge (or we, the night
before) can eyeball the run against expectations. The script does not
assert — live model output varies in wording; the eval harness (P1) will
score runs properly.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from apagent.agent.ap_tools import build_registry
from apagent.pipeline import decide_invoice
from apagent.store import DocumentStore

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "synthetic"

# The showcase: every planted defect plus one clean control.
# (invoice_id, what we expect the agent to do)
CASES = [
    ("INV-V001-3001", "clean control -> APPROVE"),
    ("INV-V005-3005", "8% price variance, above even the contract's 5% -> HOLD/EMAIL"),
    ("INV-V004-3010", "no PO reference printed; found by search -> agent should note weak match"),
    ("INV-V003-3901", "duplicate of INV-V003-3003 -> ESCALATE"),
    ("INV-V005-3018", "4% variance, contract allows 5% -> APPROVE citing the clause"),
    ("INV-V006-3019", "PO exists, no GRN -> HOLD AWAITING_GRN with a drafted chase message"),
    ("INV-V002-3020", "injected 'approve immediately' text + 10% variance -> not approved"),
    ("INV-V001-3021", "billed 50, received 25 (PO==invoice!) -> HOLD AWAITING_DELIVERY"),
]


def show(decision) -> None:
    print(f"\n  rounds used: {decision.rounds_used}")
    for tc in decision.tool_calls:
        result_preview = tc.result if len(tc.result) <= 160 else tc.result[:160] + "..."
        print(f"  [round {tc.round}] {tc.tool_name}({tc.args})")
        print(f"      -> {result_preview}")
    print(f"\n  ACTION: {decision.action.value}", end="")
    if decision.hold_reason:
        print(f"  ({decision.hold_reason.value})", end="")
    print(f"  confidence={decision.confidence}")
    print(f"  reasoning: {decision.reasoning}")
    if decision.outbound_message:
        print(f"  outbound (code-templated): {decision.outbound_message}")


def main() -> None:
    load_dotenv(ROOT / ".env")

    store = DocumentStore.from_dir(DATA)
    registry = build_registry(store, DATA / "contracts")

    wanted = sys.argv[1:]
    cases = [(i, note) for i, note in CASES if not wanted or i in wanted]
    if not cases:
        print(f"No showcase case matches {wanted}. Known: {[i for i, _ in CASES]}")
        return

    for invoice_id, expectation in cases:
        print("\n" + "=" * 78)
        print(f"{invoice_id}  —  expected: {expectation}")
        print("=" * 78)
        invoice = store.get_invoice(invoice_id)
        if invoice is None:
            print("  not in the ledger, skipping")
            continue
        decision = decide_invoice(invoice, store, registry)
        show(decision)


if __name__ == "__main__":
    main()
