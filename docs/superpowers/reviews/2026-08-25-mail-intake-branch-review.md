# `feat/mail-intake-impl` — branch review, and what is left to do

Date: 2026-08-25
Reviewed range: `b6cffe4..074b8c3` (24 commits — phases 1 and 2 of the vendor email intake)
State at time of writing: 317 tests pass, ruff clean, working tree clean, **not pushed**

**Verdict: not mergeable yet.** Two findings move money without a human click, one
takes the whole console down, and the thing a judge is most likely to ask for is
the one path that does not work.

Every finding below was reproduced by executing it against the branch, not
reasoned about. The three marked ✔ were re-verified independently afterwards.

---

## Disposition (2026-08-26)

Every finding below was worked through in commit order. The branch is 354
tests green, ruff clean, and STP 68% / touchless 82% / false approvals 0 are
byte-identical to `main`.

| # | Finding | Status |
|---|---|---|
| 1 | Re-sent correction paid once per copy | fixed — `replaces` names the newest document; a superseded document cannot APPROVE (code gate) |
| 2 | Currency copied off the vendor's paper | fixed — carried from the original, plus a currency-equals-PO gate covering upload too |
| 3 | Unreachable SMTP stops startup | fixed — `send` returns a bool, registry written only after a successful send, boot dispatch off the critical path |
| 4 | `_revise_from` raises and takes the timers down | fixed — `on_reply` wrapped in `tick`; read failures and decide failures handled differently |
| 5 | Queries only dispatched at boot | fixed — `run_case` dispatches its own, so upload / re-run / chat acceptance all ask |
| 6 | Escalation is a boolean nobody reads | fixed — `on_silence` hands the invoice to a reviewer, in the outbox, credited to "system" |
| 7 | Revisions degrade the headline numbers | fixed — one population for every rate; the rollup counts one obligation once |
| 8 | `unexpected` defeated | fixed — `_eval_view` split into `_benchmark_view` (rates, disk) and `_harness_input` (what evaluate sees) |
| 9 | Console renders none of it | fixed — vendor email thread card, per-reply flags, links to each correction, withdrawn banner |
| 10 | Two vacuous tests | fixed — both do what they claim; the missing cases are covered, including the `app.py` lifespan |
| 11 | Smaller items | fixed except the two noted below |
| Docs | Nine overstatements | fixed — five by fixing the code, four by rewriting the claim |

**Two left, both deliberate.**

- **The thin tool trail on `INV-V005-3005`.** Regenerating it means a live
  model run, and the answer is not deterministic: if it comes back anything
  other than EMAIL, touchless 82% moves and `test_eval.py` fails. That is a
  call to make with the numbers in front of you, not a cleanup.
- **Re-`register`ing an invoice leaves the old token indexed.** Left as is on
  purpose: a late reply to the earlier query still correlates to the right
  invoice, which is better than refusing it. Nothing leaks — the tokens map
  to invoice ids, not to authority.

---

## The short version

The parts work. Assembled, they do not. Per-module review passed twice; what
these findings have in common is that they only appear when a real vendor, a
real mailbox, or a real restart is involved.

---

## Findings, worst first

### 1 ✔ A vendor who re-sends the same correction gets paid once per copy
`api/service.py` (`_revise_from`, the `sequence` line), `agent/ap_tools.py` (`_revision_chain`)

`sequence` mints a fresh `-R{n}` for every reply carrying a PDF, and
`make_revision` always sets `replaces = original.doc_id` — never the previous
revision. R1, R2 and R3 therefore all sit in the same chain, and
`hard_duplicates` has been taught to skip everything in it.

Reproduced: three replies with the same corrected PDF produce three APPROVEd
invoices and three scheduled payments — USD 1,489.20 against a USD 496.40
purchase order, no human involved. **No attacker required**: a vendor chasing
their own correction is enough.

`false_approve` still reads 0 throughout, because of finding 8.

**Fix:** `replaces` points at the newest document in the chain, not the
original; and a document that another document replaces is **superseded** and
must not be able to APPROVE. Put that second rule in `pipeline.py` as a code
gate, not in the service — it is an authority question.

### 2 ✔ `currency` is copied off the vendor's paper and never checked
`mail/revise.py`

`make_revision` overrides `doc_id`, `doc_type`, `vendor_id`, `vendor_name`,
`ref_doc_id`, `replaces`, `source`, `source_ref`. Everything else comes from the
extracted document — including `currency`. Nothing anywhere in
`matching/engine.py`, `rules/tolerance.py` or `pipeline.py` compares invoice
currency to PO currency.

Reproduced: a correction at the exact PO unit prices, marked `EUR` against a USD
purchase order, passes all six guardrails and schedules a EUR transfer.
USD→GBP would be a silent ~30% overpayment.

`revise.py`'s docstring says a correction may change "prices, quantities, dates,
the total — and nothing else". Currency is a fourth thing, and it is the one
that multiplies the total. `scripts/demo_email_intake.py` demonstrates the
identity defence over three fields and is silent on this one.

**Fix:** carry `currency` from the original, and add a currency-equality gate
that also covers the upload path.

### 3 An unreachable SMTP server stops the web app from starting
`api/app.py` (lifespan), `mail/dispatch.py`, `mail/adapters.py`

`SmtpSender.send` has no error handling, `send_query` does not guard it, and the
lifespan calls `dispatch_vendor_queries()` inline **before** yielding.
Reproduced with nothing listening on the SMTP port: `ConnectionRefusedError` out
of startup, whole console gone. A host that hangs rather than refuses costs up
to 30 s per queued query first.

This is the exact inverse of `ImapAdapter.poll`'s carefully argued "a poller
must not die". The send path got none of that care and sits on the critical
startup path.

Secondary: `send_query` calls `registry.register()` **before** `sender.send()`,
so a failed send is still recorded as sent, and the chase timer will later
remind a vendor about mail they never received.

### 4 `_revise_from` does raise, and takes the timers down with it
`api/service.py`, `mail/runner.py`

`except (ExtractionError, ValueError)` does not cover what actually happens.
Reproduced: a truncated PDF that still starts with `%PDF` raises
`PdfminerException`; any LLM failure inside `run_case` raises `RuntimeError`
**and leaves a half-built revision** — in the store and in `_revisions`, with no
decision, so the console shows an undecided invoice with no way to decide it,
and the next reply becomes R2 and orphans it.

`on_reply` is the one call in `tick` that is not wrapped, and it runs the most
code. When it raises, the remaining messages in the batch are never processed
and `_run_timers()` is skipped entirely — so under a sustained failure (expired
key, provider outage) silent-vendor escalation simply stops.

`_revise_from`'s docstring says "Never raises". It does.

### 5 ✔ Vendor queries are only ever dispatched at process boot
`api/service.py`, single caller in `api/app.py`

Grep finds exactly one production call site, in the lifespan. Nothing calls it
when a decision changes. So uploading the overcharge PDF, or clicking Run on an
invoice that flips to `EMAIL`, sends **nothing**. The only query that ever goes
out is for `INV-V005-3005`, which was already `EMAIL` in the committed cache at
startup.

The method's own docstring — "Called after decisions change" — describes a
caller that does not exist. This is also the most likely judge request
("upload one and show me") and the most likely thing to visibly not work.

### 6 "Then it escalates to a human" is a boolean nobody reads
`mail/runner.py`, `mail/chase.py`

`escalated` has two writers and two readers, and both readers are inside
`chase.py`'s own filters. It never reaches the service, the decision, `get_case`,
the API or the UI. After eight days of silence a log line is printed, the query
stops being chased, and a reviewer opening that invoice sees exactly what they
saw on day one. The only observable effect of "escalation" is that the system
gives up quietly.

### 7 Every revision quietly degrades the headline numbers
`api/service.py` (`metrics`, `analytics`)

`total` counts held-out documents that `decided` excludes. Reproduced, after one
revision: `22/22/0` becomes `23/22/1`, STP 68→65, touchless 82→78, and a phantom
"Pending 1" appears. The Dashboard's decision mix (from `_eval_view`) and the
Analytics mix (from `_cache`) diverge. The per-vendor rollup adds the revision's
total alongside the original's — one obligation, billed twice on screen.

**This will happen live, on stage, and there is currently no explanation for
it.** (a) and (b) pre-exist for uploads, but an upload needs a human; a revision
arrives on its own.

### 8 The harness's coverage-hole reporter is defeated
`api/service.py` (`_eval_view`), `eval/harness.py`

`harness.py` states the purpose of `unexpected` plainly: "an approval hiding here
would be an unmeasured payment. Reported so the caller can see the coverage
hole." `_eval_view` drops revisions and uploads **before** `evaluate` sees them,
so they can never land there. With three approved revisions in the payment plan,
`unexpected` is `[]` and `false_approve` is 0.

Holding revisions out of the scored *rates* is honest — there is no ground truth.
Dropping them out of `unexpected` too is **convenient**. The Analytics page has a
slot designed to say "three approvals were made that the benchmark cannot check",
and this branch guarantees it stays empty while exactly that happens. Adding them
to the view (dropped from `cases`, present in `unexpected`) costs nothing and
would have surfaced finding 1 during the demo.

Related: `README.md` claims an uploaded invoice is "listed as *unexpected*". It
is filtered out first. Pre-existing, but this branch reuses the reasoning.

### 9 The console renders none of what the service now returns
`api/service.py` (`get_case`), `api/web/app.js`

`get_case` gained `vendor_replies` and `revisions`; `app.js` reads neither. A
reviewer opening an invoice with three vendor replies and three revisions sees
no evidence card, no link, no sign the invoice was superseded. The only UI change
on this branch is two KPI subtitle strings. **The demo of this feature is a
terminal script, not the product.**

### 10 Two tests that guard the honesty claims cannot fail
`tests/test_mail_revision.py`

- `test_the_committed_decision_and_cache_file_are_untouched` is vacuous:
  `_wired` stubs `_save_cache` to a no-op, so the write it claims to verify never
  happens. (The real behaviour **is** correct — verified separately with
  `_save_cache` live and `CACHE` redirected. The test just proves nothing.)
- `test_a_revision_does_not_move_the_headline_metrics` compares the whole
  `analytics()["metrics"]` dict but only `metrics()["false_approve"]` — precisely
  the field that does not move. That is why finding 7 passes CI.

Also missing: no test for `on_reply` raising, none for two revisions of one
invoice, and **none for the `app.py` wiring at all** — deleting
`dispatch_vendor_queries()` or `on_reply=service.on_vendor_reply` from the
lifespan breaks zero tests.

### 11 Smaller, all confirmed unless noted

- `_revise_from` uses `attachments[0]` only, while `attach.py` collects up to 3
  and a test asserts that cap. The `MAX_ATTACHMENTS` rationale ("extracting each
  one costs a model call") describes a loop that does not exist.
- `service.py` fabricates `billing@{vendor_id}.example.com` for the composer
  preview while the dispatcher mails the directory address — invoice page and
  outbox disagree. `app.js` also keys the subject off `startsWith("billing@")`.
- `vendor_chase_after_hours` / `vendor_escalate_after_hours` are absent from
  `config_info()` and the Settings page, though the comment above
  `informal_grn_ceiling_cents` says every such limit belongs there. They also
  ignore `per_vendor_overrides`.
- Idempotency is per-process. Restarting five times during a demo mails the
  vendor five identical queries. (THEORETICAL — not run.)
- Re-`register`ing an invoice orphans the old `_by_message_id`/`_by_token`
  entries. (THEORETICAL.)
- `attach.py` decodes the full payload before the size check, and `ImapAdapter`
  fetches bodies uncapped — a 200 MB attachment is materialised first.
  (THEORETICAL.)
- `runner.py` uses a blocking `time.sleep` on the error path while the success
  path uses `self._stop.wait(...)`, so `stop()` is ignored for up to 30 s.
- `runner.py`'s comment says `adapter.poll()` is left to propagate so an outage
  surfaces — but `ImapAdapter.poll` catches everything and returns `[]`, so the
  only real adapter can never produce that.
- The re-run committed decision for `INV-V005-3005` lost five of its six tool
  calls while `rounds_used` still says 2. That is the invoice a judge will click
  to see this feature, and its glass-box trail is now one entry.

---

## Docstrings and docs that overstate what the code does

Worth fixing with the code, not after: this repo's docstrings are unusually
detailed on purpose, which makes a false one more damaging than none.

1. `_revise_from` — "Never raises". It does (finding 4).
2. `runner.tick` — the whole "a bad message costs exactly that message" argument
   does not hold for `on_reply`, which is where the risk lives.
3. `dispatch_vendor_queries` — "Called after decisions change" (finding 5).
4. Phase 2 plan and `README.md` — "chases once before escalating". Nothing
   escalates (finding 6).
5. `revise.py` — "prices, quantities, dates, the total — and nothing else"
   (finding 2).
6. `README.md` — an upload is "listed as unexpected" (finding 8).
7. `dispatch.py` — "Idempotency is a rail." Per-process only.
8. `harness.py` — "EMAIL joins the numerator now that queries are actually
   sent." One query, at boot, for one pre-baked invoice.
9. "Touchless 82% is unchanged" is true, but only because the single
   `price_variance` case moved HOLD→EMAIL in lockstep with the definition
   widening. No number moved, so the harness change is untested by the
   benchmark. Say it that way.

---

## What is right, and must not be "fixed"

- **`replaces` is genuinely un-forgeable.** Every path was chased:
  `extract_invoice` builds the `Document` field by field and never sets it,
  pydantic drops unknown keys, `make_revision` always overrides it. The only
  writer is code, keyed off a correlation the vendor cannot influence. Finding 1
  is about *linked* copies, not forged links.
- **Identity separation for `doc_id` / `vendor_id` / `ref_doc_id`** is correct; a
  hostile extracted document cannot re-point at another PO or vendor.
- **No revision special-casing anywhere in `pipeline.py`.** A revision runs all
  six gates identically. That was the most important thing to get right.
- **Correlation never touches the subject line**, and the token regex captures
  only the secret. The demo's "names the invoice only in its subject → correlates
  to nothing" is worth showing a judge.
- **`_eval_view` really does keep the committed benchmark clean** (verified with
  `_save_cache` live). The test is vacuous; the code is not.
- **`inbound.py`'s hardening holds** — charset fallback, decoding `From` before
  `parseaddr`, not descending into `message/rfc822` for body text.

---

## What a judge would ask that this branch cannot answer

1. "The vendor emails the correction twice. What happens?" — three payments.
2. "Where do I see the vendor's reply?" — nowhere in the product.
3. "You said it escalates to a human. Show me the human." — there is none.
4. "Your STP dropped from 68% to 65% while I watched. Why?" — because the
   feature fired.
5. "Can I upload the overcharge PDF and watch it email the vendor?" — no.
6. "How does this survive a restart?" — it does not; every query is re-sent and
   every revision is lost.
7. "What stops a vendor billing you in a different currency?" — nothing.

---

## Fix order

Ranked by "can it mispay" and "will it break on stage", not by effort.

1. **Supersede the chain** (finding 1): `replaces` points at the newest document,
   and a superseded document cannot APPROVE — as a code gate in `pipeline.py`.
2. **Currency** (finding 2): carry it from the original, and add an
   invoice-currency-equals-PO-currency gate that covers the upload path too.
3. **Sending must not break startup** (finding 3): catch in `SmtpSender.send`,
   register only after a successful send, move the boot dispatch off the
   critical path.
4. **Dispatch when a decision changes** (finding 5), not only at boot.
5. **Wrap `on_reply`** (finding 4) so a bad reply costs that reply, and the
   timers always run.
6. **Give escalation a visible consequence** (finding 6); render `vendor_replies`
   and `revisions` in the console (finding 9); stop swallowing `unexpected`
   (finding 8); fix the two vacuous tests (finding 10).

1–5 are "can this be shown to anyone". 6 is "is the story complete".

Deadline context: submission 9/7, semifinals 9/9–11, final 9/18 (NUS).
