"""Three-way matching: pair an invoice with its PO and GRN, list what differs.

This module computes FACTS, not judgments. It says "line 1 unit price is
8.0% above the PO"; whether that is acceptable is the rules layer's call
(tolerances) and the agent's call (context). Keeping judgment out of here
means every number the agent later reasons about is deterministic and
reproducible from the documents alone.

Discrepancies leave this module with within_tolerance=False on every row.
That is not a verdict — it is "not yet checked". The rules layer re-emits
them with the flag actually computed against ToleranceConfig. Splitting it
this way keeps one owner per concern: matching owns arithmetic, rules owns
policy.
"""

import difflib

from scipy.optimize import linear_sum_assignment

from apagent.schemas import (
    Discrepancy,
    DiscrepancyField,
    Document,
    EvidenceSource,
    LineItem,
    MatchResult,
)

# Below this description similarity two lines are not the same item, even if
# the assignment algorithm would love to pair them. Without a floor, an
# invoice line that exists on no PO (the over-billing case) would get force-
# paired with whatever PO line is least dissimilar — hiding exactly the
# defect we most need to surface.
PAIR_SIMILARITY_FLOOR = 0.4

# Fallback PO search: how far the invoice's line subtotal may sit from a
# candidate PO's subtotal and still count as "probably the same order".
FALLBACK_TOTAL_PCT = 10.0


def _subtotal(doc: Document) -> int:
    return sum(line.line_total_cents or 0 for line in doc.lines)


def find_po(invoice: Document, pos: list[Document]) -> tuple[Document | None, str]:
    """Locate the PO an invoice bills against. Returns (po, how).

    'how' is evidence quality, which the agent needs as much as the PO
    itself: "ref" (the invoice named it — strong), "search" (we guessed by
    vendor + amount — weak), "none".
    """
    if invoice.ref_doc_id:
        for po in pos:
            if po.doc_id == invoice.ref_doc_id:
                # A ref is only trusted when the PO belongs to the same
                # vendor. PO ids are one shared sequence across vendors, so
                # a one-digit extraction slip (or a hostile ref) lands on
                # ANOTHER vendor's order — full "ref" confidence on that
                # match would be confidently wrong. Fall through to the
                # vendor-scoped search instead.
                if po.vendor_id == invoice.vendor_id:
                    return po, "ref"
                break
        # A named PO that does not exist (or belongs to another vendor) is
        # itself suspicious; fall through to the search so the agent still
        # gets a candidate to compare.

    candidates = [po for po in pos if po.vendor_id == invoice.vendor_id]
    inv_subtotal = _subtotal(invoice)
    best, best_gap = None, None
    for po in candidates:
        gap = abs(_subtotal(po) - inv_subtotal)
        if best_gap is None or gap < best_gap:
            best, best_gap = po, gap
    if best is not None and inv_subtotal > 0:
        gap_pct = (best_gap / inv_subtotal) * 100
        if gap_pct <= FALLBACK_TOTAL_PCT:
            return best, "search"
    return None, "none"


def find_grn(po: Document | None, grns: list[Document]) -> Document | None:
    if po is None:
        return None
    for grn in grns:
        if grn.ref_doc_id == po.doc_id:
            return grn
    return None


def _similarity(a: LineItem, b: LineItem) -> float:
    return difflib.SequenceMatcher(None, a.description.lower(), b.description.lower()).ratio()


def pair_lines(
    po_lines: list[LineItem], inv_lines: list[LineItem]
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Pair PO lines with invoice lines. Returns (pairs, unmatched_po, unmatched_inv).

    Two passes:
    1. Exact SKU match — free and unambiguous when both sides print codes.
    2. Hungarian assignment on description similarity for the rest. Small
       vendors often print no SKU, and greedy nearest-neighbour pairing can
       chain-steal (line A takes line B's best match, forcing B into a bad
       pair). The Hungarian algorithm finds the globally best assignment;
       scipy ships it, so it costs us an import.

    Pairs below PAIR_SIMILARITY_FLOOR are broken up — see the constant.
    """
    pairs: list[tuple[int, int]] = []
    po_left = {line.line_no: line for line in po_lines}
    inv_left = {line.line_no: line for line in inv_lines}

    # Pass 1: SKU
    for inv_no, inv_line in list(inv_left.items()):
        if not inv_line.sku:
            continue
        for po_no, po_line in list(po_left.items()):
            if po_line.sku == inv_line.sku:
                pairs.append((po_no, inv_no))
                del po_left[po_no]
                del inv_left[inv_no]
                break

    # Pass 2: description similarity via Hungarian assignment
    if po_left and inv_left:
        po_nos = list(po_left)
        inv_nos = list(inv_left)
        # cost = 1 - similarity, because the solver minimizes
        cost = [[1.0 - _similarity(po_left[p], inv_left[i]) for i in inv_nos] for p in po_nos]
        row_idx, col_idx = linear_sum_assignment(cost)
        # The floor is applied AFTER the global-optimal assignment, so a pair
        # the optimizer picked but that falls below the floor is dropped, not
        # re-assigned. That errs toward "unmatched" — the safe direction here,
        # since an unmatched invoice line trips the no-unordered-lines
        # guardrail rather than being waved through as a confident pairing.
        for r, c in zip(row_idx, col_idx, strict=True):
            if 1.0 - cost[r][c] >= PAIR_SIMILARITY_FLOOR:
                pairs.append((po_nos[r], inv_nos[c]))
                del po_left[po_nos[r]]
                del inv_left[inv_nos[c]]

    return sorted(pairs), sorted(po_left), sorted(inv_left)


def _pct(delta: int, base: int) -> float | None:
    """Delta as percentage points of base. None when base is zero —
    a made-up percentage against zero would poison tolerance checks.
    abs(base) because a negative base (credit-note line) would make the
    percentage negative and sail under every `<= limit` check."""
    if base == 0:
        return None
    # Multiply first: 7 * 100 / 100 is exactly 7.0, while 7 / 100 * 100 is
    # 7.000000000000001 and fails a `<= 7` tolerance it plainly meets.
    return abs(delta) * 100 / abs(base)


def _pair_sku_less_receipt_lines(po: Document, grn: Document) -> dict[int, int]:
    """Received quantity per SKU-less PO line, paired by DESCRIPTION.

    Line numbers are not a key: a receipt typed in a different order than
    the purchase order is the normal case, and keying on line_no held a
    fully delivered order. Two passes over the receipt lines that carry no
    SKU the order knows: an exact match on the normalised description takes
    every such line (a split delivery is two lines that read the same), then
    the best remaining line above the pairing floor. Each receipt line is
    used once, so a "M8 x 60mm" receipt cannot also stand in for the "M8 x
    40mm" line that never arrived -- the exact pass claims it for the line
    it names first.
    """
    order_skus = {line.sku for line in po.lines if line.sku}
    pool = [g for g in grn.lines if not g.sku or g.sku not in order_skus]
    norm = lambda text: " ".join((text or "").lower().split())  # noqa: E731
    received: dict[int, int] = {}
    used: set[int] = set()
    targets = [line for line in po.lines if not line.sku]
    for po_line in targets:
        for i, g in enumerate(pool):
            if (
                i not in used
                and norm(g.description)
                and norm(g.description) == norm(po_line.description)
            ):
                received[po_line.line_no] = received.get(po_line.line_no, 0) + g.qty
                used.add(i)
    for po_line in targets:
        if po_line.line_no in received:
            continue
        best, best_score = None, PAIR_SIMILARITY_FLOOR
        for i, g in enumerate(pool):
            if i in used:
                continue
            score = _similarity(po_line, g)
            if score >= best_score:
                best, best_score = i, score
        if best is not None:
            received[po_line.line_no] = pool[best].qty
            used.add(best)
    return received


def build_discrepancies(
    po: Document,
    grn: Document | None,
    invoice: Document,
    pairs: list[tuple[int, int]],
) -> list[Discrepancy]:
    """One Discrepancy per differing field per paired line, plus the
    document-level total check. Flat, not nested per line — the agent
    judges a qty gap and a price gap differently, so they must be separate
    rows (see schemas.Discrepancy docstring)."""
    po_by_no = {line.line_no: line for line in po.lines}
    inv_by_no = {line.line_no: line for line in invoice.lines}
    grn_by_sku = {}
    grn_qty_by_sku: dict[str, int] = {}
    if grn is not None:
        for line in grn.lines:
            if not line.sku:
                continue
            grn_by_sku[line.sku] = line
            # SUM per SKU, don't last-wins: a split delivery recorded as two
            # GRN lines (50 + 50) is 100 received, and overwriting would
            # report a phantom 50-unit shortfall on a fully-delivered order.
            grn_qty_by_sku[line.sku] = grn_qty_by_sku.get(line.sku, 0) + line.qty

    sku_less_received = _pair_sku_less_receipt_lines(po, grn) if grn is not None else {}

    out: list[Discrepancy] = []
    for po_no, inv_no in pairs:
        po_line, inv_line = po_by_no[po_no], inv_by_no[inv_no]
        grn_line = grn_by_sku.get(po_line.sku)

        # Received quantity for this line. Three cases:
        # - GRN has the SKU: the summed received qty.
        # - GRN exists, line has a SKU, but the GRN lacks it: received ZERO.
        #   Without this, "billed in full, received nothing" reads clean —
        #   the limit case of the partial-delivery miss.
        # - No GRN, or the line has no SKU (nothing to key the GRN lookup
        #   on): unknown, no invoice-vs-GRN comparison possible.
        if grn is not None and po_line.sku:
            grn_qty = grn_qty_by_sku.get(po_line.sku, 0)
        elif grn is not None:
            # No SKU to key on: the receipt line paired by description (see
            # _pair_sku_less_receipt_lines). Nothing paired is "received
            # zero", the same limit case as the SKU branch. Before this
            # branch a SKU-less line was never compared to the receipt at
            # all, so an invoice billing goods no receipt recorded read clean.
            grn_qty = sku_less_received.get(po_no, 0)
        else:
            grn_qty = None

        # A qty row fires on EITHER comparison: invoice vs PO, or invoice vs
        # GRN. The second one is easy to forget and is the worst miss in the
        # domain: PO 100 / GRN 80 / invoice 100 bills for goods that never
        # arrived, yet PO == invoice, so a two-way check stays silent. That
        # is the very case the schemas.Discrepancy docstring promises to
        # surface (and the AWAITING_DELIVERY hold exists for).
        if po_line.qty != inv_line.qty:
            delta = abs(inv_line.qty - po_line.qty)
            base = po_line.qty
        elif grn_qty is not None and grn_qty != inv_line.qty:
            delta = abs(inv_line.qty - grn_qty)
            base = grn_qty
        else:
            delta = None
        if delta is not None:
            out.append(
                Discrepancy(
                    line_pair=(po_no, inv_no),
                    field=DiscrepancyField.QTY,
                    po_value=str(po_line.qty),
                    grn_value=str(grn_qty) if grn_qty is not None else None,
                    invoice_value=str(inv_line.qty),
                    delta_abs=delta,
                    delta_pct=_pct(delta, base),
                    within_tolerance=False,
                )
            )

        # The line's own arithmetic: printed line total vs qty x unit price.
        # line_total_cents is stored, not computed, precisely so this gap is
        # evidence (see schemas.LineItem) — but stored-not-computed only
        # helps if something actually computes the gap. Without this row, an
        # invoice with honest qty and unit price but a padded line total
        # (and a grand total summed from the padded lines) read perfectly
        # clean end to end.
        if (
            inv_line.line_total_cents is not None
            and inv_line.unit_price_cents is not None
            and inv_line.line_total_cents != inv_line.qty * inv_line.unit_price_cents
        ):
            expected_line = inv_line.qty * inv_line.unit_price_cents
            delta = abs(inv_line.line_total_cents - expected_line)
            out.append(
                Discrepancy(
                    line_pair=(po_no, inv_no),
                    field=DiscrepancyField.LINE_TOTAL,
                    po_value=str(po_line.line_total_cents)
                    if po_line.line_total_cents is not None
                    else None,
                    grn_value=None,
                    invoice_value=str(inv_line.line_total_cents),
                    delta_abs=delta,
                    delta_pct=_pct(delta, expected_line),
                    within_tolerance=False,
                )
            )

        po_price, inv_price = po_line.unit_price_cents, inv_line.unit_price_cents
        if po_price is not None and inv_price is not None and po_price != inv_price:
            delta = abs(inv_price - po_price)
            out.append(
                Discrepancy(
                    line_pair=(po_no, inv_no),
                    field=DiscrepancyField.UNIT_PRICE,
                    po_value=str(po_price),
                    grn_value=None,  # a GRN records quantities, not prices
                    invoice_value=str(inv_price),
                    delta_abs=delta,
                    delta_pct=_pct(delta, po_price),
                    within_tolerance=False,
                )
            )

        if po_line.uom != inv_line.uom:
            out.append(
                Discrepancy(
                    line_pair=(po_no, inv_no),
                    field=DiscrepancyField.UOM,
                    po_value=po_line.uom,
                    grn_value=grn_line.uom if grn_line else None,
                    invoice_value=inv_line.uom,
                    delta_abs=None,
                    delta_pct=None,
                    within_tolerance=False,
                )
            )

    # Document level: does the invoice's own total add up? A gap here is
    # freight, rounding, or the vendor's arithmetic — evidence either way,
    # so we report it instead of "fixing" it (see schemas.Document).
    if invoice.total_cents is not None:
        expected = _subtotal(invoice) + (invoice.tax_cents or 0)
        if expected != invoice.total_cents:
            delta = abs(invoice.total_cents - expected)
            out.append(
                Discrepancy(
                    line_pair=None,
                    field=DiscrepancyField.INVOICE_TOTAL,
                    po_value=None,
                    grn_value=None,
                    invoice_value=str(invoice.total_cents),
                    delta_abs=delta,
                    delta_pct=_pct(delta, expected),
                    within_tolerance=False,
                )
            )
    return out


# Evidence-quality scores for match_confidence. Coarse on purpose: the
# agent reads these as "how much should I trust this match", and three
# clearly-separated levels communicate better than false precision.
# The second axis used to be "is there a GRN at all". It is now WHICH KIND,
# because a receipt confirmed in a chat group is real evidence but not the
# same evidence as one entered in the ERP against a process — so it must not
# read as the same 1.0 confidence to the agent.
CONFIDENCE = {
    ("ref", "erp"): 1.0,  # invoice named the PO, ERP receipt — full three-way
    ("ref", "chat"): 0.8,  # named PO, but delivery proof is a chat message
    ("ref", "none"): 0.7,  # named PO but no receipt — two-way only
    ("search", "erp"): 0.5,  # PO guessed from vendor+amount
    ("search", "chat"): 0.45,
    ("search", "none"): 0.4,
    ("none", "erp"): 0.0,
    ("none", "chat"): 0.0,
    ("none", "none"): 0.0,
}


def _grn_kind(grn: Document | None) -> str:
    """The CONFIDENCE table's second axis."""
    if grn is None:
        return "none"
    return "chat" if grn.source == EvidenceSource.CHAT else "erp"


def match_invoice(invoice: Document, pos: list[Document], grns: list[Document]) -> MatchResult:
    """The whole match for one invoice. This is what the agent's
    match tool returns."""
    po, how = find_po(invoice, pos)
    grn = find_grn(po, grns)

    if po is None:
        return MatchResult(
            invoice_id=invoice.doc_id,
            po_id=None,
            grn_id=None,
            line_pairs=[],
            unmatched_po_lines=[],
            unmatched_inv_lines=[line.line_no for line in invoice.lines],
            discrepancies=[],
            match_confidence=0.0,
        )

    pairs, unmatched_po, unmatched_inv = pair_lines(po.lines, invoice.lines)
    discrepancies = build_discrepancies(po, grn, invoice, pairs)

    return MatchResult(
        invoice_id=invoice.doc_id,
        po_id=po.doc_id,
        grn_id=grn.doc_id if grn else None,
        line_pairs=pairs,
        unmatched_po_lines=unmatched_po,
        unmatched_inv_lines=unmatched_inv,
        discrepancies=discrepancies,
        # .get, not a bare subscript: this runs inside decide_invoice with no
        # try around it, so with nine keys a single typo would be a 500. 0.0
        # is the fail-safe value — "trust this match not at all".
        match_confidence=CONFIDENCE.get((how, _grn_kind(grn)), 0.0),
    )
