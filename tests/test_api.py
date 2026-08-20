"""Tests for the API service layer.

Offline: these exercise the deterministic case-bundle assembly (facts,
guardrail pass/fail, ordering), not the LLM. The decision field may be
present or absent depending on whether the cache exists — the tests never
depend on it.
"""

from apagent.api.service import Service


def test_headline_case_all_guardrails_pass():
    """INV-V005-3018: 4% within the contract's 5% — every code gate passes,
    and the bundle exposes the code-parsed allowance."""
    c = Service().get_case("INV-V005-3018")
    assert c["contract_allowance_pct"] == 5.0
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert all(gates.values()), gates
    price = [d for d in c["match"]["discrepancies"] if d["field"] == "UNIT_PRICE"]
    assert len(price) == 1


def test_missing_grn_case_fails_the_grn_gate():
    c = Service().get_case("INV-V006-3019")
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert gates["grn"] is False
    assert c["grn"] is None


def test_injection_case_fails_the_price_gate():
    """INV-V002-3020: 10% overcharge, no contract allowance — price gate fails."""
    c = Service().get_case("INV-V002-3020")
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert gates["price"] is False


def test_duplicate_case_fails_the_duplicate_gate():
    c = Service().get_case("INV-V003-3901")
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert gates["duplicate"] is False
    assert "INV-V003-3003" in c["duplicates"]


def test_list_is_ordered_headline_first_and_complete():
    cases = Service().list_cases()
    assert cases[0]["invoice_id"] == "INV-V005-3018"
    assert len(cases) == 22
    assert all("vendor_name" in c and "total_cents" in c for c in cases)


def test_metrics_shape():
    m = Service().metrics()
    assert m["total"] == 22
    assert set(m["distribution"]) == {"APPROVE", "HOLD", "EMAIL", "ESCALATE"}
    assert 0 <= m["stp_pct"] <= 100


def test_analytics_scorecard_covers_every_planted_defect():
    a = Service().analytics()
    assert len(a["defects"]) == 7
    assert all(c["verdict"] == "pass" for c in a["defects"])
    assert a["clean_total"] == 15
    assert a["metrics"]["false_approve_count"] == 0
    assert len(a["vendors"]) == 6


def test_config_reports_the_enforced_policy():
    k = Service().config_info()
    assert k["tolerances"]["unit_price_pct"] == 2.0
    assert k["tolerances"]["manual_review_threshold_cents"] == 500_000
    by_id = {v["vendor_id"]: v["allowance_pct"] for v in k["contract_allowances"]}
    assert by_id["V005"] == 5.0  # the headline case's negotiated allowance
    assert k["actions"] == ["APPROVE", "HOLD", "EMAIL", "ESCALATE"]
