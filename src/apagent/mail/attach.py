"""Pulling a corrected invoice out of a reply, under limits.

Separate from inbound.py because the two have different jobs and different
risk. inbound.py builds the record a human reads and must never raise; this
decides what bytes get fed to the extraction path, which costs a model call
and ends in a document that can clear an invoice.

Every limit here is a refusal, not a repair. A reply carrying forty files is
not a correction, and neither is one carrying a 50 MB scan; the right answer
in both cases is to leave the evidence for a person rather than to try
harder.

The filename is the sender's choice and decides nothing. `%PDF` at the head
of the payload is checked instead -- not a full validation, but it is the
difference between "someone called it a pdf" and "it is one", and the
extraction layer fails closed on anything it cannot read anyway.
"""

import logging
from email import message_from_bytes

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # matches the console's upload limit
MAX_ATTACHMENTS = 3

log = logging.getLogger(__name__)


def pdf_attachments(raw: bytes) -> list[tuple[str, bytes]]:
    """[(filename, bytes)] for the PDFs in a message, oldest first.

    Returns [] rather than raising, for the same reason parse_mail does not:
    this runs inside the poller, and a message we cannot read must cost that
    message and nothing else.
    """
    try:
        message = message_from_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - a poller must not die
        log.warning("could not read a message for attachments: %s", type(exc).__name__)
        return []

    out: list[tuple[str, bytes]] = []
    for part in message.walk():
        if len(out) >= MAX_ATTACHMENTS:
            break
        filename = part.get_filename()
        if not filename:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001 - a broken part is not a broken message
            continue
        if not payload.startswith(b"%PDF"):
            continue
        if len(payload) > MAX_ATTACHMENT_BYTES:
            log.info("attachment %r is over the size limit; ignoring it", filename)
            continue
        out.append((filename, payload))
    return out
