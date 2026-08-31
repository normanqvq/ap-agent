"""Tests for the PO sanity screen (fat-finger detection).

The headline guarantee: run the screen over the real historical POs and it says
nothing (no crying wolf), yet it catches both seeded fat-finger POs — one that
does not add up, one that dwarfs the item's usual order. That is the
false-approvals=0 discipline extended into false-alarms~=0.

Each signal is pinned on its own — fires when it should, stays silent on clean
data, and holds its exact boundary — so a future edit that inverts a comparison
cannot pass unnoticed.
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

DEFAULT = SanityConfig()  # arithmetic 5x; history 10x of the median, >=4 past POs


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


def _history(qty, n, sku="SKU-X", desc="widget"):
    """n past POs each ordering `qty` of one item — a settled norm."""

    def one(i):
        line = _line(1, qty=qty, unit_price_cents=100, line_total_cents=qty * 100, sku=sku)
        line.description = desc
        return _po([line], doc_id=f"PO-H{i}")

    return [one(i) for i in range(n)]


def _signals(flags):
    return {f.signal for f in flags}


# --- resolve_config -------------------------------------------------------


def test_resolve_config_falls_back_to_base():
    override = SanityConfig(per_vendor_overrides={"V009": SanityConfig(enabled=False)})
    assert resolve_config("V001", override).enabled is True
    assert resolve_config("V009", override).enabled is False


# --- signal 1: ARITHMETIC -------------------------------------------------


def test_arithmetic_fires_on_extra_digit():
    """qty typed with an extra digit (1000 vs 100), but the printed total is
    still the intended one: 1000 x 5.50 = 5,500.00 vs a 550.00 line total."""
    po = _po([_line(1, qty=1000, unit_price_cents=550, line_total_cents=55_000)])
    assert SanityCheck.ARITHMETIC in _signals(screen_po(po, [], DEFAULT))


def test_arithmetic_ignores_a_normal_discount():
    po = _po([_line(1, qty=100, unit_price_cents=100, line_total_cents=9_500)])
    assert screen_po(po, [], DEFAULT) == []


def test_arithmetic_boundary_is_inclusive_at_5x():
    """>= 5x fires, just under does not. computed = 5000, so a 1000 total is
    exactly 5x (fires) and a 1001 total is just under 5x (silent)."""
    fires = _po([_line(1, qty=100, unit_price_cents=50, line_total_cents=1_000)])
    silent = _po([_line(1, qty=100, unit_price_cents=50, line_total_cents=1_001)])
    assert SanityCheck.ARITHMETIC in _signals(screen_po(fires, [], DEFAULT))
    assert screen_po(silent, [], DEFAULT) == []


def test_arithmetic_fires_when_printed_total_is_too_large():
    po = _po([_line(1, qty=100, unit_price_cents=10, line_total_cents=10_000)])
    assert SanityCheck.ARITHMETIC in _signals(screen_po(po, [], DEFAULT))


def test_arithmetic_skips_when_a_field_is_missing():
    po = _po([_line(1, qty=100, unit_price_cents=None, line_total_cents=10_000)])
    assert screen_po(po, [], DEFAULT) == []


# --- signal 2: HISTORY ----------------------------------------------------


def test_history_fires_on_a_spike_over_the_usual_order():
    """We usually buy 500, this PO says 5000 — 10x the median, with a settled
    history behind it."""
    po = _po([_line(1, qty=5000, unit_price_cents=120, line_total_cents=600_000)])
    flags = screen_po(po, _history(qty=500, n=4), DEFAULT)
    flag = next(f for f in flags if f.signal == SanityCheck.HISTORY)
    assert flag.observed == 5000
    assert flag.baseline == 500
    assert flag.ratio == 10.0
    assert "usual order" in flag.hint


def test_history_boundary_is_inclusive_at_10x():
    fires = _po([_line(1, qty=5000, unit_price_cents=120, line_total_cents=600_000)])
    silent = _po([_line(1, qty=4999, unit_price_cents=120, line_total_cents=599_880)])
    hist = _history(qty=500, n=4)
    assert SanityCheck.HISTORY in _signals(screen_po(fires, hist, DEFAULT))
    assert SanityCheck.HISTORY not in _signals(screen_po(silent, hist, DEFAULT))


def test_history_stays_silent_on_a_thin_record():
    """The core reason this signal was hard: with too few past orders there is
    no norm, and a single small one fakes a huge spike. Below history_min_pos
    (default 4), HISTORY says nothing even at 20x."""
    po = _po([_line(1, qty=100, unit_price_cents=100, line_total_cents=10_000)])
    assert SanityCheck.HISTORY not in _signals(screen_po(po, _history(qty=5, n=3), DEFAULT))


def test_history_uses_the_median_not_the_max():
    """One odd past order must not set the bar. History = [5, 500, 500, 500]:
    median 500, so 5000 is 10x (fires); against the max of 500 it is the same,
    but a max-based rule against the lone 5 would have screamed 1000x. Here we
    prove the lone 5 does not drag the baseline down and over-fire a normal
    order: 600 against that history is only ~1.2x and stays silent."""
    hist = [
        _po([_line(1, qty=5, unit_price_cents=100, line_total_cents=500)], doc_id="PO-Ha"),
        _po([_line(1, qty=500, unit_price_cents=100, line_total_cents=50_000)], doc_id="PO-Hb"),
        _po([_line(1, qty=500, unit_price_cents=100, line_total_cents=50_000)], doc_id="PO-Hc"),
        _po([_line(1, qty=500, unit_price_cents=100, line_total_cents=50_000)], doc_id="PO-Hd"),
    ]
    normal = _po([_line(1, qty=600, unit_price_cents=100, line_total_cents=60_000)])
    assert screen_po(normal, hist, DEFAULT) == []


def test_history_matches_by_description_when_sku_is_missing():
    line = _line(1, qty=5000, unit_price_cents=120, line_total_cents=600_000, sku=None, desc="TP")
    po = _po([line])
    hist = _history(qty=500, n=4, sku=None, desc="TP")
    assert SanityCheck.HISTORY in _signals(screen_po(po, hist, DEFAULT))


# --- config gates ---------------------------------------------------------


def test_disabled_globally_returns_nothing():
    po = _po([_line(1, qty=5000, unit_price_cents=120, line_total_cents=600_000)])
    assert screen_po(po, _history(qty=500, n=4), SanityConfig(enabled=False)) == []


def test_per_vendor_override_can_disable_one_vendor():
    po = _po([_line(1, qty=1000, unit_price_cents=550, line_total_cents=55_000)], vendor_id="V009")
    cfg = SanityConfig(per_vendor_overrides={"V009": SanityConfig(enabled=False)})
    assert screen_po(po, [], cfg) == []


def test_arithmetic_flag_carries_auditable_numbers_and_a_hint():
    po = _po([_line(1, qty=1000, unit_price_cents=550, line_total_cents=55_000, desc="A4 paper")])
    flag = next(f for f in screen_po(po, [], DEFAULT) if f.signal == SanityCheck.ARITHMETIC)
    assert flag.line_no == 1
    assert flag.observed == 550_000  # 1000 * 550
    assert flag.baseline == 55_000
    assert flag.ratio == 10.0
    assert "A4 paper" in flag.hint


# --- gold standard: the whole synthetic set -------------------------------

FATFINGER_ID = "PO-DEMO-FATFINGER"
OVERORDER_ID = "PO-DEMO-OVERORDER"
DEMO_IDS = {FATFINGER_ID, OVERORDER_ID}


def _load_pos():
    raw = json.loads((DATA / "pos.json").read_text(encoding="utf-8"))
    return [Document(**d) for d in raw]


def test_no_false_alarms_on_the_real_pos():
    """Every real historical PO must screen clean. If this ever fails, a cutoff
    is too tight, not that the PO is wrong."""
    pos = _load_pos()
    for po in (p for p in pos if p.doc_id not in DEMO_IDS):
        others = [o for o in pos if o.doc_id != po.doc_id]
        flags = screen_po(po, others, DEFAULT)
        assert flags == [], f"unexpected flag on {po.doc_id}: {[f.hint for f in flags]}"


def test_the_arithmetic_demo_po_is_caught():
    pos = _load_pos()
    demo = next(p for p in pos if p.doc_id == FATFINGER_ID)
    others = [o for o in pos if o.doc_id != FATFINGER_ID]
    flags = screen_po(demo, others, DEFAULT)
    assert SanityCheck.ARITHMETIC in _signals(flags)


def test_the_overorder_demo_po_is_caught_by_history():
    """'We usually buy 500, this says 5000' — the toilet-roll spike is flagged
    by HISTORY (and only that signal; its arithmetic is consistent)."""
    pos = _load_pos()
    demo = next(p for p in pos if p.doc_id == OVERORDER_ID)
    others = [o for o in pos if o.doc_id != OVERORDER_ID]
    flags = screen_po(demo, others, DEFAULT)
    assert _signals(flags) == {SanityCheck.HISTORY}
    assert flags[0].ratio == 10.0
