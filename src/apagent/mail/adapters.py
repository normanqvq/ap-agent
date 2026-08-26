"""Talking to a mailbox. IMAP in, SMTP out.

Both are stdlib, both are the boring choice, and both are deliberately thin:
everything interesting (who may be written to, what a reply is worth, whether
anything is released) is transport-independent and lives elsewhere.

IMAP polling rather than IDLE for the same reason the chat adapter long-polls
rather than taking a webhook: nothing new has to be exposed. IDLE would hold
a socket open for efficiency we do not need at one mailbox and a handful of
messages.

THE QUEUE IS NOT THE UNREAD FLAG. That was the first design and it failed
twice on the first real mailbox. Once because UNSEEN there meant 4,581
messages, and fetching all of them took far longer than the poll interval,
so the reply at the top was never reached. Then, with a date window added,
because the person who owns the mailbox glanced at their inbox and opened
the reply -- which cleared the flag and silently stole the message from the
poller. A queue anyone can drain by looking at it is not a queue, and in a
live demo the one person guaranteed to be watching that inbox is us.

So the window is time, not flags: SINCE a date the process could have sent a
query on, capped per poll, with an in-memory record of which uids have
already been looked at so each poll advances instead of restarting. On this
mailbox that is 22 messages instead of 4,581, and it does not care what
anybody has read.

PEEK on the fetch, so looking does not consume. The seen flag is still set
afterwards, but ONLY for a message that correlated to one of our own queries
(see MailRunner.tick), and now it is a courtesy to whoever reads the mailbox
rather than load-bearing state. Marking a stranger's newsletter read because
it happened to sit in the same inbox is not ours to do.

Known edge, not closed: the examined record is per process, so a restart
re-examines the window. Harmless today -- the thread registry is in memory
too, so a re-examined reply correlates to nothing. If the registry is ever
persisted, the set of handled message-ids has to be persisted with it, or a
restart raises a second correction from one reply.

Real UIDs (imap.uid) rather than sequence numbers: sequence numbers shift
when anything is expunged, so a number cached between the search and the
flag-set can name a different message by the time it is used.
"""

import imaplib
import logging
import os
import re
import smtplib
from datetime import date, timedelta
from typing import Protocol

log = logging.getLogger(__name__)

# A vendor reply is a page of text and at most a few invoices. Anything past
# this is not one, and fetching it would pull the whole thing into the web
# process's memory before anything had a chance to refuse it. Asked for with
# RFC822.SIZE first, which costs one small round trip per message.
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
# How many unexamined messages one poll will look at. Bounds a tick on a busy
# mailbox; the rest are picked up by the next one, a minute later.
MAX_PER_POLL = 25
# How far back a reply could possibly be. A query sent by this process cannot
# be answered before this process started, so yesterday is already generous
# -- it exists only so a run that crosses midnight does not blind itself.
# This is the whole filter now, so it has to be a window nothing relevant
# falls outside of, and small enough that re-examining it costs nothing.
LOOKBACK_DAYS = 1


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
        # Uids this process has already handed to the runner. In memory, like
        # the thread registry it serves: a restart re-examines a handful of
        # recent messages, which is cheap and correlates to nothing.
        self._examined: set[bytes] = set()
        self._since = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and _password("IMAP_PASSWORD"))

    def poll(self) -> list[tuple[bytes, bytes]]:
        """[(uid, raw)] for recent mail this process has not examined yet.

        Read or unread: see the module docstring. Returns [] on any
        transport problem.

        Never raises, for the same reason TelegramAdapter.poll does not: this
        runs in a daemon thread inside the web process. But it logs, because
        a silently broken integration looks exactly like an idle one.
        """
        try:
            with imaplib.IMAP4_SSL(self.host) as imap:
                imap.login(self.user, _password("IMAP_PASSWORD"))
                imap.select("INBOX")
                status, data = imap.uid("SEARCH", None, "SINCE", self._since)
                if status != "OK":
                    return []
                fresh = [uid for uid in data[0].split() if uid not in self._examined]
                if len(fresh) > MAX_PER_POLL:
                    log.info("%s unexamined messages; taking %s", len(fresh), MAX_PER_POLL)
                    fresh = fresh[:MAX_PER_POLL]
                out = []
                for uid in fresh:
                    # Marked examined even when the fetch is skipped or fails:
                    # the point is that the next poll moves past it rather
                    # than re-reading the same message forever.
                    self._examined.add(uid)
                    if self._too_big(imap, uid):
                        continue
                    status, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                    if status == "OK" and fetched and fetched[0]:
                        out.append((uid, fetched[0][1]))
                return out
        except Exception as exc:  # noqa: BLE001 - a poller must not die
            log.warning("imap poll failed: %s: %s", type(exc).__name__, exc)
            return []

    def _too_big(self, imap, uid: bytes) -> bool:
        """True for a message we will not pull into memory.

        Left UNSEEN on purpose: it is not handled, and a person should find
        it in the mailbox rather than discover that the system silently ate
        it. A size the server will not tell us is not a reason to refuse.
        """
        status, data = imap.uid("FETCH", uid, "(RFC822.SIZE)")
        if status != "OK" or not data or not data[0]:
            return False
        line = data[0]
        if isinstance(line, tuple):  # some servers answer with a literal
            line = b" ".join(x for x in line if isinstance(x, bytes))
        found = re.search(rb"RFC822.SIZE\s+(\d+)", line)
        if found and int(found.group(1)) > MAX_MESSAGE_BYTES:
            log.warning("message uid=%s is %s bytes; leaving it unread", uid, found.group(1))
            return True
        return False

    def mark_handled(self, uid: bytes) -> None:
        """Flag one message read. Called only for mail that was ours.

        See the module docstring: the runner marks a message handled once it
        has correlated to a query we sent. Anything else in the mailbox
        belongs to whoever owns it, and its flags are left exactly as found.
        """
        try:
            with imaplib.IMAP4_SSL(self.host) as imap:
                imap.login(self.user, _password("IMAP_PASSWORD"))
                imap.select("INBOX")
                imap.uid("STORE", uid, "+FLAGS", "\\Seen")
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
