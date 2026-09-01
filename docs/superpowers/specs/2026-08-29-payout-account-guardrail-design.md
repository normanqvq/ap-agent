# Payout-account change guardrail — vendor bank-account fraud at payment — design

Date: 2026-08-29
Status: designed, not implemented. Target branch `feat/payout-account-guardrail`.

## The problem

The whole pipeline trusts *who* an approved invoice pays. Three-way matching
proves the goods, the price, and the quantity are right; eight guardrails then
re-check an APPROVE against every computed fact. But not one of them looks at
the **payout account** — the bank account the money actually lands in. Nothing
in the system even records it.

That is the single most expensive AP fraud in the real world. A vendor's mailbox
is compromised, the attacker sends an invoice that is correct in every line —
same PO, same price, same quantity, within tolerance, goods genuinely received —
and changes only the remittance account. Every gate goes green. The money is
paid, in full, to the attacker. This is business-email-compromise (BEC), and it
is exactly the failure mode the project's own philosophy names as the one that
matters: *"the risk of automation is wrong payment, not slowness."*

Right now this is not an unlocked door — it is a wall with no door cut in it yet.
There is no account field anywhere, so no one can redirect a payment through this
path because the path does not exist. This design cuts the door **and** fits the
lock in the same change: it adds the account concept and the guardrail that
defends it together.

Out of scope, deliberately:
- Line/price/quantity fraud — three-way matching and the existing gates own it.
- Large-amount review — `manual_review_threshold_cents` already gates it.
- Real bank-details verification (callback to the vendor, penny-test) — requires
  an external process the demo does not have. Dropped (YAGNI).

## Goals

- Record the payout account an invoice is *printed with* (untrusted vendor text)
  and the payout account we have *on file* for the vendor (our authority).
- When an APPROVE is about to move money to an account that differs from the
  vendor master, deterministic code overrides it to ESCALATE — one human touch,
  never a silent payment to a changed account.
- The three headline metrics (STP 68% / touchless 82% / false approvals 0) do
  **not** move: the guardrail never fires on the 22 graded invoices, and the
  fraud demo lives outside the graded set.

## Design decisions (settled in brainstorming)

1. **Baseline source: a vendor master (`vendors.json`).** The trusted account is
   registered master data, not "whatever account we happened to see first". This
   is the authoritative, one-answer approach — an audit asks "what account was on
   file?" and gets a single answer. (Trust-on-first-use was considered and
   dropped: it makes the first invoice's account implicitly trusted, which is a
   softer guarantee than a registered master.)
2. **Hard guardrail, not advisory.** Account mismatch overrides APPROVE →
   ESCALATE, taking its place as gate #9 in `_apply_guardrails`. This matches the
   whole guardrail philosophy — *only APPROVE moves money, and a gate can only
   push toward more scrutiny* — and the currency and duplicate gates set the
   precedent. Advisory-only was rejected: a mismatch that only whispers still
   lets the wrong payment through.
3. **Freeze the headline numbers; the demo stands apart.** The 22 real invoices
   carry accounts that match their master, so the gate passes them and the
   metrics are untouched. A separate demo invoice (not in the graded set, like
   the `PO-DEMO-*` POs) carries a mismatching account to show the gate fire.

## Data model (`schemas.py`)

`schemas.py` is the single source of truth; both additions follow existing
shapes.

- **Invoice-printed account** — add `payout_account: str | None = None` to
  `Document`, alongside the other invoice-only fields (`payment_terms`,
  `due_date`, `tax_cents`, `total_cents`). It is `None` on a PO or GRN, and
  `None` on an invoice that prints no account. This is the vendor's paper — an
  untrusted string, the same standing as `currency` on an invoice.

- **Vendor master** — a new `Vendor` model (`vendor_id`, `vendor_name`,
  `payout_account: str | None`) loaded from a new `data/synthetic/vendors.json`,
  keyed by `vendor_id`. `store` gains `vendor_account(vendor_id) -> str | None`.
  The existing `store.vendors() -> dict[str, str]` (id → name) is unchanged and
  is derived from the same master, so nothing that reads it today breaks.

Provenance note (mirrors the currency gate): the account printed on the invoice
is the vendor's text; the account in the master is ours. They must agree. The
invoice account is never treated as authority — comparing it *against* our
master is the entire point.

## The guardrail (`pipeline.py`, `_apply_guardrails`, new gate #9)

Placed after the existing gates, following the identical pattern
(`if <condition>: return _override(decision, Action.ESCALATE, None, "<reason>")`).

Condition:

```
invoice.payout_account is not None
AND master_account (= store.vendor_account(invoice.vendor_id)) is not None
AND normalize(invoice.payout_account) != normalize(master_account)
    -> override APPROVE to ESCALATE
```

- **Normalization** — strip whitespace, upper-case, before comparison, so
  `"1234 5678"` and `"12345678"` are equal. Pure and deterministic.
- **Three cases pass (the gate stays silent):** the invoice prints no account
  (`None` — nothing to compare); the master has no registered account (`None` —
  a brand-new vendor has no baseline); or the two accounts are equal. This is
  what guarantees the 22 graded invoices are never touched.
- **Independent of the money gate.** A fraud invoice below the manual-review
  threshold would otherwise APPROVE — that is the case this gate exists to
  catch. The demo invoice is deliberately kept **below** the threshold so the
  reason reported is the account mismatch, not the amount.
- **Reason string** names the last four digits of each account
  (`…1234` on file → `…9876` on this invoice) so the ESCALATE is auditable
  without printing full account numbers.

No new config object. Account match is binary — there is no ratio to tune — so a
`SanityConfig`-style settings model would be YAGNI. (A per-vendor enable flag can
be added later if a vendor ever needs the check turned off; not now.)

**Guardrail count references.** The number of gates is stated in prose in a few
places (README, `app.js`, and near `_guardrails`). This change takes it from
eight to nine; every such reference is updated in lockstep, the same chore the
previous 6→8 change did.

## Data & demo (metrics stay frozen)

- **Vendor master** registers a correct account for every vendor.
- **The 22 real invoices** each carry a `payout_account` equal to their vendor's
  master account. Effect: the invoice detail can show "✓ payout account matches
  master" — proving the check is real, not staged — while every decision is
  unchanged, so STP 68% / touchless 82% / false approvals 0 do not move.
- **One demo invoice `INV-DEMO-BANKSWAP`** with its own matching PO + GRN
  (three-way all green), total kept below the review threshold, and a
  `payout_account` that differs from the vendor master → ESCALATE with the
  account reason. It is **not** part of the graded set.

**Must verify during planning:** how `run_eval` / the metrics iterate — whether
they walk all of `invoices.json` or only the manifest. The demo invoice must not
enter the graded denominator. If the harness walks `invoices.json` wholesale,
the demo lives in a separate file or is manifest-excluded. This is the one thing
to confirm before writing data.

## Frontend (minimal)

No new page. The ESCALATE reason already surfaces in the invoice detail's
decision / guardrail area. The only addition: a "Payout account" row in the
invoice reconciliation table, showing the printed account, with a ⚠ last-four
comparison against the master when they differ — reusing the sanity screen's
flag-row rendering. No business logic in the frontend (per CLAUDE.md): the match
verdict and the reason string come from the server.

## Testing

- **Unit (new `tests/test_payout_account.py` or into `test_pipeline`):** mismatch
  below threshold → ESCALATE with the account reason; match → gate passes;
  invoice account `None` → passes; master account `None` → passes; normalization
  (whitespace / case) treated as equal.
- **Headline pins hold:** the existing false-approvals-0, STP, and touchless
  tests continue to pass unchanged, because the gate never fires on the graded 22.
- **Demo assertion:** `INV-DEMO-BANKSWAP` ESCALATEs for the account reason, and
  false approvals is still 0 with it included.

## Signals / options considered and dropped

- **Trust-on-first-use baseline** — first account seen becomes the norm. Dropped
  for a registered master: TOFU gives a softer, "implicit" trust and two possible
  audit answers.
- **Advisory-only flag** (like the PO sanity screen) — dropped: this is a
  wrong-payment risk, and a gate that only advises still pays the attacker.
- **Adding the fraud case as a 23rd graded invoice** — dropped: it would move the
  nailed-down STP / touchless numbers and force README + CLAUDE.md Metrics edits.
  Freezing the graded set and standing the demo apart keeps the story clean.
- **Real bank-detail verification** (vendor callback, penny-test) — out of scope,
  no external process in the demo (YAGNI).
