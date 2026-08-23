"""The path one @mention takes, from message to recorded goods receipt.

Named harvest rather than service to avoid colliding with api/service.py --
they are different layers and reading a stack trace with two of them is
needlessly hard.

Order matters here, and it is cheapest-and-most-decisive first:

    1. bound chat?      -- else ignore entirely, no reply, no state
    2. buffer the window
    3. authorised sender? -- NOT a gate; it only decides how much the
                             resulting receipt is worth
    4. extract (the model, and the only untrusted step)
    5. resolve (code: our PO, our lines, our integers)
    6. record + re-decide the affected invoices

Step 3 sitting between the gates rather than among them is the whole design.
Refusing to look at a message from an unlisted sender would throw away the
most useful thing on a hold screen: what was actually said, and by whom. So
an unauthorised confirmation still becomes evidence -- it simply arrives with
confirmed_by=None, and pipeline.grn_gate will not release money on it alone.
"""

from datetime import datetime

from apagent.chat import templates
from apagent.chat.buffer import MessageBuffer
from apagent.chat.extract import ChatExtractionError, extract_delivery_claim
from apagent.chat.resolve import resolve_grn
from apagent.chat.roster import Roster
from apagent.schemas import ChatGrnEvidence, ChatMessage, Document
from apagent.store import DocumentStore


class HarvestResult:
    """What happened to one mention, for the caller to act on and log."""

    def __init__(
        self,
        reply: str | None,
        receipt: Document | None = None,
        evidence: ChatGrnEvidence | None = None,
        invoice_ids: list[str] | None = None,
    ) -> None:
        self.reply = reply
        self.receipt = receipt
        self.evidence = evidence
        self.invoice_ids = invoice_ids or []


class ChatHarvester:
    """Turns mentions into goods receipts. Holds the buffer and the roster.

    The store is passed in rather than loaded, so the API's singleton and a
    test's three-document store are the same code path -- the same reasoning
    that keeps DocumentStore out of module globals everywhere else.
    """

    def __init__(
        self,
        store: DocumentStore,
        roster: Roster | None = None,
        buffer: MessageBuffer | None = None,
        platform: str = "telegram",
    ) -> None:
        self.store = store
        self.roster = roster or Roster.from_file()
        self.buffer = buffer or MessageBuffer()
        self.platform = platform
        self._sequence = 0

    def observe(self, message: ChatMessage) -> None:
        """Record a message from a bound group.

        Messages from unbound groups are dropped here, before they are ever
        stored. Anyone can add a bot to a group; that must not be a way to
        get us to retain their conversation.
        """
        if self.roster.is_bound(message.chat_id):
            self.buffer.add(message)

    def on_mention(self, message: ChatMessage, provider: str | None = None) -> HarvestResult:
        """Handle an @mention. Returns the reply and anything recorded."""
        if not self.roster.is_bound(message.chat_id):
            return HarvestResult(templates.refusal("not_bound"))

        self.buffer.add(message)
        window = self.buffer.window(message.chat_id, message.message_id)
        if not window:
            window = [message]

        confirmer = self.roster.confirmer_label(self.platform, message.sender_id)
        captured_at = datetime.now().isoformat(timespec="seconds")
        self._sequence += 1
        evidence_id = f"CHAT-EV-{self._sequence:04d}"

        try:
            claim = extract_delivery_claim(window, provider=provider)
        except ChatExtractionError:
            # A model or parsing failure is OUR problem, not the group's, and
            # it must not read like a judgement on what they wrote.
            return HarvestResult("I could not read that just now. Please try again.")

        receipt, reason = resolve_grn(
            claim,
            self.store,
            window,
            confirmer_label=confirmer,
            captured_at=captured_at,
            evidence_id=evidence_id,
            sequence=self._sequence,
        )

        evidence = ChatGrnEvidence(
            evidence_id=evidence_id,
            platform=self.platform,
            chat_id=message.chat_id,
            po_id=receipt.ref_doc_id if receipt else None,
            confirmed_by=confirmer,
            captured_at=captured_at,
            messages=window,
            refusal_reason=reason,
        )
        if receipt is None:
            return HarvestResult(templates.refusal(reason or ""), evidence=evidence)

        self.store.add_grn(receipt)
        invoice_ids = [
            inv.doc_id
            for inv in self.store.invoices_for_vendor(receipt.vendor_id)
            if inv.ref_doc_id == receipt.ref_doc_id
        ]
        return HarvestResult(
            templates.recorded(receipt, authorised=confirmer is not None),
            receipt=receipt,
            evidence=evidence,
            invoice_ids=invoice_ids,
        )
