"""Talking to a chat platform. Telegram is implemented; the others are stubs.

The protocol is deliberately small -- receive messages, tell whether one is
addressed to us, send a reply -- because that is the entire surface the rest
of the package needs. Everything interesting (who may confirm, what counts as
a confirmation, whether it releases money) is platform-independent and lives
elsewhere.

WHY THESE PLATFORMS, honestly, because the differences are not cosmetic:

Telegram is implemented because its Bot API is free, official, and a bot can
sit in a group and read it. Two constraints shape everything: privacy mode
must be turned OFF via BotFather or the bot only sees messages that mention
it (losing the surrounding conversation, which is the useful part), and there
is no way to fetch history, which is why buffer.py exists.

WeCom (企业微信) is the right answer for a Chinese SME and has a real API, but
it pushes messages to a callback URL rather than letting a bot poll. That
needs a publicly reachable endpoint, an enterprise account, and message
encryption -- infrastructure, not code, so it is a stub rather than a
half-implementation.

Slack would actually be the easiest of the three: conversations.history
fetches backwards, so buffer.py would be unnecessary. It is a stub because
the target user does not use Slack, not because it is hard.

WeChat personal has no official API at all and is not stubbed. Every route to
it is a reverse-engineered client that risks the user's account. Recorded here
so the next person does not go looking.

WhatsApp is the one an SME in Singapore actually lives in, and the honest
answer is narrower than "impossible": GROUPS cannot be done, one-to-one can.
The Business Cloud API is built around a business number exchanging messages
with individuals, and group chats are simply not part of it -- so a bot cannot
sit in the delivery group and read it. But a receiver messaging the company's
own WhatsApp Business number to say the goods arrived is exactly the supported
shape, and the rest of this package works on it unchanged: buffer.py keeps the
thread, roster.py keys on the sender's WhatsApp id, nothing else cares.

What you give up is the surrounding group conversation, which is usually where
the useful sentence is. What you must also handle is the 24-hour rule: a
business may reply freely only within 24 hours of the person's last message,
and outside that window only with a pre-approved template. Since every reply
this package sends is already rendered by code from a fixed template
(templates.py), that is a registration exercise rather than a redesign.
"""

import os
from datetime import UTC, datetime
from typing import Protocol

from apagent.schemas import ChatMessage

TELEGRAM_API = "https://api.telegram.org"


class ChatAdapter(Protocol):
    """What the harvester needs from a platform."""

    platform: str

    def poll(self, timeout: int = 30) -> list[ChatMessage]:
        """New messages since the last call, oldest first."""
        ...

    def mentions_bot(self, message: ChatMessage) -> bool:
        """Whether this message is addressed to us."""
        ...

    def reply(self, chat_id: str, text: str) -> None:
        """Send a message back to a group."""
        ...


class TelegramAdapter:
    """Telegram Bot API over long polling.

    Long polling rather than a webhook, and not only for convenience: a
    webhook needs a publicly reachable HTTPS endpoint, which for this app
    would mean an unauthenticated route punched through the session
    middleware that guards every other /api path. getUpdates needs no inbound
    connectivity at all, so there is nothing new to expose.
    """

    platform = "telegram"

    def __init__(self, token: str | None = None, username: str | None = None) -> None:
        # CLAUDE.md: keys come from the environment, never a function argument
        # in normal use. The parameter exists so tests can build one.
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.username = (username or os.getenv("TELEGRAM_BOT_USERNAME", "")).lstrip("@")
        self._offset: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self.token}/{method}"

    def poll(self, timeout: int = 30) -> list[ChatMessage]:
        """Fetch new updates. Returns [] on any transport problem.

        Never raises: this runs in a background loop, and a network blip must
        not take down the process the web console is served from.
        """
        import httpx

        try:
            response = httpx.get(
                self._url("getUpdates"),
                params={"timeout": timeout, "offset": self._offset},
                timeout=timeout + 10,
            )
            payload = response.json()
        except Exception:
            return []
        if not payload.get("ok"):
            return []

        out = []
        for update in payload.get("result", []):
            self._offset = update["update_id"] + 1
            message = update.get("message") or update.get("channel_post")
            parsed = self._to_message(message)
            if parsed is not None:
                out.append(parsed)
        return out

    def _to_message(self, raw: dict | None) -> ChatMessage | None:
        """One Telegram update -> our model, or None if it carries no text.

        sender_id is str(from.id) -- the numeric id, which is the only field
        the roster is allowed to key on. A message with no text (a sticker, a
        photo, someone joining) is dropped: photos of delivery notes are a
        known gap, recorded in the plan, not silently half-handled.
        """
        if not raw:
            return None
        text = raw.get("text")
        sender = raw.get("from") or {}
        if not text or not raw.get("chat"):
            return None
        name = " ".join(
            part for part in (sender.get("first_name"), sender.get("last_name")) if part
        )
        sent = datetime.fromtimestamp(raw.get("date", 0), tz=UTC)
        return ChatMessage(
            message_id=str(raw.get("message_id")),
            chat_id=str(raw["chat"].get("id")),
            sender_id=str(sender.get("id", "")),
            sender_name=name or sender.get("username") or "unknown",
            text=text,
            sent_at=sent.isoformat(timespec="seconds"),
        )

    def mentions_bot(self, message: ChatMessage) -> bool:
        if not self.username:
            return False
        return f"@{self.username}".lower() in message.text.lower()

    def reply(self, chat_id: str, text: str) -> None:
        """Send a reply. Silent on failure, for the same reason poll is."""
        import httpx

        try:
            httpx.post(
                self._url("sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
        except Exception:
            return


class WeComAdapter:
    """企业微信 — an official API, but push-only. Not implemented.

    WeCom delivers group messages to a callback URL; there is no polling
    equivalent. Standing this up needs a verified enterprise account, a
    publicly reachable HTTPS endpoint, and AES message decryption with the
    token/EncodingAESKey pair. That is deployment work rather than code, so
    this stays a stub instead of a half-implementation that looks finished.
    """

    platform = "wecom"

    def poll(self, timeout: int = 30) -> list[ChatMessage]:
        raise NotImplementedError(
            "WeCom pushes to a callback URL and cannot be polled; it needs a "
            "public HTTPS endpoint and message decryption"
        )

    def mentions_bot(self, message: ChatMessage) -> bool:
        raise NotImplementedError

    def reply(self, chat_id: str, text: str) -> None:
        raise NotImplementedError


class WhatsAppAdapter:
    """WhatsApp Business Cloud API — one-to-one only. Not implemented.

    Viable for the platform an SME actually uses, with two caveats that shape
    any build: there are no group chats in the Cloud API, so the confirmation
    has to arrive in a direct thread with the company's business number rather
    than in the delivery group; and replies are restricted to a 24-hour window
    after the person's last message, outside which only pre-approved templates
    may be sent.

    Also push-based, like WeCom: Meta delivers messages to a webhook, so this
    needs a public HTTPS endpoint plus a verified business number. That is why
    it is a stub -- infrastructure, not logic. The rest of the package would
    work as-is, since roster.py keys on whatever numeric id the platform gives
    and templates.py already renders every outbound word from code.
    """

    platform = "whatsapp"

    def poll(self, timeout: int = 30) -> list[ChatMessage]:
        raise NotImplementedError(
            "WhatsApp Cloud API pushes to a webhook and cannot be polled; it also "
            "has no group chats, so confirmations must arrive in a direct thread"
        )

    def mentions_bot(self, message: ChatMessage) -> bool:
        raise NotImplementedError

    def reply(self, chat_id: str, text: str) -> None:
        raise NotImplementedError


class SlackAdapter:
    """Slack — technically the easiest of the three. Not implemented.

    conversations.history fetches backwards, so a Slack build would not need
    buffer.py at all: on an app_mention it could ask for the surrounding
    messages directly. This is a stub because the target user does not use
    Slack, not because it is difficult -- worth knowing if that changes.
    """

    platform = "slack"

    def poll(self, timeout: int = 30) -> list[ChatMessage]:
        raise NotImplementedError(
            "Slack needs Socket Mode or an Events API endpoint; on app_mention "
            "use conversations.history instead of a local buffer"
        )

    def mentions_bot(self, message: ChatMessage) -> bool:
        raise NotImplementedError

    def reply(self, chat_id: str, text: str) -> None:
        raise NotImplementedError
