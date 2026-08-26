"""The background loop that joins a mailbox to the running service.

In the web app's process, and for the same reason ChatRunner is: Service is a
module-level singleton holding the store in memory, and a reply's evidence is
session state that never reaches disk. A poller in another process would file
its evidence into a different store while this console kept showing the
invoice untouched.

Starting is opt-in and silent. No IMAP configuration means no thread, and the
app behaves exactly as it did before this feature existed — which is also
what keeps the test suite offline without special-casing anything.
"""

import logging
import threading
from datetime import datetime

from apagent.mail.adapters import ImapAdapter
from apagent.mail.chase import due_for_chase, due_for_escalation
from apagent.mail.inbound import parse_mail

_POLL_SECONDS = 60
_BACKOFF_SECONDS = 30

log = logging.getLogger(__name__)


class MailRunner:
    """Polls a mailbox, files replies, and runs the silence timers."""

    def __init__(
        self, adapter, harvester, dispatcher, on_reply=None, config=None, on_silence=None
    ) -> None:
        self.adapter = adapter
        self.harvester = harvester
        self.dispatcher = dispatcher
        # Called with each VendorReplyEvidence so the service can attach it
        # and re-decide. Injected, not imported: this module must not depend
        # on the API layer.
        self.on_reply = on_reply
        # Called with the invoice id when a vendor has gone silent past the
        # escalation window. Injected for the same reason as on_reply, and
        # it is what makes "then it escalates to a human" mean anything:
        # without a caller, escalation set a flag two filters in this package
        # read and printed a log line, and a reviewer opening the invoice saw
        # exactly what they saw on day one.
        self.on_silence = on_silence
        self.config = config
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # A daemon thread that dies takes the feature down silently
                # for the rest of the process's life. Sleep and carry on --
                # but log it, because "carry on quietly" is how a broken
                # integration looks identical to an idle one.
                log.exception("mail poll failed; retrying")
                # wait(), not sleep(): stop() must be heard during a backoff
                # too, or shutting the app down hangs for up to 30 s.
                self._stop.wait(_BACKOFF_SECONDS)
            else:
                self._stop.wait(_POLL_SECONDS)

    def tick(self) -> None:
        """One poll: file every reply, then run the timers.

        self.adapter.poll() is left to propagate so that an adapter which
        does raise surfaces the outage; ImapAdapter is not one -- it logs and
        returns [] -- and run_forever backs off and retries either way.

        Everything below it is wrapped per-message: a message that fails to
        parse, to harvest, or to handle must cost exactly that message and
        not the whole tick. Without the wrapping, one unreadable message
        takes _run_timers down with it, so silent-vendor escalation just
        stops.

        Only a message that correlated to a query we sent is marked handled.
        The mailbox belongs to a person, and most of what is in it has
        nothing to do with us; the adapter remembers what it has examined,
        so nothing is re-read on every poll either way.

        on_reply is inside that wrapping and is the reason it matters. It is
        the call that runs the most code: extraction, then the whole
        pipeline, then the model. An expired API key or a provider outage
        raises there on every single reply, which is exactly the sustained
        failure that would otherwise stop the timers for the rest of the
        process's life.
        """
        for uid, raw in self.adapter.poll():
            try:
                evidence = self.harvester.on_mail(parse_mail(raw))
            except Exception:
                log.exception("could not parse/harvest mail uid=%s; skipping", uid)
                continue
            if evidence is None:
                # Not an answer to anything we sent, so not ours to touch.
                # This used to be flagged read regardless, on the argument
                # that otherwise a stray message is re-read forever -- true
                # of the flag, but the adapter now remembers which uids it
                # has examined, which does that job without reaching into
                # someone's mailbox and marking their unread mail read.
                continue
            # Ours: we sent the query it answers, so flagging it read is a
            # statement about our own work queue.
            self.adapter.mark_handled(uid)
            log.info(
                "reply on %s from %s (matched by %s)",
                evidence.invoice_id,
                evidence.from_addr,
                evidence.matched_by,
            )
            if self.on_reply is not None:
                try:
                    # The raw message goes too: a corrected invoice lives in
                    # an attachment, and re-parsing it here would mean this
                    # module knowing what an attachment is worth. It does not.
                    self.on_reply(evidence, raw)
                except Exception:
                    log.exception(
                        "could not handle the reply on %s; the reply is lost, the tick is not",
                        evidence.invoice_id,
                    )
        self._run_timers()

    def _run_timers(self) -> None:
        if self.dispatcher is None or self.config is None:
            return
        now = datetime.now()
        vendor_of = self.harvester.vendor_of
        for query in due_for_chase(self.harvester.registry, self.config, now, vendor_of):
            self.dispatcher.send_chase(query.invoice_id, vendor_of(query.invoice_id))
        for query in due_for_escalation(self.harvester.registry, self.config, now, vendor_of):
            query.escalated = True
            log.info("no reply on %s; escalating", query.invoice_id)
            if self.on_silence is not None:
                try:
                    self.on_silence(query.invoice_id)
                except Exception:
                    log.exception(
                        "could not escalate %s; the flag is set either way",
                        query.invoice_id,
                    )


def start_if_configured(harvester, dispatcher, on_reply=None, config=None, on_silence=None):
    """Start the poller in a daemon thread, or return None if unconfigured.

    Returning None rather than raising: an install with no mailbox is not
    broken, it simply has no email intake.
    """
    adapter = ImapAdapter()
    if not adapter.configured:
        return None
    runner = MailRunner(
        adapter,
        harvester,
        dispatcher,
        on_reply=on_reply,
        config=config,
        on_silence=on_silence,
    )
    log.info("mail poller starting for %s", adapter.user)
    threading.Thread(target=runner.run_forever, daemon=True, name="apagent-mail").start()
    return runner
