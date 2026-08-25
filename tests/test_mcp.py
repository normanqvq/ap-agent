"""The MCP server and the resilient fallback.

Two things are pinned here. First, an MCP call returns exactly what the raw
registry returns -- byte for byte, for every published tool -- because the
server delegates to that registry. Second, when the MCP transport fails, the
resilient registry falls back to the raw path and produces the same result,
so a dropped connection never changes a decision.

Skipped when mcp is not installed (it is an optional dependency).
"""

import pytest

pytest.importorskip("mcp.server.fastmcp")

from pathlib import Path  # noqa: E402

from apagent.agent.ap_tools import build_registry  # noqa: E402
from apagent.mcp_bridge import (  # noqa: E402
    ResilientToolRegistry,
    in_process_resilient_registry,
)
from apagent.mcp_server import PUBLISHED_TOOLS  # noqa: E402
from apagent.store import DocumentStore  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"

# One realistic argument set per published tool.
TOOL_CALLS = [
    ("lookup_po", {"po_id": "PO-2026-1018"}),
    ("lookup_po", {"po_id": "PO-DOES-NOT-EXIST"}),
    ("lookup_grn", {"po_id": "PO-2026-1019"}),  # missing-GRN case
    ("get_vendor_history", {"vendor_id": "V005"}),
    ("check_duplicate_invoice", {"invoice_id": "INV-V003-3901"}),  # the duplicate
    ("search_vendor_contract", {"vendor_id": "V005", "query": "price variance allowance"}),
]


@pytest.fixture
def registries():
    store = DocumentStore.from_dir(DATA)
    raw = build_registry(store, DATA / "contracts")
    resilient = in_process_resilient_registry(raw)
    yield raw, resilient
    if resilient._client is not None:
        resilient._client.close()


def test_server_publishes_only_the_read_only_tools(registries):
    _, resilient = registries
    assert set(resilient._client.list_tools()) == set(PUBLISHED_TOOLS)
    # The guardrail's own helper is deliberately not exposed.
    assert "recheck_against_contract" not in PUBLISHED_TOOLS


@pytest.mark.parametrize("name,args", TOOL_CALLS)
def test_mcp_result_equals_the_raw_registry(name, args, registries):
    raw, resilient = registries
    assert resilient._client.call_tool(name, args) == raw.execute(name, args)


@pytest.mark.parametrize("name,args", TOOL_CALLS)
def test_execute_routes_over_mcp(name, args, registries):
    raw, resilient = registries
    before = resilient.transport_counts["mcp"]
    assert resilient.execute(name, args) == raw.execute(name, args)
    assert resilient.transport_counts["mcp"] == before + 1


def test_fallback_gives_the_same_answer_when_the_transport_dies(registries):
    raw, resilient = registries
    name, args = "lookup_po", {"po_id": "PO-2026-1018"}
    expected = raw.execute(name, args)

    resilient._client.close()  # kill the transport mid-session
    out = resilient.execute(name, args)

    assert out == expected
    assert resilient.transport_counts["fallback"] >= 1


def test_unpublished_tool_routes_to_raw_not_an_mcp_error(registries):
    """The model is offered recheck_against_contract, which the server does
    not publish. It must return the real clause via raw, not the MCP
    "Unknown tool" text — otherwise AP_MCP=inproc could flip a decision."""
    raw, resilient = registries
    args = {"invoice_id": "INV-V005-3018"}  # the headline contract-flip case
    before = resilient.transport_counts["fallback"]
    out = resilient.execute("recheck_against_contract", args)
    assert out == raw.execute("recheck_against_contract", args)
    assert "Unknown tool" not in out
    assert resilient.transport_counts["fallback"] == before + 1  # went to raw


@pytest.mark.parametrize(
    "name,args",
    [
        ("search_vendor_contract", {"query": "price variance"}),  # vendor_id omitted
        ("lookup_po", {"po_id": 12345}),  # wrong type
        ("lookup_po", {}),  # missing required arg
    ],
)
def test_malformed_args_still_equal_raw(name, args, registries):
    """A published tool called with args the MCP schema rejects must still
    end up equal to raw — either answered directly or fallen back."""
    raw, resilient = registries
    assert resilient.execute(name, args) == raw.execute(name, args)


def test_no_client_means_pure_in_process(registries):
    raw, _ = registries
    only_raw = ResilientToolRegistry(raw, None)
    name, args = "lookup_po", {"po_id": "PO-2026-1018"}
    assert only_raw.execute(name, args) == raw.execute(name, args)
    assert only_raw.transport_counts == {"mcp": 0, "fallback": 1}
