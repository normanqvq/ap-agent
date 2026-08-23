# Algorithms in AP Agent

The load-bearing algorithms behind the invoice-matching agent, in one place —
what each one does, where it lives, why it was chosen over the obvious
alternative, and how it is tested. Every reference is `file:line` against the
committed code.

There are **seven**. They split cleanly into the project's core principle —
*code computes facts, the model judges meaning, code owns authority* — so all
six are deterministic code. The model never runs any of them; it reads their
output.

| # | Algorithm | Where | Job |
|---|-----------|-------|-----|
| 1 | Hungarian assignment | `matching/engine.py` | pair invoice lines to PO lines without SKUs |
| 2 | BM25 ranking | `retrieval/search.py` | retrieve the relevant contract clause (the RAG core) |
| 3 | Regex clause parsing | `retrieval/search.py` | pull the price allowance % out of the retrieved clause, in code |
| 4 | Fuzzy name matching | `extraction/invoice.py` | map a printed supplier name to an internal vendor id |
| 5 | Duplicate-key detection | `agent/ap_tools.py` | catch a re-billed invoice under a new number |
| 6 | Modular weekday arithmetic | `scheduling/scheduler.py` | place each approved invoice in the right weekly pay run |
| 7 | Containment-or-ratio matching | `chat/resolve.py` | tie a phrase typed in a chat group to a purchase-order line |

---

## 1. Hungarian assignment — line-item pairing

**File:** `src/apagent/matching/engine.py:88` (`pair_lines`), the assignment at `:124`.

**Problem.** To three-way match an invoice against its PO we must first know
which invoice line corresponds to which PO line. When both sides print a SKU
that is a lookup. Small suppliers often print no SKU — just free-text
descriptions ("copy paper A4 ream") that drift between documents. We have to
pair `n` PO lines to `m` invoice lines by description similarity.

**Why not greedy.** The obvious approach — for each line take its most similar
partner — *chain-steals*: line A grabs line B's best match, forcing B into a
worse pair, and the total is not optimal. In reconciliation a mis-pairing is
not cosmetic: a price delta then lands on the **wrong line**, and every
downstream check is wrong.

**The algorithm.** Build a cost matrix `cost[p][i] = 1 - similarity(po_p, inv_i)`
and solve the assignment problem for the globally minimum total cost. This is
the **Hungarian algorithm** (Kuhn–Munkres), O(n³), via
`scipy.optimize.linear_sum_assignment`. It returns the one-to-one pairing that
minimises total cost across the whole matrix — no chain-stealing.

```python
cost = [[1.0 - _similarity(po_left[p], inv_left[i]) for i in inv_nos] for p in po_nos]
row_idx, col_idx = linear_sum_assignment(cost)
for r, c in zip(row_idx, col_idx, strict=True):
    if 1.0 - cost[r][c] >= PAIR_SIMILARITY_FLOOR:   # keep only real pairs
        pairs.append((po_nos[r], inv_nos[c]))
```

**Two design details.**
- *Cost = 1 − similarity* because the solver minimises; we want to maximise
  similarity.
- *The floor is applied after assignment* (`engine.py:126`, `PAIR_SIMILARITY_FLOOR = 0.4`).
  A pair the optimiser picks but that is still too dissimilar is dropped to
  "unmatched" rather than forced. That errs toward *unmatched*, which is the
  safe direction — an unmatched invoice line trips the no-unordered-lines
  guardrail instead of being waved through as a confident pairing.

**Tested:** `tests/test_matching.py` — `test_pair_lines_finds_the_optimal_not_greedy_assignment`
pins a case where greedy and optimal differ; `test_pair_lines_drops_below_floor_pair_to_unmatched`
pins the floor. Conservation (no line lost) was checked over 300 randomised cases.

---

## 2. BM25 — contract retrieval (the RAG core)

**File:** `src/apagent/retrieval/search.py` — `_score` at `:112`, index built in the class above it.

**Problem.** The default price tolerance is 2%, but a supplier's contract may
negotiate more ("invoiced prices may vary by up to 5%"). To honour that, the
agent must *retrieve* the right clause from the right contract PDF and cite it.
This is retrieval-augmented generation (RAG): search a corpus, feed the hit to
the reasoning step.

**Why BM25, not embeddings.** BM25 is a deterministic, explainable bag-of-words
ranking function — no model, no vector store, reproducible from the text alone.
For six short contracts it is more than enough, and it fits the glass-box
story: you can show exactly which clause was retrieved and why. A vector DB
would add a dependency and a black box to a system whose whole pitch is
auditability.

**The formula.** BM25 scores a document (here, a contract section) against the
query by summing, over query terms, an IDF weight times a saturating
term-frequency term with length normalisation:

```python
idf  = math.log(1 + (n_chunks - df + 0.5) / (df + 0.5))                 # +1 keeps IDF ≥ 0
norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * len(tokens) / self._avg_len))
score += idf * norm
```

- **IDF** — rare terms weigh more. The `1 +` inside the log is the smoothing
  that stops IDF going negative when a term is in every document.
- **`K1`** (term-frequency saturation) — the 5th occurrence of a word adds less
  than the 2nd; without it, score would grow linearly with repetition.
- **`B`** (length normalisation) — a long section isn't relevant just because
  it has more words; `len(tokens)/avg_len` discounts for length.

`K1`/`B` are the textbook defaults (`search.py:33`). Query and documents go
through the **same** `_tokenize` (`:55`) so the term counts line up.

**Tested:** matched to an independent Lucene-variant reference to 1e-12;
empty-corpus / empty-chunk / empty-query all return `[]` with no divide-by-zero.

---

## 3. Regex clause parsing — the allowance never passes through the model

**File:** `src/apagent/retrieval/search.py:153` (`_ALLOWANCE_RE`), `:156` (`price_variance_allowance`).

**Problem.** BM25 finds the *clause*; we still need the *number* ("5%") to
enforce it. Letting the model report the number would put an
attacker-influenceable value on the money path.

**The algorithm.** After retrieval, **code** re-parses the percentage straight
from the contract text with a regex, scoped to a pricing-headed section:

```python
_ALLOWANCE_RE = re.compile(r"up to\s+(\d+(?:\.\d+)?)\s*(?:percent|%)", re.IGNORECASE)

for chunk in chunks:
    if chunk.vendor_id != vendor_id:            continue
    if not re.search(r"\bpric", chunk.heading):  continue   # not the payment section
    match = _ALLOWANCE_RE.search(chunk.text)
    if match:
        return float(match.group(1)), chunk     # (percentage points, source)
return None                                      # silent contract → default stands
```

The `\bpric` heading filter matters: the payment section also carries a
percentage (late-payment interest, "1% per month") that must **not** be
mistaken for a price tolerance. Returning `None` (not `0.0`) when the contract
is silent keeps the default 2% in force.

**This is the headline security property:** the percentage that decides whether
money moves is derived by code, never by the LLM.

**Tested:** V004 → 3.0, V005 → 5.0 parsed from the real PDFs; the four silent
vendors → `None`; the late-interest clause is correctly excluded.

---

## 4. Fuzzy name matching — printed name → vendor id

**File:** `src/apagent/extraction/invoice.py:117` (`match_vendor_id`), `:130` (`normalize`).

**Problem.** The same vendor prints its name three ways ("ABC Pte Ltd" /
"ABC Pte. Ltd." / "ABC"). We must resolve it to one internal id, because a
wrong id silently pulls the wrong PO and the wrong contract.

**The algorithm.** Normalise both sides (lowercase, strip punctuation and legal
suffixes on word boundaries), then take the closest match by **Ratcliff–Obershelp
similarity** (`difflib.SequenceMatcher.ratio`) above a floor:

```python
name = re.sub(r"\b(pte\.?\s*ltd\.?|sdn\.?\s*bhd\.?|inc\.?)\b", "", name)   # word-boundary strip
...
score = difflib.SequenceMatcher(None, target, normalize(canonical)).ratio()
...
if best_score < 0.75 or best_score - runner_up < 0.05:
    return "UNKNOWN"
```

**Two guards, both from review findings.**
- *Word-boundary suffix strip.* A plain substring replace would delete "inc"
  out of "Pr**inc**e" and collapse distinct vendors onto one string — the `\b`
  regex fixes that.
- *Ambiguity margin.* Clearing the 0.75 floor isn't enough; the best must also
  lead the runner-up by ≥ 0.05, or a one-typo name sitting between two close
  canonicals ("Estern" between "Eastern"/"Western") would resolve to a
  coin-flip. Below the margin we return `UNKNOWN` and let the agent escalate —
  a known-unknown beats a confident wrong id.

**Tested:** `tests/test_extraction.py` — all six real vendors + drifted variants
resolve; the "Prince/Pre" substring collision and the "Estern" tie are pinned.

---

## 5. Duplicate-key detection — catch a re-billed invoice

**File:** `src/apagent/agent/ap_tools.py:40` (`hard_duplicates`), `:29` (`_totals_duplicate`).

**Problem.** A supplier re-submits the same invoice under a new number to get
paid twice. The naive key — printed reference + exact total — is defeated four
ways, all of which a security review demonstrated: drop the ref, alter the ref,
resubmit a natively ref-less invoice, or nudge the total by one cent.

**The algorithm.** Key on the **resolved purchase order** (via `find_po`, the
same vendor+amount fallback the matcher uses) plus a **near-equal total**,
scoped to the same vendor:

```python
inv_po, _ = find_po(invoice, store.all_pos())          # resolve, don't trust the printed ref
for other in store.invoices_for_vendor(invoice.vendor_id):
    if other.doc_id == invoice.doc_id:        continue  # never match itself
    other_po, _ = find_po(other, store.all_pos())
    if other_po is None or other_po.doc_id != inv_po.doc_id:  continue
    if _totals_duplicate(invoice.total_cents, other.total_cents, base):   # abs diff ≤ tol
        out.append(other)
```

Because the PO is *resolved by us*, stripping or forging the printed ref lands
on the same order anyway — all four evasions close. The amount comparison is
`abs(a - b) <= total_abs_cents` (`:37`), symmetric and inclusive, so the
one-cent nudge is caught too. It lives at module level (not inside a tool) so
the code guardrail gets the same verdict whether or not the model called the
tool — a fact must not depend on the model remembering to check.

**Honest residual (documented in the code):** a resubmission with a
*within-tolerance* price bump, kept internally consistent, is indistinguishable
from a genuinely different invoice without a paid-status ledger. That ledger is
the real fix; until then this catches the cheap evasions and the money/facts
gates catch the rest.

**Tested:** the planted pair `INV-V003-3003 ↔ INV-V003-3901` is caught;
ref-stripped and cent-nudged resubmissions are caught; cross-vendor same-amount
is correctly **not** flagged.

---

## 6. Modular weekday arithmetic — payment scheduling

**File:** `src/apagent/scheduling/scheduler.py:29` (`next_run_date`), `:34` (`last_run_on_or_before`).

**Problem.** Real AP pays in weekly batches (here: every Friday), not invoice by
invoice. Each approved invoice must land in a run, following *pay late but never
late* — hold cash as long as possible, but never miss a due date.

**The algorithm.** Two modulo-7 helpers place any date on the nearest run day:

```python
FRIDAY = 4   # date.weekday(): Monday = 0

def next_run_date(d, run_weekday=FRIDAY):        # first run on/after d
    return d + timedelta(days=(run_weekday - d.weekday()) % 7)

def last_run_on_or_before(d, run_weekday=FRIDAY):  # last run on/before d
    return d - timedelta(days=(d.weekday() - run_weekday) % 7)
```

The scheduling rule then reads: each invoice goes into
`last_run_on_or_before(due_date)`, clamped forward to the first run we can still
make. Past-due invoices go into the next run and are flagged late.

The `% 7` is the whole trick — weekday arithmetic wraps, and `(target - current) % 7`
is the forward distance to the target weekday; the mirrored form gives the
backward distance. Works for any `run_weekday`, not just Friday.

**Tested:** exhaustively — all 7 possible run weekdays × 14 consecutive dates
for both helpers (always lands on the right weekday, on the correct side, within
7 days); the late-flag boundaries (due before first run, due exactly on a run
day, missing/garbage due date) are pinned in `tests/test_scheduling.py`.

---

## In one line each (for a CV bullet or a slide)

1. **Hungarian assignment** — optimal bipartite line-item pairing, beats greedy chain-stealing.
2. **BM25** — hand-written explainable retrieval over contracts; the RAG core.
3. **Regex clause parsing** — the enforced allowance % is re-derived in code, never trusted from the model.
4. **Ratcliff–Obershelp fuzzy match** — printed name → vendor id, with a floor and an ambiguity margin.
5. **PO-resolved duplicate key** — keyed on resolved PO + amount tolerance, closing four evasion paths.
6. **Modular weekday scheduling** — pay-late-but-never-late batching by modulo-7 arithmetic.
7. **Containment-or-ratio matching** — ties a chat phrase to an order line; refuses rather than guesses when two lines score alike.

---

## 7. Containment-or-ratio matching — a chat phrase to an order line

**File:** `src/apagent/chat/resolve.py` — `_score` and `_match_line`.

**Problem.** Someone in a delivery group types "gloves came, 100 boxes". The
order line reads "Nitrile gloves size L, box of 100". To record a goods
receipt we must know *which line* they meant, and getting it wrong records a
delivery that did not happen.

**Why not the fuzzy matcher we already had.** `difflib.SequenceMatcher.ratio()`
compares two strings end to end, so it is penalised for every word the speaker
did not bother to repeat. Measured against the real dataset:

```
"nitrile gloves" vs "Nitrile gloves size L, box of 100"   ratio 0.60
"trash bag"      vs "Trash bag 120L, roll of 20"          ratio 0.51
```

With the 0.6 floor both were refused as unrecognisable — the matcher rejecting
plain, correct English. The mismatch is structural: people type a **fragment**
of what the order calls something, and ratio is the wrong shape for fragments.

**The algorithm.** Score each PO line by the better of two readings, then apply
the same floor-and-margin rule the vendor matcher uses:

```python
containment = len(probe_words & line_words) / len(probe_words)
return max(containment, difflib.SequenceMatcher(None, target, candidate).ratio())
```

- **Containment** answers "are the speaker's words in this line?", which is what
  a human reading the message does. "gloves" inside "nitrile gloves size l box
  of 100" is a complete hit.
- **Ratio** is kept for typos and for phrases that are not a clean subset.

**Why containment alone would be worse.** A bare "box" is contained in every
line that mentions a box. That is handled by the ambiguity margin rather than
by the score: a word common to two lines scores identically for both, the
margin collapses, and the item is **refused** instead of guessed at. Floor
`0.6`, margin `0.1` — stricter than the matcher's `PAIR_SIMILARITY_FLOOR = 0.4`,
because that one pairs two structured documents describing the same order,
while this one reads a sentence typed on a phone.

**Tested:** the fragment cases above resolve; an item matching nothing on the
order is refused; a word common to two lines is refused rather than coin-flipped
(`tests/test_chat.py`).
