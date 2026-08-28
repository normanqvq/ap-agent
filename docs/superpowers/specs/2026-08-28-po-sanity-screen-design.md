# PO sanity screen — fat-finger detection at intake — design

Date: 2026-08-28
Status: designed, not yet implemented, on `feat/mail-intake-impl`

## The problem

Everything downstream trusts the purchase order. Three-way matching checks the
invoice against the PO and the GRN, so if the **PO itself** carries a typo —
someone ordered 1000 reams of A4 paper meaning 100 — the invoice aligns to the
wrong number, matching goes green, and the mistake gets rubber-stamped all the
way to payment. The place with the most leverage to catch a fat-finger error is
the moment the PO enters the system, before any invoice exists.

The errors already covered elsewhere are deliberately out of scope:
- Invoice-vs-PO quantity/price gaps — three-way matching already finds these.
- Large-amount review — `manual_review_threshold_cents` already gates this.
- "Does the business even need this much?" — requires modelling the whole
  company; too hard and not safe. Dropped (YAGNI).

## Goals

- When a PO enters the system, deterministic code screens each line for the
  shape of a fat-finger error and stamps an **advisory flag** on suspicious
  lines.
- The flag is visible: a read-only PO screening view, and the same flags ride
  down into the invoice case bundle when an invoice matches that PO.
- The reminder text is rendered by code from the computed numbers — no LLM
  touches the numbers — so it is deterministic and unit-testable.
- Zero false alarms on the 21 real historical POs. This extends the project's
  headline discipline (`false approvals = 0`) into `false alarms ≈ 0`.

## Non-goals

- **No PO entry UI.** We do not build a procurement front door. Screening runs
  on POs as they load; we surface flags, we do not accept typed POs.
  (A live `POST /api/pos/intake` demo endpoint is an optional add-on, deferred —
  see "Deferred".)
- **No authority.** Screening only attaches an advisory flag / renders a
  reminder. It never blocks payment, never edits a number, never changes an
  agent decision. Decision power stays in the code rules plus the human.
- **No LLM in the number path.** The model may quote the code-rendered hint in
  its reasoning; it never computes or alters the values.
- No new tolerance semantics. This is orthogonal to `tolerance.py`, which
  judges invoice-vs-PO gaps. Sanity screening judges a PO against itself and
  its own history.

## The three signals

All three are deterministic (code computes fact), matching the project's
"code computes fact, model interprets meaning, code holds authority" principle.
Ratios, not the reserved word `threshold` (which stays the manual-review money
cutoff per CLAUDE.md).

| Signal | Catches | Rule |
|---|---|---|
| **① ARITHMETIC** | qty typed with an extra digit, but `line_total` is right | `qty × unit_price_cents` differs from the stored `line_total_cents` by **≥ 5×** (a full order of magnitude, not a discount/rounding tail) |
| **② HISTORY** | qty (and total) both wrong, but the SKU has a track record | Same SKU (fall back to `description`) across historical POs: observed qty **≥ 10×** that SKU's historical max. No history for the SKU → skip, never hard-flag |
| **③ INTRA_PO** | a brand-new SKU with no history | One line's `line_total_cents` **≥ 10×** the median of the other lines, and the PO has **≥ 3 lines** (need sibling lines to compare against) |

Why these three together: ① is the classic "extra digit, total still correct"
and is near-exact because `line_total_cents` is stored independently of
`qty × unit_price` (see `LineItem` docstring). ② catches the case where the
total was typed to match the wrong qty, using history as the yardstick. ③ is
the fallback for SKUs with no history. The old ④ "total amount over a cap" was
dropped: that is large-amount review, already owned by
`manual_review_threshold_cents`.

## Architecture

### Component 1 — the screening module (pure, deterministic)

New file `src/apagent/rules/sanity.py`, sitting beside `tolerance.py` in the
"code computes fact" layer. One pure function, no I/O, no LLM:

```
screen_po(po: Document, history: list[Document], config: SanityConfig) -> list[SanityFlag]
```

`history` is the other POs (used by signal ②); `screen_po` never reads the
store itself — the caller passes history in, keeping the function pure and
trivially testable. Per-vendor overrides resolve the same way `tolerance.py`
does, via a small `resolve_config(vendor_id, base)` helper (whole-object
override, not field merge — one answer to "which config applied?").

### Component 2 — schema additions (single source of truth)

Added to `schemas.py` only (never redefined elsewhere), mirroring
`ToleranceConfig`'s shape and money-in-cents rule:

- `SanityCheck(StrEnum)`: `ARITHMETIC | HISTORY | INTRA_PO`.
- `SanityFlag(BaseModel)`: `line_no: int`, `signal: SanityCheck`,
  `observed`, `baseline`, `ratio` (the numbers behind the call), and
  `hint: str` (the code-rendered one-line reminder).
- `SanityConfig(BaseModel)`: `enabled: bool = True`, the three ratios
  (`arithmetic_ratio = 5.0`, `history_ratio = 10.0`, `intra_po_ratio = 10.0`),
  `intra_po_min_lines: int = 3`, and
  `per_vendor_overrides: dict[str, "SanityConfig"] | None = None`.

### Component 3 — wiring into the system (load-time, zero LLM)

`Service.__init__` already does `self.store = DocumentStore.from_dir(DATA)` and
`self.config = ToleranceConfig()`. We add:

- `self.sanity_config = SanityConfig()`.
- After the store loads, screen every PO once and cache it:
  `self._po_flags: dict[str, list[SanityFlag]] = {po.doc_id: screen_po(po, others, cfg) ...}`
  where `others` is `all_pos()` minus that PO. Startup-time, pure code.
- **Surface it** — two read-only routes, thin wrappers over the service, same
  as the existing `GET /api/invoices`:
  - `GET /api/pos` → list of POs with their flag count.
  - `GET /api/pos/{po_id}` → PO detail including its `SanityFlag` list.
  - Console gets a "PO screening" (采购单体检) view rendering these. No
    business logic in the frontend — it only displays flags the API computed.
- **Flow downstream** — when an invoice matches a PO, include that PO's
  `SanityFlag`s in the case bundle, so the invoice reviewer sees "the PO this
  invoice aligns to was itself flagged for a possible typo".

### The reminder text

Rendered by code from the computed numbers (like `chat/templates.py`), e.g.:

> ⚠️ PO-3021 line 3 "A4 80g copier paper" qty **1000**: similar historical
> orders are ≤100, and `1000 × ¥0.10 = ¥100` differs from the line total ¥1000
> by **10×** — was a digit added by mistake?

Code template, not LLM, so it is deterministic and lockable by tests, matching
the `false approvals = 0` discipline. The agent may quote this hint in its
reasoning; it never generates or edits the numbers.

## The demo (做法 A — seeded fat-finger PO)

No live entry needed. `data/synthetic/pos.json` gains **one** obviously
over-written PO (e.g. a 5-person office ordering 1000 reams of A4 paper) on top
of the 21 real ones. On load it screens dirty and appears in the PO screening
view with a red advisory flag beside a wall of clean POs — enough to
demonstrate "how the site warns you" with zero extra endpoints.

## Testing

`tests/test_sanity.py`:
- Each signal fires when it should and stays silent on clean data.
- The classic "extra digit" case is covered explicitly.
- Per-vendor override changes behaviour.
- No history for a SKU → signal ② skips gracefully, no hard flag.
- INTRA_PO requires ≥ 3 lines; ARITHMETIC ignores normal discount/rounding
  tails (only ≥ 5× fires).
- **Gold standard (echoes the headline metric):** run all screening over the
  21 real POs and assert **zero flags** (no crying wolf); the one seeded
  fat-finger PO is flagged and **only** it. This is `false alarms ≈ 0`.

## Deferred

- `POST /api/pos/intake`: submit a new PO (JSON), screen it on the spot, return
  flags — a live "type a typo on stage" demo moment. Not built now; if a
  rehearsal shows the live version lands better, it is additive and does not
  change anything above.
