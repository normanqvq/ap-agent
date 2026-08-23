"""Serve the console with one chat confirmation already harvested.

There is no Telegram in this script. It fakes the platform adapter and stubs
the extraction model, so the whole path -- buffer, roster, resolve, record,
re-decide -- runs exactly as it does in production, and the console shows the
result. Useful for seeing the reviewer's side without a bot token, and for
rehearsing the demo offline.

    .venv/Scripts/python.exe scripts/demo_chat_grn.py            # supplier confirms (held)
    .venv/Scripts/python.exe scripts/demo_chat_grn.py --authorised   # receiver confirms (released)

Then open http://127.0.0.1:8000 and look at INV-V006-3019 -- the dataset's own
"PO exists, the warehouse confirmed by phone, nobody typed a receipt" case.

The agent itself is stubbed to APPROVE everything, which is deliberate: every
hold you see on screen was produced by a code guardrail overruling the model,
not by the model behaving.
"""

import json
import re
import sys

import uvicorn
from dotenv import load_dotenv

# Stub the model BEFORE the app imports anything that captures the reference.
import apagent.agent.loop as agent_loop
import apagent.chat.harvest as harvest_module

CHAT_ID = "-1001234567890"
RECEIVER_ID = "88888888"  # on the roster below
SUPPLIER_ID = "55555555"  # in the group, but not a receiver

# What the model reads out of the conversation. Mixed on purpose: two items
# confirmed, one still outstanding -- the shape a real message actually takes.
CLAIM = {
    "is_delivery_confirmation": True,
    "po_reference": "PO-2026-1019",
    "items": [
        {"description": "detergent", "qty": None, "complete": True},
        {"description": "nitrile gloves", "qty": "100", "complete": True},
        {"description": "trash bag", "qty": None, "complete": False},
    ],
    "everything_arrived": False,
}


def _stub_model() -> None:
    agent_loop.call_model = lambda messages, tools, system, provider=None: {
        "text": json.dumps(
            {
                "action": "APPROVE",
                "hold_reason": None,
                "confidence": 0.95,
                "reasoning": "Everything looks fine to me.",
            }
        ),
        "tool_calls": [],
    }
    harvest_module.extract_delivery_claim = lambda window, provider=None: CLAIM


class FakeAdapter:
    """One conversation, delivered once."""

    platform = "telegram"

    def __init__(self, messages):
        self._pending = messages
        self.sent = []

    def poll(self, timeout=30):
        out, self._pending = self._pending, []
        return out

    def mentions_bot(self, message):
        return "@apbot" in message.text

    def reply(self, chat_id, text):
        self.sent.append(text)


def seed(authorised: bool) -> None:
    from apagent.api.service import get_service
    from apagent.chat.roster import Roster
    from apagent.chat.runner import ChatRunner
    from apagent.schemas import ChatMessage

    service = get_service()
    # Never rewrite the committed decisions cache from a demo run.
    service._save_cache = lambda: None  # noqa: SLF001
    harvester = service.chat_harvester()
    harvester.roster = Roster(
        {CHAT_ID: "Ops / deliveries"},
        {f"telegram:{RECEIVER_ID}": "Li Wei (warehouse)"},
    )

    sender_id, sender_name = (
        (RECEIVER_ID, "Li Wei") if authorised else (SUPPLIER_ID, "CleanPro Sales")
    )
    # Written the way a delivery group actually reads: the useful sentence is
    # not the one addressed to the bot, and the delivery is partial.
    lines = (
        [
            "PO-2026-1019 came in this afternoon",
            "detergent all here, 100 boxes of gloves too. bags short, rest tomorrow",
            "@apbot confirm the receipt please",
        ]
        if authorised
        else [
            "delivered to your office already, please arrange payment",
            "@apbot confirm receipt",
        ]
    )
    messages = [
        ChatMessage(
            message_id=str(i),
            chat_id=CHAT_ID,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            sent_at=f"2026-08-12T14:{30 + i:02d}:00",
        )
        for i, text in enumerate(lines)
    ]

    adapter = FakeAdapter(messages)
    ChatRunner(adapter, harvester, on_receipt=service.on_chat_receipt).tick()

    case = service.get_case("INV-V006-3019")
    print(f"\n  chat group   {'Li Wei (warehouse)' if authorised else 'CleanPro Sales (supplier)'}")
    for line in lines:
        print(f"               {line}")
    print(f"\n  bot replied  {adapter.sent[0] if adapter.sent else '(nothing)'}")
    print(f"\n  INV-V006-3019 is now {case['decision']['action']}")
    if not authorised:
        print("  the sender is not a receiver, so this is evidence for a human")
        print("  -> open the invoice and use 'Accept the chat confirmation'")
    print("\n  http://127.0.0.1:8000  ->  INV-V006-3019\n")


def _keyword_extractor(window, provider=None):
    """A crude stand-in for the extraction model, for running against real
    Telegram without an LLM key.

    It is deliberately dumb: a PO pattern, a few item words, and numbers next
    to them. That is all. The point is not to replace the model but to leave
    everything AROUND it real -- the messages really arrive from Telegram, the
    roster really decides who may confirm, resolve.py really matches against
    the purchase order, and the guardrails really judge the result. Only the
    "read how people actually write" step is faked, and it shows: reorder the
    words and it will miss things a model would catch.

    Lives in the demo script, not in the package, so nobody mistakes it for a
    fallback the product ships with.
    """
    text = " ".join(m.text for m in window).lower()
    po = re.search(r"po-\d{4}-\d{4}", text)
    # Take the PO reference out before hunting for quantities, or the year in
    # PO-2026-1019 gets read as "2026 detergent". A real model does not make
    # this mistake, which is rather the point of using one.
    text = text.replace(po.group(), " ") if po else text
    items = []
    for word, aliases in (
        ("detergent", ("detergent",)),
        ("nitrile gloves", ("glove", "gloves")),
        ("trash bag", ("bag", "bags")),
    ):
        hit = next((a for a in aliases if a in text), None)
        if not hit:
            continue
        # The number must come BEFORE the item, which is how English states a
        # quantity: "100 boxes of gloves". Allowing it after as well read
        # "detergent all here, 100 boxes of gloves" as 100 detergent -- the
        # kind of mistake this whole approach is prone to, and the reason the
        # product uses a model instead.
        before = re.search(rf"(\d+)[^.]{{0,12}}?{hit}", text)
        qty = before.group(1) if before else None
        complete = None
        if qty is None:
            # "detergent all here" states completeness without a number.
            complete = bool(re.search(rf"{hit}[^.]{{0,6}}\ball\b", text))
        items.append({"description": word, "qty": qty, "complete": complete})
    return {
        "is_delivery_confirmation": bool(po) and bool(items),
        "po_reference": po.group().upper() if po else None,
        "items": items,
        "everything_arrived": "all" in text and "short" not in text,
        "notes": None,
    }


def live_telegram() -> None:
    """Serve the console with the REAL Telegram poller attached."""
    import apagent.chat.harvest as harvest

    harvest.extract_delivery_claim = _keyword_extractor
    print("\n  Live Telegram mode.")
    print("  Real: messages, roster, PO matching, guardrails, the invoice flip.")
    print("  Faked: only the extraction model (no LLM key set) — see _keyword_extractor.\n")
    print("  Message the bot, then watch http://127.0.0.1:8000 -> INV-V006-3019\n")
    uvicorn.run("apagent.api.app:app", host="127.0.0.1", port=8000, log_level="warning")


def main() -> None:
    load_dotenv()
    _stub_model()
    if "--telegram" in sys.argv:
        live_telegram()
        return
    seed(authorised="--authorised" in sys.argv)
    uvicorn.run("apagent.api.app:app", host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
