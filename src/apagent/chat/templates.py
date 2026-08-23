"""What the bot says back, rendered by code from fixed templates.

Same rule as pipeline._render_outbound_message, and stricter than it sounds:
it is not enough that a template produced the string. Every slot must come
from our own records or be a value we computed. So a reply may name the PO id
as fetched from OUR store (not as typed in chat), the vendor name from the
vendor directory, a receipt id we generated, and integers we parsed. It may
never echo the confirmer's words, their display name, or an item description
somebody wrote in the group.

The reason is that a chat group is a room full of people. Text the bot emits
is text the system appears to vouch for, and quoting a message back is how a
message gets laundered into an official-looking statement.

Refusals are deliberately vague about our records. An earlier draft named the
purchase orders we were expecting, which is a neat way of telling anyone in
the group -- including the supplier, who is often in it -- exactly which
references and item names the system responds to.
"""

from apagent.schemas import Document

# reason code -> what the group is told. Reason codes come from resolve.py.
_REFUSALS = {
    "no_confirmation": (
        "I did not read that as a delivery confirmation, so I have not recorded anything."
    ),
    "no_po": (
        "I could not tell which purchase order that delivery was for. "
        "Please mention the PO number and confirm again."
    ),
    "unmatched_item": (
        "I could not match one of the items to that order, so I have not recorded anything. "
        "Please mention the PO number and confirm again."
    ),
    "unreadable_item": (
        "I could not read the items in that confirmation, so I have not recorded anything."
    ),
    "no_quantity": (
        "I could not tell how much arrived. Please say the quantity received, "
        "or confirm that the delivery was complete."
    ),
    "not_bound": "I am not set up to record deliveries in this group.",
}

_FALLBACK = "I could not record that confirmation."


def refusal(reason: str) -> str:
    """The reply when no receipt was created."""
    return _REFUSALS.get(reason, _FALLBACK)


def recorded(receipt: Document, authorised: bool) -> str:
    """The reply when a receipt WAS created.

    It states plainly whether this counts on its own. A bot that answered
    "recorded!" identically in both cases would leave someone believing an
    invoice was released when it is actually sitting in a reviewer's queue --
    and the person best placed to correct a wrong confirmation is the one who
    just typed it, while they still remember the delivery.
    """
    items = ", ".join(f"{line.qty} x {line.sku or line.line_no}" for line in receipt.lines)
    head = f"Recorded against {receipt.ref_doc_id} ({receipt.vendor_name}): {items}."
    if authorised:
        return f"{head} This is on file as proof of delivery."
    return f"{head} A reviewer still needs to accept it before the invoice can be paid."
