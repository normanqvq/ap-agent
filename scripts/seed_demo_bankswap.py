"""One-shot: compute the demo bank-swap invoice's decision deterministically
(rules-only + the real guardrails, no model needed) and write it into the
committed decisions cache so the console shows the ESCALATE on load. Idempotent."""

import json
from pathlib import Path

from apagent.pipeline import decide_invoice_rules_only
from apagent.store import DocumentStore

DATA = Path("data/synthetic")
store = DocumentStore.from_dir(DATA)
inv = store.get_invoice("INV-DEMO-BANKSWAP")
decision = decide_invoice_rules_only(inv, store)
assert decision.action.value == "ESCALATE", decision.action

cache = json.loads((DATA / "decisions.json").read_text(encoding="utf-8"))
cache["INV-DEMO-BANKSWAP"] = decision.model_dump()
(DATA / "decisions.json").write_text(
    json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
print("seeded INV-DEMO-BANKSWAP:", decision.action.value)
