"""Our evidence tools, exposed as an MCP server.

The agent's read-only lookup tools -- PO, goods receipt, vendor history,
duplicate check, contract search -- are wrapped as an MCP (Model Context
Protocol) server so any MCP client can call them: our own agent in-process,
a teammate's agent, or Claude Desktop over a socket.

Two deliberate boundaries:

- Only READ-ONLY tools are exposed. Nothing here moves money or mutates
  state; the worst a client can do is ask a question. The decision
  authority -- the guardrails, the money gate -- never becomes an MCP tool.
  The model, and any external client, can gather evidence but cannot decide.

- Every tool delegates to the SAME ToolRegistry the hand-written loop uses
  (registry.execute), so an MCP call returns byte-for-byte what an
  in-process call returns. The server is a transport over the existing
  tools, not a second copy of them. tests/test_mcp.py pins that equality.

`mcp` is an optional dependency (pip install -e ".[mcp]"); the core system
never imports this module.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from apagent.agent.registry import ToolRegistry

# The read-only evidence tools we publish. recheck_against_contract is left
# off on purpose: it is the guardrail's own helper, closer to authority than
# to evidence, and the guardrail must stay in code.
PUBLISHED_TOOLS = (
    "lookup_po",
    "lookup_grn",
    "get_vendor_history",
    "check_duplicate_invoice",
    "search_vendor_contract",
)


def build_mcp_server(registry: ToolRegistry, name: str = "AP-Agent-Tools") -> FastMCP:
    """Wrap the read-only registry tools as an MCP server.

    Each tool keeps a typed signature so the MCP schema is real, and its body
    is a one-line delegation to registry.execute — the single source of truth.
    """
    mcp = FastMCP(name)

    @mcp.tool()
    def lookup_po(po_id: str) -> str:
        """Fetch a purchase order by id (e.g. 'PO-2026-1005'), with its line
        items and prices in integer cents."""
        return registry.execute("lookup_po", {"po_id": po_id})

    @mcp.tool()
    def lookup_grn(po_id: str) -> str:
        """Fetch the goods receipt recorded against a PO id, or a clear note
        that none exists — meaning there is no proof of delivery."""
        return registry.execute("lookup_grn", {"po_id": po_id})

    @mcp.tool()
    def get_vendor_history(vendor_id: str) -> str:
        """List a vendor's invoices on record (dates, PO references, totals),
        by internal vendor id (e.g. 'V005')."""
        return registry.execute("get_vendor_history", {"vendor_id": vendor_id})

    @mcp.tool()
    def check_duplicate_invoice(invoice_id: str) -> str:
        """Check whether an invoice duplicates one already in the ledger. The
        comparison is computed in code; the reply carries the evidence."""
        return registry.execute("check_duplicate_invoice", {"invoice_id": invoice_id})

    @mcp.tool()
    def search_vendor_contract(query: str, vendor_id: str = "") -> str:
        """Search a vendor's contract for a clause (e.g. a price-variance
        allowance). Returns the matching sections with their source file. Omit
        vendor_id to search every contract."""
        # Match the raw schema, where only query is required and vendor_id is
        # optional -- so following the tool description over MCP answers
        # directly instead of tripping a validation error and falling back.
        args = {"query": query}
        if vendor_id:
            args["vendor_id"] = vendor_id
        return registry.execute("search_vendor_contract", args)

    return mcp


def build_default_server() -> FastMCP:
    """An MCP server over the committed synthetic dataset — for `python -m
    apagent.mcp_server` (remote/stdio) and for tests."""
    from apagent.agent.ap_tools import build_registry
    from apagent.store import DocumentStore

    data = Path(__file__).resolve().parent.parent.parent / "data" / "synthetic"
    store = DocumentStore.from_dir(data)
    registry = build_registry(store, data / "contracts")
    return build_mcp_server(registry)


if __name__ == "__main__":
    # Run as a standalone MCP server over stdio — this is the "remote"
    # transport a separate agent or Claude Desktop connects to.
    build_default_server().run()
