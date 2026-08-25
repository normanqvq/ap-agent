"""Raw RFC 822 bytes -> InboundMail, and telling an answer from a bounce.

Nothing here decides anything. It exists because the wire format is genuinely
awkward and every awkward part of it was observed on a single real reply, or
on the ordinary spam that shares the same mailbox:

- The subject came back RFC 2047 encoded in gb2312, with a localised reply
  prefix. decode_header hands back a mix of str and bytes with per-chunk
  charsets, any of which can be wrong, absent, or unresolvable, so decoding
  falls back rather than raising. The concrete trap is "unknown-8bit": the
  pseudo-charset the stdlib reports for a raw 8-bit header that was never
  RFC 2047 encoded at all. It is not a codec, so codecs.lookup fails before
  errors="replace" ever gets a chance to run, and the same is true of any
  charset name a sender's client cares to write in a Content-Type. A subject
  or body is for a human to read; a display string is never worth an
  exception in a poller, because the poller re-reads the message that killed
  it on every subsequent poll, forever.
- The body was multipart/alternative. We take text/plain and fall back to
  stripping the HTML, because the plain part is what a person typed and the
  HTML part is what their client made of it. We only ever look at top-level
  parts: a message/rfc822 attachment (a forwarded email) is walked for its
  filename so it still shows up as an attachment, but never descended into
  for body text, because the vendor's own words belong at the top level and
  a forwarded message's contents are not them.

Classifying non-delivery is here rather than in harvest because it is a
property of the message, not of our records. Two signals, both cheap: the
Auto-Submitted header (RFC 3834 -- anything but "no" means a machine sent it)
and the null-ish sender addresses every MTA uses for reports. Missing one is
not fatal: a bounce that slips through becomes evidence a human reads, which
is wrong but visible. Treating a real answer as a bounce would be worse -- it
stops the timer and escalates on a vendor who did reply. The From header must
be decoded before parseaddr sees it for the same reason: parseaddr on a
still-encoded Header silently yields ('', ''), which would match neither a
real vendor address nor a daemon address and quietly launder a bounce into
"a reply we can't identify" instead of "a reply we know didn't arrive".
"""

import re
from datetime import datetime
from email import message_from_bytes
from email.errors import HeaderParseError
from email.header import Header, decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from apagent.schemas import InboundMail

_DAEMONS = ("mailer-daemon@", "postmaster@")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def _text(payload: bytes, charset: str | None) -> str:
    """Bytes -> str, whatever the sender claimed the charset was.

    The charset is attacker-controlled text: get_content_charset() hands
    back the header verbatim, and a raw 8-bit header is reported as the
    pseudo-charset "unknown-8bit", which is not a codec at all. Both raise
    LookupError before errors="replace" ever applies. Falling back to UTF-8
    costs a few mojibake characters in something a human reads; raising
    costs the entire intake, because the poller re-reads the message that
    killed it on every subsequent poll.
    """
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _decode(raw: str | Header | None) -> str:
    """RFC 2047 -> str, never raising."""
    if not raw:
        return ""
    raw = str(raw)
    try:
        chunks = decode_header(raw)
    except HeaderParseError:
        # A header claiming to be RFC 2047 (=?charset?B?...?=) but carrying
        # broken base64/quoted-printable inside it. str(raw) still gives a
        # human something to read, which is the point of this field.
        return raw
    out = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            out.append(_text(chunk, charset))
        else:
            out.append(chunk)
    return "".join(out)


def _iter_body_parts(message: Message):
    """Walk multipart structure, but never descend into a message/rfc822.

    message.walk() recurses into an attached forwarded message's internal
    parts too, which is right for attachments -- a file nested inside a
    forward is still an attachment -- but wrong for body text: it would let
    text from the forwarded message stand in for what the vendor actually
    typed at the top level.
    """
    yield message
    if message.get_content_type() == "message/rfc822":
        return
    if message.is_multipart():
        for sub in message.get_payload():
            yield from _iter_body_parts(sub)


def _body_text(message: Message) -> str:
    """The plain part, or the HTML part with its tags stripped.

    Top-level parts only, in the sense of _iter_body_parts above: an
    attached message/rfc822 (a forwarded email) contributes its filename to
    attachments but never its interior text to the body.
    """
    plain, html = "", ""
    plain_with_name, html_with_name = "", ""
    for part in _iter_body_parts(message):
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() == "message/rfc822":
            continue
        content_type = part.get_content_type()
        if not content_type.startswith("text/"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = _text(payload, part.get_content_charset())
        has_name = bool(part.get_filename())
        if content_type == "text/plain":
            if not has_name and not plain:
                plain = text
            elif has_name and not plain_with_name:
                plain_with_name = text
        elif content_type == "text/html":
            if not has_name and not html:
                html = text
            elif has_name and not html_with_name:
                html_with_name = text
    if plain:
        return plain
    if html:
        return _TAG_RE.sub(" ", _SCRIPT_STYLE_RE.sub("", html)).strip()
    # No filename-free text part was found at all. An empty body on a held
    # invoice tells the reviewer nothing was said, which is worse than a
    # slightly odd one recovered from a part that happened to carry a name.
    if plain_with_name:
        return plain_with_name
    if html_with_name:
        return _TAG_RE.sub(" ", _SCRIPT_STYLE_RE.sub("", html_with_name)).strip()
    return ""


def parse_mail(raw: bytes) -> InboundMail:
    message = message_from_bytes(raw)
    try:
        received = parsedate_to_datetime(message.get("Date", "")).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        # A malformed or missing Date is common and harmless; the poller's
        # own clock is a better answer than dropping the message.
        received = datetime.now().isoformat(timespec="seconds")
    # parseaddr must see a decoded header: on a still-RFC-2047-encoded (or
    # raw 8-bit) From, it silently returns ('', ''), which turns a bounce
    # from mailer-daemon@ into an unidentifiable sender instead of a
    # recognised non-delivery -- see the module docstring.
    _, from_addr = parseaddr(_decode(message.get("From")))
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
