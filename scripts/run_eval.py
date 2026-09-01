"""Score the cached decisions against the manifest and print the report.

Reads data/synthetic/decisions.json (fill it with precompute_decisions.py
first) and data/synthetic/manifest.json, prints one line per invoice, and
writes the full report to data/synthetic/eval_report.json.

Exit code 1 if any invoice was falsely approved — so this doubles as a CI
gate: the demo claim "false approvals: 0" fails the build the day it stops
being true.

    python scripts/run_eval.py
"""

import json
import sys
from pathlib import Path

from apagent.api.service import DEMO_HELD_OUT
from apagent.eval import evaluate

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"
REPORT = DATA / "eval_report.json"

MARKS = {"pass": "ok", "friction": "FRICTION", "false_approve": "FALSE APPROVE", "missing": "?"}


def main() -> None:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    decisions_path = DATA / "decisions.json"
    if not decisions_path.exists():
        sys.exit("No decisions cache. Run scripts/precompute_decisions.py first.")
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    # The seeded demo invoice has no manifest entry by design -- it exists to
    # be clicked, not scored. Hold it out here as the service does, so this
    # CLI, the committed report and the console agree instead of a fresh run
    # warning about a file that ships in git.
    decisions = {k: v for k, v in decisions.items() if k not in DEMO_HELD_OUT}

    report = evaluate(manifest, decisions)

    print(f"{'invoice':17s} {'defect':31s} {'decision':22s} verdict")
    for c in report["cases"]:
        action = c["action"] or "—"
        if c["hold_reason"]:
            action += f" ({c['hold_reason']})"
        print(f"{c['invoice_id']:17s} {c['defect']:31s} {action:22s} {MARKS[c['verdict']]}")

    m = report["metrics"]
    print(
        f"\ndecided {m['decided']}/{m['total']}  ·  STP {m['stp_pct']}%  ·  "
        f"touchless {m['touchless_pct']}%  ·  false approves {m['false_approve_count']}  ·  "
        f"friction {m['friction_count']}"
    )
    if report["friction"]:
        print(f"friction (safe, but not straight-through): {', '.join(report['friction'])}")
    if report["missing"]:
        print(f"missing decisions: {', '.join(report['missing'])}")
    if report["unexpected"]:
        print(
            "WARNING — decisions with no manifest entry (unscored, no ground truth): "
            + ", ".join(report["unexpected"])
        )

    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written -> {REPORT}")

    if report["false_approves"]:
        sys.exit(f"FALSE APPROVES: {', '.join(report['false_approves'])}")


if __name__ == "__main__":
    main()
