"""The path one reply takes, from inbox to evidence on an invoice.

Named harvest to match chat/harvest.py -- the two do the same job for
different channels, and calling this one "service" would collide with
api/service.py in every stack trace.

Order matters, cheapest and most decisive first:

    1. correlate  -- no tie to a query we sent? drop it, keep no state
    2. bounce?    -- a machine report ends the thread rather than answering it
    3. sender     -- NOT a gate; it decides how much the reply is worth
    4. evidence   -- record it, verbatim, for a human

Step 3 sitting outside the gates is the same decision chat/harvest.py makes
about an unlisted confirmer. Refusing to record a reply from an unexpected
address would throw away the most useful thing on a hold screen: what was
actually said. So it becomes evidence with from_registered_sender False, and
nothing automatic acts on it.

Dropping an uncorrelated message in step 1 is not politeness, it is the
inbox's only defence: this mailbox receives whatever the internet sends it,
and a message nobody can tie to a query we made is not about us.
"""

from apagent.mail.inbound import is_non_delivery
from apagent.schemas import InboundMail, VendorReplyEvidence

# A vendor's mail system can bolt a long legal disclaimer onto every reply.
# The evidence card exists for a person to read, and this is session state
# held in memory, so the body is kept only as far as anyone would read.
_BODY_LIMIT = 4000


class MailHarvester:
    """Turns inbound mail into evidence. Holds the directory and registry."""

    def __init__(self, directory, registry, vendor_of) -> None:
        self.directory = directory
        self.registry = registry
        # invoice_id -> vendor_id, injected rather than imported: this module
        # must not depend on the store or the API layer.
        self.vendor_of = vendor_of
        self._sequence = 0

    def on_mail(self, mail: InboundMail) -> VendorReplyEvidence | None:
        correlated = self.registry.correlate(mail)
        if correlated is None:
            return None
        invoice_id, matched_by = correlated
        query = self.registry.for_invoice(invoice_id)

        bounce = is_non_delivery(mail)
        registered = False
        if not bounce:
            vendor_id = self.vendor_of(invoice_id)
            registered = bool(vendor_id) and self.directory.is_registered_sender(
                vendor_id, mail.from_addr
            )

        if bounce:
            # The vendor never saw it. Waiting out the chase timer on a
            # message that was never delivered would be a week of silence
            # we already know the answer to.
            query.escalated = True
        elif registered:
            query.answered = True

        self._sequence += 1
        return VendorReplyEvidence(
            evidence_id=f"MAIL-EV-{self._sequence:04d}",
            invoice_id=invoice_id,
            from_addr=mail.from_addr,
            subject=mail.subject,
            received_at=mail.received_at,
            body_text=mail.body_text[:_BODY_LIMIT],
            matched_by=matched_by,
            from_registered_sender=registered,
            attachments=mail.attachments,
            is_non_delivery=bounce,
        )
