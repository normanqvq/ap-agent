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
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from apagent.agent.ap_tools import (
    build_registry,
    hard_duplicates,
    recheck_with_contract,
    superseded_by,
)
from apagent.eval import evaluate
from apagent.extraction.invoice import ExtractionError, extract_invoice
from apagent.mail.attach import pdf_attachments
from apagent.mail.revise import make_revision
from apagent.matching.engine import match_invoice
from apagent.pipeline import _blocking_rows, decide_invoice, grn_gate, supersede
from apagent.rules.tolerance import apply_tolerances, requires_manual_review, resolve_config
from apagent.scheduling import schedule_payments
from apagent.schemas import (
    Action,
    AgentDecision,
    ChatGrnPolicy,
    DiscrepancyField,
    Document,
    EvidenceSource,
    ToleranceConfig,
)
from apagent.store import DocumentStore

log = logging.getLogger(__name__)

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
        # receipt id -> the ChatGrnEvidence behind it, so the detail page
        # can show the conversation. Session state: the verbatim messages
        # are never written to disk.
        self._chat_evidence: dict = {}
        # The mail side, built only when a mailbox is configured. None here
        # means the app runs exactly as it did before this feature, which is
        # what keeps the test suite offline.
        self._mail_harvester = None
        self._dispatcher = None
        # invoice_id -> the replies received this session. Session state like
        # _chat_evidence: verbatim vendor text never reaches disk.
        self._vendor_replies: dict[str, list] = {}
        # invoice_id -> the revisions raised from vendor replies this session.
        # Session state like _uploaded: a corrected invoice never joins the
        # committed dataset or the decisions cache on disk.
        self._revisions: dict[str, list[str]] = {}

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
        # A revision raised from a vendor's reply is the first case, exactly
        # like an upload: it has no manifest entry, so the harness cannot
        # score it either way and dropping it is invisible. The invoice it
        # corrects keeps its own committed decision, which is what stops a
        # correction from quietly improving the benchmark.
        revisions = {doc_id for ids in self._revisions.values() for doc_id in ids}
        view = {}
        for invoice_id, decision in self._cache.items():
            if invoice_id in self._uploaded or invoice_id in revisions:
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
        # A decision that asks the vendor a question sends it. This is the
        # only place ongoing dispatch happens, so it must sit after the
        # cache write and before the bundle the caller renders.
        self._dispatch_query(invoice_id)
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
            checked,
            rechecked,
            review_gate,
            duplicates,
            allowance,
            grn,
            po,
            invoice,
            config,
            superseded_by(invoice, self.store),
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
            "vendor_replies": self._vendor_replies.get(invoice_id, []),
            # Ids, not decided bundles. The console fetches each revision's
            # own case when a reviewer opens it, and building them here would
            # cost a full match per revision on every page load of the
            # invoice they correct.
            "revisions": self._revisions.get(invoice_id, []),
        }

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
        total = len(self._ordered_invoices())
        counts = {a.value: 0 for a in Action}
        for d in decided:
            counts[d["action"]] = counts.get(d["action"], 0) + 1
        approve = counts["APPROVE"]
        # HOLD and EMAIL alike: decided, with no human touched at that moment.
        # Kept in step with eval.harness deliberately — test_api asserts the
        # two agree, which is the only thing stopping this copy from drifting
        # into a second, quieter definition of the headline number.
        untouched = counts["HOLD"] + counts["EMAIL"]
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
            "touchless_pct": round((approve + untouched) / n * 100),
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
        agent APPROVEd (i.e. that already passed every code gate) can be
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

    def attach_mail(self, directory, sender, mail_from: str) -> None:
        """Build the mail side against THIS service's store.

        Injected rather than constructed from the environment so a test can
        hand in a fake sender — the same reason the chat harvester takes its
        store instead of loading one.
        """
        from apagent.mail.dispatch import MailDispatcher
        from apagent.mail.harvest import MailHarvester
        from apagent.mail.thread import ThreadRegistry

        registry = ThreadRegistry()
        self._mail_harvester = MailHarvester(
            directory=directory, registry=registry, vendor_of=self._vendor_of
        )
        self._dispatcher = MailDispatcher(
            directory=directory, registry=registry, sender=sender, mail_from=mail_from
        )

    def mail_harvester(self):
        return self._mail_harvester

    def _vendor_of(self, invoice_id: str) -> str | None:
        invoice = self.store.get_invoice(invoice_id)
        return invoice.vendor_id if invoice else None

    def dispatch_vendor_queries(self) -> list[str]:
        """Send the queries every outstanding EMAIL decision asks for.

        The catch-up, run once at boot for whatever the cache already held.
        Ongoing dispatch is run_case's job -- this used to be the only call
        site in the whole product, so uploading the overcharge PDF, or
        clicking Run on an invoice that flipped to EMAIL, sent nothing at
        all, and the one query that ever went out was for the invoice that
        was already EMAIL in the committed cache when the process started.
        """
        return [invoice_id for invoice_id in list(self._cache) if self._dispatch_query(invoice_id)]

    def _dispatch_query(self, invoice_id: str) -> bool:
        """Send the query this invoice's decision asks for. True if one went.

        Called from run_case, so every path that changes a decision --
        upload, re-run, an accepted chat receipt, a correction that is still
        wrong -- asks the vendor by itself. Not called from inside the
        pipeline: pipeline.py is pure functions the offline suite runs
        constantly, and a send in there would mean pytest mails vendors.
        Repeats are the dispatcher's problem, and it refuses them on
        (invoice, body), so clicking Run twice does not query twice.
        """
        if self._dispatcher is None:
            return False
        decision = self._cache.get(invoice_id) or {}
        if decision.get("action") != Action.EMAIL:
            return False
        body = decision.get("outbound_message")
        vendor_id = self._vendor_of(invoice_id)
        if not body or not vendor_id:
            return False
        query = self._dispatcher.send_query(invoice_id, vendor_id, body)
        if query is None:
            return False
        # The same outbox the console already renders, so an automatic
        # send is as visible as one a reviewer triggered. Recorded here
        # rather than inside the dispatcher: the outbox is a console
        # concern, and the dispatcher must stay usable without a Service.
        self._record_sent(
            invoice_id,
            "vendor_query",
            {
                "to": self._dispatcher.directory.address_for(vendor_id),
                "subject": f"Query on invoice {invoice_id}",
                "body": body,
            },
            "system",
        )
        return True

    def on_vendor_reply(self, evidence, raw: bytes | None = None) -> None:
        """File a reply, and raise a revision if it carried a corrected invoice.

        The two halves are deliberately independent, and in this order: the
        evidence is filed first and unconditionally, so a reviewer sees what
        the vendor said even when the attachment turns out to be unreadable.
        """
        if evidence is None:
            return
        self._vendor_replies.setdefault(evidence.invoice_id, []).append(evidence.model_dump())
        if raw is None or evidence.is_non_delivery or not evidence.from_registered_sender:
            # An unregistered sender's attachment is evidence, never an
            # automatic path — the same rail the rest of the feature uses.
            # A bounce carries our OWN attachment back, which must never be
            # read as the vendor correcting themselves.
            return
        self._revise_from(evidence, raw)

    def _revise_from(self, evidence, raw: bytes) -> None:
        """A corrected invoice out of a reply, re-matched on its own merits.

        Does not raise, and this time means it: reached from the mail
        poller's daemon thread, where anything escaping costs the rest of
        the batch and the silence timers with it. The old clause named
        ExtractionError and ValueError, and neither is what actually
        happens -- a truncated file that still starts with %PDF raises out
        of pdfminer, and any LLM failure raises RuntimeError from inside
        run_case.

        The two failures are handled differently on purpose. A correction we
        cannot READ is not a document, so nothing is recorded. A correction
        we cannot DECIDE is a document the vendor really sent: it stays in
        the store, undecided, where the console lists it and a reviewer can
        run it -- and if the vendor sends another, it supersedes this one.

        The revision runs the whole pipeline afterwards — our purchase order,
        our goods receipt, our tolerances. That is what makes it safe to
        accept a document from the counterparty at all: they supply the
        figures, code decides whether the figures clear.
        """
        attachments = pdf_attachments(raw)
        original = self.store.get_invoice(evidence.invoice_id)
        if not attachments or original is None:
            return
        _, payload = attachments[0]
        # Same temp-file dance as upload_invoice: extraction takes a Path,
        # and nothing a vendor sent is ever written inside the repo.
        tmp_dir = Path(tempfile.mkdtemp(prefix="apagent-revision-"))
        pdf_path = tmp_dir / "corrected.pdf"
        pdf_path.write_bytes(payload)
        try:
            extracted = extract_invoice(pdf_path, self.store.vendors())
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.warning(
                "could not read the correction on %s: %s: %s",
                evidence.invoice_id,
                type(exc).__name__,
                exc,
            )
            return
        finally:
            pdf_path.unlink(missing_ok=True)
            tmp_dir.rmdir()

        # The chain, oldest first. A second correction supersedes the FIRST
        # correction, not the original invoice -- see make_revision: pointing
        # them all at the original leaves three payable siblings, which is
        # one payment per copy of a correction the vendor re-sent.
        chain = self._revisions.setdefault(original.doc_id, [])
        sequence = len(chain) + 1
        revision = make_revision(
            original,
            extracted,
            sequence,
            evidence_id=evidence.evidence_id,
            supersedes=chain[-1] if chain else original.doc_id,
        )
        self.store.add_invoice(revision)
        chain.append(revision.doc_id)
        log.info("raised %s from %s", revision.doc_id, evidence.evidence_id)
        self._withdraw(revision.replaces, revision.doc_id)
        try:
            self.run_case(revision.doc_id)
        except Exception:  # noqa: BLE001 - see the docstring
            log.exception(
                "%s was raised from %s but could not be decided; it is in the "
                "store undecided and a reviewer can run it",
                revision.doc_id,
                evidence.evidence_id,
            )

    def _withdraw(self, doc_id: str, successor_id: str) -> None:
        """Retire the decision on a document a correction just replaced.

        The pipeline's supersession gate only fires when a document is
        decided, and R1 was decided while it was still the newest in the
        chain -- so a vendor re-sending their correction produced three
        standing APPROVEs, one payment per copy. Applied to the cached
        decision instead of re-running the pipeline because supersession is
        a code fact, and re-asking the model a question code has already
        settled is how a settled question becomes an unsettled one.

        Only an APPROVE is touched, exactly as in _apply_guardrails. In
        practice the original invoice is never one -- it is the EMAIL action
        that sent the query in the first place -- which is also why this
        cannot quietly move the committed benchmark.
        """
        cached = self._cache.get(doc_id)
        if cached is None or cached.get("action") != Action.APPROVE:
            return
        self._cache[doc_id] = supersede(AgentDecision(**cached), successor_id).model_dump()
        self._human.pop(doc_id, None)
        self._save_cache()

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
    checked, rechecked, review_gate, duplicates, allowance, grn, po, invoice, config, superseded
) -> list[dict]:
    """The eight code gates as pass/fail, for the detail view. Mirrors
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
        {
            "key": "superseded",
            "label": (f"Superseded by {superseded.doc_id}" if superseded else "Not superseded"),
            "passed": superseded is None,
        },
        {"key": "money", "label": "Amount within threshold", "passed": not review_gate},
        {"key": "po", "label": "PO matched", "passed": checked.po_id is not None},
        {
            # Passes with no PO to compare against: the PO chip above already
            # reports that, and the pipeline never reaches the currency gate
            # without one.
            "key": "currency",
            "label": f"Billed in the currency ordered ({po.currency})" if po else "Currency",
            "passed": po is None or invoice.currency == po.currency,
        },
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
