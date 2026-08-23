"""The recent conversation in each bound group, so an @mention has context.

This exists because of a hard limit in Telegram's Bot API: a bot cannot fetch
history. There is no "give me the 30 messages before this one" call. A bot
sees a message once, at the moment it arrives, and only if it is in the group
already with privacy mode off. So context has to be kept as it streams past,
or it is gone.

That shapes the whole feature. "@bot confirm this" is useless on its own --
the useful sentence is usually a few messages earlier ("PO-2026-1019 arrived,
200 brackets, all good"), typed by someone who had no idea a bot would care.
The window around the mention is the actual input to extraction.

    Slack would not need this (conversations.history goes backwards), and a
    Telegram user-account client via MTProto could fetch history too. Both
    were rejected: Slack is not what the target SMEs use, and a user-account
    bot means logging in as a real person and reading every conversation that
    account can see, which is not a thing to ask a company for.

Retention is deliberately small. Privacy mode off means the bot receives every
message in a bound group, so this class holds the least it can get away with:
a bounded ring per chat, a time-to-live, nothing for chats we are not bound to,
and nothing on disk. Only the window around an actual mention is ever sent to
a model.
"""

from collections import deque
from datetime import UTC, datetime, timedelta

from apagent.schemas import ChatMessage

# Enough for the "someone said it three messages ago" case without keeping a
# day of chatter in memory. Both are per chat.
DEFAULT_CAPACITY = 200
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# The window handed to extraction. Asymmetric on purpose: the confirmation
# almost always PRECEDES the mention, and messages after it are usually
# replies to the bot rather than more delivery detail.
DEFAULT_BEFORE = 30
DEFAULT_AFTER = 3
DEFAULT_WITHIN_SECONDS = 6 * 60 * 60


def _parsed(stamp: str) -> datetime | None:
    """ISO string -> an aware UTC datetime, or None if it is unreadable.

    Always aware, because mixing the two kinds is a TypeError and the two
    kinds genuinely turn up here: Telegram stamps its messages with an
    offset ("...+00:00"), while hand-written fixtures and any platform that
    reports local time do not. Comparing a Telegram message against
    datetime.now() raised "can't subtract offset-naive and offset-aware
    datetimes" on every poll, which killed the poller before it ever
    fetched anything -- it retried forever, silently, doing nothing.

    A naive stamp is read as UTC. That is a guess, but it is the only one
    available and it is wrong by hours at worst, on a comparison whose
    window is measured in hours.

    Never raises: an unreadable timestamp should drop out of time filtering,
    not crash the bot mid-conversation.
    """
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _now() -> datetime:
    """Aware, to match _parsed. See its docstring for why that matters."""
    return datetime.now(UTC)


class MessageBuffer:
    """Recent messages per chat, bounded by both count and age."""

    def __init__(
        self, capacity: int = DEFAULT_CAPACITY, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> None:
        self._chats: dict[str, deque[ChatMessage]] = {}
        self._capacity = capacity
        self._ttl = timedelta(seconds=ttl_seconds)

    def add(self, message: ChatMessage) -> None:
        """Record one message. The deque's maxlen enforces the count bound."""
        chat = self._chats.setdefault(message.chat_id, deque(maxlen=self._capacity))
        chat.append(message)

    def messages(self, chat_id: str) -> list[ChatMessage]:
        return list(self._chats.get(chat_id, ()))

    def forget(self, chat_id: str) -> None:
        """Drop a chat entirely — used when a group is unbound."""
        self._chats.pop(chat_id, None)

    def prune(self, now: datetime | None = None) -> None:
        """Drop messages past the TTL. Called on each poll, so a quiet group
        does not sit on yesterday's conversation indefinitely."""
        # Normalise a caller-supplied `now` too, rather than requiring callers
        # to remember. Getting this wrong is silent until it is a TypeError in
        # a background thread.
        now = _now() if now is None else (now if now.tzinfo else now.replace(tzinfo=UTC))
        for chat_id, messages in list(self._chats.items()):
            kept = [m for m in messages if (p := _parsed(m.sent_at)) and now - p <= self._ttl]
            if kept:
                self._chats[chat_id] = deque(kept, maxlen=self._capacity)
            else:
                del self._chats[chat_id]

    def window(
        self,
        chat_id: str,
        around_message_id: str,
        before: int = DEFAULT_BEFORE,
        after: int = DEFAULT_AFTER,
        within_seconds: int = DEFAULT_WITHIN_SECONDS,
    ) -> list[ChatMessage]:
        """The conversation around one message, oldest first.

        Bounded by BOTH count and elapsed time, and the time bound is the one
        that matters. With a count bound alone, anyone who can post in the
        group can push the inconvenient part of the conversation out of the
        window -- twenty lines of chatter between "only 8 of the 10 arrived"
        and the confirmer's @mention, and extraction never sees the shortfall.
        Time-bounding does not make that free.

        Returns [] when the anchor message is not in the buffer, which is the
        honest answer: we have no context, so there is nothing to extract.
        """
        messages = list(self._chats.get(chat_id, ()))
        anchor = next(
            (i for i, m in enumerate(messages) if m.message_id == around_message_id), None
        )
        if anchor is None:
            return []

        window = messages[max(0, anchor - before) : anchor + 1 + after]
        anchored_at = _parsed(messages[anchor].sent_at)
        if anchored_at is None:
            return window
        span = timedelta(seconds=within_seconds)
        return [
            m for m in window if (p := _parsed(m.sent_at)) is None or abs(p - anchored_at) <= span
        ]
