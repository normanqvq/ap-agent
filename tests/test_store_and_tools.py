"""Tests for the document store and the AP evidence tools.

All offline. The tools are exercised through registry.execute — the same
path the agent uses — so a schema typo or a handler crash shows up here,
not in a live demo.
"""

import json
from pathlib import Path

import pytest

from apagent.agent.ap_tools import build_registry
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


@pytest.fixture(scope="module")
def store():
    return DocumentStore.from_dir(DATA)


@pytest.fixture(scope="module")
def registry(store):
    return build_registry(store, DATA / "contracts")


# --- store --------------------------------------------------------------


def test_store_loads_the_dataset(store):
    assert store.get_po("PO-2026-1001") is not None
    assert store.get_invoice("INV-V001-3001") is not None
    assert store.get_grn_for_po("PO-2026-1001") is not None


def test_store_vendor_directory_comes_from_pos(store):
    vendors = store.vendors()
    assert set(vendors) == {"V001", "V002", "V003", "V004", "V005", "V006"}
    assert all(name for name in vendors.values())


def test_store_missing_grn_case(store):
    """PO-2026-1019 is the planted missing-GRN case."""
    assert store.get_po("PO-2026-1019") is not None
    assert store.get_grn_for_po("PO-2026-1019") is None


# --- tools, via registry.execute ---------------------------------------


def test_lookup_po_returns_full_document(registry):
    result = json.loads(registry.execute("lookup_po", {"po_id": "PO-2026-1005"}))
    assert result["doc_id"] == "PO-2026-1005"
    assert result["lines"], "PO must come with its line items"


def test_lookup_po_miss_is_a_message_not_a_crash(registry):
    result = registry.execute("lookup_po", {"po_id": "PO-9999-0000"})
    assert "not found" in result


def test_lookup_grn_missing_says_no_proof_of_delivery(registry):
    result = registry.execute("lookup_grn", {"po_id": "PO-2026-1019"})
    assert "No goods receipt" in result
    assert "proof of delivery" in result


def test_vendor_history_lists_invoices(registry):
    result = json.loads(registry.execute("get_vendor_history", {"vendor_id": "V005"}))
    assert result["invoice_count"] >= 3
    ids = [inv["doc_id"] for inv in result["invoices"]]
    assert "INV-V005-3005" in ids


def test_duplicate_check_catches_the_planted_duplicate(registry):
    """INV-V003-3901 duplicates INV-V003-3003: same vendor, same PO
    reference, same total. The tool must report it as a hard duplicate."""
    result = json.loads(
        registry.execute("check_duplicate_invoice", {"invoice_id": "INV-V003-3901"})
    )
    dup_ids = [d["doc_id"] for d in result["likely_duplicates"]]
    assert dup_ids == ["INV-V003-3003"]


def test_duplicate_check_clean_invoice_reports_none(registry):
    result = json.loads(
        registry.execute("check_duplicate_invoice", {"invoice_id": "INV-V001-3001"})
    )
    assert result["likely_duplicates"] == []


def test_contract_search_is_registered_in_the_full_toolbelt(registry):
    names = [t["name"] for t in registry.get_definitions()]
    assert "search_vendor_contract" in names
    assert len(names) == 5
