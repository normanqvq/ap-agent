"""Tests for the tool registry.

All tests run offline with no API keys or network calls.

Why we test error paths:
The registry promises that execute() never raises. If we don't test that promise,
we won't know if it's true until production. These tests prove the error handling
works before we trust it with real tools.
"""

from apagent.agent.registry import Tool, ToolRegistry


def test_register_and_execute():
    """Test basic tool registration and execution."""
    registry = ToolRegistry()

    def greet_handler(args: dict) -> str:
        name = args.get("name", "stranger")
        return f"Hello, {name}!"

    tool = Tool(
        name="greet",
        description="Greets someone by name",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        handler=greet_handler,
    )

    registry.register(tool)

    result = registry.execute("greet", {"name": "Alice"})
    assert result == "Hello, Alice!"


def test_unknown_tool_returns_error():
    """Test that calling a non-existent tool returns an error string, not a crash.

    Why this matters:
    The LLM might hallucinate a tool name. If execute() raises on unknown tools,
    the whole loop crashes. By returning an error string, the agent sees the error
    in the next round and can try again with a real tool.
    """
    registry = ToolRegistry()

    result = registry.execute("does_not_exist", {})

    # Should return an error message, not raise
    assert "Error" in result
    assert "does_not_exist" in result
    assert "not found" in result


def test_handler_exception_returns_error():
    """Test that a handler that raises returns an error string, not a crash.

    Why this matters:
    Handlers are written by domain experts who might not think about error handling.
    A database connection failure, a missing file, a division by zero — any of those
    could raise. If we don't catch exceptions, one broken tool kills the whole loop.

    By catching and returning an error string, the agent sees what went wrong and
    can decide whether to retry, call a different tool, or escalate.
    """
    registry = ToolRegistry()

    def broken_handler(args: dict) -> str:
        raise ValueError("Something went wrong")

    tool = Tool(
        name="broken",
        description="Always raises",
        input_schema={"type": "object", "properties": {}},
        handler=broken_handler,
    )

    registry.register(tool)

    result = registry.execute("broken", {})

    # Should return an error message with the exception details, not raise
    assert "Error" in result
    assert "ValueError" in result
    assert "Something went wrong" in result


def test_get_definitions():
    """Test that get_definitions returns tools in provider-neutral format."""
    registry = ToolRegistry()

    def dummy_handler(args: dict) -> str:
        return "ok"

    tool = Tool(
        name="test_tool",
        description="A test tool",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "number"}},
        },
        handler=dummy_handler,
    )

    registry.register(tool)

    definitions = registry.get_definitions()

    assert len(definitions) == 1
    assert definitions[0]["name"] == "test_tool"
    assert definitions[0]["description"] == "A test tool"
    assert definitions[0]["input_schema"]["type"] == "object"


def test_register_replaces_existing():
    """Test that registering a tool with the same name replaces the old one.

    Why we allow replacement:
    Makes testing easier (register a mock tool with the same name to override
    the real one). In production, registering the same name twice is probably
    a mistake, but crashing on it is worse than letting the last one win.
    """
    registry = ToolRegistry()

    def handler_v1(args: dict) -> str:
        return "version 1"

    def handler_v2(args: dict) -> str:
        return "version 2"

    tool_v1 = Tool(
        name="versioned",
        description="First version",
        input_schema={"type": "object", "properties": {}},
        handler=handler_v1,
    )

    tool_v2 = Tool(
        name="versioned",
        description="Second version",
        input_schema={"type": "object", "properties": {}},
        handler=handler_v2,
    )

    registry.register(tool_v1)
    result_v1 = registry.execute("versioned", {})
    assert result_v1 == "version 1"

    # Replace it
    registry.register(tool_v2)
    result_v2 = registry.execute("versioned", {})
    assert result_v2 == "version 2"
