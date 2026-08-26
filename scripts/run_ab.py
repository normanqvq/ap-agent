"""Rules-only baseline vs the agent, scored by the same eval harness.

Shows what the agent's judgement buys over pure deterministic rules: the
invoices it recovers that a rules-only pipeline would hold, with false
approvals zero on BOTH sides — more straight-through, no more risk.

The agent column is the committed decisions cache (data/synthetic/decisions.json);
the baseline is computed live with no LLM by pipeline.decide_invoice_rules_only,
which reuses the real code guardrails but never looks at a contract. The one
invoice that moves is INV-V005-3018: 4% over PO, held by rules alone, approved
by the agent once it read the contract's 5% allowance.

    python scripts/run_ab.py
"""

from apagent.api.service import Service


def main() -> None:
    ab = Service().baseline_comparison()
    b, a = ab["baseline"], ab["agent"]

    print("Rules-only vs agent  (same invoices, same eval harness)\n")
    print(f"{'':18}{'rules-only':>12}{'agent':>10}")
    print(f"{'STP rate':18}{b['stp_pct']:>11}%{a['stp_pct']:>9}%")
    print(f"{'False approves':18}{b['false_approve_count']:>12}{a['false_approve_count']:>10}")
    # touchless is deliberately not compared here: rules-only holds more, so its
    # touchless is HIGHER, which reads backwards next to the STP the agent lifts.

    if ab["recovered"]:
        print("\nRecovered by the agent's judgement (rules-only held these):")
        for r in ab["recovered"]:
            reason = f" ({r['baseline_reason']})" if r["baseline_reason"] != "—" else ""
            print(f"  {r['invoice_id']:16} {r['baseline_action']}{reason} -> APPROVE")
    else:
        print("\n(no invoices recovered — baseline and agent agree)")


if __name__ == "__main__":
    main()
