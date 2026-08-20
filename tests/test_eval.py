"""Tests for the eval harness.

Two layers: unit tests of the scoring logic on fabricated inputs, and one
test that scores the committed decisions cache against the committed
manifest — pinning "false approvals: 0" so it fails the build the day it
stops being true.
"""

import json
from pathlib import Path

from apagent.eval import MUST_NOT_APPROVE, SHOULD_APPROVE, evaluate

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


def _decision(action, hold_reason=None):
    return {"action": action, "hold_reason": hold_reason}


def test_every_manifest_defect_has_an_expectation():
    """A new defect type added to the generator must be classified here,
    or the harness would silently treat it as payable."""
    manifest = json.loads((DATA / "manifest.json").read_text())
    defects = {e["defect"] for e in manifest}
    known = MUST_NOT_APPROVE | SHOULD_APPROVE
    assert defects <= known, defects - known


def test_approving_a_defect_is_a_false_approve():
    manifest = [{"invoice_id": "INV-1", "defect": "prompt_injection"}]
    report = evaluate(manifest, {"INV-1": _decision("APPROVE")})
    assert report["false_approves"] == ["INV-1"]
    assert report["metrics"]["false_approve_count"] == 1


def test_holding_a_defect_passes_and_holding_a_clean_is_friction():
    manifest = [
        {"invoice_id": "INV-1", "defect": "duplicate"},
        {"invoice_id": "INV-2", "defect": "clean"},
    ]
    decisions = {"INV-1": _decision("ESCALATE"), "INV-2": _decision("HOLD", "PRICE_VARIANCE")}
    report = evaluate(manifest, decisions)
    assert report["false_approves"] == []
    assert report["friction"] == ["INV-2"]
    verdicts = {c["invoice_id"]: c["verdict"] for c in report["cases"]}
    assert verdicts == {"INV-1": "pass", "INV-2": "friction"}


def test_missing_decision_is_reported_not_scored():
    manifest = [{"invoice_id": "INV-1", "defect": "clean"}]
    report = evaluate(manifest, {})
    assert report["missing"] == ["INV-1"]
    assert report["metrics"]["decided"] == 0


def test_metrics_math():
    """3 decided: 2 approve + 1 hold -> STP 67, touchless 100."""
    manifest = [
        {"invoice_id": "INV-1", "defect": "clean"},
        {"invoice_id": "INV-2", "defect": "clean"},
        {"invoice_id": "INV-3", "defect": "missing_grn"},
    ]
    decisions = {
        "INV-1": _decision("APPROVE"),
        "INV-2": _decision("APPROVE"),
        "INV-3": _decision("HOLD", "AWAITING_GRN"),
    }
    m = evaluate(manifest, decisions)["metrics"]
    assert m["stp_pct"] == 67
    assert m["touchless_pct"] == 100
    assert m["false_approve_count"] == 0


def test_committed_decisions_have_zero_false_approves():
    """The headline claim, measured over the committed cache: every planted
    defect was blocked, and the demo metrics are what the README says."""
    manifest = json.loads((DATA / "manifest.json").read_text())
    decisions = json.loads((DATA / "decisions.json").read_text())
    report = evaluate(manifest, decisions)
    assert report["false_approves"] == []
    assert report["missing"] == []
    assert report["metrics"]["stp_pct"] == 68
    assert report["metrics"]["touchless_pct"] == 82
