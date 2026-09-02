"""Tests for payment scheduling.

Unit tests on fabricated invoices, plus one test over the committed
decisions cache pinning the demo plan: only APPROVEd invoices are paid,
every run lands on a Friday, and nothing (except flagged-late items) is
paid after its due date.
"""

import json
from datetime import date
from pathlib import Path

from apagent.api.service import DEMO_AS_OF, Service
from apagent.scheduling import FRIDAY, last_run_on_or_before, next_run_date, schedule_payments
from apagent.schemas import Document, LineItem

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


def _invoice(doc_id, vendor_id="V001", total_cents=10000, due_date="2026-08-20", currency="SGD"):
    return Document(
        doc_id=doc_id,
        doc_type="INVOICE",
        vendor_id=vendor_id,
        vendor_name=vendor_id,
        issue_date="2026-07-21",
        ref_doc_id="PO-2026-9999",
        currency=currency,
        due_date=due_date,
        total_cents=total_cents,
        lines=[
            LineItem(
                line_no=1,
                sku="X-1",
                description="x",
                qty=1,
                uom="PCS",
                unit_price_cents=total_cents,
                line_total_cents=total_cents,
            )
        ],
    )


def _approve(*ids):
    return {i: {"action": "APPROVE", "hold_reason": None} for i in ids}


def test_run_date_helpers():
    # 2026-08-14 is a Friday; the week around it exercises both directions.
    assert next_run_date(date(2026, 8, 14)) == date(2026, 8, 14)
    assert next_run_date(date(2026, 8, 15)) == date(2026, 8, 21)
    assert last_run_on_or_before(date(2026, 8, 14)) == date(2026, 8, 14)
    assert last_run_on_or_before(date(2026, 8, 20)) == date(2026, 8, 14)


def test_pays_late_as_possible_but_never_late():
    """Due Thursday 8/20: the Friday before (8/14) is the run, not 8/21."""
    invoices = [_invoice("INV-1", due_date="2026-08-20")]
    plan = schedule_payments(invoices, _approve("INV-1"), "2026-08-14")
    assert [r["run_date"] for r in plan["runs"]] == ["2026-08-14"]
    assert plan["runs"][0]["payments"][0]["invoices"][0]["late"] is False


def test_past_due_goes_into_next_run_flagged_late():
    invoices = [_invoice("INV-1", due_date="2026-08-11")]
    plan = schedule_payments(invoices, _approve("INV-1"), "2026-08-14")
    inv = plan["runs"][0]["payments"][0]["invoices"][0]
    assert plan["runs"][0]["run_date"] == "2026-08-14"
    assert inv["late"] is True
    assert plan["summary"]["late_count"] == 1


def test_only_approve_moves_money():
    invoices = [_invoice("INV-1"), _invoice("INV-2"), _invoice("INV-3")]
    decisions = {
        "INV-1": {"action": "APPROVE", "hold_reason": None},
        "INV-2": {"action": "HOLD", "hold_reason": "AWAITING_GRN"},
        # INV-3 undecided
    }
    plan = schedule_payments(invoices, decisions, "2026-08-14")
    assert plan["summary"]["scheduled_count"] == 1
    held = {n["invoice_id"]: n for n in plan["not_scheduled"]}
    assert held["INV-2"]["hold_reason"] == "AWAITING_GRN"
    assert held["INV-3"]["action"] is None


def test_one_payment_per_vendor_per_run():
    invoices = [
        _invoice("INV-1", total_cents=100, due_date="2026-08-19"),
        _invoice("INV-2", total_cents=250, due_date="2026-08-20"),
    ]
    plan = schedule_payments(invoices, _approve("INV-1", "INV-2"), "2026-08-14")
    payments = plan["runs"][0]["payments"]
    assert len(payments) == 1
    assert payments[0]["total_cents"] == 350
    assert len(payments[0]["invoices"]) == 2


def test_currencies_are_never_added_together():
    """Two vendors in different currencies in the same run: separate
    transfers, and every total is per-currency — no cross-currency sum."""
    invoices = [
        _invoice("INV-1", vendor_id="V001", total_cents=100, currency="SGD"),
        _invoice("INV-2", vendor_id="V004", total_cents=250, currency="MYR"),
    ]
    plan = schedule_payments(invoices, _approve("INV-1", "INV-2"), "2026-08-14")
    run = plan["runs"][0]
    assert run["totals"] == {"MYR": 250, "SGD": 100}
    by_vendor = {p["vendor_id"]: p for p in run["payments"]}
    assert by_vendor["V001"]["currency"] == "SGD"
    assert by_vendor["V004"]["currency"] == "MYR"
    assert plan["summary"]["scheduled_totals"] == {"MYR": 250, "SGD": 100}


def test_malformed_or_missing_due_date_never_crashes():
    """due_date is attacker-authored text: unparseable or absent means
    pay in the next run, not a 500."""
    invoices = [
        _invoice("INV-1", due_date="upon receipt"),
        _invoice("INV-2", due_date="0001-01-01"),
        _invoice("INV-3", due_date=None),
    ]
    plan = schedule_payments(invoices, _approve("INV-1", "INV-2", "INV-3"), "2026-08-14")
    assert [r["run_date"] for r in plan["runs"]] == ["2026-08-14"]
    by_id = {i["invoice_id"]: i for p in plan["runs"][0]["payments"] for i in p["invoices"]}
    assert by_id["INV-1"]["late"] is False  # unparseable -> treated as no due date
    assert by_id["INV-2"]["late"] is True  # valid ISO, long past due
    assert by_id["INV-3"]["late"] is False


def test_due_on_the_run_day_is_on_time():
    invoices = [_invoice("INV-1", due_date="2026-08-21")]
    plan = schedule_payments(invoices, _approve("INV-1"), "2026-08-14")
    assert plan["runs"][0]["run_date"] == "2026-08-21"
    assert plan["runs"][0]["payments"][0]["invoices"][0]["late"] is False


def test_run_weekday_other_than_friday():
    """Monday runs: due Thursday 8/20 lands on Monday 8/17."""
    invoices = [_invoice("INV-1", due_date="2026-08-20")]
    plan = schedule_payments(invoices, _approve("INV-1"), "2026-08-14", run_weekday=0)
    assert plan["runs"][0]["run_date"] == "2026-08-17"


def test_late_invoices_sort_first_within_a_payment():
    invoices = [
        _invoice("INV-1", due_date="2026-08-20"),
        _invoice("INV-2", due_date="2026-08-11"),  # past due
    ]
    plan = schedule_payments(invoices, _approve("INV-1", "INV-2"), "2026-08-14")
    ids = [i["invoice_id"] for i in plan["runs"][0]["payments"][0]["invoices"]]
    assert ids == ["INV-2", "INV-1"]


def test_demo_plan_over_committed_decisions():
    """The plan the judges see: 15 paid across Friday runs, 7 withheld,
    and no invoice paid after its due date unless flagged late."""
    decisions = json.loads((DATA / "decisions.json").read_text(encoding="utf-8"))
    plan = Service().schedule(DEMO_AS_OF)

    approved = {k for k, v in decisions.items() if v["action"] == "APPROVE"}
    paid = {i["invoice_id"] for r in plan["runs"] for p in r["payments"] for i in p["invoices"]}
    assert paid == approved
    # 22 graded invoices plus the held-out bank-swap demo (ESCALATE, never paid).
    assert plan["summary"]["not_scheduled_count"] == 23 - len(approved)

    for run in plan["runs"]:
        assert date.fromisoformat(run["run_date"]).weekday() == FRIDAY
        for p in run["payments"]:
            for i in p["invoices"]:
                if not i["late"]:
                    assert run["run_date"] <= i["due_date"]


def test_plan_survives_an_invoice_with_no_currency_or_total():
    """An upload whose currency the extractor could not read is escalated,
    not paid -- but it still sits in not_scheduled, and one None among strings
    used to make the per-currency sort raise, taking the Payments page down."""
    invoices = [_invoice("INV-1"), _invoice("INV-X", currency=None, total_cents=None)]
    plan = schedule_payments(invoices, _approve("INV-1"), "2026-08-14")
    assert plan["summary"]["scheduled_count"] == 1
    assert plan["summary"]["not_scheduled_totals"] == {"?": 0}
