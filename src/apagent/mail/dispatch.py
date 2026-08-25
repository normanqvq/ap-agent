"""Build the query, refuse to send it twice, and hand it to the transport.

Everything a vendor reads is rendered here from a template, exactly as
chat/templates.py renders every word the bot says. The body text comes from
pipeline._render_outbound_message, which is already code-generated from our
own records — a caller cannot pass free text through this and put words in
the system's mouth.

Idempotency is a rail, not a nicety. The service re-decides invoices whenever
evidence changes, and each re-decision produces the same EMAIL action; without
a key on (invoice, body) a vendor gets one copy per re-decision, which is how
an automated system becomes a spammer.

Sending happens here rather than in pipeline.py because that module is pure
functions the offline test suite runs constantly — a send in there would mean
pytest mails vendors.
"""

import logging
from datetime import datetime
from email.message import EmailMessage

log = logging.getLogger(__name__)


class MailDispatcher:
    """Sends vendor queries. Owns the registry of what went out."""

    def __init__(self, directory, registry, sender, mail_from: str) -> None:
        self.directory = directory
        self.registry = registry
        self.sender = sender
        self.mail_from = mail_from
        self._sent_keys: set[tuple[str, str]] = set()

    def send_query(self, invoice_id: str, vendor_id: str, body: str):
        """Send one query. Returns the SentQuery, or None if we did not."""
        to = self.directory.address_for(vendor_id)
        if not to:
            # Not an error: a vendor with no registered address is a vendor
            # we do not automate with. The caller routes the invoice to a
            # human rather than guessing at an address off a document.
            log.info("no registered address for %s; not mailing %s", vendor_id, invoice_id)
            return None
        key = (invoice_id, body)
        if key in self._sent_keys:
            return self.registry.for_invoice(invoice_id)
        query = self.registry.register(invoice_id, self.mail_from)

        message = EmailMessage()
        message["From"] = self.mail_from
        message["To"] = to
        message["Subject"] = f"Query on invoice {invoice_id}"
        message["Message-ID"] = query.message_id
        message["Reply-To"] = query.reply_to
        message.set_content(body)

        self.sender.send(message)
        self._sent_keys.add(key)
        log.info("queried %s about %s", vendor_id, invoice_id)
        return query

    def send_chase(self, invoice_id: str, vendor_id: str):
        """One reminder, inside the original thread.

        Threading it rather than starting fresh so the vendor sees their own
        earlier context, and so a reply to the CHASE still carries the
        original Message-ID in References and correlates.
        """
        query = self.registry.for_invoice(invoice_id)
        to = self.directory.address_for(vendor_id)
        if query is None or not to or query.chased_at:
            return None
        message = EmailMessage()
        message["From"] = self.mail_from
        message["To"] = to
        message["Subject"] = f"Reminder: query on invoice {invoice_id}"
        message["In-Reply-To"] = query.message_id
        message["References"] = query.message_id
        message["Reply-To"] = query.reply_to
        message.set_content(
            f"We wrote about invoice {invoice_id} and have not had a reply. "
            "Please send a corrected invoice, or the agreed basis for the difference."
        )
        self.sender.send(message)
        query.chased_at = datetime.now().isoformat(timespec="seconds")
        return query
