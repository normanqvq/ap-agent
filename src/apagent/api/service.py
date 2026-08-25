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

# The channels a document can arrive through. "upload" is the manual web
# upload; "email" and "telegram" are the external fetchers' seam (docs/INTAKE.md).
# A set — order carries no meaning here.
VALID_INTAKE_SOURCES = {"upload", "email", "telegram"}


class Service:
    """Holds the loaded dataset and the decisions cache for the API."""

    def __init__(self) -> None:
        self.store = DocumentStore.from_dir(DATA)
        # The raw in-process registry is always kept: it is the fallback, and
        # the path uploaded (session-only) invoices always take.
        self._raw_registry = build_registry(self.store, CONTRACTS)
        self.registry = self._raw_registry
        # AP_MCP routes the agent's tool calls over MCP, with an automatic
        # fallback to the raw registry on any transport failure. Default off:
        # the committed demo path is the plain registry, unchanged. Results are
        # identical either way (tests/test_mcp.py pins that), so the toggle
        # never changes a decision.
        #   inproc  -- an in-process MCP session (shares this store)
        #   remote  -- a separate `python -m apagent.mcp_server` process
        self._mcp_mode = os.getenv("AP_MCP", "off")
        if self._mcp_mode == "inproc":
            from apagent.mcp_bridge import in_process_resilient_registry

            self.registry = in_process_resilient_registry(self._raw_registry)
        elif self._mcp_mode == "remote":
            from apagent.mcp_bridge import remote_resilient_registry

            self.registry = remote_resilient_registry(self._raw_registry)
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
        # receipt id -> the ChatGrnEvidence behind it, so the detail page
        # can show the conversation. Session state: the verbatim messages
        # are never written to disk.
        self._chat_evidence: dict = {}
        # invoice id -> the channel it arrived through (upload / email /
        # telegram). Session state like _uploaded; the seam the external
        # fetchers tag their documents with. See docs/INTAKE.md.
        self._intake_source: dict[str, str] = {}

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
        # Drop token fields when the provider did not report usage, so a live
        # re-run that produced no counts leaves the committed cache byte-for-byte
        # unchanged instead of adding `input_tokens: null` noise.
        view = {}
        for doc_id, decision in self._eval_view().items():
            view[doc_id] = {
                k: v
                for k, v in decision.items()
                if not (k in ("input_tokens", "output_tokens") and v is None)
            }
        CACHE.write_text(json.dumps(view, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def cached_decision(self, invoice_id: str) -> dict | None:
        return self._cache.get(invoice_id)

    def run_case(self, invoice_id: str) -> dict:
        """Run the agent live on one invoice, cache the decision, return the
        full case bundle."""
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            raise KeyError(invoice_id)
        # A remote MCP server has its own store and cannot see an invoice
        # uploaded this session, so those decisions must use the in-process
        # registry — otherwise a duplicate check would miss the upload. An
        # in-process MCP session shares this store, so no special case there.
        registry = self.registry
        if invoice_id in self._uploaded and getattr(self.registry, "shares_store", True) is False:
            registry = self._raw_registry
        decision = decide_invoice(
            invoice, self.store, registry, self.config, contracts_dir=CONTRACTS
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
            "chat_grn": self._chat_grn_view(grn, po),
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
            # None for the committed dataset; set only for a document that came
            # in through intake() this session. Makes the provenance label
            # outlive the intake response, as docs/INTAKE.md promises.
            "intake_source": self._intake_source.get(invoice.doc_id),
        }

    def performance(self, report: dict | None = None) -> dict:
        """The six agent-performance metrics from the training deck, MEASURED
        over the decided runs — the honest version, where a cell can show a
        failure. Scores the same _eval_view() the other scorecards use, so the
        panel can never disagree with the defect table above it.
        """
        from apagent.agent.loop import CAP_ESCALATE_PREFIX, MAX_ROUNDS

        view = self._eval_view()
        decided = list(view.values())
        n = len(decided) or 1
        if report is None:
            report = evaluate(json.loads(MANIFEST.read_text(encoding="utf-8")), view)

        # 1. schema-validation pass rate: a decision that did NOT produce usable
        # JSON — the parse failure, or an empty model response — is a miss,
        # rather than calling every cached row a pass.
        schema_misses = ("Failed to parse agent", "Model returned empty response")
        schema_ok = sum(1 for d in decided if not d.get("reasoning", "").startswith(schema_misses))
        # 2. tool-call success: registry.execute returns an "Error:" string for
        # an unknown tool or a handler crash; everything else is a served
        # result (a plain "not found" is a valid answer, not a failure).
        tool_results = [tc.get("result", "") for d in decided for tc in d.get("tool_calls", [])]
        tool_ok = sum(1 for r in tool_results if not r.startswith("Error:"))
        # 3. task completion: reached a decision without force-escalating at the
        # round cap (loop.py writes CAP_ESCALATE_PREFIX only on that exit).
        completed = sum(
            1 for d in decided if not d.get("reasoning", "").startswith(CAP_ESCALATE_PREFIX)
        )
        # 4. token cost per run, averaged over runs the provider reported usage for
        rounds = [d.get("rounds_used", 0) for d in decided]
        token_runs = [
            (d.get("input_tokens") or 0) + (d.get("output_tokens") or 0)
            for d in decided
            if d.get("input_tokens") is not None
        ]
        return {
            "schema_pass": {"ok": schema_ok, "total": len(decided)},
            "tool_calls": len(tool_results),
            "tool_success_pct": round(tool_ok / len(tool_results) * 100) if tool_results else None,
            "completion_pct": round(completed / n * 100),
            "hit_cap": len(decided) - completed,
            "avg_tokens_per_run": round(sum(token_runs) / len(token_runs)) if token_runs else None,
            "token_runs_measured": len(token_runs),
            "avg_rounds": round(sum(rounds) / n, 2),
            "max_rounds": MAX_ROUNDS,
            # 6. answer fidelity vs the reviewed ground-truth set (the eval)
            "false_approve": report["metrics"]["false_approve_count"],
            "defects_handled": sum(
                1 for c in report["cases"] if c["defect"] != "clean" and c["verdict"] == "pass"
            ),
            "defects_total": sum(1 for c in report["cases"] if c["defect"] != "clean"),
        }

    def mcp_status(self) -> dict:
        """How the agent is calling its tools: off, in-process MCP, or a remote
        MCP server -- plus the transport split and breaker state when MCP is on.
        A degraded-to-fallback run is visible here without reading logs."""
        status = {"mode": self._mcp_mode}
        if hasattr(self.registry, "status"):
            status.update(self.registry.status())
        return status

    def metrics(self) -> dict:
        """The dashboard's headline numbers, all measured over the same set.

        Every number here reads _eval_view, not the live cache. They used to
        disagree: STP counted this session's decisions while false approvals
        were scored against the committed benchmark, so a chat confirmation
        flipping an invoice pushed STP up without the defect it cleared ever
        being re-scored. Three tiles measured over one population and a
        fourth over another, side by side, presented as one scorecard.

        Which one wins is not arbitrary. The benchmark's ground truth knows
        nothing about a message someone sent this morning, so counting that
        invoice as straight-through inflates a headline number with evidence
        the manifest cannot check. The chat flip is real and worth showing —
        it is shown on the invoice's own page, where the conversation behind
        it is visible too, rather than folded into a rate.
        """
        decided = list(self._eval_view().values())
        # The benchmark population excludes session uploads, matching `decided`
        # (which reads _eval_view) — otherwise an uploaded-and-decided invoice
        # would swell `total` and be counted as pending, dropping STP.
        total = sum(1 for i in self._ordered_invoices() if i.doc_id not in self._uploaded)
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

    def baseline_comparison(self) -> dict:
        """Rules-only vs the agent, scored over the same committed benchmark.

        The baseline runs the deterministic pipeline with no agent and no
        contract lookup (pipeline.decide_invoice_rules_only); the agent column
        is the committed decisions. Both go through the same eval harness, so
        the panel shows what the agent's judgement actually buys: the invoices
        it recovers that pure rules would hold — with false approvals still
        zero on BOTH sides, i.e. the agent adds STP without adding risk.
        """
        from apagent.pipeline import decide_invoice_rules_only

        view = self._eval_view()
        baseline_decisions: dict[str, dict] = {}
        for invoice_id in view:
            invoice = self.store.get_invoice(invoice_id)
            if invoice is None:
                continue
            baseline_decisions[invoice_id] = decide_invoice_rules_only(
                invoice, self.store, self.config
            ).model_dump()
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        base_report = evaluate(manifest, baseline_decisions)
        agent_report = evaluate(manifest, view)
        # Invoices the agent approved that pure rules held: the measured payoff
        # of the judgement, same-direction only (a recovered approve, never a
        # recovered risk — a baseline approve the agent held would show as a
        # negative and is not what this panel claims).
        # Only count a recovery the ground truth agrees with: the agent's
        # APPROVE must be a verdict=="pass" (the invoice really was payable),
        # never a false approve the baseline happened to hold. Without this the
        # panel could parade an invoice the agent wrongly paid as a "recovery"
        # (the false_approve tile and the A/B test would still catch it, but
        # the recovered list must not present risk as a win).
        passed_ids = {c["invoice_id"] for c in agent_report["cases"] if c["verdict"] == "pass"}
        recovered = []
        for invoice_id, agent_dec in view.items():
            base_dec = baseline_decisions.get(invoice_id)
            if base_dec is None or agent_dec["action"] != Action.APPROVE:
                continue
            if base_dec["action"] == Action.APPROVE or invoice_id not in passed_ids:
                continue
            inv = self.store.get_invoice(invoice_id)
            recovered.append(
                {
                    "invoice_id": invoice_id,
                    "vendor_name": self.store.vendors().get(inv.vendor_id, "") if inv else "",
                    "baseline_action": base_dec["action"],
                    "baseline_reason": _reason_label(base_dec),
                }
            )
        return {
            "baseline": base_report["metrics"],
            "agent": agent_report["metrics"],
            "recovered": recovered,
        }

    def roi(self) -> dict:
        """The cost case: measured where it can be, cited where it cannot.

        Manual processing runs about US$9.40 per invoice, and fixing a
        mis-keyed or mis-approved one adds 25-40% on top (Ardent Partners,
        2025 — the source the README already cites). The agent's own cost is
        token cost: agent_avg_tokens is the measured tokens per run when a real
        provider reported usage, and None otherwise — left unmeasured rather
        than guessed, the same honesty rule as the rest of the panel, and the
        UI shows a dash rather than a dollar figure until it is measured (a run
        carries the system prompt, the full invoice dump and several tool
        results, so "a fraction of a cent" is a claim, not a measurement). The
        headline is the manual cost and the 25-40% rework a zero-false-approve
        run never triggers, not the token bill.
        """
        perf = self.performance()
        n = perf["schema_pass"]["total"]
        manual_cents = 940  # US$9.40 per invoice, Ardent Partners 2025
        return {
            "invoices": n,
            "manual_cost_cents": manual_cents,
            "manual_batch_cents": manual_cents * n,
            "rework_low_pct": 25,
            "rework_high_pct": 40,
            "agent_avg_tokens": perf["avg_tokens_per_run"],
            "agent_cost_measured": perf["avg_tokens_per_run"] is not None,
            "false_approve": perf["false_approve"],
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

    def intake(self, source: str, filename: str, content: bytes) -> dict:
        """Land a document from an external channel into the upload pipeline,
        tagged with where it came from.

        The seam for the email / Telegram intake work: a fetcher on the other
        side normalises whatever it received (an email attachment, a file
        exported from a chat) into (source, filename, PDF bytes) and calls
        this. Extraction and the agent decision are EXACTLY the upload path —
        one intake, one set of guardrails — so a new channel adds a provenance
        label, never a second decision path that could drift from the first.
        Like uploads, an intake document is session state and never touches the
        committed dataset. See docs/INTAKE.md for the full contract, including
        why the Telegram fetcher must not open a second getUpdates consumer.
        """
        if source not in VALID_INTAKE_SOURCES:
            raise ValueError(
                f"unknown intake source {source!r} (expected one of {sorted(VALID_INTAKE_SOURCES)})"
            )
        bundle = self.upload_invoice(filename, content)
        self._intake_source[bundle["invoice_id"]] = source
        bundle["intake_source"] = source
        return bundle

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

    def _chat_grn_view(self, grn, po) -> dict | None:
        """The chat confirmation behind a receipt, for the detail page.

        None when the receipt is an ordinary ERP one, so the card simply does
        not render. Everything a reviewer needs to judge the confirmation is
        assembled HERE, in Python, because CLAUDE.md keeps business logic out
        of the frontend — "was this person allowed to confirm" and "which
        ordered lines went unconfirmed" are exactly that.

        The verbatim messages ride in `messages`. They are the one genuinely
        attacker-authored thing on this page, so the browser must escape them
        like any supplier-printed string.
        """
        if grn is None or grn.source != EvidenceSource.CHAT:
            return None
        evidence = self._chat_evidence.get(grn.doc_id)
        confirmed = {line.line_no for line in grn.lines}
        unconfirmed = (
            [line.description for line in po.lines if line.line_no not in confirmed] if po else []
        )
        return {
            "receipt_id": grn.doc_id,
            "confirmed_by": grn.confirmed_by,
            "authorised": grn.confirmed_by is not None,
            "endorsed_by": grn.endorsed_by,
            "captured_at": grn.captured_at,
            "policy": resolve_config(grn.vendor_id, self.config).chat_grn_policy.value,
            "lines": [
                {"sku": line.sku, "description": line.description, "qty": line.qty}
                for line in grn.lines
            ],
            "unconfirmed": unconfirmed,
            "messages": [m.model_dump() for m in evidence.messages] if evidence else [],
        }

    def on_chat_receipt(self, result) -> None:
        """A chat-harvested receipt landed; re-decide what it affects.

        Called from the chat poller's thread. The receipt is already in the
        store (the harvester shares this service's DocumentStore, which is
        the reason the poller runs in-process), so all that is left is to
        re-run the invoices whose proof of delivery just changed -- that
        re-run is what makes the console flip while someone is looking at it.
        """
        if result.receipt is not None and result.evidence is not None:
            self._chat_evidence[result.receipt.doc_id] = result.evidence
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

        # Distribution over the SAME eval view every other number uses, so the
        # decision mix agrees with the dashboard and does not swell when an
        # invoice is uploaded this session.
        distribution = {a.value: 0 for a in Action}
        for d in self._eval_view().values():
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
            "performance": self.performance(report),  # reuse the report — no second evaluate()
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
