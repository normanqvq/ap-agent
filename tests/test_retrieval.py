"""Tests for contract retrieval.

These run against the committed synthetic contracts in data/synthetic/ — the
real artifact the agent will search, not a mock. Still offline and fast:
loading six small PDFs takes well under a second, and there is no network.

Why we test retrieval quality and not just plumbing:
A retriever that runs but returns the wrong clause is worse than a crash,
because the agent will confidently act on it. The tests below pin the one
behavior the demo depends on: asking about price tolerance for V005 must
surface the 5% clause, and vendors whose contract is silent must NOT get a
made-up answer.
"""

import json
from pathlib import Path

import pytest

from apagent.agent.registry import ToolRegistry
from apagent.retrieval.search import ContractIndex, load_contracts, register_contract_tools

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "contracts"


@pytest.fixture(scope="module")
def index() -> ContractIndex:
    # Built once per test module — the corpus is read-only, so sharing the
    # index between tests is safe and keeps the suite fast.
    return ContractIndex(load_contracts(CONTRACTS_DIR))


def test_contracts_load_into_section_chunks(index):
    """Every vendor contributes chunks, and chunks are whole sections."""
    vendor_ids = {chunk.vendor_id for chunk in index.chunks}
    assert vendor_ids == {"V001", "V002", "V003", "V004", "V005", "V006"}
    # 6 vendors x 5 sections
    assert len(index.chunks) == 30
    # Section headings survived the chunking — they carry search signal
    assert any(chunk.heading.startswith("2. Pricing") for chunk in index.chunks)


def test_price_tolerance_query_finds_the_5pct_clause(index):
    """The demo case: V005 negotiated 5%, and search must surface it."""
    hits = index.search("unit price variance tolerance", vendor_id="V005")
    assert hits, "expected at least one hit for the pricing clause"
    top_score, top_chunk = hits[0]
    assert top_chunk.heading.startswith("2. Pricing")
    assert "5 percent (5%)" in top_chunk.text


def test_vendor_filter_excludes_other_vendors(index):
    """Filtering by vendor must never leak another vendor's clauses."""
    hits = index.search("price variance", vendor_id="V004")
    assert hits
    assert all(chunk.vendor_id == "V004" for _, chunk in hits)


def test_payment_terms_query(index):
    """V003 negotiated net 14 — a different clause than the pricing one."""
    hits = index.search("payment terms net days", vendor_id="V003")
    assert hits
    texts = " ".join(chunk.text for _, chunk in hits)
    assert "net 14 days" in texts


def test_irrelevant_query_returns_nothing(index):
    """MIN_SCORE must keep nonsense queries from returning noise.

    'No clause found' has to be trustworthy: the agent treats it as 'the
    contract is silent, apply the default policy'.
    """
    hits = index.search("zebra spacecraft holiday")
    assert hits == []


def test_registered_tool_returns_cited_json():
    """End-to-end through the registry, the way the agent will call it."""
    registry = ToolRegistry()
    register_contract_tools(registry, CONTRACTS_DIR)

    result = registry.execute(
        "search_vendor_contract",
        {"query": "price variance tolerance", "vendor_id": "V005"},
    )
    hits = json.loads(result)
    assert hits[0]["vendor_id"] == "V005"
    assert hits[0]["source"] == "V005_supply_agreement.pdf"  # citation for the demo
    assert "5%" in hits[0]["text"]


def test_registered_tool_says_silent_when_nothing_matches():
    registry = ToolRegistry()
    register_contract_tools(registry, CONTRACTS_DIR)

    result = registry.execute(
        "search_vendor_contract",
        {"query": "zebra spacecraft holiday", "vendor_id": "V001"},
    )
    assert "No contract clause found" in result
    assert "default policy" in result


def test_registered_tool_requires_query():
    registry = ToolRegistry()
    register_contract_tools(registry, CONTRACTS_DIR)

    result = registry.execute("search_vendor_contract", {})
    assert "Error" in result
