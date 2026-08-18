"""Tolerance rules: turn matching's raw facts into checked facts.

Matching says "unit price is 4.0% above PO". This module answers "is 4.0%
acceptable for THIS vendor?" — by policy, not judgment. The agent still
decides what to do about an out-of-tolerance row; we only stamp each row
with within_tolerance so the agent reasons from checked facts.

Where per-vendor overrides come from: ToleranceConfig.per_vendor_overrides
mirrors the ERP "tolerance key" idea (see schemas.py). In our pipeline the
values originate in the vendor's supply agreement — the contract retrieval
tool surfaces the clause, and whoever configures the system (or, in the
demo, the agent's own reading of the clause) records it here. resolve_config
is the single lookup point so that story stays consistent.
"""

from apagent.schemas import Discrepancy, DiscrepancyField, MatchResult, ToleranceConfig


def resolve_config(vendor_id: str, base: ToleranceConfig) -> ToleranceConfig:
    """The tolerance config that actually applies to this vendor.

    Falls back to the base config when the vendor has no override — and
    when it does have one, the override is used whole, not merged field by
    field. Merging sounds friendly but makes "which limit applied?" a
    two-source question during an audit; whole-object override keeps one
    answer.
    """
    if base.per_vendor_overrides and vendor_id in base.per_vendor_overrides:
        return base.per_vendor_overrides[vendor_id]
    return base


def _check(d: Discrepancy, config: ToleranceConfig) -> bool:
    """within_tolerance for one discrepancy row."""
    if d.field == DiscrepancyField.QTY:
        # qty_exact=True means any quantity gap is a real finding: goods
        # missing or over-billed, exactly what the agent must look at.
        return not config.qty_exact

    if d.field == DiscrepancyField.UNIT_PRICE:
        return d.delta_pct is not None and d.delta_pct <= config.unit_price_pct

    if d.field == DiscrepancyField.INVOICE_TOTAL:
        # BOTH bounds must hold (see ToleranceConfig docstring): the
        # absolute one forgives cents of rounding on small invoices, the
        # percentage one stops big invoices hiding big gaps under it.
        abs_ok = d.delta_abs is not None and d.delta_abs <= config.total_abs_cents
        pct_ok = d.delta_pct is not None and d.delta_pct <= config.total_pct
        return abs_ok and pct_ok

    # No tolerance for the rest, deliberately:
    # - UOM mismatch is structural (pieces vs boxes); extraction should have
    #   converted units, so reaching here means something upstream needs a
    #   human.
    # - LINE_TOTAL means the line's own arithmetic doesn't add up (printed
    #   total != qty x unit price) — that is either padding or a broken
    #   document, and neither is a rounding matter. If real-world invoices
    #   turn out to carry legitimate 1-cent print rounding, add a small
    #   absolute allowance HERE, in one place.
    return False


def apply_tolerances(match: MatchResult, config: ToleranceConfig) -> MatchResult:
    """Re-emit the match with within_tolerance actually computed.

    Returns a new MatchResult instead of mutating — the raw match is
    evidence, and evidence should not change shape after the fact.
    Callers pass the config from resolve_config(vendor_id, base).
    """
    checked = [
        d.model_copy(update={"within_tolerance": _check(d, config)}) for d in match.discrepancies
    ]
    return match.model_copy(update={"discrepancies": checked})


def requires_manual_review(invoice_total_cents: int | None, config: ToleranceConfig) -> bool:
    """The money gate. Above the threshold a human signs off even on a
    perfectly clean match — this cap on the agent's authority is what makes
    'the model has no power to move real money' true, so it lives in code,
    not in the system prompt."""
    if invoice_total_cents is None:
        # No printed total = we cannot bound the exposure = treat as large.
        return True
    return invoice_total_cents >= config.manual_review_threshold_cents
