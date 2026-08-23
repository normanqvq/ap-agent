"""Read the ids Telegram will not tell you any other way, and check the trap.

Binding a group needs two numbers that only appear inside an update: the
group's chat id and each person's numeric user id. Neither is visible in the
Telegram UI, so the usual instruction is "call getUpdates and read the JSON",
which is fiddly and easy to get subtly wrong -- picking `from.username`
instead of `from.id`, say, which would key the roster on something the user
can change.

So this prints them, and prints a roster.json you can paste.

It also checks the thing that silently breaks this feature. A bot with
privacy mode ON receives only messages that @-mention it, so the buffer never
sees "200 brackets arrived" and extraction has nothing to read. Everything
looks configured, the bot answers, and every confirmation is refused for no
visible reason. getMe reports the flag, so we can just say so.

    .venv/Scripts/python.exe scripts/telegram_setup.py
"""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

API = "https://api.telegram.org"
ROOT = Path(__file__).resolve().parent.parent
ROSTER = ROOT / "data" / "chat" / "roster.json"


def call(token: str, method: str, **params) -> dict:
    try:
        return httpx.get(f"{API}/bot{token}/{method}", params=params, timeout=20).json()
    except Exception as exc:  # noqa: BLE001 - a setup script should say what broke
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env — nothing to check.")
        return

    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"Could not reach Telegram: {me.get('description')}")
        return
    bot = me["result"]
    print(f"\nBot            @{bot['username']}")

    # The trap, checked first because everything downstream depends on it.
    if bot.get("can_read_all_group_messages"):
        print("Privacy mode   OFF — the bot sees the whole conversation. Correct.")
    else:
        print("Privacy mode   ON  — THIS WILL BREAK THE FEATURE.")
        print("               The bot only receives messages that @-mention it, so the")
        print("               line that says what actually arrived never reaches it.")
        print("               Fix: BotFather -> /setprivacy -> this bot -> Disable,")
        print("               then REMOVE THE BOT FROM THE GROUP AND ADD IT AGAIN.")

    updates = call(token, "getUpdates", timeout=0)
    if not updates.get("ok"):
        print(f"\ngetUpdates failed: {updates.get('description')}")
        return

    chats: dict[str, str] = {}
    people: dict[str, str] = {}
    for update in updates.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("id") is not None and chat.get("type") in ("group", "supergroup"):
            chats[str(chat["id"])] = chat.get("title") or "group"
        if sender.get("id") is not None:
            name = " ".join(
                p for p in (sender.get("first_name"), sender.get("last_name")) if p
            ) or sender.get("username", "")
            people[str(sender["id"])] = name

    if not chats:
        print("\nNo group messages seen yet. Add the bot to a group and send a few")
        print("messages there — the bot only receives what is sent AFTER it joins.")
        return

    print("\nGroups")
    for chat_id, title in chats.items():
        print(f"  {chat_id:<18} {title}")
    print("\nPeople who spoke")
    for user_id, name in people.items():
        print(f"  {user_id:<18} {name}")

    roster = {
        "_README": json.loads(ROSTER.read_text(encoding="utf-8")).get("_README", [])
        if ROSTER.exists()
        else [],
        "bound_chats": {cid: {"label": title} for cid, title in chats.items()},
        "confirmers": {
            f"telegram:{uid}": {"employee_id": f"EMP-{i:03d}", "employee_label": name}
            for i, (uid, name) in enumerate(people.items(), start=1)
        },
    }
    print(f"\nSuggested {ROSTER.relative_to(ROOT)} — REVIEW BEFORE USING:\n")
    print(json.dumps(roster, indent=2, ensure_ascii=False))
    print("\nEveryone who spoke is listed as a confirmer, which is almost certainly")
    print("wrong. Delete anyone who is not a receiver — above all a supplier, who")
    print("would otherwise be confirming their own deliveries. Their messages are")
    print("still recorded as evidence; they just cannot release payment.\n")


if __name__ == "__main__":
    main()
