# Intake: bringing documents in from email and Telegram

The pipeline decides an invoice the same way no matter how the PDF arrived —
dragged onto the console, pulled off an email, or exported from a chat. The
intake seam is the one place that variety is normalised, so the rest of the
system never learns a new decision path. This is the contract the email and
Telegram fetchers build against.

## The contract

A fetcher (the email poller, the Telegram exporter) does the channel-specific
work and hands the pipeline three things:

| field | meaning |
| --- | --- |
| `source` | `"email"` or `"telegram"` — the channel it arrived through |
| `filename` | the original attachment name (display + safe-id shaping) |
| `content` | the invoice PDF as bytes |

It calls either the HTTP endpoint or the service method:

```
POST /api/intake?source=email      # multipart body: file=<the PDF>
service.intake("email", filename, content)
```

Both return the full case bundle — the extracted invoice, the agent's
decision, the guardrail results — plus an `intake_source` field. That is the
whole contract. A fetcher that can produce `(source, filename, PDF bytes)` is
done; everything downstream is the existing upload path.

## One intake, one set of guardrails

`intake()` calls `upload_invoice()`: the same extraction, the same agent, the
same six code guardrails, and the same session-only handling — an intake
document never touches `data/synthetic/` or the committed decisions cache, just
like a manual upload. A new channel adds a provenance label, not a second
decision path that could quietly diverge from the one the demo exercises. If
email starts approving things the console would hold, that is a bug in one
place, not two.

## Why Telegram intake does not fight the chat bot

The console already runs a Telegram integration: `chat/runner.py` polls
`getUpdates` in a daemon thread to catch delivery confirmations. `getUpdates`
has exactly **one** consumer per bot token — a second poller on the same token
makes the two steal each other's updates, and the Bot API answers `409
Conflict`.

So the rule, stated once: **the bot token's `getUpdates` consumer is always
`chat/runner.py`, and only `chat/runner.py`.** The Telegram *intake* fetcher —
pulling invoice PDFs a supplier dropped into a chat — is a different job, and
by construction it does not touch `getUpdates`:

- Historical messages cannot come from `getUpdates` at all: the Bot API cannot
  look backwards, which is the whole reason `chat/buffer.py` keeps a ring
  buffer. Reading a chat's history needs the **client API** (a user session,
  e.g. Telethon) or an **exported file**. Neither opens a `getUpdates` consumer.
- If the fetcher ever wants live messages over the Bot API, it uses **its own
  bot token**, never this one.

A new callback that genuinely belongs on the existing bot (an inline approve
button, say) is added *inside* `runner.tick()`, on the one `getUpdates` stream —
never as a second poller.

## Working in parallel before the fetcher exists

The contract is small enough to mock. The fetcher side can develop against
`service.intake("email", name, pdf_bytes)` with any PDF, and this side is
already finished — the endpoint, the source tagging, and the session handling
are in place and tested (`tests/test_intake.py`). The real join is just the
fetcher calling `intake()`; until then both halves run, and demo, on their own.
