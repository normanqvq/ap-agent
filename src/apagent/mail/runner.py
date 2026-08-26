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

    def __init__(self, adapter, harvester, dispatcher, on_reply=None, config=None) -> None:
        self.adapter = adapter
        self.harvester = harvester
        self.dispatcher = dispatcher
        # Called with each VendorReplyEvidence so the service can attach it
        # and re-decide. Injected, not imported: this module must not depend
        # on the API layer.
        self.on_reply = on_reply
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
        never gets mark_handled and is re-read (and re-fails) on every
        subsequent poll, forever -- and because it never reaches
        _run_timers, it takes the chase and escalation timers down with it,
        so silent-vendor escalation just stops.

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
                self.adapter.mark_handled(uid)
                continue
            # Flagged even when it correlates to nothing. Otherwise every
            # stray message in the mailbox is re-read on every poll, forever.
            self.adapter.mark_handled(uid)
            if evidence is None:
                continue
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
                        "could not handle the reply on %s; the reply is lost, "
                        "the tick is not",
                        evidence.invoice_id,
                    )
        self._run_timers()

    def _run_timers(self) -> None:
        if self.dispatcher is None or self.config is None:
            return
        now = datetime.now()
        for query in due_for_chase(self.harvester.registry, self.config, now):
            self.dispatcher.send_chase(query.invoice_id, self.harvester.vendor_of(query.invoice_id))
        for query in due_for_escalation(self.harvester.registry, self.config, now):
            query.escalated = True
            log.info("no reply on %s; escalating", query.invoice_id)


def start_if_configured(harvester, dispatcher, on_reply=None, config=None):
    """Start the poller in a daemon thread, or return None if unconfigured.

    Returning None rather than raising: an install with no mailbox is not
    broken, it simply has no email intake.
    """
    adapter = ImapAdapter()
    if not adapter.configured:
        return None
    runner = MailRunner(adapter, harvester, dispatcher, on_reply=on_reply, config=config)
    log.info("mail poller starting for %s", adapter.user)
    threading.Thread(target=runner.run_forever, daemon=True, name="apagent-mail").start()
    return runner
