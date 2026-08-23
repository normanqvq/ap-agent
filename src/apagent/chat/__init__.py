"""Capturing goods-receipt confirmations from a company chat group.

The layering mirrors extraction/: the model reads messy human text, code
does everything that must not be fuzzy, and nothing here can decide to pay
an invoice. The most this package can produce is a goods receipt marked
EvidenceSource.CHAT; whether that receipt is worth anything is decided by
pipeline.grn_gate, which this package does not import and cannot influence.

    adapters  -- talk to a platform (Telegram today)
    buffer    -- keep the recent conversation so an @mention has context
    roster    -- who is allowed to confirm, and in which groups
    extract   -- LLM: a window of messages -> ChatGrnEvidence
    resolve   -- code: evidence -> a validated goods receipt, or a refusal
    templates -- what the bot says back, rendered by code
    harvest   -- the orchestration that joins them
"""
