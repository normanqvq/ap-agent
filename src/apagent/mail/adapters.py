"""Talking to a mailbox. IMAP in, SMTP out.

Both are stdlib, both are the boring choice, and both are deliberately thin:
everything interesting (who may be written to, what a reply is worth, whether
anything is released) is transport-independent and lives elsewhere.

IMAP polling rather than IDLE for the same reason the chat adapter long-polls
rather than taking a webhook: nothing new has to be exposed. IDLE would hold
a socket open for efficiency we do not need at one mailbox and a handful of
messages.

UNSEEN as the queue, with a PEEK fetch so reading does not consume it. The
flag is then set explicitly once the message has been handled, which means a
crash mid-handling costs a re-read rather than a silently dropped reply.
"""

import imaplib
import logging
import os
import smtplib
from typing import Protocol

log = logging.getLogger(__name__)


def _password(name: str) -> str:
    """Read a credential, stripping whitespace.

    Gmail shows an app password as four space-separated groups and it is
    pasted that way more often than not. The failure is a login error that
    says only "invalid credentials", which sends people looking at the wrong
    thing for an hour.
    """
    return "".join(os.getenv(name, "").split())


class MailAdapter(Protocol):
    """What the runner needs from a mailbox."""

    def poll(self) -> list[tuple[bytes, bytes]]:
        """[(uid, raw)] for messages not yet handled, oldest first."""
        ...

    def mark_handled(self, uid: bytes) -> None: ...


class ImapAdapter:
    """Unread mail over IMAP, one connection per poll.

    Reconnecting each time rather than holding a session: a poller that keeps
    one connection for days has to handle every way a server can drop it, and
    the cost here is one TLS handshake a minute.
    """

    def __init__(self, host: str | None = None, user: str | None = None) -> None:
        # CLAUDE.md: keys come from the environment. The parameters exist so
        # tests can build one, never for normal use.
        self.host = host or os.getenv("IMAP_HOST", "")
        self.user = user or os.getenv("IMAP_USER", "")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and _password("IMAP_PASSWORD"))

    def poll(self) -> list[tuple[bytes, bytes]]:
        """[(uid, raw)] for unseen mail. Returns [] on any transport problem.

        Never raises, for the same reason TelegramAdapter.poll does not: this
        runs in a daemon thread inside the web process. But it logs, because
        a silently broken integration looks exactly like an idle one.
        """
        try:
            with imaplib.IMAP4_SSL(self.host) as imap:
                imap.login(self.user, _password("IMAP_PASSWORD"))
                imap.select("INBOX")
                status, data = imap.search(None, "UNSEEN")
                if status != "OK":
                    return []
                out = []
                for uid in data[0].split():
                    status, fetched = imap.fetch(uid, "(BODY.PEEK[])")
                    if status == "OK" and fetched and fetched[0]:
                        out.append((uid, fetched[0][1]))
                return out
        except Exception as exc:  # noqa: BLE001 - a poller must not die
            log.warning("imap poll failed: %s: %s", type(exc).__name__, exc)
            return []

    def mark_handled(self, uid: bytes) -> None:
        try:
            with imaplib.IMAP4_SSL(self.host) as imap:
                imap.login(self.user, _password("IMAP_PASSWORD"))
                imap.select("INBOX")
                imap.store(uid, "+FLAGS", "\\Seen")
        except Exception as exc:  # noqa: BLE001
            log.warning("imap flag failed: %s: %s", type(exc).__name__, exc)


class MailSender(Protocol):
    """What the dispatcher needs from a transport."""

    def send(self, message) -> bool:
        """True if the message left the building. Never raises."""
        ...


class SmtpSender:
    """Outbound over STARTTLS."""

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        self.host = host or os.getenv("SMTP_HOST", "")
        self.port = port or int(os.getenv("SMTP_PORT") or 587)

    @property
    def configured(self) -> bool:
        return bool(self.host and os.getenv("SMTP_USER") and _password("SMTP_PASSWORD"))

    def send(self, message) -> bool:
        """True if the server took it. Never raises, and says so out loud.

        The mirror of ImapAdapter.poll, and it was missing: this is called
        from the web app's startup and from a daemon thread, so an
        unreachable relay used to be a ConnectionRefusedError out of the
        lifespan and no console at all. A mail relay being down is an outage
        of the mail feature, never of the product.

        The caller needs the boolean rather than a log line: the dispatcher
        records a query only once it has actually gone out, or the chase
        timer would remind a vendor about mail they never received.
        """
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.starttls()
                server.login(os.getenv("SMTP_USER", ""), _password("SMTP_PASSWORD"))
                server.send_message(message)
            return True
        except Exception as exc:  # noqa: BLE001 - a send must not take the app down
            log.warning("smtp send failed: %s: %s", type(exc).__name__, exc)
            return False
