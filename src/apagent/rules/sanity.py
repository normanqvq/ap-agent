"""Sanity screen: catch a fat-finger typo in a purchase order at intake.

Everything downstream trusts the PO. Three-way matching checks the invoice
against the PO and the GRN, so a typo in the PO ITSELF — 1000 reams of paper
ordered when 100 was meant — makes the invoice align to the wrong number,
matching goes green, and the mistake is rubber-stamped to payment. The one
place with real leverage is the moment the PO enters the system, before any
invoice exists. This module screens a PO against itself and stamps an advisory
flag on any line that does not add up.

One signal, on purpose. We designed three (arithmetic self-check, a history
baseline on quantity, an intra-PO line-total outlier) and measured all three on
the real data. The two quantity-magnitude signals could not be made clean:
legitimate business variation there reaches 10-20x (a small prior order
restocked; an expensive item beside cheap ones), which overlaps a genuine
one-digit typo (10x), so any cutoff either cried wolf on real POs or missed the
very error it was for — and false alarms are the one thing this project must not
produce. The arithmetic check has no such overlap: it keys on a line being
internally inconsistent (qty * unit_price far from the stored line total), and
all 21 real POs are internally consistent, so it is a structurally
zero-false-alarm signal. See the design doc for the numbers.

Advisory only. Like tolerance.py this layer computes fact and nothing more: it
never blocks payment, edits a number, or changes an agent decision. It exists
to make a person look twice. That is why screen_po returns flags and has no
"reject" path. Deterministic, no LLM, no I/O.
"""

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


def screen_po(po: Document, config: SanityConfig) -> list[SanityFlag]:
    """Screen one PO for fat-finger errors, returning advisory flags.

    Returns an empty list when screening is disabled for this vendor. Pure: it
    reads only the PO passed in, so it is trivially testable and free of I/O.
    """
    config = resolve_config(po.vendor_id, config)
    if not config.enabled:
        return []

    flags: list[SanityFlag] = []
    for line in po.lines:
        flag = _arithmetic(line, config, po.currency)
        if flag is not None:
            flags.append(flag)
    return flags
