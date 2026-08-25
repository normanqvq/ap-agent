"""When silence stops being normal.

Pure functions over the registry, with `now` passed in rather than read. A
timer that reads the clock itself can only be tested by waiting, so it ends up
untested — and this is the part of the feature that decides an invoice goes to
a human, which is exactly the part that should not be.

Two windows rather than a retry loop. The first is a reminder, on the theory
that AP queries get buried rather than refused. The second is an admission
that email was the wrong channel for this vendor, and that a person should
pick up a phone. A third reminder would add no information.
"""

from datetime import datetime


def _hours_since(stamp: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        # An unparseable stamp is our own bug, not the vendor's. Treat it as
        # brand new: the cost is a late chase, where the alternative is
        # escalating an invoice because a string was malformed.
        return 0.0


def due_for_chase(registry, config, now: datetime) -> list:
    """Queries old enough to remind about, and not yet reminded."""
    return [
        query
        for query in registry.outstanding()
        if not query.chased_at
        and not query.escalated
        and _hours_since(query.sent_at, now) >= config.vendor_chase_after_hours
    ]


def due_for_escalation(registry, config, now: datetime) -> list:
    """Queries old enough to hand to a human, and not yet handed over."""
    return [
        query
        for query in registry.outstanding()
        if not query.escalated
        and _hours_since(query.sent_at, now) >= config.vendor_escalate_after_hours
    ]
