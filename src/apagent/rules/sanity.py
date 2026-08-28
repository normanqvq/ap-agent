"""Sanity screen: catch a fat-finger typo in a purchase order at intake.

Everything downstream trusts the PO. Three-way matching checks the invoice
against the PO and the GRN, so a typo in the PO ITSELF — 1000 reams of paper
ordered when 100 was meant — makes the invoice align to the wrong number,
matching goes green, and the mistake is rubber-stamped to payment. The one
place with real leverage is the moment the PO enters the system, before any
invoice exists. This module screens a PO and stamps an advisory flag on any
line that looks mistyped.

Two signals, both deterministic:

- ARITHMETIC: qty * unit_price is an order of magnitude off the stored line
  total — "extra digit on qty, but the printed total is still the intended one".
  Keys on internal inconsistency, not magnitude, so an expensive item or a big
  honest order never trips it. All 21 real POs are internally consistent, so it
  is a structurally zero-false-alarm signal.
- HISTORY: the qty dwarfs how much of this item is normally ordered — "we
  usually buy 500, this PO says 5000". Guarded so it does not cry wolf: it only
  judges an item with a SETTLED norm (seen on >= history_min_pos past POs) and
  compares against the MEDIAN of that history, not the max. A first attempt used
  the max with no minimum sample and was dropped — on a thin history one small
  past order reads as a 20x explosion. Median plus a minimum sample removes that.

A third signal (an intra-PO line-total outlier for items with no history) stays
dropped: line total = qty * price, so an expensive-per-unit item is
indistinguishable from a wrong quantity. See the design doc.

Advisory only. Like tolerance.py this layer computes fact and nothing more: it
never blocks payment, edits a number, or changes an agent decision. It exists
to make a person look twice. That is why screen_po returns flags and has no
"reject" path. Deterministic, no LLM; history is passed in by the caller (the
other POs), so the function stays pure and trivially testable.
"""

from statistics import median

from apagent.schemas import (
    Document,
    LineItem,
    SanityCheck,
    SanityConfig,
    SanityFlag,
)


def resolve_config(vendor_id: str, base: SanityConfig) -> SanityConfig:
    """The sanity config that actually applies to this vendor.

    Whole-object override, not a field-by-field merge — the same choice as
    tolerance.resolve_config, and for the same reason: "which config applied?"
    should have one answer during an audit, not two.
    """
    if base.per_vendor_overrides and vendor_id in base.per_vendor_overrides:
        return base.per_vendor_overrides[vendor_id]
    return base


def _money(cents: int, currency: str | None) -> str:
    """Cents -> a human amount for a hint string, e.g. 'SGD 5,000.00'."""
    return f"{currency or ''} {cents / 100:,.2f}".strip()


def _fold(a: int, b: int) -> float:
    """Order-of-magnitude ratio between two positive numbers, direction-agnostic.

    max/min so it reads the same whether the observed value is too big or too
    small — a printed total can err in either direction."""
    hi, lo = (a, b) if a >= b else (b, a)
    return hi / lo


def _key(line: LineItem) -> str:
    """How we identify "the same item" across POs for the history baseline.

    Prefer the SKU. Fall back to the description when the SKU is missing — small
    vendors print no item code, only text — lower-cased so trivial casing does
    not split one item into two histories. This mirrors why matching needs text
    similarity at all (see LineItem docstring)."""
    if line.sku:
        return f"sku:{line.sku}"
    return f"desc:{line.description.strip().lower()}"


def _history_qtys(history: list[Document]) -> dict[str, list[int]]:
    """Every past ordered quantity, grouped by item key.

    Built once per screen. A list (not just a max) because HISTORY needs the
    median and the sample size, and one PO can carry the same item on two lines,
    each a real data point."""
    qtys: dict[str, list[int]] = {}
    for doc in history:
        for line in doc.lines:
            qtys.setdefault(_key(line), []).append(line.qty)
    return qtys


def _arithmetic(line: LineItem, config: SanityConfig, currency: str | None) -> SanityFlag | None:
    """qty * unit_price is an order of magnitude off the stored line total.

    Catches the classic "extra digit on qty, but the printed total is still the
    intended one". We compare against the STORED line_total_cents rather than
    recompute it because that stored gap is the evidence (a real discount or fee
    lives in that gap too — which is why the cutoff is a full 5x, well above any
    real discount, so we only speak up for a mistyped digit, never a discount;
    small gaps are tolerance.py's job, not ours)."""
    if line.unit_price_cents is None or line.line_total_cents is None:
        return None
    computed = line.qty * line.unit_price_cents
    stored = line.line_total_cents
    if computed <= 0 or stored <= 0:
        return None
    ratio = _fold(computed, stored)
    if ratio < config.arithmetic_ratio:
        return None
    hint = (
        f'line {line.line_no} "{line.description}": '
        f"qty x unit price = {_money(computed, currency)} but the line total is "
        f"{_money(stored, currency)}, off by {ratio:.0f}x — was a digit mistyped?"
    )
    return SanityFlag(
        line_no=line.line_no,
        signal=SanityCheck.ARITHMETIC,
        observed=computed,
        baseline=stored,
        ratio=ratio,
        hint=hint,
    )


def _history(
    line: LineItem, history_qtys: dict[str, list[int]], config: SanityConfig
) -> SanityFlag | None:
    """This line's qty dwarfs the median quantity this item is normally ordered at.

    Two guards keep it quiet on honest orders: the item must appear on at least
    history_min_pos past lines (a settled norm exists), and we compare against
    the median of that history, not the max — so one odd past order cannot set
    the bar. Below the sample gate, or below the ratio, it says nothing; a
    brand-new item has no norm and is ARITHMETIC's problem, not this one's."""
    seen = history_qtys.get(_key(line), [])
    if len(seen) < config.history_min_pos:
        return None
    baseline = int(median(seen))
    if baseline <= 0:
        return None
    ratio = line.qty / baseline
    if ratio < config.history_ratio:
        return None
    hint = (
        f'line {line.line_no} "{line.description}": '
        f"qty {line.qty:,} is {ratio:.0f}x the usual order for this item "
        f"(normally about {baseline:,}) — is that intended?"
    )
    return SanityFlag(
        line_no=line.line_no,
        signal=SanityCheck.HISTORY,
        observed=line.qty,
        baseline=baseline,
        ratio=ratio,
        hint=hint,
    )


def screen_po(po: Document, history: list[Document], config: SanityConfig) -> list[SanityFlag]:
    """Screen one PO for fat-finger errors, returning advisory flags.

    history is the other POs, used only for the HISTORY baseline. Returns an
    empty list when screening is disabled for this vendor. A single line can
    raise more than one flag (a mistyped digit can trip both arithmetic and
    history); each is reported so a reviewer sees every reason it stood out.
    """
    config = resolve_config(po.vendor_id, config)
    if not config.enabled:
        return []

    history_qtys = _history_qtys(history)
    flags: list[SanityFlag] = []
    for line in po.lines:
        for flag in (
            _arithmetic(line, config, po.currency),
            _history(line, history_qtys, config),
        ):
            if flag is not None:
                flags.append(flag)
    return flags
