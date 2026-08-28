# PO sanity screen — fat-finger detection at intake — design

Date: 2026-08-28
Status: implemented, on `feat/mail-intake-impl`. Narrowed from three signals to
one (ARITHMETIC) during implementation after measuring the other two against the
real data — see "Signals we tried and dropped".

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
  judges invoice-vs-PO gaps. Sanity screening judges a PO against itself.

## The signal

Deterministic (code computes fact), matching the project's "code computes fact,
model interprets meaning, code holds authority" principle. A ratio, not the
reserved word `threshold` (which stays the manual-review money cutoff per
CLAUDE.md).

| Signal | Catches | Rule |
|---|---|---|
| **ARITHMETIC** | qty typed with an extra digit, but the printed `line_total` is still the intended one | `qty × unit_price_cents` differs from the stored `line_total_cents` by **≥ 5×** (a full order of magnitude, not a discount/rounding tail) |

Why this one: `line_total_cents` is stored independently of `qty × unit_price`
(see `LineItem` docstring), so a line that does not multiply out is internally
inconsistent — and the cutoff of 5× sits above any real discount (a few percent)
and below a single mistyped digit (10×). Crucially it keys on *inconsistency*,
not on *magnitude*, so an expensive item or a big honest order never trips it.
Small consistent gaps (a genuine discount or fee) stay `tolerance.py`'s job.

### Signals we tried and dropped

Two more signals were designed (this is what the original spec approved) and
then measured against the 21 real POs. Both were cut because on this data
legitimate business variation overlaps a genuine one-digit typo (10×), so no
cutoff separates signal from noise without either crying wolf on real POs or
missing the very error the check exists for — and a false alarm is the one thing
this project must not produce.

- **HISTORY** (observed qty ≥ 10× the SKU's historical max): the largest
  *legitimate* jump on the real set is **20×** (OPP tape ordered 5, later
  restocked at 100) and A4 paper legitimately jumps exactly **10×** (10 → 100).
  A single small prior order makes any normal restock look like an explosion.
- **INTRA_PO** (a line total ≥ 10× the median of its sibling lines): the largest
  *legitimate* fold on the real set is **55×** (a toner cartridge among cheap
  stationery), because line total = qty × price and expensive-per-unit items
  simply exist. Comparing totals cannot tell "pricey item" from "wrong qty".

ARITHMETIC has no such overlap: **0 of 21** real POs are internally inconsistent,
so it is a structurally zero-false-alarm signal. The `SanityCheck` enum keeps a
single member and the code is written so a second signal can be added later
without reshaping `SanityFlag`, if a dataset with richer, cleaner history ever
makes one of these viable.

## Architecture

### Component 1 — the screening module (pure, deterministic)

New file `src/apagent/rules/sanity.py`, sitting beside `tolerance.py` in the
"code computes fact" layer. One pure function, no I/O, no LLM:

```
screen_po(po: Document, config: SanityConfig) -> list[SanityFlag]
```

`screen_po` reads only the PO passed in (the dropped HISTORY signal was the only
one that needed other POs), keeping the function pure and trivially testable.
Per-vendor overrides resolve the same way `tolerance.py` does, via a small
`resolve_config(vendor_id, base)` helper (whole-object override, not field merge
— one answer to "which config applied?").

### Component 2 — schema additions (single source of truth)

Added to `schemas.py` only (never redefined elsewhere), mirroring
`ToleranceConfig`'s shape and money-in-cents rule:

- `SanityCheck(StrEnum)`: `ARITHMETIC` (single member; see "Signals we tried and
  dropped").
- `SanityFlag(BaseModel)`: `line_no: int`, `signal: SanityCheck`,
  `observed`, `baseline`, `ratio` (the numbers behind the call), and
  `hint: str` (the code-rendered one-line reminder).
- `SanityConfig(BaseModel)`: `enabled: bool = True`,
  `arithmetic_ratio: float = 5.0`, and
  `per_vendor_overrides: dict[str, "SanityConfig"] | None = None`.

### Component 3 — wiring into the system (load-time, zero LLM)

`Service.__init__` already does `self.store = DocumentStore.from_dir(DATA)` and
`self.config = ToleranceConfig()`. We add:

- `self.sanity_config = SanityConfig()`.
- After the store loads, screen every PO once and cache it (as plain dicts,
  ready for JSON):
  `self._po_flags: dict[str, list[dict]] = {po.doc_id: [f.model_dump() for f in screen_po(po, cfg)] ...}`
  over `all_pos()`. Startup-time, pure code, zero LLM.
- **Surface it** — two read-only routes, thin wrappers over the service, same
  as the existing `GET /api/invoices`:
  - `GET /api/pos` → list of POs with their flag count (via `Service.pos()`).
  - `GET /api/pos/{po_id}` → PO detail including its flags (via
    `Service.po_detail()`; unknown id → 404).
  - Console gets a "Purchase orders" (采购单体检) view rendering these. No
    business logic in the frontend — it only displays flags the API computed.
- **Flow downstream** — the case bundle carries `po_sanity_flags` for the PO an
  invoice matched, so the invoice reviewer sees "the PO this invoice aligns to
  was itself flagged for a possible typo" (empty list when the PO is clean).

### The reminder text

Rendered by code from the computed numbers (like `chat/templates.py`). The
actual template, as shipped:

> line 2 "A4 copy paper 80gsm, ream": qty x unit price = SGD 5,500.00 but the
> line total is SGD 550.00, off by 10x — was a digit mistyped?

Code template, not LLM, so it is deterministic and lockable by tests, matching
the `false approvals = 0` discipline. The agent may quote this hint in its
reasoning; it never generates or edits the numbers.

## The demo (做法 A — seeded fat-finger PO)

No live entry needed. `data/synthetic/pos.json` gains **one** obviously
over-written PO, `PO-DEMO-FATFINGER`: a small office order where the A4-paper
line reads qty **1000** at SGD 5.50 while the line total stays SGD 550.00 (the
intended 100 reams). On load it screens dirty and appears in the Purchase-orders
view with an advisory flag beside a wall of clean POs — enough to demonstrate
"how the site warns you" with zero extra endpoints. Because it reuses the real
`A4-80G` SKU, it is a realistic office order, not an obviously synthetic one.

## Testing

`tests/test_sanity.py` (11 tests, all green):
- ARITHMETIC fires on the classic "extra digit, total unchanged", in both
  directions (total too small and too large), and holds its **exact 5× boundary**
  (5× fires, just under is silent).
- A normal 5% discount stays silent; a missing price/total is skipped.
- Global disable and per-vendor override both suppress screening.
- A flag carries auditable numbers (`observed`, `baseline`, `ratio`) and a
  non-empty hint.
- **Gold standard (echoes the headline metric):** screening the 21 real POs
  asserts **zero flags** (no crying wolf); the one seeded fat-finger PO is
  flagged and **only** it. This is `false alarms ≈ 0`.

`tests/test_api.py` adds: the `/api/pos` list flags only the demo PO,
`/api/pos/{id}` exposes the flag on the mistyped line, an unknown id raises, and
every case bundle carries a `po_sanity_flags` field (empty for a clean PO).

## Deferred

- `POST /api/pos/intake`: submit a new PO (JSON), screen it on the spot, return
  flags — a live "type a typo on stage" demo moment. Not built now; if a
  rehearsal shows the live version lands better, it is additive and does not
  change anything above.
