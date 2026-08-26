"""The vendor query loop: a question out, an answer back, evidence attached.

Layered like chat/, for the same reason — code does everything that must not
be fuzzy, and nothing in this package can decide to pay an invoice. The most
it can produce is a VendorReplyEvidence record hanging off an invoice.

    directory  -- who we may write to, and whose reply counts
    inbound    -- raw bytes -> InboundMail, and what is a bounce
    thread     -- what we sent, and tying a reply back to it
    dispatch   -- build and send, once
    adapters   -- IMAP and SMTP
    harvest    -- one inbound message, start to finish
    chase      -- the silence timers
    runner     -- the background loop that joins it to the service

Named mail rather than email because every module here imports the stdlib
`email` package, and a sibling with the same name is a trap for the next
reader even though absolute imports resolve it correctly.
"""
