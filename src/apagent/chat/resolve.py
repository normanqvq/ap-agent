"""A model's reading of a conversation -> a goods receipt we will act on.

This is where the chat feature is made safe, and it is deliberately the
suspicious step. Everything above it is untrusted: the messages are written
by whoever is in the group, and the claim is a model's paraphrase of them.
Nothing here trusts either. The PO is looked up in OUR records, the items are
matched against OUR order lines, and the quantities are integers WE parsed.
If any of that fails, no receipt is created at all.

Refusing is the normal outcome, not an error path. "the stuff arrived" with
no reference is a perfectly reasonable thing for a colleague to type and an
impossible thing to act on, and the honest answer is to ask rather than to
guess which order it meant.

The sku rule is the subtle one, and getting it wrong would be worse than not
building the feature. matching.build_discrepancies indexes a receipt BY SKU,
and treats a receipt that exists but lacks a line's sku as ZERO RECEIVED
(engine.py:185). So a receipt assembled from free text, with descriptions but
no item codes, does not merely fail to help -- it manufactures a phantom
shortfall on every line and turns a clean invoice into a hold. Receipt lines
therefore only ever come from PO lines, copied whole, with the confirmed
quantity substituted. We never invent a line.
"""

import difflib
import re

from apagent.schemas import ChatMessage, DocType, Document, EvidenceSource, LineItem
from apagent.store import DocumentStore

# The same floor-plus-margin policy extraction/invoice.py uses to resolve a
# printed vendor name (invoice.py:149), and for the same reason: clearing a
# similarity floor is not enough when two candidates are close together.
# PO-2026-1019 lists both "Nitrile gloves size L" and "Trash bag 120L"; a
# message saying "gloves and bags came" must not coin-flip between them.
# Deliberately stricter than the matcher's PAIR_SIMILARITY_FLOOR of 0.4:
# that one pairs two structured documents describing the same order, this one
# reads a sentence somebody typed on a phone.
ITEM_SIMILARITY_FLOOR = 0.6
ITEM_AMBIGUITY_MARGIN = 0.1

_QTY_RE = re.compile(r"-?\d+")


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _score(probe: str, description: str) -> float:
    """How well a chat phrase identifies a PO line.

    Whole-string similarity alone is the wrong metric here, and measurably
    so: people type a FRAGMENT of what the order calls something. Against
    "Nitrile gloves size L, box of 100", the phrase "nitrile gloves" scores
    0.60 and "trash bag" scores 0.51 on SequenceMatcher.ratio(), because the
    ratio is penalised for every word the speaker did not bother to repeat.
    Both would have been rejected as unrecognisable.

    So the score is the better of two readings:

    - containment: what fraction of the speaker's words appear in the line.
      "gloves" inside "nitrile gloves size l box of 100" is a complete hit,
      which is what a human reading the message would say too.
    - sequence ratio: still useful for typos and for phrases that are not a
      clean subset ("glove" vs "gloves").

    Containment alone would make a bare "box" match anything with a box in
    it. That is handled by ITEM_AMBIGUITY_MARGIN rather than here: a word
    common to two lines scores identically for both, the margin collapses,
    and the item is refused instead of being guessed at.
    """
    target, candidate = _normalize(probe), _normalize(description)
    if not target or not candidate:
        return 0.0
    probe_words = set(target.split())
    line_words = set(candidate.split())
    containment = len(probe_words & line_words) / len(probe_words) if probe_words else 0.0
    return max(containment, difflib.SequenceMatcher(None, target, candidate).ratio())


def _parse_qty(printed: object) -> int | None:
    """The first integer in whatever the model copied out of the message.

    Deliberately narrow. "200", "200 pcs" and "200 pieces" all mean 200 here;
    "a couple of boxes" yields nothing and the caller refuses. Guessing a
    number for vague words is how a receipt ends up confirming quantities
    nobody stated.
    """
    if isinstance(printed, bool) or printed is None:
        return None
    if isinstance(printed, int):
        return printed if printed >= 0 else None
    match = _QTY_RE.search(str(printed))
    if match is None:
        return None
    if len(match.group().lstrip("-")) > 9:
        # Not a quantity anyone confirms in a chat; also keeps int() away
        # from Python's digit limit, which raises instead of overflowing.
        return None
    value = int(match.group())
    return value if value >= 0 else None


def _match_line(description: str, po: Document) -> LineItem | None:
    """The PO line a chat description refers to, or None if unsure."""
    target = _normalize(description)
    if not target:
        return None
    scored = sorted(
        ((_score(description, line.description), line) for line in po.lines),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored:
        return None
    best_score, best_line = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < ITEM_SIMILARITY_FLOOR or best_score - runner_up < ITEM_AMBIGUITY_MARGIN:
        return None
    return best_line


def _receipt_id(po_id: str, sequence: int) -> str:
    """A receipt id WE generate.

    Never derived from message text. The id is echoed into the UI and into
    tool results, and api/web/app.js interpolates a receipt id without
    escaping it (safe today precisely because ids are ours). A chat-derived
    id would put attacker text on that path.
    """
    tail = po_id.split("-")[-1] if "-" in po_id else po_id
    return f"GRN-CHAT-{tail}-{sequence}"


def resolve_grn(
    claim: dict,
    store: DocumentStore,
    messages: list[ChatMessage],
    confirmer_label: str | None,
    captured_at: str,
    evidence_id: str,
    sequence: int = 1,
) -> tuple[Document | None, str | None]:
    """Turn an extracted claim into a goods receipt, or refuse with a reason.

    Returns (receipt, None) or (None, reason). The reason is for a code
    template to turn into a reply -- it is never shown to the model and never
    sent verbatim to a chat group.
    """
    # `is True`, not truthiness: the string "false" is a confirmation under
    # `if claim.get(...)`, and the model is asked for a JSON boolean.
    if claim.get("is_delivery_confirmation") is not True:
        return None, "no_confirmation"

    po_ref = claim.get("po_reference")
    po = store.get_po(str(po_ref).strip()) if po_ref else None
    if po is None:
        # The single most common refusal, and the one worth being careful
        # about in the reply: naming the orders we were expecting would tell
        # anyone in the group which references the system responds to.
        return None, "no_po"

    items = claim.get("items") or []
    if not isinstance(items, list):
        return None, "unreadable_item"
    stated: dict[int, int] = {}
    skipped = False
    for item in items:
        if not isinstance(item, dict):
            return None, "unreadable_item"
        line = _match_line(str(item.get("description") or ""), po)
        if line is None:
            # An item we cannot tie to the order is the one thing we refuse
            # outright: it may be a different delivery entirely, and guessing
            # which line it meant is how a receipt confirms the wrong goods.
            return None, "unmatched_item"

        qty = _parse_qty(item.get("qty"))
        if qty is None:
            # No number given. Real confirmations mix states -- "the detergent
            # all came, gloves only 60, still waiting on the bags" -- so this
            # is resolved PER ITEM rather than failing the whole message.
            # An earlier version refused the lot, which threw away the two
            # lines the sender was perfectly clear about.
            complete = item.get("complete")
            if complete is None:
                complete = claim.get("everything_arrived")
            if not complete:
                # Say nothing about this line rather than guess. It ends up
                # absent from the receipt, which build_discrepancies reads as
                # zero received -- so the invoice holds on it, which is the
                # safe direction and exactly what "still waiting" means.
                skipped = True
                continue
            qty = line.qty
        stated[line.line_no] = stated.get(line.line_no, 0) + qty

    if not stated and skipped:
        # Every named item was too vague to record. Nothing was confirmed.
        return None, "no_quantity"

    if not stated:
        if not claim.get("everything_arrived"):
            # "The delivery came in" with no items and no completeness claim
            # says nothing about how much arrived.
            return None, "no_quantity"
        stated = {line.line_no: line.qty for line in po.lines}

    lines = [
        LineItem(
            line_no=line.line_no,
            sku=line.sku,  # copied from the PO, never invented — see module docstring
            description=line.description,
            qty=stated[line.line_no],
            uom=line.uom,
            unit_price_cents=None,  # a receipt records quantities, not prices
            line_total_cents=None,
        )
        for line in po.lines
        if line.line_no in stated
    ]

    receipt = Document(
        doc_id=_receipt_id(po.doc_id, sequence),
        doc_type=DocType.GRN,
        vendor_id=po.vendor_id,
        vendor_name=po.vendor_name,  # ours, from the PO — not a name typed in chat
        issue_date=captured_at[:10],
        ref_doc_id=po.doc_id,
        currency=po.currency,
        lines=lines,
        source=EvidenceSource.CHAT,
        source_ref=evidence_id,
        confirmed_by=confirmer_label,
        captured_at=captured_at,
    )
    return receipt, None
