"""Print the planned payment runs from the cached decisions.

Reads data/synthetic/decisions.json and batches every APPROVEd invoice
into weekly Friday pay runs (pay-late-but-never-late). Invoices the agent
did not approve are listed with their reason — the plan shows what is NOT
being paid, too.

    python scripts/run_scheduling.py               # demo as-of date
    python scripts/run_scheduling.py 2026-08-21    # plan from another date
"""

import sys

from apagent.api.service import DEMO_AS_OF, get_service


def _money(cents: int) -> str:
    return f"SGD {cents / 100:,.2f}"


def main() -> None:
    as_of = sys.argv[1] if len(sys.argv) > 1 else DEMO_AS_OF
    plan = get_service().schedule(as_of)

    print(f"Payment plan as of {plan['as_of']} (runs every Friday)\n")
    for run in plan["runs"]:
        print(
            f"Pay run {run['run_date']}  ·  {run['invoice_count']} invoice(s)  ·  "
            f"{_money(run['total_cents'])}"
        )
        for p in run["payments"]:
            print(f"  {p['vendor_id']}  {p['vendor_name']:34s} {_money(p['total_cents']):>15s}")
            for i in p["invoices"]:
                late = "  LATE (past due)" if i["late"] else ""
                print(
                    f"      {i['invoice_id']:17s} due {i['due_date']}  "
                    f"{_money(i['total_cents']):>15s}{late}"
                )
        print()

    if plan["not_scheduled"]:
        print("Not scheduled (agent did not approve):")
        for n in plan["not_scheduled"]:
            reason = n["hold_reason"] or n["action"] or "no decision"
            print(f"  {n['invoice_id']:17s} {reason:18s} {_money(n['total_cents']):>15s}")

    s = plan["summary"]
    print(
        f"\n{s['scheduled_count']} invoice(s) scheduled, "
        f"{_money(s['scheduled_total_cents'])}  ·  {s['late_count']} late  ·  "
        f"{s['not_scheduled_count']} withheld, {_money(s['not_scheduled_total_cents'])}"
    )


if __name__ == "__main__":
    main()
