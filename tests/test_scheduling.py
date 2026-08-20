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


def _invoice(doc_id, vendor_id="V001", total_cents=10000, due_date="2026-08-20"):
    return Document(
        doc_id=doc_id,
        doc_type="INVOICE",
        vendor_id=vendor_id,
        vendor_name=vendor_id,
        issue_date="2026-07-21",
        ref_doc_id="PO-2026-9999",
        currency="SGD",
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


def test_demo_plan_over_committed_decisions():
    """The plan the judges see: 15 paid across Friday runs, 7 withheld,
    and no invoice paid after its due date unless flagged late."""
    decisions = json.loads((DATA / "decisions.json").read_text())
    plan = Service().schedule(DEMO_AS_OF)

    approved = {k for k, v in decisions.items() if v["action"] == "APPROVE"}
    paid = {i["invoice_id"] for r in plan["runs"] for p in r["payments"] for i in p["invoices"]}
    assert paid == approved
    assert plan["summary"]["not_scheduled_count"] == 22 - len(approved)

    for run in plan["runs"]:
        assert date.fromisoformat(run["run_date"]).weekday() == FRIDAY
        for p in run["payments"]:
            for i in p["invoices"]:
                if not i["late"]:
                    assert run["run_date"] <= i["due_date"]
