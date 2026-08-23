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
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from apagent.agent.ap_tools import build_registry, hard_duplicates, recheck_with_contract
from apagent.eval import evaluate
from apagent.extraction.invoice import ExtractionError, extract_invoice
from apagent.matching.engine import match_invoice
from apagent.pipeline import _blocking_rows, decide_invoice, grn_gate
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.scheduling import schedule_payments
from apagent.schemas import (
    Action,
    ChatGrnPolicy,
    DiscrepancyField,
    Document,
    EvidenceSource,
    ToleranceConfig,
)
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
            self._cache = json.loads(CACHE.read_text(encoding="utf-8"))
        # Human sign-off state, per invoice: "confirmed" or "sent_to_human".
        # In memory only — demo session state, not part of the committed
        # decisions cache (a restart clears it, which is what a demo wants).
        self._human: dict[str, str] = {}
        # Invoices uploaded through the web. Session state like _human:
        # they live in the in-memory store, and _save_cache excludes their
        # decisions so the committed demo cache never picks them up.
        self._uploaded: set[str] = set()
        # Invoices whose decision this session used chat evidence for. Session
        # state like the two above, but held out of the eval differently — see
        # _eval_view for why dropping them would be wrong.
        self._chat_confirmed: set[str] = set()
        # The decisions as committed, before this session touched anything.
        # _eval_view serves these for chat-confirmed invoices so the measured
        # benchmark stays the benchmark.
        self._committed: dict[str, dict] = dict(self._cache)
        # Every message the system "sent" this session. Demo build: recorded
        # here instead of delivered (no SMTP). Bodies are always rebuilt
        # server-side from the code templates — never accepted from the
        # client, so nobody can put words in the system's mouth.
        self._outbox: list[dict] = []
        # Confirmed payments, newest last: who signed off which invoice,
        # when, for how much. The "where did my click go" record.
        self._payment_record: list[dict] = []
        # Built lazily and shared with the chat poller, so a harvested
        # receipt lands in THIS store and the console reflects it.
        self._harvester = None

    # --- decisions cache ---------------------------------------------------

    def _eval_view(self) -> dict[str, dict]:
        """The decisions the eval harness scores: the committed benchmark,
        with this session's own evidence held out.

        The two kinds of session evidence need OPPOSITE treatment, which is
        the whole reason this helper exists:

        - An uploaded invoice has no manifest entry, so the harness cannot
          score it either way. Dropping it is invisible and correct.
        - A chat-confirmed invoice DOES have a manifest entry (INV-V006-3019
          is planted as `missing_grn`). Drop its key and the harness reports
          it under `missing`, which fails the committed assertions and drags
          the touchless rate down. So it keeps its committed decision here.

        The benchmark measures the system against the ERP dataset, and chat
        evidence is outside that ground truth; the invoice's own detail page
        still shows the live decision. Stated openly rather than quietly
        filtered, because "false approvals: 0" is only worth something if it
        is measured over something honest.

        Used by _save_cache, metrics and analytics, so the file on disk and
        the two on-screen scorecards can never disagree.
        """
        view = {}
        for invoice_id, decision in self._cache.items():
            if invoice_id in self._uploaded:
                continue
            if invoice_id in self._chat_confirmed and invoice_id in self._committed:
                view[invoice_id] = self._committed[invoice_id]
            else:
                view[invoice_id] = decision
        return view

    def _save_cache(self) -> None:
        CACHE.write_text(
            json.dumps(self._eval_view(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

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
        # A re-run is a NEW decision: any human sign-off belonged to the old
        # one and is void — otherwise a "confirmed" badge could outlive the
        # APPROVE it certified. Mark the recorded payment as voided too, so
        # the payment log stops asserting "Paid" for a decision that changed.
        if self._human.pop(invoice_id, None) == "confirmed":
            for entry in reversed(self._payment_record):
                if entry["invoice_id"] == invoice_id and not entry["voided"]:
                    entry["voided"] = True
                    break
        self._save_cache()
        return self.get_case(invoice_id)

    def _human_state(self, invoice_id: str, action: str | None) -> str | None:
        """The human-review state, guarded: a "confirmed" is only ever shown
        while the current decision is still APPROVE. Belt-and-braces on top
        of run_case clearing the state — a sign-off must never be displayed
        against a decision it did not certify."""
        state = self._human.get(invoice_id)
        if state == "confirmed" and action != Action.APPROVE:
            return None
        return state

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
                    "human_review": self._human_state(inv.doc_id, dec["action"] if dec else None),
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
        gates = _guardrails(
            checked, rechecked, review_gate, duplicates, allowance, grn, po, invoice, config
        )
        decision = self._cache.get(invoice.doc_id)
        vendor_name = self.store.vendors().get(invoice.vendor_id, invoice.vendor_name)

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
            "guardrails": gates,
            "decision": decision,
            "human_review": self._human_state(
                invoice.doc_id, decision.get("action") if decision else None
            ),
            "handoff_draft": _handoff_draft(invoice, vendor_name, decision, gates),
            "outbound_to": self.outbound_recipient(invoice.doc_id),
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
        report = evaluate(json.loads(MANIFEST.read_text(encoding="utf-8")), self._eval_view())
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
        plan = schedule_payments(
            self._ordered_invoices(),
            self._cache,
            as_of,
            vendor_names=self.store.vendors(),
        )
        # Annotate with the session's human state so the Payments page can
        # show which scheduled invoices a reviewer already signed off.
        # Done here, not in the scheduler — the scheduler stays pure.
        for run in plan["runs"]:
            for payment in run["payments"]:
                for item in payment["invoices"]:
                    item["confirmed"] = self._human.get(item["invoice_id"]) == "confirmed"
        for n in plan["not_scheduled"]:
            n["human_review"] = self._human_state(n["invoice_id"], n["action"])
        plan["payment_record"] = list(reversed(self._payment_record))
        return plan

    def upload_invoice(self, filename: str, content: bytes) -> dict:
        """Extract an uploaded invoice PDF live, add it to the store, run
        the agent on it, and return the full case bundle.

        Uploads are session state: they never touch the committed dataset
        or the decisions cache on disk. The eval harness will list them as
        "unexpected" (no manifest ground truth) rather than scoring them —
        the metrics stay honest.
        """
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("PDF too large (5 MB limit)")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", Path(filename).stem) or "upload"
        tmp_dir = Path(tempfile.mkdtemp(prefix="apagent-upload-"))
        pdf_path = tmp_dir / f"{safe_name}.pdf"
        pdf_path.write_bytes(content)
        try:
            doc = extract_invoice(pdf_path, self.store.vendors())
        except ExtractionError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            pdf_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        # The printed invoice number goes into URLs and dict keys — keep it
        # to a safe shape, and never overwrite an existing document.
        doc_id = re.sub(r"[^A-Za-z0-9._-]", "-", doc.doc_id)[:40] or safe_name
        doc = doc.model_copy(update={"doc_id": doc_id})
        if self.store.get_invoice(doc.doc_id) is not None:
            raise ValueError(f"invoice {doc.doc_id} already exists in the dataset")
        self.store.add_invoice(doc)
        self._uploaded.add(doc.doc_id)
        return self.run_case(doc.doc_id)

    def confirm_payment(self, invoice_id: str, actor: str = "reviewer") -> dict:
        """A human confirms an APPROVEd invoice for payment.

        Code checks the precondition, not the frontend: only an invoice the
        agent APPROVEd (i.e. that already passed all six gates) can be
        confirmed. Anything else is refused here regardless of what the UI
        sends — same authority rule as everywhere.
        """
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            raise KeyError(invoice_id)
        decision = self._cache.get(invoice_id)
        if decision is None or decision["action"] != Action.APPROVE:
            raise ValueError("only an APPROVEd invoice can be confirmed for payment")
        # Idempotent on the RECORD, not on _human: send_to_human can
        # overwrite the human state, so the real "already paid" signal is an
        # existing un-voided payment row for this invoice, not the badge.
        self._human[invoice_id] = "confirmed"
        already_recorded = any(
            e["invoice_id"] == invoice_id and not e["voided"] for e in self._payment_record
        )
        if already_recorded:
            return self.get_case(invoice_id)
        self._payment_record.append(
            {
                "invoice_id": invoice_id,
                "vendor_name": self.store.vendors().get(invoice.vendor_id, invoice.vendor_name),
                "currency": invoice.currency,
                "total_cents": invoice.total_cents,
                "confirmed_by": actor,
                "confirmed_at": datetime.now().isoformat(timespec="seconds"),
                "voided": False,
            }
        )
        return self.get_case(invoice_id)

    def accept_chat_grn(self, invoice_id: str, actor: str = "reviewer") -> dict:
        """A reviewer vouches for a chat-confirmed delivery.

        This is the manual half of the chat-confirmation feature, and the
        reason the automatic half can afford to be strict. Everything the
        gate refuses on its own — a sender who is not on the receiver list,
        an amount above the informal ceiling — lands here as a hold with the
        conversation attached, and clearing it is one click by someone who
        is signed in.

        Deliberately NOT "record a formal goods receipt": the small
        businesses this serves confirm delivery in a chat group precisely
        because they keep no receipt book, so a formal record is not a step
        they can be sent away to perform. Endorsement is the terminal state,
        not a placeholder for one.

        It never touches the quantities. Accepting a confirmation that says
        80 arrived leaves an invoice billing 100 blocked at the facts gate,
        where it belongs — the reviewer vouched for the delivery, not for
        the bill.
        """
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            raise KeyError(invoice_id)
        match = match_invoice(invoice, self.store.all_pos(), self.store.all_grns())
        grn = self.store.get_grn_for_po(match.po_id) if match.po_id else None
        if grn is None or grn.source != EvidenceSource.CHAT:
            raise ValueError("only a chat-confirmed goods receipt can be accepted this way")
        self.store.add_grn(grn.model_copy(update={"endorsed_by": actor}))
        # Session-only, like every other piece of chat evidence: the endorsed
        # receipt lives in memory and never reaches data/synthetic/.
        self._chat_confirmed.add(invoice_id)
        return self.run_case(invoice_id)

    def on_chat_receipt(self, result) -> None:
        """A chat-harvested receipt landed; re-decide what it affects.

        Called from the chat poller's thread. The receipt is already in the
        store (the harvester shares this service's DocumentStore, which is
        the reason the poller runs in-process), so all that is left is to
        re-run the invoices whose proof of delivery just changed -- that
        re-run is what makes the console flip while someone is looking at it.
        """
        for invoice_id in result.invoice_ids:
            self._chat_confirmed.add(invoice_id)
            try:
                self.run_case(invoice_id)
            except Exception:
                # One bad invoice must not stop the others, and must not
                # propagate into the poller loop.
                continue

    def chat_harvester(self):
        """The harvester bound to THIS service's store, built once."""
        from apagent.chat.harvest import ChatHarvester

        if self._harvester is None:
            self._harvester = ChatHarvester(self.store)
        return self._harvester

    def send_to_human(self, invoice_id: str, actor: str = "reviewer") -> dict:
        """Route an invoice to a human reviewer and record the hand-off
        email in the outbox. The body comes from the code template in
        get_case — whatever the client sent is ignored."""
        if self.store.get_invoice(invoice_id) is None:
            raise KeyError(invoice_id)
        draft = self.get_case(invoice_id)["handoff_draft"]
        self._human[invoice_id] = "sent_to_human"
        self._record_sent(invoice_id, "handoff", draft, actor)
        return self.get_case(invoice_id)

    def send_outbound(self, invoice_id: str, actor: str = "reviewer") -> dict:
        """Record the decision's system-generated message in the outbox.

        The recipient is decided by CODE from the action, not by the button:
        an EMAIL action is a vendor query (billing@vendor); a HOLD carries an
        internal operations note, so it goes to operations — never leaking an
        internal note to the counterparty. Only possible when the decision
        carries an outbound message; there is no free-text path.
        """
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            raise KeyError(invoice_id)
        decision = self._cache.get(invoice_id) or {}
        message = decision.get("outbound_message")
        if not message:
            raise ValueError("this invoice has no system-generated message")
        vendor_name = self.store.vendors().get(invoice.vendor_id, invoice.vendor_name)
        if decision.get("action") == Action.EMAIL:
            to, kind, subject = (
                f"billing@{invoice.vendor_id.lower()}.example.com",
                "vendor_query",
                f"Query on invoice {invoice_id}",
            )
        else:  # internal operations note (HOLD)
            to, kind, subject = (
                "ap-supervisor@demo.local",
                "ops_note",
                f"[{invoice_id}] Action needed — {vendor_name}",
            )
        self._record_sent(invoice_id, kind, {"to": to, "subject": subject, "body": message}, actor)
        return self.get_case(invoice_id)

    def outbound_recipient(self, invoice_id: str) -> str | None:
        """Where the decision's outbound message would go — for the composer
        preview. Same code path that send_outbound enforces."""
        invoice = self.store.get_invoice(invoice_id)
        decision = self._cache.get(invoice_id) or {}
        if invoice is None or not decision.get("outbound_message"):
            return None
        if decision.get("action") == Action.EMAIL:
            return f"billing@{invoice.vendor_id.lower()}.example.com"
        return "ap-supervisor@demo.local"

    def _record_sent(self, invoice_id: str, kind: str, draft: dict, actor: str) -> dict:
        entry = {
            "invoice_id": invoice_id,
            "kind": kind,
            "to": draft["to"],
            "subject": draft["subject"],
            "body": draft["body"],
            "sent_by": actor,
            "sent_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._outbox.append(entry)
        return entry

    def outbox(self) -> list[dict]:
        """Sent messages, newest first."""
        return list(reversed(self._outbox))

    def analytics(self) -> dict:
        """The eval scorecard and per-vendor rollup for the Analytics view.

        Same evaluate() the CLI and the CI gate use — the page shows the
        measured numbers, not a separate hand-maintained copy of them.
        """
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        report = evaluate(manifest, self._eval_view())
        defects = [c for c in report["cases"] if c["defect"] != "clean"]
        clean = [c for c in report["cases"] if c["defect"] == "clean"]

        vendors = []
        names = self.store.vendors()
        for vendor_id in sorted(names):
            invs = self.store.invoices_for_vendor(vendor_id)
            approved = [
                i for i in invs if (self._cache.get(i.doc_id) or {}).get("action") == "APPROVE"
            ]
            totals: dict[str, int] = {}
            approved_totals: dict[str, int] = {}
            for i in invs:
                totals[i.currency] = totals.get(i.currency, 0) + (i.total_cents or 0)
            for i in approved:
                cur = i.currency
                approved_totals[cur] = approved_totals.get(cur, 0) + (i.total_cents or 0)
            vendors.append(
                {
                    "vendor_id": vendor_id,
                    "vendor_name": names[vendor_id],
                    "invoice_count": len(invs),
                    "approved_count": len(approved),
                    "billed_totals": dict(sorted(totals.items())),
                    "approved_totals": dict(sorted(approved_totals.items())),
                }
            )

        # Distribution counted inline — calling self.metrics() here would
        # run evaluate() a second time just to throw most of it away.
        distribution = {a.value: 0 for a in Action}
        for d in self._cache.values():
            distribution[d["action"]] = distribution.get(d["action"], 0) + 1
        return {
            "metrics": report["metrics"],
            "distribution": distribution,
            "unexpected": report["unexpected"],
            "defects": defects,
            "clean_total": len(clean),
            "clean_approved": sum(1 for c in clean if c["action"] == "APPROVE"),
            "clean_friction": sum(1 for c in clean if c["verdict"] == "friction"),
            "vendors": vendors,
        }

    def config_info(self) -> dict:
        """The policy the code enforces, for the (read-only) Settings view.

        Deliberately not editable from the web: every limit here lives in
        version-controlled code, so changing one is a reviewed commit, not
        a click. The page shows the policy; it does not own it.
        """
        from apagent.retrieval.search import price_variance_allowance

        names = self.store.vendors()
        allowances = []
        for vendor_id in sorted(names):
            found = price_variance_allowance(list(self._chunks()), vendor_id)
            allowances.append(
                {
                    "vendor_id": vendor_id,
                    "vendor_name": names[vendor_id],
                    "allowance_pct": found[0] if found else None,
                    "source": found[1].source if found else None,
                }
            )
        # Honest provider label: default must match llm/client.py's default,
        # and when the anthropic path is pointed at another host (DeepSeek
        # serves an anthropic-style API), say so instead of implying the
        # calls go to Anthropic.
        provider = os.getenv("LLM_PROVIDER", "anthropic")
        base_url = os.getenv("ANTHROPIC_BASE_URL")
        if provider == "anthropic" and base_url:
            host = urlparse(base_url).netloc or base_url
            provider = f"anthropic (via {host})"

        c = self.config
        return {
            "provider": provider,
            "tolerances": {
                "unit_price_pct": c.unit_price_pct,
                "total_abs_cents": c.total_abs_cents,
                "total_pct": c.total_pct,
                "qty_exact": c.qty_exact,
                "manual_review_threshold_cents": c.manual_review_threshold_cents,
                # Shown here because it decides whether money moves, and every
                # such limit belongs on this page rather than buried in code
                # nobody reads. Read-only like the rest of it.
                "informal_grn_ceiling_cents": c.informal_grn_ceiling_cents,
            },
            "chat_grn": {
                "policy": c.chat_grn_policy.value,
                "options": [p.value for p in ChatGrnPolicy],
            },
            "contract_allowances": allowances,
            "schedule": {"as_of": DEMO_AS_OF, "run_day": "Friday"},
            "actions": [a.value for a in Action],
        }

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


def _handoff_draft(invoice, vendor_name: str, decision: dict | None, gates: list[dict]) -> dict:
    """The internal hand-off email, rendered by CODE from a fixed template.

    Same rule as vendor-facing messages: the model never authors outbound
    text. The reviewer sees exactly what the facts say — action, reason,
    which gates failed — nothing more.
    """
    amount = f"{invoice.currency} {(invoice.total_cents or 0) / 100:,.2f}"
    action = decision["action"] if decision else "not decided yet"
    failed = [g["label"] for g in gates if not g["passed"]]
    lines = [
        f"Invoice {invoice.doc_id} from {vendor_name} ({amount}) needs your review.",
        "",
        f"Agent decision: {action}"
        + (f" ({decision['hold_reason']})" if decision and decision.get("hold_reason") else ""),
        "Failed gates: " + (", ".join(failed) if failed else "none"),
        f"Console: http://127.0.0.1:8000 -> Invoices -> {invoice.doc_id}",
        "",
        "This message was rendered by code from a fixed template.",
    ]
    return {
        "to": "ap-supervisor@demo.local",
        "subject": f"[{invoice.doc_id}] Review request — {vendor_name}",
        "body": "\n".join(lines),
    }


def _guardrails(
    checked, rechecked, review_gate, duplicates, allowance, grn, po, invoice, config
) -> list[dict]:
    """The six code gates as pass/fail, for the detail view. Mirrors
    pipeline._apply_guardrails so the UI shows exactly what code enforces.

    The GRN gate is not mirrored by hand any more — it CALLS pipeline.grn_gate,
    the same function the pipeline enforces with. Hand-copying it was already
    drifting (this copy folded gate-5 outcomes into the GRN chip, which the
    pipeline keeps separate), and the chat tier would have widened the gap. A
    UI that says a gate passed while code refuses it is worse than no UI.
    """
    blocking = _blocking_rows(rechecked)
    price_blocked = any(b.field == DiscrepancyField.UNIT_PRICE for b in blocking)
    qty_blocked = any(b.field == DiscrepancyField.QTY for b in blocking)
    other_blocked = any(
        b.field not in (DiscrepancyField.UNIT_PRICE, DiscrepancyField.QTY) for b in blocking
    )
    pct = f"{allowance[0]:g}%" if allowance else "default 2%"
    grn_passed, _ = grn_gate(checked, grn, po, invoice, config)
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
            "label": _grn_gate_label(grn),
            "passed": grn_passed and not qty_blocked and not other_blocked,
        },
    ]


def _grn_gate_label(grn) -> str:
    """The gate chip's wording. Computed here, in Python, because CLAUDE.md
    forbids business logic in the frontend — and "was this confirmed in chat
    or entered in the ERP" is exactly that."""
    if grn is not None and grn.source == EvidenceSource.CHAT:
        return "Goods received (chat-confirmed)"
    return "Goods received"


_service: Service | None = None


def get_service() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service
