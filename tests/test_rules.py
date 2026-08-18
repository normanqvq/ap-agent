"""Tests for the tolerance rules layer.

The one behavior that must never regress: the same 4% price variance is
out of tolerance under the default config and WITHIN tolerance under
V005's contractual override. That flip is the demo's headline case, and
rules is the layer that makes it happen.
"""

import json
from pathlib import Path

from apagent.matching.engine import match_invoice
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.schemas import DiscrepancyField, Document, ToleranceConfig

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"

DEFAULT = ToleranceConfig()  # 2% price, $5/1% total, qty exact, $5k review gate

# What the system looks like once V005's contract clause (5% price
# variance) has been recorded as an override.
WITH_V005_OVERRIDE = ToleranceConfig(
    per_vendor_overrides={"V005": ToleranceConfig(unit_price_pct=5.0)}
)


def _match(invoice_id):
    def load(name):
        return [Document(**d) for d in json.loads((DATA / name).read_text())]

    pos, grns, invoices = load("pos.json"), load("grns.json"), load("invoices.json")
    invoice = next(i for i in invoices if i.doc_id == invoice_id)
    return match_invoice(invoice, pos, grns)


def _price_rows(match):
    return [d for d in match.discrepancies if d.field == DiscrepancyField.UNIT_PRICE]


def test_resolve_config_falls_back_to_base():
    assert resolve_config("V001", WITH_V005_OVERRIDE).unit_price_pct == 2.0
    assert resolve_config("V005", WITH_V005_OVERRIDE).unit_price_pct == 5.0


def test_headline_case_flips_with_the_contract_override():
    """INV-V005-3018 (4% variance): out of tolerance by default, within
    tolerance once the contract's 5% is applied."""
    match = _match("INV-V005-3018")

    by_default = apply_tolerances(match, resolve_config("V005", DEFAULT))
    assert _price_rows(by_default)[0].within_tolerance is False

    by_contract = apply_tolerances(match, resolve_config("V005", WITH_V005_OVERRIDE))
    assert _price_rows(by_contract)[0].within_tolerance is True


def test_8pct_variance_is_out_even_under_the_override():
    """INV-V005-3005 (8%): beyond even the contractual 5% — the override
    must not turn into a blank cheque."""
    match = _match("INV-V005-3005")
    checked = apply_tolerances(match, resolve_config("V005", WITH_V005_OVERRIDE))
    assert _price_rows(checked)[0].within_tolerance is False


def test_apply_tolerances_does_not_mutate_the_original():
    """The raw match is evidence; apply_tolerances must return a copy."""
    match = _match("INV-V005-3018")
    apply_tolerances(match, resolve_config("V005", WITH_V005_OVERRIDE))
    assert all(d.within_tolerance is False for d in match.discrepancies)


def test_manual_review_gate():
    assert requires_manual_review(500_000, DEFAULT) is True  # at threshold: review
    assert requires_manual_review(499_999, DEFAULT) is False
    assert requires_manual_review(None, DEFAULT) is True  # unbounded exposure


def test_clean_invoice_stays_clean():
    match = _match("INV-V001-3001")
    checked = apply_tolerances(match, resolve_config("V001", DEFAULT))
    assert checked.discrepancies == []
