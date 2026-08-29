# PO sanity screen — fat-finger detection at intake — design

Date: 2026-08-28
Status: implemented. ARITHMETIC on `feat/mail-intake-impl`; a refined HISTORY
signal ("we usually buy 500, this says 5000") added on
`feat/po-sanity-history-spike`. Of the three originally-designed signals, two now
ship (ARITHMETIC and HISTORY) and one (intra-PO outlier) stays dropped — see
"Signals we tried and dropped".

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
- Zero false alarms on the real historical POs. This extends the project's
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

## The signals

Both deterministic (code computes fact), matching the project's "code computes
fact, model interprets meaning, code holds authority" principle. Ratios, not the
reserved word `threshold` (which stays the manual-review money cutoff per
CLAUDE.md).

| Signal | Catches | Rule |
|---|---|---|
| **ARITHMETIC** | qty typed with an extra digit, but the printed `line_total` is still the intended one | `qty × unit_price_cents` differs from the stored `line_total_cents` by **≥ 5×** (a full order of magnitude, not a discount/rounding tail) |
| **HISTORY** | qty dwarfs how much of this item is normally ordered — "we usually buy 500, this says 5000" | qty **≥ 10×** the **median** past qty for the item, and the item has been ordered on **≥ 4** past POs (a settled norm exists) |

Why ARITHMETIC: `line_total_cents` is stored independently of `qty × unit_price`
(see `LineItem` docstring), so a line that does not multiply out is internally
inconsistent — and the cutoff of 5× sits above any real discount (a few percent)
and below a single mistyped digit (10×). It keys on *inconsistency*, not
*magnitude*, so an expensive item or a big honest order never trips it. Small
consistent gaps (a genuine discount or fee) stay `tolerance.py`'s job.

Why HISTORY, and why it took two tries: the first version (qty ≥ 10× the SKU's
historical **max**, no minimum sample) was dropped — on a thin history a single
small past order (OPP tape ordered once at 5, restocked at 100) reads as a 20×
explosion, a false alarm. Two guards fix it: compare against the **median** of
the past quantities, not the max, so one odd order cannot set the bar; and only
judge an item with a **settled norm** (≥ 4 past POs). Measured on the real set,
every item with that much history sits within **~3× of its median** (nut 100 vs
median 62, file 100 vs 50, ESP32 100 vs 35, resistor 50 vs 24), so the 10×
default is clear of legitimate restocking — **0 false alarms** across the real
set. Where an item has too little history there is no norm to compare against and
HISTORY stays silent; a brand-new item is ARITHMETIC's job, or nobody's.

### Signal we tried and dropped

One more signal was designed and cut for good:

- **INTRA_PO** (a line total ≥ 10× the median of its sibling lines on the same
  PO): the largest *legitimate* fold on the real set is **55×** (a toner
  cartridge among cheap stationery), because line total = qty × price and
  expensive-per-unit items simply exist. Comparing totals cannot tell "pricey
  item" from "wrong quantity", so no cutoff separates signal from noise. Unlike
  HISTORY it has no median/min-sample rescue — the confound is the price itself,
  not the sample size.

Both shipped signals are structurally clean on the real data: **0 of the real
POs** raise a flag.

## Architecture

### Component 1 — the screening module (pure, deterministic)

New file `src/apagent/rules/sanity.py`, sitting beside `tolerance.py` in the
"code computes fact" layer. One pure function, no I/O, no LLM:

```
screen_po(po: Document, history: list[Document], config: SanityConfig) -> list[SanityFlag]
```

`history` is the other POs, used only for the HISTORY baseline; `screen_po` never
reads the store itself — the caller passes history in, keeping the function pure
and trivially testable. Per-vendor overrides resolve the same way `tolerance.py`
does, via a small `resolve_config(vendor_id, base)` helper (whole-object
override, not field merge — one answer to "which config applied?").

### Component 2 — schema additions (single source of truth)

Added to `schemas.py` only (never redefined elsewhere), mirroring
`ToleranceConfig`'s shape and money-in-cents rule:

- `SanityCheck(StrEnum)`: `ARITHMETIC | HISTORY`.
- `SanityFlag(BaseModel)`: `line_no: int`, `signal: SanityCheck`,
  `observed`, `baseline`, `ratio` (the numbers behind the call), and
  `hint: str` (the code-rendered one-line reminder).
- `SanityConfig(BaseModel)`: `enabled: bool = True`,
  `arithmetic_ratio: float = 5.0`, `history_ratio: float = 10.0`,
  `history_min_pos: int = 4`, and
  `per_vendor_overrides: dict[str, "SanityConfig"] | None = None`.

### Component 3 — wiring into the system (load-time, zero LLM)

`Service.__init__` already does `self.store = DocumentStore.from_dir(DATA)` and
`self.config = ToleranceConfig()`. We add:

- `self.sanity_config = SanityConfig()`.
- After the store loads, screen every PO once and cache it (as plain dicts,
  ready for JSON): for each PO, `screen_po(po, other_pos, cfg)` where `other_pos`
  is `all_pos()` minus that PO (its history baseline), stored in
  `self._po_flags: dict[str, list[dict]]`. Startup-time, pure code, zero LLM.
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

## The demo (做法 A — seeded fat-finger POs)

No live entry needed. `data/synthetic/pos.json` gains **two** obviously
over-written POs, one per signal, beside a wall of clean POs:

- `PO-DEMO-FATFINGER` (ARITHMETIC): a small office order where the A4-paper line
  reads qty **1000** at SGD 5.50 while the line total stays SGD 550.00 (the
  intended 100 reams). It does not multiply out. Reuses the real `A4-80G` SKU, so
  it looks like a real order.
- `PO-DEMO-OVERORDER` (HISTORY): the "we usually buy 500, this says 5000" case. A
  new toilet-roll SKU (`TP-2PLY`) is given a **settled history** — five CleanPro
  POs (`PO-2026-1030..1034`) each ordering ~500 packs — and then this PO orders
  **5000**. Its arithmetic is consistent (5000 × SGD 1.20 = SGD 6,000), so only
  HISTORY fires: "qty 5,000 is 10x the usual order for this item (normally about
  500) — is that intended?" The five history POs are ordinary clean POs and are
  part of the false-alarm gold standard below.

## Testing

`tests/test_sanity.py`:
- **ARITHMETIC**: fires on "extra digit, total unchanged" in both directions,
  holds its **exact 5× boundary**, ignores a 5% discount, skips missing fields.
- **HISTORY**: fires on a 10× spike over the **median** with a settled history;
  holds its exact 10× boundary; stays silent on a **thin record** (< 4 past POs)
  even at 20×; uses the **median not the max** (a lone tiny past order neither
  fakes a spike nor drags the baseline down); matches by description when the SKU
  is missing.
- Global disable and per-vendor override both suppress screening; each flag
  carries auditable numbers (`observed`, `baseline`, `ratio`) and a hint.
- **Gold standard (echoes the headline metric):** screening every real PO asserts
  **zero flags** (no crying wolf); `PO-DEMO-FATFINGER` is caught by ARITHMETIC and
  `PO-DEMO-OVERORDER` by HISTORY only. This is `false alarms ≈ 0`.

`tests/test_api.py` adds: the `/api/pos` list flags **exactly** the two seeded
POs, `/api/pos/{id}` exposes the arithmetic flag on the mistyped line and the
history flag on the over-ordered line, an unknown id raises, and every case
bundle carries a `po_sanity_flags` field (empty for a clean PO).

## Deferred

- `POST /api/pos/intake`: submit a new PO (JSON), screen it on the spot, return
  flags — a live "type a typo on stage" demo moment. Not built now; if a
  rehearsal shows the live version lands better, it is additive and does not
  change anything above.
