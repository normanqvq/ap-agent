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
import time
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
                time.sleep(_BACKOFF_SECONDS)
            else:
                self._stop.wait(_POLL_SECONDS)

    def tick(self) -> None:
        """One poll: file every reply, then run the timers."""
        for uid, raw in self.adapter.poll():
            evidence = self.harvester.on_mail(parse_mail(raw))
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
                self.on_reply(evidence)
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
