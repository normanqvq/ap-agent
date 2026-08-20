# AP Agent

Agentic AP (accounts payable) invoice matching and payment scheduling, built
for the SimplifyNext Agentic AI Hackathon 2026 (Digital track). Target user:
the finance team of a Singapore SME.

**The pitch in one line:** upload a supplier invoice → the agent three-way
matches it against the purchase order and goods receipt → checks tolerances
and the supplier's contract → decides APPROVE / HOLD / ESCALATE with a full,
auditable tool-call trail.

## Where things stand

| Module | Status | What it is |
|---|---|---|
| `schemas.py` | ✅ done | All data models — the single source of truth. Read its docstrings before writing any module. |
| `agent/` | ✅ done | Hand-written agent loop (no framework, on purpose — see `agent/README.md`) + tool registry. Only self-test tools registered so far. |
| `llm/` | ✅ done | Provider layer: `LLM_PROVIDER` switches DeepSeek / Groq / OpenAI. Bedrock branch is a documented placeholder until hackathon AWS credits arrive. |
| `retrieval/` | ✅ done | BM25 search over the vendor contract PDFs + the `search_vendor_contract` agent tool. |
| `scripts/` + `data/synthetic/` | ✅ done | Deterministic dataset generator: 19 invoice PDFs, POs, GRNs, 6 vendor contracts, ground-truth manifests. |
| `extraction/` | ⬜ empty | Invoice PDF → `Document`. |
| `matching/` | ⬜ empty | Three-way match engine (invoice ↔ PO ↔ GRN), Hungarian algorithm for SKU-less line pairing. |
| `rules/` | ⬜ empty | Tolerance checks against `ToleranceConfig`, incl. `per_vendor_overrides` fed by contract retrieval. |
| `scheduling/` | ⬜ empty | Payment scheduling for approved invoices. |
| `api/` | ⬜ empty | FastAPI endpoints (upload / run / results). |
| `eval/` | ⬜ empty | STP rate / touchless rate / false-approve rate over the synthetic set. |

Task breakdown, priorities and owners: `docs/hackathon_gap_analysis.md`.
Project conventions (glossary, money-as-cents, metric definitions): `CLAUDE.md`.

## The demo storyline

The synthetic dataset plants four defects the agent must handle live
(ground truth in `data/synthetic/manifest.json`):

- `INV-V005-3005` — unit price 8% above PO → must not be approved
- `INV-V003-3901` — exact duplicate under a new invoice number → catch it
- `INV-V004-3010` — no PO reference → fall back to vendor + amount search
- `INV-V005-3018` — price 4% above PO: default 2% tolerance says HOLD, but
  V005's supply agreement allows 5% → the agent searches the contract,
  cites the clause, and APPROVEs. This is the headline case.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install
```

Then copy `.env.example` to `.env` and set `LLM_PROVIDER` plus the matching
API key (each provider reads its own key — see the comments in the file).
Tests never need a key; they all run offline.

> Moved or re-cloned the repo? Recreate `.venv` — the scripts inside it pin
> absolute paths and break silently after a move.

## Run the web app

```bash
python scripts/precompute_decisions.py     # run the agent on all invoices, cache to data/synthetic/decisions.json
uvicorn apagent.api.app:app --reload       # then open http://127.0.0.1:8000
```

Two views: a dashboard (KPIs, invoice queue, decision distribution) and an
invoice-detail page (the decision, the code-guardrail chips, the glass-box
tool trail, three-way reconciliation, reasoning). The dashboard reads the
cached decisions (instant, offline); "重新运行" on a detail page re-runs the
agent live. Regenerate the cache after any pipeline or dataset change.

## Everyday commands

```bash
pytest                              # full suite, offline, no API key
ruff check . && ruff format .       # lint + format (pre-commit runs these too)
python scripts/generate_dataset.py  # regenerate data/synthetic/ (deterministic)
```

## Repo map

```
src/apagent/
├── schemas.py        # data contract — change here changes every module
├── agent/            # loop + tool registry (see agent/README.md)
├── llm/              # provider abstraction (client.py)
├── retrieval/        # contract search (BM25) + agent tool
├── extraction/       # invoice PDF -> Document (LLM + code)
├── matching/         # three-way match engine
├── rules/            # tolerance checks
├── pipeline.py       # match -> rules -> agent -> code guardrails
├── scheduling/       # ⬜ payment scheduling
└── api/              # FastAPI + single-page web UI (web/)
scripts/              # dataset generator, demo runner, decision precompute
data/synthetic/       # committed test data: PDFs, JSON docs, contracts, manifests
docs/                 # gap analysis / task list
eval/                 # ⬜ metrics harness
tests/                # offline test suite
```

## Key dates

Solution submission **Mon 7 Sep, 12:00**. Semi-finals 9–11 Sep. Finals 18 Sep
(NUS, presenting to real industry clients).
