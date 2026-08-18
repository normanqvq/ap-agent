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
    corrected = False  # whether we already spent the one JSON-format retry

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
            if decision is not None:
                return decision

            # No JSON found in the answer. Live DeepSeek runs showed the model
            # sometimes writes a prose analysis and puts broken JSON (or none)
            # after it, even though the decision itself is right. One corrective
            # nudge fixes that at the cost of one round; only when the nudge
            # also fails do we escalate with the raw text for a human.
            # We don't nudge on the last round — no rounds left to answer in.
            if not corrected and round_num < max_rounds:
                corrected = True
                messages.append({"role": "assistant", "content": final_text, "tool_calls": []})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your reply was not a parseable JSON object. Reply again "
                            "with ONLY the JSON decision object — no prose before or "
                            "after it. Put all analysis inside the reasoning field."
                        ),
                    }
                )
                continue

            return AgentDecision(
                invoice_id=invoice_id,
                action=Action.ESCALATE,
                hold_reason=None,
                confidence=0.0,
                reasoning=f"Failed to parse agent response as JSON. Raw text: {final_text}",
                tool_calls=tool_calls_history,
                rounds_used=round_num,
            )

        # Model wants to call tools.
        # We append the assistant turn and the results in the client's internal
        # format (see llm/client.py module docstring). The client converts to
        # each provider's real tool protocol (tool_use/tool_result blocks for
        # Anthropic, role="tool" messages for OpenAI-compatible APIs).
        # Earlier we flattened results into plain user text. That ran, but the
        # model lost the structured link between a call and its result, which
        # hurts multi-tool rounds and is off-spec for every provider.
        messages.append(
            {
                "role": "assistant",
                "content": response.get("text"),
                "tool_calls": response["tool_calls"],
            }
        )

        # Execute each tool call and record the results
        tool_results = []
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

            tool_results.append({"id": tc["id"], "name": tc["name"], "result": result_str})

        messages.append({"role": "tool_results", "results": tool_results})

    # If we get here, we hit max_rounds without a final answer
    # Force-return ESCALATE so we don't hang forever
    tool_names = [tc.tool_name for tc in tool_calls_history]
    reasoning = (
        f"Agent did not reach a decision after {max_rounds} rounds. Tool call history: {tool_names}"
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


def _extract_decision_json(text: str) -> dict | None:
    """Find the decision JSON object inside the model's final text.

    The prompt says "reply with ONLY a JSON object", but live runs showed
    models (DeepSeek especially) often write a prose analysis first and put
    the JSON at the end, or wrap it in ```json fences. Requiring the whole
    text to be JSON turned three correct decisions into parse-failure
    escalations in one demo run.

    So instead of parsing the whole text, we scan it: try raw_decode at
    every '{' and keep the LAST object that carries an "action" key.
    That "action" check matters — the prose may contain other brace pairs,
    and an arbitrary JSON fragment is not a decision. Last, not first,
    because a model that writes a draft and then corrects itself puts the
    answer it means at the end — same convention as the final JSON after a
    prose analysis.

    Returns None when no such object exists (caller decides what that means).
    """
    # Strip markdown code fences if present — cheap, and keeps the common
    # fenced case from depending on the scan below.
    text = text.strip()
    if text.startswith("```json"):
        text = text[len("```json") :]
    if text.startswith("```"):
        text = text[len("```") :]
    if text.endswith("```"):
        text = text[: -len("```")]
    text = text.strip()

    decoder = json.JSONDecoder()
    found = None
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            found = obj
    return found


def _parse_final_answer(
    text: str,
    invoice_id: str,
    tool_calls_history: list[ToolCall],
    rounds_used: int,
) -> AgentDecision | None:
    """Parse the model's final answer into an AgentDecision.

    Why we ask for JSON:
    The agent needs to return structured data (action, hold_reason, confidence,
    reasoning). Free-form text is hard to parse reliably. JSON is easier.

    Returns None when no decision JSON can be found in the text. The loop
    then spends one corrective round asking for JSON-only output, and only
    escalates if that also fails — see run_agent. (We used to escalate
    immediately; live runs showed that threw away correct decisions over a
    formatting slip.)

    Bad VALUES inside a found object (unknown action, junk hold_reason) do
    not return None: they fall back to safe defaults, because a malformed
    enum is the model's decision problem, not a formatting problem — asking
    again would just get the same wrong value more tidily formatted.
    """
    data = _extract_decision_json(text)
    if data is None:
        return None

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

    # hold_reason only means something on a HOLD. Models occasionally attach
    # one to an APPROVE or ESCALATE; keeping it would make downstream
    # reports count phantom holds, so code drops it.
    hold_reason = None
    if hold_reason_str and action == Action.HOLD:
        from apagent.schemas import HoldReason

        try:
            hold_reason = HoldReason(hold_reason_str)
        except ValueError:
            # Invalid hold_reason, ignore it
            pass

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5

    return AgentDecision(
        invoice_id=invoice_id,
        action=action,
        hold_reason=hold_reason,
        confidence=confidence,
        reasoning=reasoning,
        tool_calls=tool_calls_history,
        rounds_used=rounds_used,
    )
