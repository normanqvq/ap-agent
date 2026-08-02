"""Agent loop implementation.

This is the heart of the system. We hand-write the tool-calling loop instead of
using LangChain or any agent framework.

Why hand-written:
We need to explain every step to the hackathon judges. "The agent called this tool,
saw this result, then decided X because of Y" is the whole demo. If we wrap it in
a framework, we can't walk through the decision path line by line. Framework code
is a black box. This code is a glass box.

Other option: use LangChain. Faster to write, but we lose the ability to show
exactly how the agent reached its decision. For a production system with 50 tools
that might be fine. For a hackathon demo where the judges want to see the reasoning,
hand-written is better.

Why we track rounds:
Showing "the agent solved this in 1 round" vs "it took 4 rounds and multiple tool
calls" matters for the demo. Also helps debug when the agent gets stuck in a loop.

Why we force a decision at max_rounds:
Hanging is worse than escalating. If the agent never stops calling tools, the whole
queue blocks. Better to return ESCALATE with "hit round limit" than to hang forever.
A human can handle escalations. A stuck process is a production incident.
"""

import json

from apagent.llm.client import call_model
from apagent.schemas import Action, AgentDecision, ToolCall

from .registry import ToolRegistry


def run_agent(
    system_prompt: str,
    user_message: str,
    registry: ToolRegistry,
    invoice_id: str,
    max_rounds: int = 5,
) -> AgentDecision:
    """Run the agent loop until it returns a final decision.

    Args:
        system_prompt: instructions for the agent
        user_message: the initial task (usually includes the match result)
        registry: tool registry with all available tools
        invoice_id: which invoice we're deciding on
        max_rounds: maximum number of LLM calls before we force-stop

    Returns:
        AgentDecision with the action, reasoning, and full tool call history

    Why max_rounds exists:
    An agent can get stuck in a loop (calling the same tool over and over, or
    bouncing between two tools). Without a limit, the process hangs forever.
    With a limit, we force-stop and return ESCALATE. A human can look at the
    tool call history and see what went wrong.

    Why we return AgentDecision even on error:
    The caller needs a decision object to store, log, and report. Returning None
    or raising an exception means the caller has to handle a special case.
    Better to always return the same shape, with action=ESCALATE when we hit
    the limit or something goes wrong.
    """
    messages = [{"role": "user", "content": user_message}]
    tool_calls_history: list[ToolCall] = []
    round_num = 0

    tools = registry.get_definitions()

    for round_num in range(1, max_rounds + 1):
        # Why we remind the model which tools it already called:
        # LLMs can forget what they did in earlier rounds, especially if the
        # conversation is long. If we don't remind them, they might call the
        # same tool again with the same args, not because they want to retry,
        # but because they forgot they already did it.
        #
        # We don't need a fancy mechanism. Just show the history in the system
        # prompt or in a message. The model sees "I already called lookup_grn
        # and got 'not found'", so it knows not to try again.
        #
        # For now we rely on the conversation history (messages list) to carry
        # this context. Each tool call and result is appended as assistant and
        # user messages. If the model forgets anyway, we can add a more explicit
        # reminder later.

        response = call_model(
            messages=messages,
            tools=tools,
            system=system_prompt,
        )

        # If the model returned text with no tool calls, that's the final answer
        if not response["tool_calls"]:
            final_text = response["text"]
            if final_text is None:
                # Model returned nothing. This should not happen, but if it does,
                # treat it as an error and escalate.
                return AgentDecision(
                    invoice_id=invoice_id,
                    action=Action.ESCALATE,
                    hold_reason=None,
                    confidence=0.0,
                    reasoning="Model returned empty response with no tool calls",
                    tool_calls=tool_calls_history,
                    rounds_used=round_num,
                )

            # Parse the final answer into an AgentDecision
            decision = _parse_final_answer(final_text, invoice_id, tool_calls_history, round_num)
            return decision

        # Model wants to call tools
        # Build the assistant message with tool calls (needed for next round)
        assistant_message = {"role": "assistant", "content": response.get("text") or ""}

        # Anthropic and OpenAI both support tool_calls in messages, but the format
        # differs slightly. For simplicity, we store tool call info in our internal
        # format and let the client handle provider-specific serialization if needed.
        # In practice, we don't need to send tool_calls back to the model in the
        # assistant message for our use case. We just send the results as user messages.
        messages.append(assistant_message)

        # Execute each tool call and record the results
        tool_results_parts = []
        for tc in response["tool_calls"]:
            result_str = registry.execute(tc["name"], tc["args"])

            # Record in history
            tool_calls_history.append(
                ToolCall(
                    round=round_num,
                    tool_name=tc["name"],
                    args=tc["args"],
                    result=result_str,
                )
            )

            tool_results_parts.append(
                f"Tool: {tc['name']}\nArguments: {json.dumps(tc['args'])}\nResult: {result_str}"
            )

        # Send tool results back as a user message
        tool_results_message = {
            "role": "user",
            "content": "\n\n".join(tool_results_parts),
        }
        messages.append(tool_results_message)

    # If we get here, we hit max_rounds without a final answer
    # Force-return ESCALATE so we don't hang forever
    tool_names = [tc.tool_name for tc in tool_calls_history]
    reasoning = (
        f"Agent did not reach a decision after {max_rounds} rounds. "
        f"Tool call history: {tool_names}"
    )
    return AgentDecision(
        invoice_id=invoice_id,
        action=Action.ESCALATE,
        hold_reason=None,
        confidence=0.0,
        reasoning=reasoning,
        tool_calls=tool_calls_history,
        rounds_used=max_rounds,
    )


def _parse_final_answer(
    text: str,
    invoice_id: str,
    tool_calls_history: list[ToolCall],
    rounds_used: int,
) -> AgentDecision:
    """Parse the model's final answer into an AgentDecision.

    Why we ask for JSON:
    The agent needs to return structured data (action, hold_reason, confidence,
    reasoning). Free-form text is hard to parse reliably. JSON is easier.

    We tell the model in the system prompt to return a JSON object. It often wraps
    it in markdown code fences (```json ... ```), so we strip those before parsing.

    Why we have a fallback:
    LLMs sometimes ignore instructions and return free text anyway. Or they return
    almost-JSON with a typo. If parsing fails, we don't crash. We return ESCALATE
    with the raw text as reasoning. A human can read it and figure out what the
    agent meant.

    Other option: retry the LLM call with "please return valid JSON". Costs more,
    takes longer, and might still fail. Simpler to just escalate.
    """
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```json"):
        text = text[len("```json") :]
    if text.startswith("```"):
        text = text[len("```") :]
    if text.endswith("```"):
        text = text[: -len("```")]
    text = text.strip()

    try:
        data = json.loads(text)

        # Extract fields with defaults
        action_str = data.get("action", "ESCALATE")
        hold_reason_str = data.get("hold_reason")
        confidence = data.get("confidence", 0.5)
        reasoning = data.get("reasoning", "")

        # Convert strings to enums
        try:
            action = Action(action_str)
        except ValueError:
            action = Action.ESCALATE
            reasoning = f"Unknown action '{action_str}'. Original reasoning: {reasoning}"

        # hold_reason is optional, only set when action is HOLD
        hold_reason = None
        if hold_reason_str:
            from apagent.schemas import HoldReason

            try:
                hold_reason = HoldReason(hold_reason_str)
            except ValueError:
                # Invalid hold_reason, ignore it
                pass

        return AgentDecision(
            invoice_id=invoice_id,
            action=action,
            hold_reason=hold_reason,
            confidence=float(confidence),
            reasoning=reasoning,
            tool_calls=tool_calls_history,
            rounds_used=rounds_used,
        )

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # Parsing failed. Return ESCALATE with the raw text.
        return AgentDecision(
            invoice_id=invoice_id,
            action=Action.ESCALATE,
            hold_reason=None,
            confidence=0.0,
            reasoning=f"Failed to parse agent response. Error: {e}. Raw text: {text}",
            tool_calls=tool_calls_history,
            rounds_used=rounds_used,
        )
