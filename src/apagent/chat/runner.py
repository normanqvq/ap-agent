"""The background loop that joins a chat platform to the running service.

It runs INSIDE the web app's process, which is a design constraint rather
than a convenience. Service is a module-level singleton holding the
DocumentStore in memory, and chat-sourced receipts are session state that
never touches disk. A bot in a separate process would add its receipts to a
different store, and the console would sit there showing the invoice on hold
while the bot cheerfully replied that it had recorded the delivery.

Everything here is defensive because it is a daemon thread in a web server:
the loop never raises, a platform outage costs one empty poll, and a bad
message costs one skipped message. Nothing it can do should be able to take
down the page the reviewer is reading.

Starting is opt-in and silent: no TELEGRAM_BOT_TOKEN means no thread, and the
app behaves exactly as it did before this feature existed. That also keeps
the test suite offline without special-casing anything.
"""

import logging
import threading
import time

from apagent.chat.adapters import TelegramAdapter, redact_tokens_from_logs
from apagent.chat.harvest import ChatHarvester

# After an error, wait before trying again. Long polling already blocks for
# its timeout, so this only paces the failure path.
_BACKOFF_SECONDS = 5

log = logging.getLogger(__name__)


class ChatRunner:
    """Polls a platform and feeds mentions to a harvester."""

    def __init__(self, adapter, harvester: ChatHarvester, on_receipt=None) -> None:
        self.adapter = adapter
        self.harvester = harvester
        # Called with the invoice ids a recorded receipt affects, so the
        # service can re-decide them. Injected rather than imported: this
        # module must not depend on the API layer.
        self.on_receipt = on_receipt
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
                log.exception("chat poll failed; retrying")
                time.sleep(_BACKOFF_SECONDS)

    def tick(self) -> None:
        """One poll: buffer everything, act on mentions."""
        self.harvester.buffer.prune()
        for message in self.adapter.poll():
            self.harvester.observe(message)
            if not self.adapter.mentions_bot(message):
                continue
            log.info("mention from %s in %s: %r", message.sender_id, message.chat_id, message.text)
            result = self.harvester.on_mention(message)
            log.info(
                "replied: %r (receipt=%s)", result.reply, result.receipt and result.receipt.doc_id
            )
            if result.reply:
                self.adapter.reply(message.chat_id, result.reply)
            if result.receipt is not None and self.on_receipt is not None:
                self.on_receipt(result)


def start_if_configured(harvester: ChatHarvester, on_receipt=None) -> ChatRunner | None:
    """Start the poller in a daemon thread, or return None if unconfigured.

    Returning None rather than raising is the point: an install with no bot
    token is not broken, it just has no chat integration.
    """
    # Before anything can log a request URL — the token lives in it.
    redact_tokens_from_logs()
    adapter = TelegramAdapter()
    if not adapter.configured:
        return None
    runner = ChatRunner(adapter, harvester, on_receipt=on_receipt)
    log.info("chat poller starting for @%s", adapter.username or "?")
    threading.Thread(target=runner.run_forever, daemon=True, name="apagent-chat").start()
    return runner
