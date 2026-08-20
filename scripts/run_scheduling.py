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


def _money(cents: int, currency: str) -> str:
    return f"{currency} {cents / 100:,.2f}"


def _totals(by_currency: dict[str, int]) -> str:
    # Different currencies are never added together — list them side by side.
    return "  +  ".join(_money(c, cur) for cur, c in by_currency.items()) or "—"


def main() -> None:
    as_of = sys.argv[1] if len(sys.argv) > 1 else DEMO_AS_OF
    plan = get_service().schedule(as_of)

    print(f"Payment plan as of {plan['as_of']} (runs every Friday)\n")
    for run in plan["runs"]:
        print(
            f"Pay run {run['run_date']}  ·  {run['invoice_count']} invoice(s)  ·  "
            f"{_totals(run['totals'])}"
        )
        for p in run["payments"]:
            amount = _money(p["total_cents"], p["currency"])
            print(f"  {p['vendor_id']}  {p['vendor_name']:34s} {amount:>15s}")
            for i in p["invoices"]:
                late = "  LATE (past due)" if i["late"] else ""
                print(
                    f"      {i['invoice_id']:17s} due {i['due_date'] or '—':10s}  "
                    f"{_money(i['total_cents'], i['currency']):>15s}{late}"
                )
        print()

    if plan["not_scheduled"]:
        print("Not scheduled (agent did not approve):")
        for n in plan["not_scheduled"]:
            reason = n["hold_reason"] or n["action"] or "no decision"
            amount = _money(n["total_cents"], n["currency"])
            print(f"  {n['invoice_id']:17s} {reason:18s} {amount:>15s}")

    s = plan["summary"]
    print(
        f"\n{s['scheduled_count']} invoice(s) scheduled: {_totals(s['scheduled_totals'])}  ·  "
        f"{s['late_count']} late  ·  "
        f"{s['not_scheduled_count']} withheld: {_totals(s['not_scheduled_totals'])}"
    )


if __name__ == "__main__":
    main()
