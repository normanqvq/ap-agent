"""Service layer between the pipeline and the web API.

The API stays thin: it calls these functions and serializes what comes back.
Everything here is deterministic assembly on top of the pipeline — the only
non-deterministic step is run_case (it invokes the LLM), and its result is
cached so the dashboard and every re-open are instant and offline.

Why a decisions cache: running the agent on all 22 invoices is 22 LLM calls.
Browsing the dashboard should not pay that, and the demo should still work
with no network. precompute_decisions.py fills the cache once; the API reads
it, and run_case refreshes a single invoice live for the "watch it decide"
moment.
"""

import json
from pathlib import Path

from apagent.agent.ap_tools import build_registry, hard_duplicates, recheck_with_contract
from apagent.eval import evaluate
from apagent.matching.engine import match_invoice
from apagent.pipeline import _blocking_rows, decide_invoice
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.scheduling import schedule_payments
from apagent.schemas import Action, DiscrepancyField, Document, ToleranceConfig
from apagent.store import DocumentStore

ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA = ROOT / "data" / "synthetic"
CONTRACTS = DATA / "contracts"
CACHE = DATA / "decisions.json"
MANIFEST = DATA / "manifest.json"

# The demo's "today". The dataset lives in Aug 2026, so the schedule is
# planned from a fixed Friday inside that window — deterministic and
# offline, same idea as the decisions cache. A real deployment would use
# the actual date.
DEMO_AS_OF = "2026-08-14"

# A stable demo order: the clean control first, then the planted defects in a
# storyline order, then the rest. Anything not listed keeps its file order.
DEMO_ORDER = [
    "INV-V005-3018",  # headline: contract-flip approve
    "INV-V001-3001",  # clean
    "INV-V005-3005",  # 8% over contract
    "INV-V006-3019",  # missing GRN
    "INV-V002-3020",  # injection
    "INV-V001-3021",  # partial delivery
    "INV-V003-3901",  # duplicate
    "INV-V004-3010",  # missing PO ref
]


class Service:
    """Holds the loaded dataset and the decisions cache for the API."""

    def __init__(self) -> None:
        self.store = DocumentStore.from_dir(DATA)
        self.registry = build_registry(self.store, CONTRACTS)
        self.config = ToleranceConfig()
        self._cache: dict[str, dict] = {}
        if CACHE.exists():
            self._cache = json.loads(CACHE.read_text())

    # --- decisions cache ---------------------------------------------------

    def _save_cache(self) -> None:
        CACHE.write_text(json.dumps(self._cache, indent=2, ensure_ascii=False) + "\n")

    def cached_decision(self, invoice_id: str) -> dict | None:
        return self._cache.get(invoice_id)

    def run_case(self, invoice_id: str) -> dict:
        """Run the agent live on one invoice, cache the decision, return the
        full case bundle."""
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            raise KeyError(invoice_id)
        decision = decide_invoice(
            invoice, self.store, self.registry, self.config, contracts_dir=CONTRACTS
        )
        self._cache[invoice_id] = decision.model_dump()
        self._save_cache()
        return self.get_case(invoice_id)

    # --- reads -------------------------------------------------------------

    def _ordered_invoices(self) -> list[Document]:
        invoices = list(self.store._invoices.values())  # noqa: SLF001 (service owns the store)
        rank = {doc_id: i for i, doc_id in enumerate(DEMO_ORDER)}
        return sorted(invoices, key=lambda d: (rank.get(d.doc_id, 999), d.doc_id))

    def list_cases(self) -> list[dict]:
        out = []
        for inv in self._ordered_invoices():
            dec = self._cache.get(inv.doc_id)
            out.append(
                {
                    "invoice_id": inv.doc_id,
                    "vendor_id": inv.vendor_id,
                    "vendor_name": self.store.vendors().get(inv.vendor_id, inv.vendor_name),
                    "currency": inv.currency,
                    "total_cents": inv.total_cents,
                    "action": dec["action"] if dec else None,
                    "hold_reason": dec.get("hold_reason") if dec else None,
                    "reason": _reason_label(dec) if dec else None,
                }
            )
        return out

    def get_case(self, invoice_id: str) -> dict:
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            raise KeyError(invoice_id)
        config = resolve_config(invoice.vendor_id, self.config)
        match = match_invoice(invoice, self.store.all_pos(), self.store.all_grns())
        checked = apply_tolerances(match, config)
        review_gate = requires_manual_review(invoice.total_cents, config)
        duplicates = hard_duplicates(invoice, self.store, config)
        allowance, rechecked = recheck_with_contract(
            checked, invoice.vendor_id, self._chunks(), config
        )
        po = self.store.get_po(match.po_id) if match.po_id else None
        grn = self.store.get_grn_for_po(match.po_id) if match.po_id else None

        return {
            "invoice_id": invoice.doc_id,
            "vendor_id": invoice.vendor_id,
            "vendor_name": self.store.vendors().get(invoice.vendor_id, invoice.vendor_name),
            "currency": invoice.currency,
            "total_cents": invoice.total_cents,
            "issue_date": invoice.issue_date,
            "due_date": invoice.due_date,
            "payment_terms": invoice.payment_terms,
            "ref_doc_id": invoice.ref_doc_id,
            "lines": [line.model_dump() for line in invoice.lines],
            "po": po.model_dump() if po else None,
            "grn": grn.model_dump() if grn else None,
            "match": checked.model_dump(),
            "review_gate": review_gate,
            "duplicates": [d.doc_id for d in duplicates],
            "contract_allowance_pct": allowance[0] if allowance else None,
            "guardrails": _guardrails(checked, rechecked, review_gate, duplicates, allowance),
            "decision": self._cache.get(invoice.doc_id),
        }

    def metrics(self) -> dict:
        decided = [d for d in self._cache.values()]
        total = len(self._ordered_invoices())
        counts = {a.value: 0 for a in Action}
        for d in decided:
            counts[d["action"]] = counts.get(d["action"], 0) + 1
        approve = counts["APPROVE"]
        hold = counts["HOLD"]
        # Denominator is ALL invoices (CLAUDE.md metric definitions): an
        # undecided invoice is not straight-through, so missing decisions
        # lower STP instead of inflating it.
        n = total or 1
        # Measured, not asserted: the eval harness scores every decision
        # against the manifest ground truth and counts wrong approvals.
        report = evaluate(json.loads(MANIFEST.read_text()), self._cache)
        return {
            "total": total,
            "decided": len(decided),
            "pending": total - len(decided),
            "stp_pct": round(approve / n * 100),
            "touchless_pct": round((approve + hold) / n * 100),
            "false_approve": report["metrics"]["false_approve_count"],
            "distribution": counts,
        }

    def schedule(self, as_of: str = DEMO_AS_OF) -> dict:
        """Plan the weekly payment runs from the cached decisions."""
        return schedule_payments(
            self._ordered_invoices(),
            self._cache,
            as_of,
            vendor_names=self.store.vendors(),
        )

    _chunks_cache = None

    def _chunks(self):
        if Service._chunks_cache is None:
            from apagent.retrieval.search import load_contracts

            Service._chunks_cache = tuple(load_contracts(CONTRACTS))
        return Service._chunks_cache


def _reason_label(dec: dict) -> str:
    action = dec["action"]
    if action == "APPROVE":
        return "—"
    hr = dec.get("hold_reason")
    reasons = {
        "PRICE_VARIANCE": "Price variance",
        "AWAITING_GRN": "No goods receipt",
        "AWAITING_DELIVERY": "Short delivery",
    }
    if hr in reasons:
        return reasons[hr]
    r = dec.get("reasoning", "").lower()
    if "hard-duplicate" in r or "duplicate of inv" in r or "duplicates inv" in r:
        return "Duplicate"
    if "manual_review_required=true" in r or "at or above the manual-review threshold" in r:
        return "Over threshold"
    if action == "ESCALATE":
        return "Needs review"
    return "—"


def _guardrails(checked, rechecked, review_gate, duplicates, allowance) -> list[dict]:
    """The six code gates as pass/fail, for the detail view. Mirrors
    pipeline._apply_guardrails so the UI shows exactly what code enforces."""
    blocking = _blocking_rows(rechecked)
    price_blocked = any(b.field == DiscrepancyField.UNIT_PRICE for b in blocking)
    qty_blocked = any(b.field == DiscrepancyField.QTY for b in blocking)
    other_blocked = any(
        b.field not in (DiscrepancyField.UNIT_PRICE, DiscrepancyField.QTY) for b in blocking
    )
    pct = f"{allowance[0]:g}%" if allowance else "default 2%"
    return [
        {"key": "money", "label": "Amount within threshold", "passed": not review_gate},
        {"key": "po", "label": "PO matched", "passed": checked.po_id is not None},
        {
            "key": "unmatched",
            "label": "No unordered lines",
            "passed": not checked.unmatched_inv_lines,
        },
        {"key": "duplicate", "label": "No duplicate", "passed": not duplicates},
        {"key": "price", "label": f"Price within tolerance ({pct})", "passed": not price_blocked},
        {
            "key": "grn",
            "label": "Goods received",
            "passed": checked.grn_id is not None and not qty_blocked and not other_blocked,
        },
    ]


_service: Service | None = None


def get_service() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service
