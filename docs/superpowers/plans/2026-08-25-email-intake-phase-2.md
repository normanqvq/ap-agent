# Vendor Email Intake — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A corrected invoice attached to a vendor's reply is re-matched against our own purchase order and goods receipt, and clears on its own if it really is correct — with no human touched at any point in the loop.

**Architecture:** The reply's attachment goes through the existing PDF extraction path into a **revision** document whose identity is derived by code, never read off the vendor's paper. The revision then runs the whole pipeline: our PO, our GRN, our tolerances. The vendor supplies a document; code computes the facts.

**Tech Stack:** Python 3.12, stdlib `email`, pdfplumber + the existing `extraction/invoice.py`, pydantic v2 models in `schemas.py`, pytest, ruff (line-length 100, `E,F,I,UP,B`).

**Design source:** `docs/superpowers/specs/2026-08-25-email-intake-design.md`, section "What a reply is allowed to do".

**Phase 1 is complete** on this branch: `src/apagent/mail/` sends a query automatically, correlates the reply by headers and token, files it as `VendorReplyEvidence`, and chases a silent vendor once before escalating. 293 tests, all offline.

---

## Two deliberate departures from the spec

**No LLM classifier.** The spec listed a `mail/extract.py` that would read the reply body and classify the vendor's intent. It is dropped. What actually triggers the automatic path is whether a corrected invoice is attached — a fact code establishes by looking. A model call whose output changes nothing is cost without benefit; a model call whose output *does* change the decision is precisely the authority this architecture refuses to hand over. A text-only reply stays what Phase 1 made it: evidence a human reads.

**Nothing on the vendor's paper decides identity.** The extracted document supplies prices, quantities and dates. `doc_id`, `vendor_id` and `ref_doc_id` are all carried over from the invoice being revised. Trusting the PDF's printed PO reference would let a "correction" re-point an invoice at a different purchase order — a cheaper attack than any of the ones the correlation layer already refuses.

---

## File structure

**Create:**

| File | Responsibility |
|---|---|
| `src/apagent/mail/attach.py` | Pull PDF attachments out of a raw message, under limits |
| `src/apagent/mail/revise.py` | An extracted document + the original -> a revision, identity owned by code |
| `tests/test_mail_revision.py` | Everything above, offline |

**Modify:**

| File | Change |
|---|---|
| `src/apagent/schemas.py` | `Document.replaces` |
| `src/apagent/agent/ap_tools.py` | `hard_duplicates` skips a revision chain |
| `src/apagent/api/service.py` | `on_vendor_reply` grows the revision path; revisions held out of the eval |
| `src/apagent/mail/runner.py` | Hand the raw message to the reply callback |
| `scripts/demo_email_intake.py` | A seventh section: the corrected invoice clears itself |
| `docs/superpowers/specs/2026-08-25-email-intake-design.md` | Status line, and the two departures above |

---

## Task 1: A document can supersede another

**Files:** Modify `src/apagent/schemas.py`; create `tests/test_mail_revision.py`

- [ ] **Step 1: Write the failing test**

```python
"""A corrected invoice, attached to a vendor's reply, matched again by code.

Offline: extraction is stubbed everywhere. These tests are about what WE do
with a document a counterparty sent us, not about reading PDFs.
"""

from apagent.schemas import Document, DocType


def _doc(doc_id, **kw):
    base = dict(
        doc_id=doc_id,
        doc_type=DocType.INVOICE,
        vendor_id="V005",
        vendor_name="Pacific Circuit Components Inc",
        issue_date="2026-08-14",
        ref_doc_id="PO-2026-1005",
        currency="USD",
        lines=[],
        total_cents=53040,
    )
    base.update(kw)
    return Document(**base)


def test_a_document_records_what_it_supersedes():
    revision = _doc("INV-V005-3005-R1", replaces="INV-V005-3005")
    assert revision.replaces == "INV-V005-3005"


def test_an_ordinary_document_supersedes_nothing():
    assert _doc("INV-V005-3005").replaces is None
```

- [ ] **Step 2: Run to verify it fails**

`.venv/Scripts/python.exe -m pytest tests/test_mail_revision.py -v`
Expected: FAIL, `ValidationError: Object has no attribute 'replaces'` (or the field is silently dropped — either way the first assertion fails)

- [ ] **Step 3: Add the field**

In `Document`, beside the other optional fields:

```python
    replaces: str | None = None  # the doc_id this revision supersedes, set by code
```

And in the `Document` docstring, after the `ref_doc_id` paragraph:

```
    replaces names the document this one supersedes — set only by code, when a
    vendor sends a corrected invoice in answer to a query. It is what stops the
    duplicate gate from flagging a correction as a resubmission: same vendor,
    same purchase order, near-identical total is exactly what a duplicate looks
    like, and exactly what a correction looks like too. The link is the only
    thing that distinguishes them, which is why it is never read off the
    vendor's paper.
```

- [ ] **Step 4: Verify**

`.venv/Scripts/python.exe -m pytest tests/test_mail_revision.py -v` → 2 passed
`.venv/Scripts/python.exe -m pytest -q` → 295 passed

- [ ] **Step 5: Commit**

```bash
git add src/apagent/schemas.py tests/test_mail_revision.py
git commit -m "Let a document record what it supersedes"
```

---

## Task 2: A correction is not a duplicate

This is the task that keeps Phase 2 from shipping a silent false negative on the `duplicate` defect — the one failure class this project measures itself on.

**Files:** Modify `src/apagent/agent/ap_tools.py`; modify `tests/test_mail_revision.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

import pytest

from apagent.agent.ap_tools import hard_duplicates
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


@pytest.fixture
def store():
    return DocumentStore.from_dir(DATA)


def test_a_revision_is_not_a_duplicate_of_what_it_replaces(store):
    original = store.get_invoice("INV-V005-3005")
    revision = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    store.add_invoice(revision)
    assert hard_duplicates(revision, store) == []


def test_the_original_is_not_a_duplicate_of_its_revision(store):
    """The link is symmetric in effect, whichever side is being decided."""
    original = store.get_invoice("INV-V005-3005")
    revision = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    store.add_invoice(revision)
    assert hard_duplicates(original, store) == []


def test_a_second_revision_is_not_a_duplicate_of_the_first(store):
    """R2 replaces R1 replaces the original: the whole chain is one invoice."""
    original = store.get_invoice("INV-V005-3005")
    first = original.model_copy(
        update={"doc_id": "INV-V005-3005-R1", "replaces": "INV-V005-3005"}
    )
    second = original.model_copy(
        update={"doc_id": "INV-V005-3005-R2", "replaces": "INV-V005-3005-R1"}
    )
    store.add_invoice(first)
    store.add_invoice(second)
    assert hard_duplicates(second, store) == []


def test_a_genuine_duplicate_is_still_caught(store):
    """The guard must not become a way to launder a resubmission: an
    unlinked copy of the same invoice is still a duplicate."""
    original = store.get_invoice("INV-V005-3005")
    copy = original.model_copy(update={"doc_id": "INV-V005-3005-COPY"})
    store.add_invoice(copy)
    assert [d.doc_id for d in hard_duplicates(copy, store)] == ["INV-V005-3005"]


def test_a_forged_replaces_pointing_at_nothing_is_still_a_duplicate(store):
    """`replaces` is set by code, but if a document ever arrives carrying one
    that names no document we hold, it buys nothing."""
    original = store.get_invoice("INV-V005-3005")
    copy = original.model_copy(
        update={"doc_id": "INV-V005-3005-COPY", "replaces": "INV-NOT-A-DOCUMENT"}
    )
    store.add_invoice(copy)
    assert [d.doc_id for d in hard_duplicates(copy, store)] == ["INV-V005-3005"]
```

- [ ] **Step 2: Run to verify it fails**

Expected: the first three fail — `hard_duplicates` reports the other document, because same vendor, same resolved PO and an equal total is exactly its key. The last two must pass already; if they do not, stop and report.

- [ ] **Step 3: Teach `hard_duplicates` about revision chains**

Add above it:

```python
def _revision_chain(invoice: Document, store: DocumentStore) -> set[str]:
    """Every doc_id in this invoice's correction chain, both directions.

    A corrected invoice is indistinguishable from a resubmission by the
    duplicate key alone -- same vendor, same purchase order, near-identical
    total is the definition of both. What separates them is the `replaces`
    link, which only code sets, and only when the document arrived as an
    answer to a query we ourselves sent.

    Walked in both directions, and transitively, because which end of the
    chain is being decided is an accident of timing: the revision is scored
    when it arrives, and the original is re-scored whenever anything else
    about it changes.

    A `replaces` naming a document we do not hold is ignored rather than
    trusted -- it buys an attacker nothing, which is the point of checking
    against the store instead of against the field alone.
    """
    chain = {invoice.doc_id}
    frontier = [invoice]
    while frontier:
        current = frontier.pop()
        candidates = []
        if current.replaces:
            older = store.get_invoice(current.replaces)
            if older is not None:
                candidates.append(older)
        candidates.extend(
            other
            for other in store.invoices_for_vendor(invoice.vendor_id)
            if other.replaces == current.doc_id
        )
        for doc in candidates:
            if doc.doc_id not in chain:
                chain.add(doc.doc_id)
                frontier.append(doc)
    return chain
```

and in `hard_duplicates`, after resolving `inv_po`:

```python
    chain = _revision_chain(invoice, store)
    out = []
    for other in store.invoices_for_vendor(invoice.vendor_id):
        if other.doc_id in chain:
            continue
```

(replacing the existing `if other.doc_id == invoice.doc_id: continue`, which the chain now covers — it always contains the invoice's own id).

Extend the `hard_duplicates` docstring with a paragraph naming what the chain is for and what it deliberately does not do (it does not make an unlinked copy safe).

- [ ] **Step 4: Verify**

`.venv/Scripts/python.exe -m pytest tests/test_mail_revision.py -v` → 7 passed
`.venv/Scripts/python.exe -m pytest -q` → 300 passed
`.venv/Scripts/python.exe scripts/run_eval.py` → **false approvals 0, STP 68%, touchless 82%** — unchanged. The duplicate defect (`INV-V003-3901`) must still be caught; if it is not, stop.

- [ ] **Step 5: Commit**

```bash
git add src/apagent/agent/ap_tools.py tests/test_mail_revision.py
git commit -m "Tell a correction apart from a resubmission"
```

---

## Task 3: Getting the attachment out

**Files:** Create `src/apagent/mail/attach.py`; modify `tests/test_mail_revision.py`

- [ ] **Step 1: Write the failing tests**

```python
from apagent.mail.attach import MAX_ATTACHMENT_BYTES, pdf_attachments

_PDF = b"%PDF-1.4\n... not a real pdf, but it starts like one ...\n%%EOF\n"


def _with_attachment(payload: bytes, filename: str = "corrected.pdf") -> bytes:
    import base64

    encoded = base64.b64encode(payload).decode()
    return (
        "From: AR Dept <ar-dept@pacific.example>\n"
        "To: ap@example.test\n"
        "Subject: corrected\n"
        'Content-Type: multipart/mixed; boundary="B"\n'
        "\n"
        "--B\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\n"
        "Corrected invoice attached.\n"
        "--B\n"
        "Content-Type: application/pdf\n"
        "Content-Transfer-Encoding: base64\n"
        f'Content-Disposition: attachment; filename="{filename}"\n'
        "\n"
        f"{encoded}\n"
        "--B--\n"
    ).encode()


def test_a_pdf_attachment_comes_back_with_its_bytes():
    found = pdf_attachments(_with_attachment(_PDF))
    assert [name for name, _ in found] == ["corrected.pdf"]
    assert found[0][1].startswith(b"%PDF")


def test_a_message_with_no_attachment_yields_nothing():
    assert pdf_attachments(b"From: a@b.test\n\njust text\n") == []


def test_something_merely_named_pdf_is_refused():
    """The filename is the sender's choice; the magic bytes are not."""
    assert pdf_attachments(_with_attachment(b"MZ\x90\x00 this is a windows binary")) == []


def test_an_oversized_attachment_is_refused():
    assert pdf_attachments(_with_attachment(b"%PDF-" + b"x" * MAX_ATTACHMENT_BYTES)) == []


def test_only_the_first_few_attachments_are_considered():
    """A reply with forty attachments is not a correction, and extracting
    each one costs a model call."""
    import base64

    parts = "".join(
        "--B\nContent-Type: application/pdf\n"
        "Content-Transfer-Encoding: base64\n"
        f'Content-Disposition: attachment; filename="a{i}.pdf"\n\n'
        f"{base64.b64encode(_PDF).decode()}\n"
        for i in range(10)
    )
    raw = (
        "From: a@b.test\n"
        'Content-Type: multipart/mixed; boundary="B"\n\n' + parts + "--B--\n"
    ).encode()
    assert len(pdf_attachments(raw)) == 3


def test_a_malformed_message_yields_nothing_instead_of_raising():
    assert pdf_attachments(b"\xff\xfe not a message at all") == []
```

- [ ] **Step 2: Run to verify it fails**

Expected: `ModuleNotFoundError: No module named 'apagent.mail.attach'`

- [ ] **Step 3: Write it**

```python
"""Pulling a corrected invoice out of a reply, under limits.

Separate from inbound.py because the two have different jobs and different
risk. inbound.py builds the record a human reads and must never raise;
this decides what bytes get fed to the extraction path, which costs a model
call and ends in a document that can clear an invoice.

Every limit here is a refusal, not a repair. A reply carrying forty files is
not a correction, and neither is one carrying a 50 MB scan; the right answer
in both cases is to leave the evidence for a person rather than to try
harder.

The filename is the sender's choice and decides nothing. `%PDF` at the head
of the payload is checked instead -- not a full validation, but it is the
difference between "someone called it a pdf" and "it is one", and the
extraction layer fails closed on anything it cannot read anyway.
"""

import logging
from email import message_from_bytes

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # matches the console's upload limit
MAX_ATTACHMENTS = 3

log = logging.getLogger(__name__)


def pdf_attachments(raw: bytes) -> list[tuple[str, bytes]]:
    """[(filename, bytes)] for the PDFs in a message, oldest first.

    Returns [] rather than raising, for the same reason parse_mail does not:
    this runs inside the poller, and a message we cannot read must cost that
    message and nothing else.
    """
    try:
        message = message_from_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - a poller must not die
        log.warning("could not read a message for attachments: %s", type(exc).__name__)
        return []

    out: list[tuple[str, bytes]] = []
    for part in message.walk():
        if len(out) >= MAX_ATTACHMENTS:
            break
        filename = part.get_filename()
        if not filename:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001 - a broken part is not a broken message
            continue
        if not payload.startswith(b"%PDF"):
            continue
        if len(payload) > MAX_ATTACHMENT_BYTES:
            log.info("attachment %r is over the size limit; ignoring it", filename)
            continue
        out.append((filename, payload))
    return out
```

- [ ] **Step 4: Verify**

`.venv/Scripts/python.exe -m pytest tests/test_mail_revision.py -v` → 13 passed
`.venv/Scripts/python.exe -m pytest -q` → 306 passed

- [ ] **Step 5: Commit**

```bash
git add src/apagent/mail/attach.py tests/test_mail_revision.py
git commit -m "Take the corrected invoice out of the reply, under limits"
```

---

## Task 4: Building the revision

**Files:** Create `src/apagent/mail/revise.py`; modify `tests/test_mail_revision.py`

- [ ] **Step 1: Write the failing tests**

```python
from apagent.mail.revise import make_revision


def test_the_revision_keeps_our_identity_not_the_vendors(store):
    original = store.get_invoice("INV-V005-3005")
    # Everything a hostile "correction" might try to redirect:
    extracted = original.model_copy(
        update={
            "doc_id": "TOTALLY-DIFFERENT-NUMBER",
            "vendor_id": "V001",
            "vendor_name": "Someone Else Pte Ltd",
            "ref_doc_id": "PO-2026-1001",
            "total_cents": 49000,
        }
    )
    revision = make_revision(original, extracted, sequence=1)
    assert revision.doc_id == "INV-V005-3005-R1"
    assert revision.vendor_id == "V005"
    assert revision.ref_doc_id == "PO-2026-1005"
    assert revision.replaces == "INV-V005-3005"
    # ... while the numbers it is allowed to correct do come from the paper:
    assert revision.total_cents == 49000


def test_a_second_revision_numbers_itself(store):
    original = store.get_invoice("INV-V005-3005")
    revision = make_revision(original, original, sequence=2)
    assert revision.doc_id == "INV-V005-3005-R2"


def test_the_revision_is_flagged_as_having_arrived_by_email(store):
    original = store.get_invoice("INV-V005-3005")
    revision = make_revision(original, original, sequence=1, evidence_id="MAIL-EV-0001")
    assert revision.source == EvidenceSource.EMAIL
    assert revision.source_ref == "MAIL-EV-0001"
```

(add `EvidenceSource` to the imports)

- [ ] **Step 2: Run to verify it fails**

Expected: `ModuleNotFoundError: No module named 'apagent.mail.revise'`

- [ ] **Step 3: Write it**

```python
"""An extracted correction plus the invoice it answers -> a revision.

The split is the whole point. A vendor's corrected invoice is allowed to
change what it is entitled to change -- prices, quantities, dates, the total
-- and nothing else. Identity comes from OUR records:

    doc_id      derived here, INV-...-R1, never the number printed on the paper
    vendor_id   carried from the invoice under query
    ref_doc_id  carried from the invoice under query

Without that, a "correction" is a way to re-point an invoice at a different
purchase order, or to bill under a different vendor's terms -- both cheaper
than any attack the correlation layer already refuses, and both invisible
afterwards because the resulting document looks entirely ordinary.

Marked EvidenceSource.EMAIL with the evidence id that carried it, so the
provenance of every figure on it is one lookup away. A revision is still a
document like any other after this: it goes through the same pipeline, the
same gates, and the same tolerances as an invoice that arrived by post.
"""

from apagent.schemas import Document, EvidenceSource


def make_revision(
    original: Document,
    extracted: Document,
    sequence: int,
    evidence_id: str | None = None,
) -> Document:
    """The revision document, with identity owned by code."""
    return extracted.model_copy(
        update={
            "doc_id": f"{original.doc_id}-R{sequence}",
            "doc_type": original.doc_type,
            "vendor_id": original.vendor_id,
            "vendor_name": original.vendor_name,
            "ref_doc_id": original.ref_doc_id,
            "replaces": original.doc_id,
            "source": EvidenceSource.EMAIL,
            "source_ref": evidence_id,
        }
    )
```

- [ ] **Step 4: Verify**

`.venv/Scripts/python.exe -m pytest tests/test_mail_revision.py -v` → 16 passed
`.venv/Scripts/python.exe -m pytest -q` → 309 passed

- [ ] **Step 5: Commit**

```bash
git add src/apagent/mail/revise.py tests/test_mail_revision.py
git commit -m "Let the vendor correct the figures, never the identity"
```

---

## Task 5: The service closes the loop

**Files:** Modify `src/apagent/api/service.py`, `src/apagent/mail/runner.py`; modify `tests/test_mail_revision.py`

- [ ] **Step 1: Write the failing tests**

Extraction is stubbed — no PDF is read and no model is called.

```python
import apagent.api.service as service_module
from apagent.api.service import Service


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def _service_with_reply(monkeypatch, corrected_total_cents):
    """A service holding one vendor reply, with extraction stubbed to return
    a corrected invoice."""
    from apagent.mail.directory import VendorDirectory

    svc = Service()
    svc.attach_mail(
        VendorDirectory({"V005": {"email": "billing@pacific.example"}}),
        FakeSender(),
        "ap@example.test",
    )
    original = svc.store.get_invoice("INV-V005-3005")
    corrected = original.model_copy(update={"total_cents": corrected_total_cents})
    monkeypatch.setattr(
        service_module, "extract_invoice", lambda path, vendors, **kw: corrected
    )
    return svc


def test_a_corrected_invoice_is_matched_again_by_code(monkeypatch, ...):
    ...
```

Write the group to cover, at minimum:

1. A reply from the registered vendor **with** a PDF produces a revision in the store, decided, and reachable from the original's case (`get_case("INV-V005-3005")["revisions"]`).
2. A reply from an **unregistered** sender with a PDF produces **no** revision — evidence only. (The same rail as Phase 1's automatic path.)
3. A **bounce** carrying a PDF produces no revision.
4. A **text-only** reply produces no revision and no error.
5. Extraction failing (`ExtractionError`) leaves the evidence in place, produces no revision, and does not raise out of the callback.
6. The revision does **not** move the measured benchmark: `analytics()["metrics"]` and `metrics()["false_approve"]` are unchanged, and `data/synthetic/decisions.json` is untouched on disk.
7. `INV-V005-3005`'s own committed decision is still `EMAIL` after a revision arrives.

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Wire it**

In `Service.__init__`:

```python
        # invoice_id -> the revision doc_ids raised from vendor replies this
        # session. Session state like _uploaded: a revision never joins the
        # committed dataset or the decisions cache on disk.
        self._revisions: dict[str, list[str]] = {}
```

`on_vendor_reply` grows a second half. Keep the first half exactly as it is — filing the evidence must not depend on anything below it succeeding:

```python
    def on_vendor_reply(self, evidence, raw: bytes | None = None) -> None:
        """File a reply, and raise a revision if it carried a corrected invoice.

        The two halves are deliberately independent: the evidence is filed
        first and unconditionally, so a reviewer sees what the vendor said
        even when the attachment turns out to be unreadable.
        """
        if evidence is None:
            return
        self._vendor_replies.setdefault(evidence.invoice_id, []).append(evidence.model_dump())
        if raw is None or evidence.is_non_delivery or not evidence.from_registered_sender:
            # An unregistered sender's attachment is evidence, never an
            # automatic path -- the same rail the rest of the feature uses.
            return
        self._revise_from(evidence, raw)
```

`_revise_from` does: `pdf_attachments(raw)` -> first PDF -> write to a temp file (the extraction path takes a `Path`, exactly as `upload_invoice` does) -> `extract_invoice` -> `make_revision` -> `store.add_invoice` -> `run_case(revision.doc_id)` -> record in `self._revisions`. It must catch `ExtractionError` and log, never raise into the poller. Follow `upload_invoice` for the temp-file handling, including the cleanup in `finally`.

`get_case` gains `"revisions": [...]` — the decided case bundle for each revision id, or just the ids if a full bundle risks recursion. Choose one and say which in your report.

`_eval_view` must hold revisions out the way uploads are held out: they have no manifest entry, so dropping them is invisible and correct. **Verify this by reading `_eval_view`, not by assuming** — if uploads are dropped by membership in `self._uploaded`, revisions need the same treatment.

In `runner.py`, `tick` passes the raw message through:

```python
            if self.on_reply is not None:
                self.on_reply(evidence, raw)
```

and Phase 1's `on_reply=seen.append` test callbacks take one argument, so **check every existing caller and test** before changing the signature. Either give `on_vendor_reply` a defaulted second parameter (it has one above) and update the runner's call, or keep the runner passing one argument and add a separate hook. Pick the smaller change and say which.

- [ ] **Step 4: Verify**

```
.venv/Scripts/python.exe -m pytest -q                    # everything green
.venv/Scripts/python.exe scripts/run_eval.py             # 0 / 68% / 82%, unchanged
git status --short                                        # data/ must be untouched
```

- [ ] **Step 5: Commit**

```bash
git commit -m "Match a corrected invoice again, and let it clear itself"
```

---

## Task 6: Show it

**Files:** Modify `scripts/demo_email_intake.py`, `docs/superpowers/specs/2026-08-25-email-intake-design.md`

- [ ] **Step 1: Extend the demo**

Add a seventh section to `scripts/demo_email_intake.py` that runs offline with no model: build a reply carrying a PDF attachment, call `pdf_attachments` on it, and show `make_revision` producing a revision whose identity survived a hostile "correction" (vendor and PO carried from our records, figures taken from the paper). Print the before/after totals.

Do **not** call `extract_invoice` in the demo — it needs a model and a real PDF. Construct the "extracted" document directly, and say so in a printed line, honestly: the demo shows what code does with an extracted document, not the extraction itself.

- [ ] **Step 2: Run it**

`.venv/Scripts/python.exe scripts/demo_email_intake.py` → seven sections, no network

- [ ] **Step 3: Update the design doc**

Change the status line to record that Phase 2 is implemented, and add a short section recording the two departures at the top of this plan (no LLM classifier; identity never read off the vendor's paper) with their reasoning.

- [ ] **Step 4: Commit**

```bash
git add scripts/demo_email_intake.py docs/superpowers/specs/2026-08-25-email-intake-design.md
git commit -m "Show a corrected invoice clearing itself"
```

---

## Verification

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/run_eval.py                # 0 false approvals, 68% / 82%
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe scripts/demo_email_intake.py
git status --short                                           # data/ untouched
```

End to end against the live mailbox, with `INV-V005-3005` decided `EMAIL`:

1. The query arrives at the vendor mailbox.
2. Reply to it **with a corrected invoice PDF attached** — one whose unit price matches `PO-2026-1005`.
3. Within a minute the console shows the reply as evidence **and** a revision `INV-V005-3005-R1`, decided on its own merits.
4. Repeat with the price still wrong: the revision is raised and still does not approve.
5. Repeat from an address outside the vendor's domain: evidence only, no revision.

## Self-review

**Spec coverage.** "With a PDF attachment" — Tasks 3, 4, 5. "Text only" — Task 5, case 4. "Always: the reply body is untrusted" — unchanged from Phase 1; nothing in this plan reads the body. "The duplicate-gate interaction" — Task 2, which the spec explicitly asked for a test on.

**Not covered, deliberately:** the LLM reply classifier, per the departure recorded above.

**Type consistency.** `Document.replaces` is `str | None` in Tasks 1, 2 and 4. `pdf_attachments` returns `list[tuple[str, bytes]]` in Tasks 3 and 5. `make_revision(original, extracted, sequence, evidence_id=None)` is called with the same shape in Tasks 4, 5 and 6. `on_vendor_reply(evidence, raw=None)` is the signature in Task 5 and is the one the runner calls.
