"""Batch approved invoices into weekly payment runs.

Real SME accounts payable does not pay invoices one by one: the finance
team runs a payment batch on a fixed day (here: every Friday) and makes one
bank transfer per vendor. This module plans those batches from the agent's
decisions.

The scheduling rule is pay-late-but-never-late: each APPROVEd invoice lands
in the LAST run on or before its due date — hold on to cash as long as
possible, but never miss a due date. An invoice already past due (or due
before the first possible run) goes into the next run and is flagged late.

Only APPROVE moves money. Everything else (HOLD / EMAIL / ESCALATE /
undecided) is listed under not_scheduled with its reason, so the plan also
shows what is deliberately NOT being paid — the same authority rule as the
pipeline guardrails, applied to the payment step.

Pure functions: no store, no filesystem, no clock. The caller passes as_of,
so the plan is deterministic and testable — same inputs, same batches.
"""

from datetime import date, timedelta

from apagent.schemas import Action, Document

FRIDAY = 4  # date.weekday() numbering: Monday is 0


def next_run_date(d: date, run_weekday: int = FRIDAY) -> date:
    """The first run day on or after d."""
    return d + timedelta(days=(run_weekday - d.weekday()) % 7)


def last_run_on_or_before(d: date, run_weekday: int = FRIDAY) -> date:
    """The last run day on or before d."""
    return d - timedelta(days=(d.weekday() - run_weekday) % 7)


def schedule_payments(
    invoices: list[Document],
    decisions: dict[str, dict],
    as_of: str,
    run_weekday: int = FRIDAY,
    vendor_names: dict[str, str] | None = None,
) -> dict:
    """Plan weekly payment runs for the APPROVEd invoices.

    Returns runs (one per Friday, one payment per vendor per run) plus a
    not_scheduled list explaining every invoice the plan refuses to pay.
    """
    start = date.fromisoformat(as_of)
    first_run = next_run_date(start, run_weekday)
    names = vendor_names or {}

    scheduled: dict[date, list[dict]] = {}
    not_scheduled = []
    for inv in sorted(invoices, key=lambda i: i.doc_id):
        decision = decisions.get(inv.doc_id)
        action = decision["action"] if decision else None
        if action != Action.APPROVE:
            not_scheduled.append(
                {
                    "invoice_id": inv.doc_id,
                    "vendor_id": inv.vendor_id,
                    "vendor_name": names.get(inv.vendor_id, inv.vendor_name),
                    "currency": inv.currency,
                    "total_cents": inv.total_cents,
                    "action": action,
                    "hold_reason": decision.get("hold_reason") if decision else None,
                }
            )
            continue
        # due_date is attacker-authored text. Unparseable (or absent) means
        # there is nothing to wait for: pay in the next run, never crash.
        try:
            due = date.fromisoformat(inv.due_date) if inv.due_date else None
        except ValueError:
            due = None
        if due is None:
            run, late = first_run, False
        elif due < first_run:
            run, late = first_run, True  # past due before we can even pay
        else:
            run, late = last_run_on_or_before(due, run_weekday), False
        scheduled.setdefault(run, []).append(
            {
                "invoice_id": inv.doc_id,
                "vendor_id": inv.vendor_id,
                "currency": inv.currency,
                "due_date": inv.due_date,
                "total_cents": inv.total_cents,
                "late": late,
            }
        )

    def totals_by_currency(items: list[dict]) -> dict[str, int]:
        # Cents in different currencies must never be added together, so
        # every total in the plan is a per-currency mapping.
        # A currency or total the extractor could not read is None. It never
        # reaches a payment run (the currency gate escalates it), but it does
        # sit in not_scheduled, and one None among strings used to make this
        # sort raise -- taking the Payments page down for the session.
        out: dict[str, int] = {}
        for i in items:
            key = i["currency"] or "?"
            out[key] = out.get(key, 0) + (i["total_cents"] or 0)
        return dict(sorted(out.items()))

    runs = []
    for run_date in sorted(scheduled):
        items = scheduled[run_date]
        payments = []
        # One transfer per vendor per currency. Late invoices sort first
        # within a payment — they are the ones to release first.
        for vendor_id, currency in sorted(
            {(i["vendor_id"], i["currency"]) for i in items}, key=lambda t: (t[0], t[1] or "")
        ):
            vendor_items = sorted(
                (i for i in items if (i["vendor_id"], i["currency"]) == (vendor_id, currency)),
                key=lambda i: (not i["late"], i["invoice_id"]),
            )
            payments.append(
                {
                    "vendor_id": vendor_id,
                    "vendor_name": names.get(vendor_id, vendor_id),
                    "currency": currency,
                    "total_cents": sum(i["total_cents"] or 0 for i in vendor_items),
                    "invoices": vendor_items,
                }
            )
        runs.append(
            {
                "run_date": run_date.isoformat(),
                "totals": totals_by_currency(items),
                "invoice_count": len(items),
                "payments": payments,
            }
        )

    all_items = [i for items in scheduled.values() for i in items]
    return {
        "as_of": as_of,
        "run_weekday": run_weekday,
        "runs": runs,
        "not_scheduled": not_scheduled,
        "summary": {
            "scheduled_count": len(all_items),
            "scheduled_totals": totals_by_currency(all_items),
            "late_count": sum(1 for i in all_items if i["late"]),
            "not_scheduled_count": len(not_scheduled),
            "not_scheduled_totals": totals_by_currency(not_scheduled),
        },
    }
