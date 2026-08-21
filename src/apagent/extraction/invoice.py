"""Invoice extraction: a PDF in, a validated Document out.

The split of labour, and why:
- pdfplumber pulls the raw text out of the PDF. Vendors print invoices in
  different layouts, date formats and column orders, so
- the LLM turns that messy text into named fields. Reading is what it is
  good at; it normalizes "17 Aug 2026" and "17/08/2026" to ISO without us
  writing a parser per vendor.
- OUR code does every conversion that must not be fuzzy: money to integer
  cents, vendor name to internal vendor_id, schema validation. CLAUDE.md
  says unit conversion happens once, in the extraction layer — this file is
  that place, and the LLM is deliberately not trusted to do arithmetic.

Why the LLM returns amounts as printed strings ("1,234.56") and not cents:
asking a model to multiply by 100 invites off-by-100 bugs that no test on
happy-path data will catch. The model copies what it reads; _to_cents does
the arithmetic with Decimal, exactly.

Other option: pure-rules parsing with pdfplumber tables, no LLM. Free and
deterministic, but it breaks on every new invoice layout — and handling
messy real-world documents is the point of the product. The LLM path costs
under a cent per invoice on DeepSeek.
"""

import difflib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

from apagent.llm.client import call_model
from apagent.schemas import DocType, Document, LineItem


class ExtractionError(Exception):
    """Extraction failed in a way the caller should surface, not hide.

    A separate exception class (not ValueError) so the API layer can map
    'unreadable invoice' to a clean 4xx with the reason, instead of a 500.
    """


# The LLM's contract. Field-by-field, with the two rules that prevent the
# most damage: copy amounts exactly as printed, and null beats guessing.
EXTRACTION_PROMPT = """\
You extract structured data from supplier invoice text.

Reply with ONLY a JSON object, no prose, in exactly this shape:
{
  "invoice_number": "string, the invoice's own number/id",
  "vendor_name": "string, the supplier's name as printed",
  "issue_date": "YYYY-MM-DD (convert from any printed format)",
  "po_reference": "string or null, the purchase order number if printed",
  "currency": "3-letter code like SGD, or null if not printed",
  "payment_terms": "string or null, e.g. NET 30",
  "due_date": "YYYY-MM-DD or null",
  "tax_amount": "string or null, the tax/GST amount exactly as printed",
  "total_amount": "string or null, the grand total exactly as printed",
  "lines": [
    {
      "line_no": 1,
      "sku": "string or null, the item/product code",
      "description": "string",
      "qty": 1,
      "uom": "string, unit of measure like PCS, or null",
      "unit_price": "string, exactly as printed",
      "line_total": "string, exactly as printed"
    }
  ]
}

Rules:
- Copy every amount EXACTLY as printed (keep commas and decimals, drop
  currency symbols). Never compute, convert or round a number.
- Use null for anything not printed on the invoice. Do not guess.
- line_no counts from 1 in the order the lines appear.
"""


def extract_pdf_text(pdf_path: Path) -> str:
    """All text from all pages. Raises ExtractionError on empty output —
    an image-only scan would otherwise flow downstream as an empty invoice."""
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not text.strip():
        raise ExtractionError(
            f"{pdf_path.name}: no extractable text. Likely a scanned image; "
            "OCR is out of scope for the MVP."
        )
    return text


def _strip_fences(text: str) -> str:
    """Models fence JSON in ```json blocks even when told not to.
    Strip in code rather than fighting it in the prompt."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        text = text.strip().removesuffix("```").strip()
    return text


def _to_cents(amount: str | None) -> int | None:
    """'1,234.56' -> 123456. Decimal, not float — float would make
    19.99 into 1998.9999... and int() would truncate a cent away."""
    if amount is None or str(amount).strip() == "":
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(amount))
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal("1")))
    except InvalidOperation as exc:
        raise ExtractionError(f"unparseable amount: {amount!r}") from exc


def match_vendor_id(printed_name: str, vendors: dict[str, str]) -> str:
    """Map the name printed on the invoice to our internal vendor id.

    Printed names drift ("ABC Pte Ltd" vs "ABC Pte. Ltd." vs all-caps), so
    exact lookup is not enough. We normalize (lowercase, strip punctuation
    and legal suffixes) and then take the closest match above a floor.
    Below the floor we return "UNKNOWN" rather than the least-bad guess —
    a wrong vendor id would silently pull the wrong PO and contract, which
    is far worse than an explicit unknown the agent can escalate.

    vendors maps vendor_id -> canonical name, e.g. {"V001": "Tan Hardware..."}.
    """

    def normalize(name: str) -> str:
        name = name.lower()
        # Legal-form suffixes carry no identity and differ across documents.
        # Strip them on WORD boundaries — a plain substring replace would eat
        # "inc" out of "Prince" and collapse distinct vendors to one string.
        name = re.sub(r"\b(pte\.?\s*ltd\.?|sdn\.?\s*bhd\.?|inc\.?)\b", "", name)
        return re.sub(r"[^a-z0-9 ]", "", name).strip()

    target = normalize(printed_name)
    # Track the two best scores: a confident match must not only clear the
    # floor but also stand clearly ahead of the runner-up, or a one-typo name
    # sitting between two close canonicals would resolve to a coin-flip id.
    best_id, best_score, runner_up = "UNKNOWN", 0.0, 0.0
    for vendor_id, canonical in vendors.items():
        score = difflib.SequenceMatcher(None, target, normalize(canonical)).ratio()
        if score > best_score:
            best_id, best_score, runner_up = vendor_id, score, best_score
        elif score > runner_up:
            runner_up = score
    if best_score < 0.75 or best_score - runner_up < 0.05:
        return "UNKNOWN"
    return best_id


def extract_invoice(
    pdf_path: Path, vendors: dict[str, str], provider: str | None = None
) -> Document:
    """PDF -> validated invoice Document. The one public entry point.

    Raises ExtractionError when the PDF is unreadable or the model's output
    cannot be turned into a valid Document. Callers decide what that means
    (API returns 422; a batch run logs and skips).
    """
    text = extract_pdf_text(pdf_path)

    response = call_model(
        messages=[{"role": "user", "content": f"Invoice text:\n\n{text}"}],
        tools=[],
        system=EXTRACTION_PROMPT,
        provider=provider,
    )
    raw = response.get("text")
    if not raw:
        raise ExtractionError(f"{pdf_path.name}: model returned no text")

    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"{pdf_path.name}: model output is not JSON: {exc}") from exc

    try:
        lines = [
            LineItem(
                line_no=int(ln.get("line_no", i + 1)),
                sku=ln.get("sku"),
                description=ln.get("description") or "",
                qty=int(ln.get("qty", 0)),
                uom=ln.get("uom") or "UNIT",
                unit_price_cents=_to_cents(ln.get("unit_price")),
                line_total_cents=_to_cents(ln.get("line_total")),
            )
            for i, ln in enumerate(data.get("lines", []))
        ]
        # Matching keys its dicts by line_no, so duplicates (a model slip,
        # e.g. every line numbered 1) would silently swallow lines — the
        # worst failure mode in a reconciliation system. Renumber in order
        # of appearance; positions are what the model was told line_no means.
        if len({line.line_no for line in lines}) != len(lines):
            lines = [line.model_copy(update={"line_no": i + 1}) for i, line in enumerate(lines)]
        printed_name = data.get("vendor_name") or ""
        return Document(
            doc_id=data.get("invoice_number") or pdf_path.stem,
            doc_type=DocType.INVOICE,
            vendor_id=match_vendor_id(printed_name, vendors),
            vendor_name=printed_name,
            issue_date=data.get("issue_date") or "",
            ref_doc_id=data.get("po_reference"),
            currency=data.get("currency"),
            lines=lines,
            payment_terms=data.get("payment_terms"),
            due_date=data.get("due_date"),
            tax_cents=_to_cents(data.get("tax_amount")),
            total_cents=_to_cents(data.get("total_amount")),
        )
    except (TypeError, ValueError) as exc:
        raise ExtractionError(f"{pdf_path.name}: invalid field in model output: {exc}") from exc
