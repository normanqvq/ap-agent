"""Raw RFC 822 bytes -> InboundMail, and telling an answer from a bounce.

Nothing here decides anything. It exists because the wire format is genuinely
awkward and every awkward part of it was observed on a single real reply:

- The subject came back RFC 2047 encoded in gb2312, with a localised reply
  prefix. decode_header hands back a mix of str and bytes with per-chunk
  charsets, any of which can be wrong or absent, so decoding falls back
  rather than raising: a subject is for a human to read, and a display string
  is never worth an exception in a poller.
- The body was multipart/alternative. We take text/plain and fall back to
  stripping the HTML, because the plain part is what a person typed and the
  HTML part is what their client made of it.

Classifying non-delivery is here rather than in harvest because it is a
property of the message, not of our records. Two signals, both cheap: the
Auto-Submitted header (RFC 3834 — anything but "no" means a machine sent it)
and the null-ish sender addresses every MTA uses for reports. Missing one is
not fatal: a bounce that slips through becomes evidence a human reads, which
is wrong but visible. Treating a real answer as a bounce would be worse — it
stops the timer and escalates on a vendor who did reply.
"""

import re
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from apagent.schemas import InboundMail

_DAEMONS = ("mailer-daemon@", "postmaster@")
_TAG_RE = re.compile(r"<[^>]+>")


def _decode(raw: str | None) -> str:
    """RFC 2047 -> str, never raising."""
    if not raw:
        return ""
    out = []
    for chunk, charset in decode_header(raw):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _body_text(message: Message) -> str:
    """The plain part, or the HTML part with its tags stripped."""
    plain, html = "", ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not html:
            html = text
    if plain:
        return plain
    return _TAG_RE.sub(" ", html).strip()


def parse_mail(raw: bytes) -> InboundMail:
    message = message_from_bytes(raw)
    try:
        received = parsedate_to_datetime(message.get("Date", "")).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        # A malformed Date is common and harmless; the poller's own clock is
        # a better answer than dropping the message.
        received = datetime.now().isoformat(timespec="seconds")
    _, from_addr = parseaddr(message.get("From", ""))
    return InboundMail(
        message_id=(message.get("Message-ID") or "").strip(),
        in_reply_to=(message.get("In-Reply-To") or "").strip() or None,
        references=(message.get("References") or "").split(),
        to_addrs=[
            a for _, a in getaddresses(message.get_all("To", []) + message.get_all("Cc", []))
        ],
        from_addr=from_addr.lower(),
        subject=_decode(message.get("Subject")),
        body_text=_body_text(message),
        received_at=received,
        auto_submitted=(message.get("Auto-Submitted") or "").strip() or None,
        attachments=[p.get_filename() for p in message.walk() if p.get_filename()],
    )


def is_non_delivery(mail: InboundMail) -> bool:
    """Whether this is a machine report rather than the vendor's position."""
    if mail.auto_submitted and mail.auto_submitted.lower() != "no":
        return True
    return any(mail.from_addr.startswith(d) for d in _DAEMONS)
