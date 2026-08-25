"""Replay the whole vendor query loop with no network and no API key.

The counterpart of demo_chat_grn.py, and useful for the same two reasons: it
runs on a laptop with no credentials, and it shows the shape of the feature
in one screen without anyone having to read eight modules.

    .venv/Scripts/python.exe scripts/demo_email_intake.py
"""

import sys
from datetime import datetime, timedelta

from apagent.mail.chase import due_for_chase
from apagent.mail.directory import VendorDirectory
from apagent.mail.dispatch import MailDispatcher
from apagent.mail.harvest import MailHarvester
from apagent.mail.inbound import parse_mail
from apagent.mail.thread import ThreadRegistry
from apagent.schemas import ToleranceConfig

INVOICE = "INV-V005-3005"
VENDOR = "V005"

REPLY_TEMPLATE = """\
From: AR Dept <ar-dept@pacific.example>
To: {reply_to}
Subject: =?gb2312?B?u9i4tDog?= Query on invoice {invoice}
Message-ID: <reply-1@pacific.example>
In-Reply-To: {message_id}
References: {message_id}
Date: Mon, 25 Aug 2026 10:00:00 +0800
Content-Type: text/plain; charset="utf-8"

You are right, the unit price was off an old price list.
A corrected invoice follows.
"""


class PrintingSender:
    """Stands in for SMTP. Prints what would have gone out."""

    def send(self, message):
        print(f"    -> {message['To']}  |  {message['Subject']}")


def _console_safe(text: str) -> str:
    """A decoded header, made safe for whatever codepage stdout has.

    The subject in the reply fixture is RFC 2047 encoded in gb2312 -- on
    purpose, because a demo that only ever prints ASCII subjects would hide
    exactly the decoding trap parse_mail() had to handle (see
    apagent/mail/inbound.py). The decoded text round-trips fine on a real
    Chinese Windows console (codepage cp936/GBK is a superset of gb2312),
    but this script may also run somewhere with a narrower stdout encoding
    (a CI log, a redirected pipe, an English-locale box), where the same
    print would raise UnicodeEncodeError and kill the demo. Falling back to
    an escaped form only when the console cannot show the real characters
    keeps the happy path showing real Chinese text.
    """
    encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode("ascii", "backslashreplace").decode()
    return text


def main() -> None:
    directory = VendorDirectory({VENDOR: {"email": "billing@pacific.example"}})
    registry = ThreadRegistry()
    dispatcher = MailDispatcher(directory, registry, PrintingSender(), "ap@example.test")
    harvester = MailHarvester(directory, registry, vendor_of=lambda _: VENDOR)

    print("\n1. The decision says EMAIL, so a query goes out by itself:")
    query = dispatcher.send_query(INVOICE, VENDOR, "Please send a corrected invoice.")
    print(f"    Message-ID {query.message_id}")
    print(f"    Reply-To   {query.reply_to}")

    print("\n2. The same decision again sends nothing (idempotency):")
    dispatcher.send_query(INVOICE, VENDOR, "Please send a corrected invoice.")

    print("\n3. Silence past the chase window earns exactly one reminder:")
    query.sent_at = (datetime.now() - timedelta(hours=80)).isoformat(timespec="seconds")
    for due in due_for_chase(registry, ToleranceConfig(), datetime.now()):
        dispatcher.send_chase(due.invoice_id, VENDOR)

    print("\n4. The vendor replies, and code ties it back:")
    raw = REPLY_TEMPLATE.format(
        reply_to=query.reply_to, message_id=query.message_id, invoice=INVOICE
    ).encode()
    evidence = harvester.on_mail(parse_mail(raw))
    print(f"    invoice        {evidence.invoice_id}")
    print(f"    matched by     {evidence.matched_by}")
    print(f"    from vendor?   {evidence.from_registered_sender}")
    print(f"    subject        {_console_safe(evidence.subject)}")

    print("\n5. The same reply, sent from a lookalike domain:")
    forged = raw.replace(b"ar-dept@pacific.example", b"ar-dept@pacific.example.attacker.test")
    fake = harvester.on_mail(parse_mail(forged))
    print(
        f"    from vendor?   {fake.from_registered_sender}   <- evidence only, no automatic path"
    )

    print("\n6. And one that only names the invoice in its subject:")
    stripped = REPLY_TEMPLATE.format(
        reply_to="ap@example.test", message_id="<unrelated@elsewhere.test>", invoice=INVOICE
    ).encode()
    correlated = harvester.on_mail(parse_mail(stripped))
    print(f"    correlated?    {correlated}   <- nothing to attach to\n")


if __name__ == "__main__":
    main()
