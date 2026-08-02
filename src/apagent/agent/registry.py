"""Tool registry for the agent.

This is the plugin system. Each domain (matching, rules, extraction) can register
its tools here without touching the loop code. The loop just asks for all tools
and executes by name.

Why execute never raises:
Tool failures are normal in this domain. "Invoice not found", "vendor not in system",
"GRN does not exist yet" are all expected cases, not bugs. They are evidence the
agent needs to reason about. If we crash the loop on a lookup miss, we treat data
problems as code problems, which is wrong.

The agent needs to see "not found" in the tool result and decide what to do:
maybe HOLD and wait for the GRN, maybe ESCALATE because the vendor is wrong.
Crashing takes away that choice.

Why we return error strings instead of raising:
The loop builds a list of tool calls with results. If execute() raises, the loop
has to catch, stringify, and store the error anyway. Simpler to just return it.
The agent sees the same thing either way (an error message), but our code is cleaner.
"""


class Tool:
    """One tool the agent can call.

    name: unique identifier (snake_case, no spaces)
    description: shown to the LLM. Should state what the tool does and when to use it.
    input_schema: JSON Schema dict, provider-neutral. The client converts to the
                  provider's format (Anthropic vs OpenAI).
    handler: function that takes a dict (the args) and returns a string (the result).
             The handler should never raise. If something goes wrong, return an
             error message as a string.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: callable,
    ):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


class ToolRegistry:
    """Registry of all tools the agent can use.

    Why a registry:
    Tools can be registered from different modules. The matching engine adds its
    tools, the rules layer adds its tools, extraction adds its tools. None of them
    need to know about each other. They just register and the loop sees everything.

    Other option: hard-code a list of tools in the loop. Works for 3 tools, breaks
    when you have 20 spread across 5 modules.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Add a tool to the registry.

        If a tool with the same name already exists, this replaces it.
        Why we allow replacement: makes testing easier (register a mock tool with
        the same name). In production, registering the same name twice is probably
        a bug, but crashing on it is worse than letting the last one win.
        """
        self._tools[tool.name] = tool

    def get_definitions(self) -> list[dict]:
        """Return tool definitions in provider-neutral format.

        The client.py module converts these to the provider's format
        (Anthropic vs OpenAI). The registry stays agnostic.

        Why we return dicts not Tool objects:
        The LLM API wants plain dicts. Returning Tool objects just means the
        caller has to unpack them.
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: dict) -> str:
        """Execute a tool by name and return the result as a string.

        This method never raises. If the tool is not found, or if the handler
        raises, we return an error message as a string.

        Why never raise:
        Tool failures are data, not bugs. "Invoice not found" is a legitimate
        answer the agent needs to see and reason about. Crashing the loop on a
        lookup miss would be wrong behavior.

        The agent might call a tool that doesn't exist because the LLM hallucinated
        a name. That should not kill the whole process. Return an error string, the
        agent sees it in the next round, and it can try again with a real tool.

        Why we catch all exceptions from the handler:
        Handlers are written by domain experts (matching team, rules team), not
        necessarily people who think about error handling. A missing database
        connection, a malformed invoice, a division by zero — any of those could
        raise. We catch everything so one broken tool doesn't kill the loop.

        The error message goes back to the agent. If it is a transient error
        (network timeout), the agent can retry. If it is permanent (invoice really
        does not exist), the agent can decide to escalate.
        """
        tool = self._tools.get(name)

        if tool is None:
            available = ", ".join(self._tools.keys())
            return f"Error: Tool '{name}' not found. Available tools: {available}"

        try:
            result = tool.handler(args)
            return result
        except Exception as e:
            # Why we include the exception type and message:
            # The agent can distinguish "file not found" from "network timeout"
            # and make different decisions. A generic "error" loses that signal.
            return f"Error executing {name}: {type(e).__name__}: {e}"
