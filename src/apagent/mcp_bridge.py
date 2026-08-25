"""The agent's MCP client, with an in-process fallback.

The agent loop is synchronous; MCP is asynchronous. This module bridges the
two and adds the resilience the design asks for: use MCP while it is
healthy, and the moment a call fails at the transport level -- a dropped
connection, a timeout, a killed server -- fall straight through to the raw
in-process registry, so a decision never stalls or changes.

Why the fallback is safe, not a guess: the MCP server (mcp_server.py) wraps
the SAME registry, so an MCP call and a raw call return the same string.
Falling back therefore swaps the transport, never the answer. The two are
pinned equal by tests/test_mcp.py.

McpToolClient runs the async MCP session on its own event loop in a
background thread, so a synchronous caller just gets a string back. The same
client serves the in-process (in-memory) and, later, the remote transports
-- only the connect factory differs.
"""

import asyncio
import atexit
import logging
import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager

from mcp.client.session import ClientSession

from apagent.agent.registry import ToolRegistry

_log = logging.getLogger(__name__)

# A tool call that cannot answer in this long has, for our purposes, dropped
# the packet -- fall back rather than let the agent hang on a stalled server.
DEFAULT_TIMEOUT_S = 5.0

# The factory opens a connected MCP ClientSession over some transport.
SessionFactory = Callable[[], AbstractAsyncContextManager[ClientSession]]


def in_process_session_factory(server) -> SessionFactory:
    """Connect an MCP client to our own FastMCP server over in-memory streams.

    This is real MCP -- the server processes CallToolRequest/ListToolsRequest
    -- with no socket and no subprocess, so it shares this process's store and
    adds no network surface."""
    from mcp.shared.memory import create_connected_server_and_client_session

    def factory():
        return create_connected_server_and_client_session(server._mcp_server)  # noqa: SLF001

    return factory


def remote_stdio_session_factory(command: str, args: list[str]) -> SessionFactory:
    """Connect to an MCP server running as a SEPARATE process over stdio.

    This is the decoupled-microservice transport the §5 curriculum shows:
    the tools run in their own process (here, `python -m apagent.mcp_server`).
    It is also the one transport that can genuinely fail -- the process can be
    killed, hang, or drop the pipe -- which is exactly what the circuit
    breaker and the fallback exist to survive."""
    from contextlib import asynccontextmanager

    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=command, args=args)

    @asynccontextmanager
    async def factory():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                yield session

    return factory


class CircuitBreaker:
    """Stop hammering a dead MCP server. After `threshold` consecutive
    failures the breaker OPENS and every call goes straight to the fallback
    for `cooldown_s`; then it HALF-OPENS and lets one probe through -- a
    success closes it, a failure re-opens it. Without this, a killed remote
    server would cost a fresh timeout on every single tool call.

    monotonic time is injected so the logic is testable without a real clock.
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 20.0, clock=None):
        import time

        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._clock = clock or time.monotonic
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True  # closed
        if self._clock() - self._opened_at >= self._cooldown_s:
            return True  # half-open: allow one probe
        return False  # open, still cooling down

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


class McpToolClient:
    """A synchronous handle to an MCP session running on a background loop."""

    def __init__(self, factory: SessionFactory, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._factory = factory
        self._timeout_s = timeout_s
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop = asyncio.Event()
        self._session: ClientSession | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="mcp-client", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=self._timeout_s + 5):
            raise TimeoutError("MCP client session did not start")
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        try:
            async with self._factory() as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the constructor
            self._error = exc
            self._ready.set()

    def _submit(self, coro: Awaitable) -> object:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self._timeout_s)
        except TimeoutError:
            # Don't leave the coroutine running on the loop after we give up.
            future.cancel()
            raise

    def _check_open(self) -> None:
        # Raise BEFORE the coroutine is built, so a dead client never leaves an
        # un-awaited coroutine behind — the caller just falls back. Checking the
        # thread catches a session that died abnormally (server crash) while the
        # loop object still looks open, which would otherwise stall every call
        # for the full timeout.
        if self._loop.is_closed() or self._session is None or not self._thread.is_alive():
            raise ConnectionError("MCP client is not available")

    def list_tools(self) -> list[str]:
        self._check_open()
        result = self._submit(self._session.list_tools())
        return [t.name for t in result.tools]

    def call_tool(self, name: str, args: dict) -> str:
        self._check_open()
        result = self._submit(self._session.call_tool(name, args))
        # An isError result is an MCP-layer artifact -- an unknown tool, or a
        # schema-validation failure -- NOT a tool answer: our tools delegate to
        # registry.execute, which never raises and always returns a plain
        # string. So treat isError as a transport failure and fall back to the
        # raw registry, which handles unknown names and bad args gracefully.
        if getattr(result, "isError", False):
            raise ConnectionError(f"MCP call errored: {name}")
        # A read-only tool always returns one text block.
        return result.content[0].text if result.content else ""

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=self._timeout_s + 5)
        # Only close the loop once the thread has really stopped, or loop.close()
        # would raise "Cannot close a running event loop".
        if not self._thread.is_alive():
            self._loop.close()


class ResilientToolRegistry:
    """A drop-in ToolRegistry that runs tool calls over MCP, and falls back to
    the raw registry on any transport failure.

    The agent loop is unchanged: it still calls get_definitions() and
    execute(). The LLM-facing tool schemas come from the raw registry, so the
    model sees exactly the same tools whichever transport serves the call.
    """

    def __init__(
        self,
        raw: ToolRegistry,
        client: McpToolClient | None,
        breaker: "CircuitBreaker | None" = None,
        shares_store: bool = True,
    ):
        self._raw = raw
        self._client = client
        self._breaker = breaker or CircuitBreaker()
        # The model is offered raw.get_definitions() -- every registered tool --
        # but the MCP server publishes only the read-only subset. A call to a
        # tool the server does not publish (recheck_against_contract) would come
        # back as an isError result, so route those straight to raw instead of
        # bouncing off MCP. Snapshot the published names once, while the client
        # is alive.
        self._mcp_tools: set[str] = set(client.list_tools()) if client is not None else set()
        # True for in-process MCP (same store); False for a remote server that
        # has its own store and cannot see this session's uploaded invoices.
        self.shares_store = shares_store
        self.transport_counts = {"mcp": 0, "fallback": 0}

    def get_definitions(self) -> list[dict]:
        return self._raw.get_definitions()

    def execute(self, name: str, args: dict) -> str:
        # The breaker skips MCP while a dead server cools down; the name check
        # skips it for tools the server does not publish. Both go straight to
        # the raw path, which is authoritative and always available.
        if self._client is not None and name in self._mcp_tools and self._breaker.allow():
            try:
                result = self._client.call_tool(name, args)
                self._breaker.record_success()
                self.transport_counts["mcp"] += 1
                return result
            except Exception:  # noqa: BLE001 — any transport failure degrades gracefully
                # Connection / timeout / protocol / isError path only. A real
                # tool "not found" is a normal string, handled by raw below.
                self._breaker.record_failure()
                if self.transport_counts["fallback"] == 0:
                    _log.warning("MCP tool call failed for %r; falling back to in-process", name)
        self.transport_counts["fallback"] += 1
        return self._raw.execute(name, args)


def in_process_resilient_registry(raw: ToolRegistry) -> ResilientToolRegistry:
    """Wrap a registry so the agent calls its tools over in-process MCP, with
    the raw registry as the fallback. Returns the raw registry unwrapped if mcp
    is not installed."""
    try:
        from apagent.mcp_server import build_mcp_server

        server = build_mcp_server(raw)
        client = McpToolClient(in_process_session_factory(server))
        atexit.register(client.close)  # no orphan thread/loop at process exit
        return ResilientToolRegistry(raw, client, shares_store=True)
    except Exception:  # noqa: BLE001 — no mcp, no problem: run in-process only
        return ResilientToolRegistry(raw, None)


def remote_resilient_registry(raw: ToolRegistry) -> ResilientToolRegistry:
    """Wrap a registry so the agent calls its tools over a SEPARATE MCP server
    process (`python -m apagent.mcp_server`), with the raw registry as the
    fallback and a circuit breaker in front. Falls back to in-process-only if
    the server cannot be started."""
    import sys

    try:
        factory = remote_stdio_session_factory(sys.executable, ["-m", "apagent.mcp_server"])
        client = McpToolClient(factory)
        atexit.register(client.close)  # terminate the server subprocess at exit
        # shares_store=False: the server has its own store, so the caller must
        # keep session-only invoices (uploads) on the in-process path.
        return ResilientToolRegistry(raw, client, shares_store=False)
    except Exception:  # noqa: BLE001 — no remote, degrade to in-process only
        return ResilientToolRegistry(raw, None)
