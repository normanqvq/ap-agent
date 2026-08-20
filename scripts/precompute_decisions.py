"""Run the agent on every dataset invoice once and cache the decisions.

The web dashboard reads this cache (data/synthetic/decisions.json) so it
loads instantly and works offline. Re-run whenever the pipeline or dataset
changes. Uses whatever LLM_PROVIDER is set (DeepSeek by default).

    python scripts/precompute_decisions.py            # all invoices
    python scripts/precompute_decisions.py INV-V005-3018   # just one
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from apagent.api.service import CACHE, Service

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(ROOT / ".env")
    svc = Service()
    wanted = sys.argv[1:]
    invoices = [i for i in svc._ordered_invoices() if not wanted or i.doc_id in wanted]
    print(f"Running the agent on {len(invoices)} invoice(s)...")
    for inv in invoices:
        decision = svc.run_case(inv.doc_id)["decision"]
        hr = f" ({decision['hold_reason']})" if decision.get("hold_reason") else ""
        print(f"  {inv.doc_id:16s} -> {decision['action']}{hr}")
    print(f"\nCached {len(svc._cache)} decisions -> {CACHE}")


if __name__ == "__main__":
    main()
