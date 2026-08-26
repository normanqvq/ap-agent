"""Replay the whole vendor query loop with no network and no API key.

The counterpart of demo_chat_grn.py, and useful for the same two reasons: it
runs on a laptop with no credentials, and it shows the shape of the feature
in one screen without anyone having to read eight modules.

    .venv/Scripts/python.exe scripts/demo_email_intake.py
"""

import base64
import sys
from datetime import datetime, timedelta
from pathlib import Path

from apagent.agent.ap_tools import superseded_by
from apagent.mail.attach import pdf_attachments
from apagent.mail.chase import due_for_chase
from apagent.mail.directory import VendorDirectory
from apagent.mail.dispatch import MailDispatcher
from apagent.mail.harvest import MailHarvester
from apagent.mail.inbound import parse_mail
from apagent.mail.revise import make_revision
from apagent.mail.thread import ThreadRegistry
from apagent.schemas import ToleranceConfig
from apagent.store import DocumentStore

INVOICE = "INV-V005-3005"
VENDOR = "V005"
DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"

# Not a real PDF. Section 7 shows what code does with a corrected document;
# extraction itself needs a model and a real file, so these bytes only have
# to be recognisable as a PDF -- and the demo says so out loud rather than
# implying it read one.
_FAKE_PDF = b"%PDF-1.4 a corrected invoice would be here %%EOF"

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
    """Stands in for SMTP. Prints what would have gone out.

    Returns True, as a real sender does: nothing is recorded in the thread
    registry until the transport says the message left the building.
    """

    def send(self, message) -> bool:
        print(f"    -> {message['To']}  |  {message['Subject']}")
        return True


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
    print(f"    from vendor?   {fake.from_registered_sender}   <- evidence only, no automatic path")

    print("\n6. And one that only names the invoice in its subject:")
    stripped = REPLY_TEMPLATE.format(
        reply_to="ap@example.test", message_id="<unrelated@elsewhere.test>", invoice=INVOICE
    ).encode()
    correlated = harvester.on_mail(parse_mail(stripped))
    print(f"    correlated?    {correlated}   <- nothing to attach to")

    print("\n7. A corrected invoice, attached, re-matched on our terms:")
    with_pdf = (
        "From: AR Dept <ar-dept@pacific.example>\n"
        f"To: {query.reply_to}\n"
        "Subject: corrected\n"
        f"In-Reply-To: {query.message_id}\n"
        'Content-Type: multipart/mixed; boundary="B"\n'
        "\n--B\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\nHere is the corrected invoice.\n"
        "--B\n"
        "Content-Type: application/pdf\n"
        "Content-Transfer-Encoding: base64\n"
        'Content-Disposition: attachment; filename="corrected.pdf"\n'
        f"\n{base64.b64encode(_FAKE_PDF).decode()}\n"
        "--B--\n"
    ).encode()
    found = pdf_attachments(with_pdf)
    print(f"    attachment     {found[0][0]}, {len(found[0][1])} bytes, starts with %PDF")

    original = DocumentStore.from_dir(DATA).get_invoice(INVOICE)
    # What extraction WOULD return, written by hand: reading a real PDF needs
    # a model, and this script runs without one. Every field below is hostile
    # except the corrected price.
    extracted = original.model_copy(
        update={
            "doc_id": "WHATEVER-THE-VENDOR-PRINTED",
            "vendor_id": "V001",
            "vendor_name": "Someone Else Pte Ltd",
            "ref_doc_id": "PO-2026-1001",
            "currency": "EUR",
            "total_cents": 49000,
        }
    )
    revision = make_revision(original, extracted, sequence=1, evidence_id=evidence.evidence_id)
    print("    (the extracted document is hand-written here; extraction needs a model)")
    print(f"    doc_id         {revision.doc_id}   <- ours, not the number on their paper")
    print(f"    vendor         {revision.vendor_id}   <- ours, though the paper said V001")
    print(f"    purchase order {revision.ref_doc_id}   <- ours, though the paper said PO-2026-1001")
    # Currency belongs on this list for a different reason from the three
    # above: it is not identity, it is the unit every other figure is
    # counted in, and nothing in matching or tolerance reads it. A
    # correction at the exact ordered prices, relabelled EUR, would clear
    # every arithmetic check and ask for a different amount of money.
    print(f"    currency       {revision.currency}   <- ours, though the paper said EUR")
    print(f"    replaces       {revision.replaces}")
    print(f"    total          {original.total_cents} -> {revision.total_cents} cents   <- theirs")
    print("    the revision runs the same gates as any other invoice.")

    print("\n8. The vendor chases their own correction and sends it again:")
    second = make_revision(
        original, extracted, sequence=2, supersedes=revision.doc_id, evidence_id="MAIL-EV-0002"
    )
    store = DocumentStore.from_dir(DATA)
    store.add_invoice(revision)
    store.add_invoice(second)
    print(f"    {second.doc_id} replaces {second.replaces}   <- the newest, not the original")
    for doc in (original, revision, second):
        successor = superseded_by(doc, store)
        verdict = f"withdrawn by {successor.doc_id}" if successor else "the one that can be paid"
        print(f"    {doc.doc_id:<22} {verdict}")
    print("    one obligation, one payable document -- code refuses the rest.\n")


if __name__ == "__main__":
    main()
