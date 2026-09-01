"""One-shot: give every committed invoice the payout account on file for its
vendor, so the payout-account gate sees a MATCH on the graded set and the
metrics do not move. Run once, then commit invoices.json. Idempotent."""

import json
from pathlib import Path

DATA = Path("data/synthetic")
vendors = json.loads((DATA / "vendors.json").read_text(encoding="utf-8"))
accounts = {v["vendor_id"]: v["payout_account"] for v in vendors}
invoices = json.loads((DATA / "invoices.json").read_text(encoding="utf-8"))
for inv in invoices:
    if inv["doc_id"] == "INV-DEMO-BANKSWAP":
        continue  # the demo carries a mismatching account, set in a later task
    inv["payout_account"] = accounts.get(inv["vendor_id"])
out = json.dumps(invoices, indent=2, ensure_ascii=False) + "\n"
(DATA / "invoices.json").write_text(out, encoding="utf-8")
print(f"stamped {len(invoices)} invoices")
