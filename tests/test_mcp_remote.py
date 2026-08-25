"""The remote MCP transport, the circuit breaker, and the session-local rule.

Remote MCP runs the tools in a SEPARATE process over stdio -- the one
transport that can really fail. These tests prove the same two properties
as the in-process ones (parity, and a fallback that keeps the answer) plus
the circuit breaker that stops us paying a timeout on every call once the
server is gone. The subprocess is always closed in teardown so no orphan
process is left behind.

Skipped when mcp is not installed (optional dependency).
"""

import pytest

pytest.importorskip("mcp.client.stdio")

from pathlib import Path  # noqa: E402

from apagent.agent.ap_tools import build_registry  # noqa: E402
from apagent.mcp_bridge import CircuitBreaker, remote_resilient_registry  # noqa: E402
from apagent.store import DocumentStore  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"


@pytest.fixture
def remote():
    store = DocumentStore.from_dir(DATA)
    raw = build_registry(store, DATA / "contracts")
    rr = remote_resilient_registry(raw)
    if rr._client is None:
        pytest.skip("remote MCP server could not start")
    yield raw, rr
    if rr._client is not None:
        rr._client.close()  # terminate the subprocess


def test_remote_publishes_the_tools_and_has_its_own_store(remote):
    _, rr = remote
    assert set(rr._client.list_tools()) >= {"lookup_po", "check_duplicate_invoice"}
    # A separate process cannot see this session's uploads.
    assert rr.shares_store is False


def test_remote_result_equals_the_raw_registry(remote):
    raw, rr = remote
    for name, args in [
        ("lookup_po", {"po_id": "PO-2026-1018"}),
        ("check_duplicate_invoice", {"invoice_id": "INV-V003-3901"}),
        ("search_vendor_contract", {"vendor_id": "V005", "query": "price variance"}),
    ]:
        assert rr._client.call_tool(name, args) == raw.execute(name, args)


def test_killing_the_server_falls_back_to_the_same_answer(remote):
    raw, rr = remote
    name, args = "lookup_po", {"po_id": "PO-2026-1018"}
    expected = raw.execute(name, args)

    assert rr.execute(name, args) == expected  # served by remote
    assert rr.transport_counts["mcp"] >= 1

    rr._client.close()  # kill the server process
    for _ in range(5):
        assert rr.execute(name, args) == expected  # served by fallback, unchanged
    assert rr.transport_counts["fallback"] >= 5


# --- circuit breaker, unit-tested with an injected clock ---


def test_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    now = [0.0]
    b = CircuitBreaker(threshold=3, cooldown_s=20.0, clock=lambda: now[0])

    assert b.allow() is True  # closed
    for _ in range(2):
        b.record_failure()
    assert b.allow() is True  # 2 failures < threshold, still closed
    b.record_failure()  # 3rd failure opens it
    assert b.is_open is True
    assert b.allow() is False  # open, cooling down

    now[0] = 21.0  # cooldown elapsed
    assert b.allow() is True  # half-open: one probe allowed
    b.record_success()  # probe succeeded
    assert b.is_open is False  # closed again
    assert b.allow() is True


def test_breaker_reopens_when_the_probe_fails():
    now = [0.0]
    b = CircuitBreaker(threshold=1, cooldown_s=10.0, clock=lambda: now[0])
    b.record_failure()  # opens immediately (threshold 1)
    assert b.allow() is False
    now[0] = 11.0
    assert b.allow() is True  # half-open probe
    b.record_failure()  # probe fails -> reopen
    now[0] = 12.0
    assert b.allow() is False  # still open, cooldown restarted
