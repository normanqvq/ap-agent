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

from apagent.rules.tolerance import resolve_config


def _hours_since(stamp: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        # An unparseable stamp is our own bug, not the vendor's. Treat it as
        # brand new: the cost is a late chase, where the alternative is
        # escalating an invoice because a string was malformed.
        return 0.0


def _config_for(query, config, vendor_of):
    """This vendor's windows, not just the defaults.

    Both windows sit in ToleranceConfig alongside every other limit, and
    ToleranceConfig carries per_vendor_overrides -- so a vendor who is known
    to answer in a day, or one who never answers at all, can have their own.
    These two functions read the base config and ignored that, which made
    the override silently a lie for the only two fields in the model that
    are about time rather than money.

    vendor_of is optional so a caller with no vendor lookup (a test over the
    registry alone) still gets the defaults rather than an error.
    """
    if vendor_of is None:
        return config
    return resolve_config(vendor_of(query.invoice_id), config)


def due_for_chase(registry, config, now: datetime, vendor_of=None) -> list:
    """Queries old enough to remind about, and not yet reminded."""
    return [
        query
        for query in registry.outstanding()
        if not query.chased_at
        and not query.escalated
        and _hours_since(query.sent_at, now)
        >= _config_for(query, config, vendor_of).vendor_chase_after_hours
    ]


def due_for_escalation(registry, config, now: datetime, vendor_of=None) -> list:
    """Queries old enough to hand to a human, and not yet handed over."""
    return [
        query
        for query in registry.outstanding()
        if not query.escalated
        and _hours_since(query.sent_at, now)
        >= _config_for(query, config, vendor_of).vendor_escalate_after_hours
    ]
