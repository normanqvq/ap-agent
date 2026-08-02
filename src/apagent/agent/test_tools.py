"""Test tools for validating the agent runtime.

These are not real AP tools. They exist to test the machinery (registry, loop,
error handling) without needing real databases or APIs.

Why we include a deliberately broken tool:
We must prove error handling works before we trust it with real tools. If a handler
raises and we don't catch it properly, the whole loop crashes. Better to discover
that in a unit test than in the demo.

Why these tools are simple:
They test one thing each: echo tests basic execution, add tests argument parsing,
broken tests error handling. Real tools will be more complex (database lookups,
matching logic), but the registry and loop don't care about that complexity.
If these three work, the real ones will work too.
"""

from .registry import Tool, ToolRegistry


def echo_handler(args: dict) -> str:
    """Return the input message unchanged."""
    message = args.get("message", "")
    return f"Echo: {message}"


def add_handler(args: dict) -> str:
    """Add two numbers and return the sum."""
    a = args.get("a", 0)
    b = args.get("b", 0)
    return f"Sum: {a + b}"


def broken_handler(args: dict) -> str:
    """Always raises an exception.

    Why this exists:
    We need to test that execute() catches exceptions from handlers and returns
    an error string instead of crashing. If we don't test the error path, we
    won't know if it works until a real tool breaks in production.
    """
    raise RuntimeError("This tool is intentionally broken")


def register_test_tools(registry: ToolRegistry) -> None:
    """Register all test tools into the given registry.

    Why a registration function:
    Tests can call this once to get a registry with all test tools, instead of
    manually registering each one. Less boilerplate in test code.
    """
    echo_tool = Tool(
        name="echo",
        description="Returns the input message unchanged. Use this to test basic tool execution.",
        input_schema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to echo back",
                }
            },
            "required": ["message"],
        },
        handler=echo_handler,
    )

    add_tool = Tool(
        name="add",
        description="Adds two numbers and returns the sum. Use this to test argument parsing.",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
        handler=add_handler,
    )

    broken_tool = Tool(
        name="broken",
        description="Always raises an exception. Use this to test error handling.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=broken_handler,
    )

    registry.register(echo_tool)
    registry.register(add_tool)
    registry.register(broken_tool)
