"""Tests for invoice extraction.

The LLM call is monkeypatched — tests feed canned model output and check
what WE do with it: cents conversion, vendor matching, fence stripping,
error paths. The model's own reading quality is checked separately by the
live end-to-end run, not by unit tests that would need an API key.
"""

import json
from pathlib import Path

import pytest

from apagent.extraction.invoice import (
    ExtractionError,
    _to_cents,
    extract_invoice,
    extract_pdf_text,
    match_vendor_id,
)
from apagent.schemas import DocType

PDF_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "pdf"

VENDORS = {
    "V001": "Tan Hardware Supplies Pte Ltd",
    "V005": "Pacific Circuit Components Inc",
}

# What a well-behaved model returns for a simple two-line invoice.
GOOD_JSON = {
    "invoice_number": "INV-V001-3001",
    "vendor_name": "Tan Hardware Supplies Pte. Ltd.",
    "issue_date": "2026-07-30",
    "po_reference": "PO-2026-1001",
    "currency": "SGD",
    "payment_terms": "NET 30",
    "due_date": "2026-08-29",
    "tax_amount": "8.78",
    "total_amount": "106.38",
    "lines": [
        {
            "line_no": 1,
            "sku": "SKU-WASH-8",
            "description": "8mm flat washer",
            "qty": 100,
            "uom": "PCS",
            "unit_price": "0.04",
            "line_total": "4.00",
        },
        {
            "line_no": 2,
            "sku": None,
            "description": "M8 hex bolt, zinc plated",
            "qty": 5,
            "uom": "PCS",
            "unit_price": "1,234.56",
            "line_total": "6,172.80",
        },
    ],
}


def fake_model(payload):
    """A call_model stand-in returning the given text."""

    def _call(messages, tools, system, provider=None):
        return {"text": payload, "tool_calls": [], "stop_reason": "end_turn"}

    return _call


def test_to_cents_handles_commas_and_small_amounts():
    assert _to_cents("1,234.56") == 123456
    assert _to_cents("0.04") == 4
    assert _to_cents("106.38") == 10638
    assert _to_cents(None) is None
    assert _to_cents("") is None


def test_to_cents_rejects_garbage():
    with pytest.raises(ExtractionError):
        _to_cents("N/A")


def test_match_vendor_id_survives_name_drift():
    """The same vendor prints its name three different ways across
    documents; all must land on the same internal id."""
    assert match_vendor_id("Tan Hardware Supplies Pte. Ltd.", VENDORS) == "V001"
    assert match_vendor_id("TAN HARDWARE SUPPLIES", VENDORS) == "V001"
    assert match_vendor_id("Pacific Circuit Components Inc.", VENDORS) == "V005"


def test_match_vendor_id_says_unknown_below_floor():
    """A wrong vendor id silently pulls the wrong PO and contract — an
    explicit UNKNOWN is the safe failure."""
    assert match_vendor_id("Totally Different Trading LLC", VENDORS) == "UNKNOWN"


def test_extract_invoice_builds_document(monkeypatch):
    monkeypatch.setattr("apagent.extraction.invoice.call_model", fake_model(json.dumps(GOOD_JSON)))
    doc = extract_invoice(PDF_DIR / "INV-V001-3001.pdf", VENDORS)

    assert doc.doc_type == DocType.INVOICE
    assert doc.doc_id == "INV-V001-3001"
    assert doc.vendor_id == "V001"  # mapped despite the "Pte. Ltd." drift
    assert doc.ref_doc_id == "PO-2026-1001"
    assert doc.tax_cents == 878
    assert doc.total_cents == 10638
    assert doc.lines[0].unit_price_cents == 4
    assert doc.lines[1].unit_price_cents == 123456  # comma amount survived


def test_extract_invoice_strips_markdown_fences(monkeypatch):
    fenced = f"```json\n{json.dumps(GOOD_JSON)}\n```"
    monkeypatch.setattr("apagent.extraction.invoice.call_model", fake_model(fenced))
    doc = extract_invoice(PDF_DIR / "INV-V001-3001.pdf", VENDORS)
    assert doc.doc_id == "INV-V001-3001"


def test_extract_invoice_raises_on_non_json(monkeypatch):
    monkeypatch.setattr(
        "apagent.extraction.invoice.call_model",
        fake_model("Sure! Here is the extracted data: name=..."),
    )
    with pytest.raises(ExtractionError, match="not JSON"):
        extract_invoice(PDF_DIR / "INV-V001-3001.pdf", VENDORS)


def test_extract_pdf_text_reads_real_pdf():
    """Offline sanity: pdfplumber sees the invoice id on our own PDFs.
    If this breaks, the generator's layout changed under extraction's feet."""
    text = extract_pdf_text(PDF_DIR / "INV-V001-3001.pdf")
    assert "INV-V001-3001" in text
    assert "PO-2026-1001" in text
