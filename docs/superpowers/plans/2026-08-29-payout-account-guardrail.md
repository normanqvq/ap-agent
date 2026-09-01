# Payout-Account Change Guardrail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic guardrail (gate #9) that overrides APPROVE → ESCALATE when an invoice's printed payout account differs from the account on file for the vendor, defending against vendor bank-account (BEC) fraud — without moving STP 68% / touchless 82% / false approvals 0.

**Architecture:** Two new data facts (an invoice-printed `payout_account`, and a vendor master with the on-file account), one new code guardrail in `_apply_guardrails`, one server-computed view field, one additive frontend row, and a held-out demo invoice that exercises the gate outside the graded set.

**Tech Stack:** Python 3.14, Pydantic v2, FastAPI, pytest. On this machine the interpreter is `.venv/Scripts/python.exe` — every `python`/`pytest` command below runs as `.venv/Scripts/python.exe -m pytest ...`. Run pytest with `APAGENT_MAIL_FROM=` and `TELEGRAM_BOT_TOKEN=` set empty so no live poller starts.

**Spec:** `docs/superpowers/specs/2026-08-29-payout-account-guardrail-design.md`

**Branch:** `feat/payout-account-guardrail` (already created off `origin/main`, holds the spec commit).

---

## File Structure

- `src/apagent/schemas.py` — add `payout_account` to `Document`; add `Vendor` model. (modify)
- `src/apagent/store.py` — load `vendors.json`, expose `vendor_account()`. (modify)
- `src/apagent/pipeline.py` — `_norm_account` helper + gate #9; thread `vendor_account` through `_apply_guardrails` and both callers. (modify)
- `data/synthetic/vendors.json` — vendor master with on-file accounts. (create)
- `data/synthetic/invoices.json` — real invoices gain a matching `payout_account`; plus the demo invoice. (modify, via seed script)
- `data/synthetic/pos.json`, `grns.json` — demo PO + GRN. (modify)
- `data/synthetic/decisions.json` — committed ESCALATE decision for the demo. (modify, via seed script)
- `src/apagent/api/service.py` — held-out wiring for the demo; `_payout_account_view` in the case bundle; `DEMO_ORDER` entry. (modify)
- `src/apagent/api/web/app.js` — payout-account row on the invoice detail. (modify)
- `README.md`, `src/apagent/api/web/app.js` — guardrail count 8 → 9. (modify)
- `tests/test_payout_account.py` — gate unit tests. (create)
- `scripts/seed_payout_accounts.py`, `scripts/seed_demo_bankswap.py` — one-shot data seeders. (create)

---

## Task 1: Data model — `payout_account` + `Vendor`

**Files:**
- Modify: `src/apagent/schemas.py` (Document invoice-only fields block; new model after `ToleranceConfig`)
- Test: `tests/test_payout_account.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_payout_account.py`:

```python
from apagent.schemas import Document, DocType, LineItem, Vendor


def _line():
    return LineItem(
        line_no=1, sku="SKU-1", description="widget",
        qty=10, uom="PCS", unit_price_cents=100, line_total_cents=1000,
    )


def test_document_carries_payout_account_defaulting_none():
    doc = Document(
        doc_id="INV1", doc_type=DocType.INVOICE, vendor_id="V1",
        vendor_name="Acme", issue_date="2026-01-01", ref_doc_id="PO1",
        currency="SGD", lines=[_line()],
    )
    assert doc.payout_account is None
    doc2 = doc.model_copy(update={"payout_account": "SG12 3456"})
    assert doc2.payout_account == "SG12 3456"


def test_vendor_master_model():
    v = Vendor(vendor_id="V1", vendor_name="Acme", payout_account="SG12 3456")
    assert v.vendor_id == "V1"
    assert v.payout_account == "SG12 3456"
    assert Vendor(vendor_id="V2", vendor_name="B").payout_account is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_payout_account.py -q`
Expected: FAIL — `ImportError: cannot import name 'Vendor'` (and/or unexpected keyword `payout_account`).

- [ ] **Step 3: Add the field and model**

In `src/apagent/schemas.py`, in `Document`, add `payout_account` to the invoice-only fields block (next to `total_cents`):

```python
    # These only have a value on an Invoice. On a PO or GRN they stay None.
    payment_terms: str | None = None
    due_date: str | None = None
    tax_cents: int | None = None
    total_cents: int | None = None
    # The remittance account printed on the invoice. Vendor text, i.e.
    # untrusted — the payout-account guardrail compares it against the
    # account we hold on file for the vendor. None on a PO/GRN, and None on
    # an invoice that prints no account.
    payout_account: str | None = None
```

After the `ToleranceConfig` class (end of file), add:

```python
class Vendor(BaseModel):
    """Vendor master record. The `payout_account` here is OUR authority — the
    account on file — against which the guardrail checks each invoice's
    printed account. Kept separate from the id->name map derived from POs
    because an account is registered master data, not something read off a
    document."""

    vendor_id: str
    vendor_name: str
    payout_account: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_payout_account.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/apagent/schemas.py tests/test_payout_account.py
git commit -m "feat: payout_account field and Vendor master model"
```

---

## Task 2: Store — load the vendor master

**Files:**
- Modify: `src/apagent/store.py` (`__init__`, `from_dir`, new `vendor_account`)
- Test: `tests/test_payout_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_payout_account.py`:

```python
from apagent.store import DocumentStore


def _po():
    return Document(
        doc_id="PO1", doc_type=DocType.PO, vendor_id="V1", vendor_name="Acme",
        issue_date="2026-01-01", ref_doc_id=None, currency="SGD", lines=[_line()],
    )


def test_store_returns_vendor_account_and_none_when_absent():
    store = DocumentStore([_po()], [], [], {"V1": "SG12 3456"})
    assert store.vendor_account("V1") == "SG12 3456"
    assert store.vendor_account("V-unknown") is None


def test_store_without_accounts_returns_none():
    store = DocumentStore([_po()], [], [])
    assert store.vendor_account("V1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_payout_account.py -q`
Expected: FAIL — `TypeError` (4th positional arg) or `AttributeError: 'DocumentStore' object has no attribute 'vendor_account'`.

- [ ] **Step 3: Implement**

In `src/apagent/store.py`, extend `__init__` (keep the new arg optional so existing constructors keep working):

```python
    def __init__(
        self,
        pos: list[Document],
        grns: list[Document],
        invoices: list[Document],
        vendor_accounts: dict[str, str | None] | None = None,
    ):
        self._pos = {doc.doc_id: doc for doc in pos}
        self._grns_by_po = {doc.ref_doc_id: doc for doc in grns if doc.ref_doc_id}
        self._invoices = {doc.doc_id: doc for doc in invoices}
        # vendor_id -> on-file payout account (our authority). Empty when no
        # vendors.json is present, e.g. the tiny stores tests build by hand.
        self._vendor_accounts = vendor_accounts or {}
```

Update `from_dir` to load `vendors.json` when present (add import `from apagent.schemas import Vendor` at top of file next to the existing schema import):

```python
    @classmethod
    def from_dir(cls, data_dir: Path) -> "DocumentStore":
        """Load the synthetic dataset directory (pos/grns/invoices/vendors)."""

        def load(name: str) -> list[Document]:
            return [
                Document(**d) for d in json.loads((data_dir / name).read_text(encoding="utf-8"))
            ]

        accounts: dict[str, str | None] = {}
        vpath = data_dir / "vendors.json"
        if vpath.exists():
            for v in json.loads(vpath.read_text(encoding="utf-8")):
                vendor = Vendor(**v)
                accounts[vendor.vendor_id] = vendor.payout_account

        return cls(load("pos.json"), load("grns.json"), load("invoices.json"), accounts)
```

Add the accessor near `vendors()`:

```python
    def vendor_account(self, vendor_id: str) -> str | None:
        """The payout account we hold on file for this vendor, or None if the
        vendor is unknown or has no registered account. This is the trusted
        baseline the payout-account guardrail checks an invoice against."""
        return self._vendor_accounts.get(vendor_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_payout_account.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/apagent/store.py tests/test_payout_account.py
git commit -m "feat: load vendor master and expose vendor_account()"
```

---

## Task 3: The guardrail — gate #9

**Files:**
- Modify: `src/apagent/pipeline.py` (`_norm_account` helper; `_apply_guardrails` signature + gate #9; call sites in `decide_invoice` and `decide_invoice_rules_only`)
- Test: `tests/test_payout_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_payout_account.py`:

```python
from apagent.pipeline import decide_invoice_rules_only
from apagent.schemas import Action


def _triple(invoice_account, master_account):
    """A clean-matching PO/GRN/invoice and a store; only the account varies."""
    line = _line()
    po = Document(
        doc_id="PO1", doc_type=DocType.PO, vendor_id="V1", vendor_name="Acme",
        issue_date="2026-01-01", ref_doc_id=None, currency="SGD", lines=[line],
    )
    grn = Document(
        doc_id="GRN1", doc_type=DocType.GRN, vendor_id="V1", vendor_name="Acme",
        issue_date="2026-01-02", ref_doc_id="PO1", currency="SGD", lines=[line],
    )
    inv = Document(
        doc_id="INV1", doc_type=DocType.INVOICE, vendor_id="V1", vendor_name="Acme",
        issue_date="2026-01-02", ref_doc_id="PO1", currency="SGD", lines=[line],
        total_cents=1000, payout_account=invoice_account,
    )
    accounts = {"V1": master_account} if master_account is not None else {}
    return DocumentStore([po], [grn], [inv], accounts), inv


def test_mismatch_escalates():
    store, inv = _triple("SG99 8888 7777 6666", "SG12 3456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.ESCALATE
    assert "payout account" in dec.reasoning.lower()


def test_match_stays_approve():
    store, inv = _triple("SG12 3456 7890 1234", "SG12 3456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE


def test_spacing_and_case_ignored():
    store, inv = _triple("sg12 34567890 1234", "SG123456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE


def test_invoice_without_account_passes():
    store, inv = _triple(None, "SG12 3456 7890 1234")
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE


def test_no_master_account_passes():
    store, inv = _triple("SG99 8888 7777 6666", None)
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.APPROVE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_payout_account.py -q`
Expected: FAIL — `test_mismatch_escalates` gets APPROVE (gate does not exist yet). The four "pass" tests already pass (baseline APPROVEs a clean match); confirm `test_match_stays_approve` passes now, which also verifies the clean triple really APPROVEs. If a "pass" test unexpectedly does NOT APPROVE, the fixture is not a clean match — fix the fixture before continuing.

- [ ] **Step 3: Implement the helper and the gate**

In `src/apagent/pipeline.py`, add the helper next to `_override`/`supersede`:

```python
def _norm_account(account: str) -> str:
    """Compare accounts ignoring spacing and case, so '1234 5678' and
    '12345678' are the same account. Deterministic, no cleverness."""
    return "".join(account.split()).upper()
```

Change the `_apply_guardrails` signature to accept the on-file account (append it after `superseded`, keyword-friendly, default None so nothing breaks):

```python
def _apply_guardrails(
    decision: AgentDecision,
    invoice: Document,
    checked: MatchResult,
    review_gate: bool,
    duplicates: list[Document],
    config: ToleranceConfig,
    chunks: tuple[Chunk, ...],
    grn: Document | None = None,
    po: Document | None = None,
    superseded: Document | None = None,
    vendor_account: str | None = None,
) -> AgentDecision:
```

Add gate #9 immediately BEFORE the final `return decision` (after the gate-8 grn_gate block):

```python
    # 9. The payout-account gate. Every gate above proves WHAT is paid; none
    # proves WHOM. An invoice correct in every line but printed with a changed
    # remittance account is business-email-compromise — a compromised vendor
    # mailbox redirecting the money. The account on the invoice is the vendor's
    # text; the account on file (store.vendor_account, from the vendor master)
    # is ours, and they must agree before money moves. Silent when either side
    # is unknown (a new vendor has no baseline; an invoice may print no account)
    # or when they match — which is why it never fires on the graded set, whose
    # invoices carry their on-file account. This is the last check before an
    # APPROVE releases payment.
    if (
        invoice.payout_account
        and vendor_account
        and _norm_account(invoice.payout_account) != _norm_account(vendor_account)
    ):
        return _override(
            decision,
            Action.ESCALATE,
            None,
            f"The invoice's payout account (…{invoice.payout_account[-4:]}) "
            f"differs from the account on file for this vendor "
            f"(…{vendor_account[-4:]}), so code overrides APPROVE to ESCALATE.",
        )

    return decision
```

Update BOTH call sites to resolve and pass the account.

In `decide_invoice` (the call that already passes `superseded`):

```python
    decision = _apply_guardrails(
        decision, invoice, checked, review_gate, duplicates, config, chunks,
        grn, po, superseded, vendor_account=store.vendor_account(invoice.vendor_id),
    )
```

In `decide_invoice_rules_only` (the call that ends with `grn, po`):

```python
    return _apply_guardrails(
        baseline, invoice, checked, review_gate, duplicates, config, (), grn, po,
        vendor_account=store.vendor_account(invoice.vendor_id),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_payout_account.py -q`
Expected: PASS (all payout tests pass, `test_mismatch_escalates` now ESCALATEs).

- [ ] **Step 5: Run the full suite to prove nothing else moved**

Run: `APAGENT_MAIL_FROM= TELEGRAM_BOT_TOKEN= .venv/Scripts/python.exe -m pytest -q`
Expected: PASS — same count as before this task plus the new payout tests, 3 skipped. No pre-existing test fails (the gate is silent on every store that has no vendor account).

- [ ] **Step 6: Commit**

```bash
git add src/apagent/pipeline.py tests/test_payout_account.py
git commit -m "feat: payout-account guardrail (gate #9) overrides APPROVE to ESCALATE"
```

---

## Task 4: Vendor master data + accounts on the real invoices

**Files:**
- Create: `data/synthetic/vendors.json`
- Create: `scripts/seed_payout_accounts.py`
- Modify: `data/synthetic/invoices.json` (via the seed script)

- [ ] **Step 1: Create the vendor master**

Create `data/synthetic/vendors.json` (six vendors, each with a distinct on-file account):

```json
[
  {"vendor_id": "V001", "vendor_name": "Tan Hardware Supplies Pte Ltd", "payout_account": "SG21 DBSS 0110 0001 2345"},
  {"vendor_id": "V002", "vendor_name": "Mei Ling Office Solutions", "payout_account": "SG34 OCBC 0220 0002 3456"},
  {"vendor_id": "V003", "vendor_name": "Golden Wok Food Trading", "payout_account": "SG47 UOVB 0330 0003 4567"},
  {"vendor_id": "V004", "vendor_name": "Sinar Jaya Packaging Sdn Bhd", "payout_account": "MY55 MBBE 0440 0004 5678"},
  {"vendor_id": "V005", "vendor_name": "Pacific Circuit Components Inc", "payout_account": "US60 CHAS 0550 0005 6789"},
  {"vendor_id": "V006", "vendor_name": "CleanPro Facilities Services", "payout_account": "SG73 SCBL 0660 0006 7890"}
]
```

- [ ] **Step 2: Write the seed script that stamps each real invoice with its vendor's on-file account**

Create `scripts/seed_payout_accounts.py`:

```python
"""One-shot: give every committed invoice the payout account on file for its
vendor, so the payout-account gate sees a MATCH on the graded set and the
metrics do not move. Run once, then commit invoices.json. Idempotent."""

import json
from pathlib import Path

DATA = Path("data/synthetic")
accounts = {v["vendor_id"]: v["payout_account"] for v in json.loads((DATA / "vendors.json").read_text(encoding="utf-8"))}
invoices = json.loads((DATA / "invoices.json").read_text(encoding="utf-8"))
for inv in invoices:
    if inv["doc_id"] == "INV-DEMO-BANKSWAP":
        continue  # the demo carries a mismatching account, set elsewhere
    inv["payout_account"] = accounts.get(inv["vendor_id"])
(DATA / "invoices.json").write_text(json.dumps(invoices, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"stamped {len(invoices)} invoices")
```

- [ ] **Step 3: Run the seed script**

Run: `.venv/Scripts/python.exe scripts/seed_payout_accounts.py`
Expected: prints `stamped 22 invoices` (or the current invoice count).

- [ ] **Step 4: Prove the metrics did not move**

Run: `APAGENT_MAIL_FROM= TELEGRAM_BOT_TOKEN= .venv/Scripts/python.exe -m pytest -q`
Expected: PASS, unchanged count. In particular the existing metrics/eval tests (STP, touchless, false-approve = 0) still pass, because every real invoice now matches its on-file account so the gate stays silent.

If any fixture test fails on an exact-invoice-dict comparison, update that fixture's expected dict to include the new `payout_account` key — a mechanical addition, not a behavior change.

- [ ] **Step 5: Commit**

```bash
git add data/synthetic/vendors.json data/synthetic/invoices.json scripts/seed_payout_accounts.py
git commit -m "data: vendor master + on-file payout accounts on the graded invoices"
```

---

## Task 5: The bank-swap demo, held out of the graded set

**Files:**
- Modify: `data/synthetic/pos.json` (demo PO), `data/synthetic/grns.json` (demo GRN), `data/synthetic/invoices.json` (demo invoice)
- Modify: `src/apagent/api/service.py` (`_session_documents`, `DEMO_ORDER`)
- Create: `scripts/seed_demo_bankswap.py` (committed ESCALATE decision)
- Test: `tests/test_payout_account.py`

- [ ] **Step 1: Add the demo bundle to the data files**

Append to `data/synthetic/pos.json` (before the closing `]`):

```json
{"doc_id": "PO-DEMO-BANKSWAP", "doc_type": "PO", "vendor_id": "V001", "vendor_name": "Tan Hardware Supplies Pte Ltd", "issue_date": "2026-08-20", "ref_doc_id": null, "currency": "SGD", "lines": [{"line_no": 1, "sku": "SKU-BOLT-M8", "description": "M8 hex bolt, zinc plated", "qty": 200, "uom": "PCS", "unit_price_cents": 12, "line_total_cents": 2400}], "payment_terms": null, "due_date": null, "tax_cents": null, "total_cents": null}
```

Append to `data/synthetic/grns.json`:

```json
{"doc_id": "GRN-DEMO-BANKSWAP", "doc_type": "GRN", "vendor_id": "V001", "vendor_name": "Tan Hardware Supplies Pte Ltd", "issue_date": "2026-08-22", "ref_doc_id": "PO-DEMO-BANKSWAP", "currency": "SGD", "lines": [{"line_no": 1, "sku": "SKU-BOLT-M8", "description": "M8 hex bolt, zinc plated", "qty": 200, "uom": "PCS", "unit_price_cents": 12, "line_total_cents": 2400}], "payment_terms": null, "due_date": null, "tax_cents": null, "total_cents": null}
```

Append to `data/synthetic/invoices.json` — clean match, total SGD 24.00 (well below the SGD 5,000 threshold), account NOT V001's on-file `SG21 DBSS 0110 0001 2345`:

```json
{"doc_id": "INV-DEMO-BANKSWAP", "doc_type": "INVOICE", "vendor_id": "V001", "vendor_name": "Tan Hardware Supplies Pte Ltd", "issue_date": "2026-08-22", "ref_doc_id": "PO-DEMO-BANKSWAP", "currency": "SGD", "lines": [{"line_no": 1, "sku": "SKU-BOLT-M8", "description": "M8 hex bolt, zinc plated", "qty": 200, "uom": "PCS", "unit_price_cents": 12, "line_total_cents": 2400}], "payment_terms": "NET 30", "due_date": "2026-09-21", "tax_cents": 0, "total_cents": 2400, "payout_account": "SG99 HACK 9990 0009 8765"}
```

- [ ] **Step 2: Write the failing test (demo ESCALATEs, held out of metrics)**

Append to `tests/test_payout_account.py`:

```python
from pathlib import Path

from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def test_demo_bankswap_escalates_from_committed_dataset():
    store = DocumentStore.from_dir(DATA)
    inv = store.get_invoice("INV-DEMO-BANKSWAP")
    assert inv is not None
    dec = decide_invoice_rules_only(inv, store)
    assert dec.action == Action.ESCALATE
    assert "payout account" in dec.reasoning.lower()
```

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_payout_account.py::test_demo_bankswap_escalates_from_committed_dataset -q`
Expected: PASS immediately — the data and the gate already exist. (If it FAILs with APPROVE, the demo bundle is not a clean match or the account matches; fix the data.) This test also proves the from_dir wiring loads vendors.json.

- [ ] **Step 4: Hold the demo out of the graded metrics**

In `src/apagent/api/service.py`, add a module constant near `DEMO_ORDER`:

```python
# Held out of every scored rate: it has no manifest ground truth, exactly like
# an upload or a correction. Present so the bank-swap demo can ESCALATE in the
# live console without moving STP / touchless / false-approve.
DEMO_HELD_OUT = {"INV-DEMO-BANKSWAP"}
```

Extend `_session_documents` to include it:

```python
    def _session_documents(self) -> set[str]:
        return (
            self._uploaded
            | {doc_id for ids in self._revisions.values() for doc_id in ids}
            | DEMO_HELD_OUT
        )
```

Add the demo id to `DEMO_ORDER` (append to the existing list literal at the top of the file) so it lands in a sensible queue position — put it right after the other showcase rows:

```python
    "INV-DEMO-BANKSWAP",
```

- [ ] **Step 5: Seed the committed ESCALATE decision for the demo**

Create `scripts/seed_demo_bankswap.py`:

```python
"""One-shot: compute the demo bank-swap invoice's decision deterministically
(rules-only + the real guardrails, no model needed) and write it into the
committed decisions cache so the console shows the ESCALATE on load. Idempotent."""

import json
from pathlib import Path

from apagent.pipeline import decide_invoice_rules_only
from apagent.store import DocumentStore

DATA = Path("data/synthetic")
store = DocumentStore.from_dir(DATA)
inv = store.get_invoice("INV-DEMO-BANKSWAP")
decision = decide_invoice_rules_only(inv, store)
assert decision.action.value == "ESCALATE", decision.action

cache = json.loads((DATA / "decisions.json").read_text(encoding="utf-8"))
cache["INV-DEMO-BANKSWAP"] = decision.model_dump()
(DATA / "decisions.json").write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("seeded INV-DEMO-BANKSWAP:", decision.action.value)
```

Run: `.venv/Scripts/python.exe scripts/seed_demo_bankswap.py`
Expected: prints `seeded INV-DEMO-BANKSWAP: ESCALATE`.

- [ ] **Step 6: Prove metrics still frozen with the demo present**

Run: `APAGENT_MAIL_FROM= TELEGRAM_BOT_TOKEN= .venv/Scripts/python.exe -m pytest -q`
Expected: PASS. The metrics tests still pass: the demo is in `_session_documents`, so it is excluded from `total` and every rate, and false-approve stays 0 (it ESCALATEs and has no manifest entry).

- [ ] **Step 7: Commit**

```bash
git add data/synthetic/pos.json data/synthetic/grns.json data/synthetic/invoices.json data/synthetic/decisions.json src/apagent/api/service.py scripts/seed_demo_bankswap.py tests/test_payout_account.py
git commit -m "demo: held-out bank-swap invoice that ESCALATEs on account mismatch"
```

---

## Task 6: Surface it — case bundle field + invoice detail row

**Files:**
- Modify: `src/apagent/api/service.py` (case bundle dict + `_payout_account_view`)
- Modify: `src/apagent/api/web/app.js` (invoice detail render)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

`tests/test_api.py` builds the service as plain `Service()` and reads a bundle with `Service().get_case(invoice_id)` (see `test_headline_case_all_guardrails_pass` at the top of the file). Append, in that same style:

```python
def test_case_bundle_exposes_payout_account_mismatch():
    c = Service().get_case("INV-DEMO-BANKSWAP")
    pa = c["payout_account"]
    assert pa is not None
    assert pa["matches"] is False
    assert pa["invoice"].endswith("8765")
    assert pa["on_file"].endswith("2345")


def test_case_bundle_payout_account_matches_on_a_real_invoice():
    c = Service().get_case("INV-V001-3001")
    assert c["payout_account"]["matches"] is True
```

(If `Service` is imported at the top of `tests/test_api.py` already — it is, the other tests use it — no new import is needed.)

- [ ] **Step 2: Run test to verify it fails**

Run: `APAGENT_MAIL_FROM= TELEGRAM_BOT_TOKEN= .venv/Scripts/python.exe -m pytest tests/test_api.py -k payout_account -q`
Expected: FAIL — `KeyError: 'payout_account'`.

- [ ] **Step 3: Implement the server view**

In `src/apagent/api/service.py`, add the helper method on `Service`:

```python
    def _payout_account_view(self, invoice: Document) -> dict | None:
        """The payout-account comparison for the invoice detail. Server-side so
        the frontend renders a verdict it did not compute (CLAUDE.md: no
        business logic in the frontend). None when there is nothing to show."""
        on_file = self.store.vendor_account(invoice.vendor_id)
        printed = invoice.payout_account
        if printed is None and on_file is None:
            return None
        matches = (
            printed is not None
            and on_file is not None
            and "".join(printed.split()).upper() == "".join(on_file.split()).upper()
        )
        return {"invoice": printed, "on_file": on_file, "matches": matches}
```

In `get_case`'s returned dict (around lines 378–392 in `service.py`, the one with the `"lines"` / `"po"` / `"grn"` / `"match"` keys), add a line alongside them:

```python
            "po": po.model_dump() if po else None,
            "grn": grn.model_dump() if grn else None,
            "payout_account": self._payout_account_view(invoice),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APAGENT_MAIL_FROM= TELEGRAM_BOT_TOKEN= .venv/Scripts/python.exe -m pytest tests/test_api.py -k payout_account -q`
Expected: PASS.

- [ ] **Step 5: Render it on the invoice detail (app.js)**

In `src/apagent/api/web/app.js`, in the invoice-detail render, find the three-way match card anchor:

```javascript
        <div class="card recon"><h3>Three-way match</h3>
```

Immediately AFTER that card's closing (the `</div>` that ends the recon card), insert a payout-account card built from `c.payout_account`. Add this to the template (all data escaped via the existing `esc`):

```javascript
  const pa = c.payout_account;
  const last4 = (s) => esc((s || "").replace(/\s/g, "").slice(-4));
  const payoutCard = !pa ? "" : pa.matches
    ? `<div class="card"><h3>Payout account</h3>
        <div class="allgood">✓ …${last4(pa.invoice)} matches the account on file</div></div>`
    : `<div class="card chatev unauth"><h3>⚠ Payout account changed</h3>
        <p class="evnote" style="font-weight:600">Invoice pays …${last4(pa.invoice)}, but the account on file for this vendor is …${last4(pa.on_file)}.</p>
        <p class="evnote">Verify the vendor's bank details out of band before releasing payment — a changed remittance account with everything else correct is the signature of vendor-email compromise.</p></div>`;
```

Then place `${payoutCard}` in the detail template right after the three-way match card. (Reuses the same card classes the PO sanity flag card uses — `chatev unauth` / `allgood` / `evnote` — so no CSS is added.)

- [ ] **Step 6: Manual smoke check**

Run: `APAGENT_MAIL_FROM= TELEGRAM_BOT_TOKEN= .venv/Scripts/python.exe -m uvicorn apagent.api.app:app --port 8000` (background), open `http://127.0.0.1:8000`, sign in, open the Invoices queue, click `INV-DEMO-BANKSWAP`. Expect the ⚠ Payout account changed card and an ESCALATE decision. Open any real invoice: expect ✓ matches the account on file. Stop the server.

- [ ] **Step 7: Commit**

```bash
git add src/apagent/api/service.py src/apagent/api/web/app.js tests/test_api.py
git commit -m "feat: surface payout-account check on the invoice detail"
```

---

## Task 7: Update the guardrail count 8 → 9

**Files:**
- Modify: `README.md`, `src/apagent/api/web/app.js` (and any other prose that states the gate count)

- [ ] **Step 1: Find every place that says the count**

Run: `git grep -niE "eight guardrail|eight gate|8 guardrail|8 gate|eight code"`
Also check the number spelled near the guardrail description: `git grep -niE "guardrail" README.md src/apagent/api/web/app.js`

- [ ] **Step 2: Update each hit from eight to nine**

Change the count wherever it appears (e.g. README's "eight guardrails (not superseded by a later correction, …, goods received)" gains ", payout account matches the vendor master"; the app.js prose likewise). Keep the list of named gates in sync — add the payout-account gate to any enumerated list.

- [ ] **Step 3: Verify no stale count remains**

Run: `git grep -niE "eight guardrail|8 guardrail|eight gate"`
Expected: no hits (all now say nine / 9).

- [ ] **Step 4: Commit**

```bash
git add README.md src/apagent/api/web/app.js
git commit -m "docs: guardrail count is now nine (payout-account gate)"
```

---

## Task 8: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite, clean environment**

Run: `APAGENT_MAIL_FROM= TELEGRAM_BOT_TOKEN= .venv/Scripts/python.exe -m pytest -q`
Expected: all pass, 3 skipped (optional langgraph/mcp deps). Note the passed count.

- [ ] **Step 2: Confirm the headline numbers are untouched**

Run the metrics check the repo already has (the test that asserts STP / touchless / false-approve). Confirm STP 68, touchless 82, false approvals 0 are unchanged from `origin/main`.

- [ ] **Step 3: Lint**

Run: `.venv/Scripts/python.exe -m ruff check src tests scripts` (and `ruff format --check` if the repo's hook uses it).
Expected: clean, or auto-fix and re-commit.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/payout-account-guardrail
```

- [ ] **Step 5: Hand back to the user** to open the PR (do not open it automatically — it is the user's team repo).

---

## Self-review notes (for the implementer)

- **Metrics freeze is load-bearing.** Two things protect it: every real invoice carries its own on-file account (gate silent), and the demo is in `_session_documents()` (excluded from the denominator). If a metrics test moves, check both before touching anything else.
- **The gate is silent by default.** It only fires when BOTH accounts are known and differ. Every hand-built test store with no `vendor_accounts` is therefore unaffected — that is why the pre-existing suite keeps passing.
- **Demo committed decision** is produced by `decide_invoice_rules_only`, so its `reasoning` reads "[code guardrail] The invoice's payout account (…8765) differs … Model reasoning was: [rules-only baseline] …". That is truthful; if a cleaner demo string is wanted later, hand-author the cache entry instead — not required for correctness.
