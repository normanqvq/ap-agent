"""Tests for the PO sanity screen (fat-finger detection).

The headline guarantee: run the screen over the 21 real historical POs and
it says nothing (no crying wolf), yet it catches the one seeded fat-finger PO.
That is the false-approvals=0 discipline extended into false-alarms~=0.

The one signal, ARITHMETIC, is pinned on its own — fires when it should, stays
silent on clean data, and holds its exact 5x boundary — so a future edit that
inverts the comparison cannot pass unnoticed.
"""

import json
from pathlib import Path

from apagent.rules.sanity import resolve_config, screen_po
from apagent.schemas import (
    DocType,
    Document,
    LineItem,
    SanityCheck,
    SanityConfig,
)

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"

DEFAULT = SanityConfig()  # arithmetic self-check at 5x, on for every vendor


def _line(line_no, qty, unit_price_cents, line_total_cents, sku="SKU-X", desc="widget"):
    return LineItem(
        line_no=line_no,
        sku=sku,
        description=desc,
        qty=qty,
        uom="PCS",
        unit_price_cents=unit_price_cents,
        line_total_cents=line_total_cents,
    )


def _po(lines, vendor_id="V001", doc_id="PO-T"):
    return Document(
        doc_id=doc_id,
        doc_type=DocType.PO,
        vendor_id=vendor_id,
        vendor_name="Test Vendor",
        issue_date="2026-08-28",
        ref_doc_id=None,
        currency="SGD",
        lines=lines,
    )


def _signals(flags):
    return {f.signal for f in flags}


# --- resolve_config -------------------------------------------------------


def test_resolve_config_falls_back_to_base():
    override = SanityConfig(per_vendor_overrides={"V009": SanityConfig(enabled=False)})
    assert resolve_config("V001", override).enabled is True
    assert resolve_config("V009", override).enabled is False


# --- the ARITHMETIC signal ------------------------------------------------


def test_arithmetic_fires_on_extra_digit():
    """qty typed with an extra digit (1000 vs 100), but the printed total is
    still the intended one: 1000 x 5.50 = 5,500.00 vs a 550.00 line total."""
    po = _po([_line(1, qty=1000, unit_price_cents=550, line_total_cents=55_000)])
    assert SanityCheck.ARITHMETIC in _signals(screen_po(po, DEFAULT))


def test_arithmetic_ignores_a_normal_discount():
    """A 5% discount moves the line total a little; not a fat finger."""
    po = _po([_line(1, qty=100, unit_price_cents=100, line_total_cents=9_500)])
    assert screen_po(po, DEFAULT) == []


def test_arithmetic_boundary_is_inclusive_at_5x():
    """>= 5x fires, just under does not. computed = 5000, so a 1000 total is
    exactly 5x (fires) and a 1001 total is just under 5x (silent)."""
    fires = _po([_line(1, qty=100, unit_price_cents=50, line_total_cents=1_000)])
    silent = _po([_line(1, qty=100, unit_price_cents=50, line_total_cents=1_001)])
    assert SanityCheck.ARITHMETIC in _signals(screen_po(fires, DEFAULT))
    assert screen_po(silent, DEFAULT) == []


def test_arithmetic_fires_when_printed_total_is_too_large():
    """Direction-agnostic: a total 10x too big is as suspicious as one 10x too
    small. computed = 1000, printed total = 10000."""
    po = _po([_line(1, qty=100, unit_price_cents=10, line_total_cents=10_000)])
    assert SanityCheck.ARITHMETIC in _signals(screen_po(po, DEFAULT))


def test_arithmetic_skips_when_a_field_is_missing():
    po = _po([_line(1, qty=100, unit_price_cents=None, line_total_cents=10_000)])
    assert screen_po(po, DEFAULT) == []


# --- config gates ---------------------------------------------------------


def test_disabled_globally_returns_nothing():
    po = _po([_line(1, qty=1000, unit_price_cents=550, line_total_cents=55_000)])
    assert screen_po(po, SanityConfig(enabled=False)) == []


def test_per_vendor_override_can_disable_one_vendor():
    po = _po([_line(1, qty=1000, unit_price_cents=550, line_total_cents=55_000)], vendor_id="V009")
    cfg = SanityConfig(per_vendor_overrides={"V009": SanityConfig(enabled=False)})
    assert screen_po(po, cfg) == []


def test_flag_carries_auditable_numbers_and_a_hint():
    po = _po([_line(1, qty=1000, unit_price_cents=550, line_total_cents=55_000, desc="A4 paper")])
    flag = next(f for f in screen_po(po, DEFAULT) if f.signal == SanityCheck.ARITHMETIC)
    assert flag.line_no == 1
    assert flag.observed == 550_000  # 1000 * 550
    assert flag.baseline == 55_000
    assert flag.ratio == 10.0
    assert flag.hint  # a non-empty human sentence
    assert "A4 paper" in flag.hint


# --- gold standard: the whole synthetic set -------------------------------

DEMO_PO_ID = "PO-DEMO-FATFINGER"


def _load_pos():
    raw = json.loads((DATA / "pos.json").read_text(encoding="utf-8"))
    return [Document(**d) for d in raw]


def test_no_false_alarms_on_the_real_pos():
    """Every real historical PO must screen clean. If this ever fails, the
    cutoff is too tight, not that the PO is wrong."""
    for po in (p for p in _load_pos() if p.doc_id != DEMO_PO_ID):
        flags = screen_po(po, DEFAULT)
        assert flags == [], f"unexpected flag on {po.doc_id}: {[f.hint for f in flags]}"


def test_the_seeded_fatfinger_po_is_caught():
    """The one obviously over-written PO must be flagged — and it is the only
    one in the set that is."""
    pos = _load_pos()
    demo = next((p for p in pos if p.doc_id == DEMO_PO_ID), None)
    assert demo is not None, "seed pos.json with the demo fat-finger PO"
    assert screen_po(demo, DEFAULT), "the demo PO should raise at least one flag"
