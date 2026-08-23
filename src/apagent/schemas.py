"""All data models for this project live here. This is the single source of truth.

Only model definitions, no business logic functions.
The matching engine, rules layer, agent, and API all import types from here.
"""

from enum import StrEnum

from pydantic import BaseModel


class DocType(StrEnum):
    """What kind of document this is.

    Why one model plus a type enum: PO, GRN and Invoice look almost the same
    (vendor, date, a list of line items). So we use one Document model and mark
    the type here. If we had three separate models, the matching engine would
    need three sets of "compare A with B" code.
    Other option: write PurchaseOrder / GoodsReceipt / Invoice as three models.
    More exact, but more code to write and keep in sync.
    The cost of our choice: some fields do not make sense for some types
    (a PO has no payment terms). We just put None there.
    """

    PO = "PO"
    GRN = "GRN"
    INVOICE = "INVOICE"


class EvidenceSource(StrEnum):
    """How a document reached us — the difference between a record someone
    typed into the ERP and a confirmation harvested from a chat group.

    This exists because a goods receipt is a claim about the physical world,
    and who made the claim decides how much it is worth. An ERP GRN was
    entered by warehouse staff against a process; a CHAT one is a colleague
    saying "yeah it came" in Telegram. Both are evidence, and the code
    guardrail treats them differently (see pipeline.grn_gate).

    Only GRNs are CHAT today. POs and invoices are ERP by definition here.
    Other option: a bool like `informal`. Rejected — the moment a third
    channel appears (a supplier portal, an email confirmation) a bool has to
    become an enum anyway, and a bool cannot be read in a JSON dump.
    """

    ERP = "ERP"
    CHAT = "CHAT"


class LineItem(BaseModel):
    """One line of goods on a document.

    sku can be missing. Small vendors often print no item code on the invoice,
    only a text description. When the code is missing, the only way to pair the
    lines is text similarity on description. That is why we need the Hungarian
    algorithm later, instead of just looking up a dict by sku.

    qty is always in the base unit. Converting box or dozen into pieces happens
    once, in the extraction layer. Every module after that can trust qty is the
    same unit. Otherwise every module has to remember to convert, and one of
    them will forget.
    Other option: keep the vendor's original unit and number, convert when
    comparing. Closer to the paper document, but easy to get wrong.

    qty is int because we assume items are countable (pieces, boxes).
    If the problem set has goods priced by weight or length (like 12.5 kg),
    change qty to an integer of "one thousandth of the base unit"
    (12500 means 12.5 kg). Same rule as money: store the smallest unit as int.

    Money is always an integer number of cents. SQLite has no real Decimal type.
    Storing money as float never raises an error, it just quietly makes totals
    not add up (0.1 + 0.2 != 0.3). That kind of bug is the hardest to find in a
    reconciliation system.
    Other option: use Decimal stored as text. Better precision, but you convert
    on every read and write.

    line_total_cents is stored, not computed from qty * unit_price.
    On a real invoice the line total may not equal qty * unit_price because of
    rounding, a discount, or an extra fee. That gap is itself a discrepancy.
    If we compute the total, we throw away the evidence.
    """

    line_no: int
    sku: str | None
    description: str
    qty: int
    uom: str
    unit_price_cents: int | None
    line_total_cents: int | None


class Document(BaseModel):
    """One document. Shared by PO, GRN and Invoice.

    vendor_name is stored next to vendor_id because the vendor name printed on
    an invoice often has small spelling differences ("ABC Pte Ltd",
    "ABC Pte. Ltd.", "ABC Private Limited"). We keep the original text so we can
    do name similarity matching and look-ups. vendor_id is the clean internal id.

    issue_date is an ISO string ("2026-08-14"), not a date object, because the
    data travels between JSON, SQLite and the API. With a date object you have
    to convert at every boundary and deal with time zones.
    Other option: use datetime.date. Safer type, but more conversion code.

    ref_doc_id points to the document upstream (invoice points to PO, GRN points
    to PO). A missing ref_doc_id is a very common defect in real documents. When
    it is missing, the only way is to search back by vendor plus amount.

    The invoice-only fields (payment_terms, due_date, tax_cents, total_cents)
    sit directly in Document and are None when they do not apply, instead of
    living in an Invoice subclass. The matching engine handles all three
    document types at once, and a subclass would make the type hints messy
    (isinstance and cast everywhere).
    Other option: Document as a base class plus an Invoice subclass. Cleaner
    meaning. If the invoice-only fields keep growing (withholding tax, exchange
    rate, payment method...), this decision is worth revisiting.

    total_cents is stored on its own. It may not equal the sum of the lines plus
    tax. We do not "fix" that gap. We leave it as a signal for the agent to
    judge: it could be freight, it could be rounding, or the vendor really did
    add it up wrong.
    """

    doc_id: str
    doc_type: DocType
    vendor_id: str
    vendor_name: str
    issue_date: str
    ref_doc_id: str | None
    currency: str | None
    lines: list[LineItem]

    # These only have a value on an Invoice. On a PO or GRN they stay None.
    payment_terms: str | None = None
    due_date: str | None = None
    tax_cents: int | None = None
    total_cents: int | None = None

    # Where this document came from. Until chat confirmation existed every
    # document was an ERP record and provenance was not a question, so these
    # all carry defaults — the committed dataset has no such keys and must
    # keep loading (store.from_dir builds Document(**d) straight from JSON).
    #
    # Only code-controlled scalars live here, never the confirmer's words.
    # agent/ap_tools._doc_summary dumps the whole model into the prompt on
    # every lookup_grn, so a raw chat quote on this model would be a fresh
    # injection surface on a path that currently has none. The quotes live
    # in ChatGrnEvidence and are reached through a tool instead.
    source: EvidenceSource = EvidenceSource.ERP
    source_ref: str | None = None  # a ChatGrnEvidence id, code-generated
    confirmed_by: str | None = None  # internal employee label, resolved via the roster
    captured_at: str | None = None  # ISO string, same rule as issue_date

    # A reviewer who looked at the chat evidence in the console and vouched
    # for it. This is the escape hatch for everything the automatic tier
    # refuses, and it exists because the alternative does not: telling an SME
    # "record a formal goods receipt to release this" assumes an ERP process
    # they do not have. If a formal receipt were the only way out of a hold,
    # the whole chat-confirmation feature would be pointless for the people
    # it is built for. So the escalation path is a human accepting the
    # evidence, not a human doing data entry.
    # It waives WHO may confirm and HOW MUCH, never the quantity arithmetic:
    # endorsing "80 arrived" says nothing about an invoice billing 100.
    endorsed_by: str | None = None


class DiscrepancyField(StrEnum):
    """Which field the difference was found in."""

    QTY = "QTY"
    UNIT_PRICE = "UNIT_PRICE"
    LINE_TOTAL = "LINE_TOTAL"
    UOM = "UOM"
    INVOICE_TOTAL = "INVOICE_TOTAL"


class Discrepancy(BaseModel):
    """One difference we found.

    Differences are recorded flat, one per field, not nested per line.
    If one line has both a wrong quantity and a wrong price, that makes two
    Discrepancy objects. This way the agent can judge them one by one (a
    quantity gap may just be a partial delivery, a price gap is a pricing
    problem, totally different things), and counting "how many invoices have a
    price problem" is just a filter on field.
    Other option: nest per line (one object per line holding several field
    differences). Closer to the shape of the paper document, but the agent has
    to unpack it every time.
    We do not lose the line link: line_pair keeps it (po line no, invoice line no).

    grn_value has its own field because a quantity mismatch only makes sense
    when you compare all three sides:
      PO 100 / GRN 100 / invoice 80  -> the invoice was billed short
      PO 100 / GRN 80  / invoice 100 -> goods have not all arrived but the
                                        invoice bills for everything
    This is the key input for the agent deciding APPROVE vs HOLD.

    All three value fields are str. The field types are not the same (quantity
    is a count, money is cents, unit of measure is text), and these values are
    just evidence text for the agent to read. If you need to do math, use the
    delta fields.

    line_pair is None when the difference is at document level, for example the
    invoice total not matching the sum of the lines.

    UNITS, do not get this wrong:
    delta_abs is in cents (or in the qty base unit for a QTY difference).
    delta_pct is in PERCENTAGE POINTS, not a fraction. 2.0 means 2%.
    It is always the absolute size of the gap, never negative, so the direction
    of the difference has to be read off po_value vs invoice_value.
    ToleranceConfig uses the same unit, so the check is a plain
    `delta_pct > config.unit_price_pct`. If one side used 0.02 and the other 2.0,
    every tolerance check would be off by 100x and nothing would raise an error.
    Other option: store fractions (0.02) everywhere. Fewer mistakes when doing
    math, but harder to read in logs and in the agent's prompt.
    """

    line_pair: tuple[int, int] | None
    field: DiscrepancyField
    po_value: str | None
    grn_value: str | None
    invoice_value: str | None
    delta_abs: int | None
    delta_pct: float | None
    within_tolerance: bool


class MatchResult(BaseModel):
    """The three-way match result for one invoice.

    One object per invoice, not one per pair of line items. The agent makes its
    decision per invoice (approve or hold this invoice), so getting the whole
    picture at once (matched lines, unmatched lines, all differences) is more
    reliable than making it stitch pieces together.

    This is the output of one matching run, not a database table shape. If we
    later store it, row-per-line storage is closer to how industrial ERP systems
    do it, and easier for reports and audit.

    unmatched_inv_lines matters a lot on its own. A line on the invoice that
    does not exist on the PO at all is the classic over-billing or wrong-customer
    case (someone else's goods billed to you). That is much more serious than a
    quantity being off by a few, and usually goes straight to ESCALATE.
    unmatched_po_lines is usually mild: it normally just means not billed yet.

    po_id and grn_id can be None. Not finding the upstream document is itself a
    result worth reporting.
    """

    invoice_id: str
    po_id: str | None
    grn_id: str | None
    line_pairs: list[tuple[int, int]]
    unmatched_po_lines: list[int]
    unmatched_inv_lines: list[int]
    discrepancies: list[Discrepancy]
    match_confidence: float


class Action(StrEnum):
    """What the agent finally does with an invoice.

    Only four values, not a longer list, because the fewer options the agent has,
    the less likely it picks the wrong one. With a dozen similar values it keeps
    flip-flopping between two close ones. The finer reason goes in hold_reason.
    Other option: split into a dozen actions (chase goods, chase GRN, ask about
    price, send to purchasing...). More direct for downstream systems, but the
    agent's decisions get worse.

    For the STP rate (straight-through processing rate), only APPROVE counts in
    the numerator. A HOLD had no human touch at that moment, but the process is
    not finished, someone still has to deal with it later. Counting HOLD would
    make the number look better than it is.
    """

    APPROVE = "APPROVE"
    HOLD = "HOLD"
    EMAIL = "EMAIL"
    ESCALATE = "ESCALATE"


class HoldReason(StrEnum):
    """Why it is on hold. Only has a value when action is HOLD."""

    AWAITING_DELIVERY = "AWAITING_DELIVERY"
    AWAITING_GRN = "AWAITING_GRN"
    PRICE_VARIANCE = "PRICE_VARIANCE"


class ChatMessage(BaseModel):
    """One message out of a chat group, as the adapter saw it.

    sender_id is the platform's NUMERIC user id, kept as a string only so it
    survives JSON. It is the only field the roster is allowed to key on:
    sender_name is whatever the person set as their display name today, and on
    Telegram both the display name and the @username can be changed freely (a
    released @username can even be re-registered by someone else). Trusting
    either would let anyone in the group become "the warehouse manager".

    text is untrusted third-party data, exactly like an invoice description.
    It is never rendered into an outbound message and never treated as an
    instruction — see AP_SYSTEM_PROMPT's SECURITY block.
    """

    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    sent_at: str  # ISO string, same rule as issue_date


class ChatGrnEvidence(BaseModel):
    """A goods-receipt confirmation harvested from a chat group.

    Named ChatGrnEvidence, not ChatReceiptEvidence: per CLAUDE.md's glossary a
    goods receipt is a `grn`, never a "receipt".

    This is EVIDENCE, not a verdict. There is deliberately no action field and
    no approve flag — the most this can ever become is an informal GRN that the
    code guardrail then judges. A chat message that says "approve this
    immediately" has nowhere to put that instruction, which is what makes the
    injection defence architectural rather than a matter of prompt wording.

    confirmed_by is None when the speaker is not on the authorised roster. We
    still keep the evidence in that case — a human reviewing the hold should
    see what was said and by whom — but the guardrail will not act on it.

    messages keeps the verbatim window for the audit trail. It stays OFF the
    GRN Document on purpose (see Document.source), because that model gets
    dumped wholesale into the agent's prompt.
    """

    evidence_id: str  # code-generated, e.g. CHAT-EV-0001 — never text from chat
    platform: str
    chat_id: str
    po_id: str | None
    confirmed_by: str | None
    captured_at: str
    messages: list[ChatMessage]
    refusal_reason: str | None = None


class ToolCall(BaseModel):
    """A record of one tool call the agent made.

    round is which round it was, so we can see whether the agent got there in one
    step or poked around. result is a str because every tool returns a different
    shape, and all we need to keep is what the agent saw at that moment.
    """

    round: int
    tool_name: str
    args: dict
    result: str


class AgentDecision(BaseModel):
    """The agent's decision on one invoice, with the full reasoning trail.

    tool_calls must keep the complete, ordered sequence. This is the heart of the
    demo: the judges do not want to see the final APPROVE, they want to see
    "which tool did it call, what data did it see, and so what did it conclude".
    If we only store the conclusion there is nothing to show, and nothing to
    debug when it gets something wrong.
    Other option: keep only one reasoning string. Saves space, but you cannot
    audit it.
    """

    invoice_id: str
    action: Action
    hold_reason: HoldReason | None
    confidence: float
    reasoning: str
    tool_calls: list[ToolCall]
    rounds_used: int

    # The message that goes to a human (ops chase, vendor query). Filled by
    # CODE from a fixed template with slots taken from our own documents —
    # never written by the model. reasoning is internal audit text; this
    # field is the only agent output an outsider-facing human acts on, so a
    # poisoned invoice must have no way to author it. The enum constrains
    # the action; this constrains the words.
    outbound_message: str | None = None


class ToleranceConfig(BaseModel):
    """Tolerance settings.

    Made into a config object instead of constants scattered through the code
    (things like `if delta > 500`). This copies the "tolerance key" idea from ERP
    systems: in a real deployment the client will definitely ask for different
    limits per vendor or per material group (loose for big partners, strict for
    new vendors).
    Other option: hard-code them as module level constants. Shorter code, but
    adding one vendor exception means changing logic.

    per_vendor_overrides is keyed by vendor_id and points to another
    ToleranceConfig. If the vendor is not in there, fall back to the defaults.
    The nested one does not nest again (no overrides inside an override).

    UNITS, must match Discrepancy:
    every *_pct field is in PERCENTAGE POINTS, not a fraction.
    unit_price_pct = 2.0 means "a unit price may differ by up to 2%".
    total_pct = 1.0 means "the invoice total may differ by up to 1%".
    Discrepancy.delta_pct uses the same unit, so comparing them directly is
    correct. Never put 0.02 in here.
    Every *_cents field is an integer number of cents, same as everywhere else.

    Why these defaults:
    unit_price_pct 2.0 - a 2% price drift is normal (rounding, small surcharge).
        Above that it looks like the vendor billed off an old price list.
    total_abs_cents 500 (SGD 5.00) and total_pct 1.0 - a total is checked against
        BOTH, and passes only if it is inside both. The absolute one stops us
        arguing over 3 cents of rounding on a small invoice; the percentage one
        stops a big invoice hiding a big gap under a loose absolute limit.
    qty_exact True - quantity must match exactly. Quantity gaps are not noise,
        they mean goods did not arrive or were over-billed, and that is exactly
        the case we want the agent to think about, not wave through.
    manual_review_threshold_cents 500_000 (SGD 5,000) - above this amount a human
        looks at the invoice even if the match is clean. Reasoning for an SME:
        most of their invoices are a few hundred dollars, so SGD 5,000 is already
        a big cheque, and a wrong payment that size actually hurts cash flow.
        SGD 10,000 would give a nicer STP rate but leaves a painful amount of
        money unchecked. This number directly moves our headline metric, so it is
        a deliberate choice, not a leftover default.
    informal_grn_ceiling_cents 200_000 (SGD 2,000) - the most we will pay on a
        goods receipt that came from a chat message rather than the ERP. A
        colleague typing "yeah it arrived" is real evidence but weaker evidence,
        so it buys automation only for small routine spend. Sits well under
        manual_review_threshold_cents (SGD 5,000) on purpose: an informal
        receipt must never be the thing that carries a large invoice.
        It is called a CEILING, not a threshold — per CLAUDE.md, "threshold" is
        reserved for the manual-review cutoff above.
        SGD 1,000 was the first draft and was wrong: it sits below
        INV-V006-3019 (SGD 1,270.29), the exact case this feature exists to
        solve, so the rule would never have fired on its own motivating
        example. Same class of number as the one above — it moves the headline
        metric, so it is chosen, not defaulted.

    All of these are still first guesses. Once we see the problem set on 8/14 and
    can measure the STP rate against it, tune them here, in one place.
    """

    unit_price_pct: float = 2.0
    total_abs_cents: int = 500
    total_pct: float = 1.0
    qty_exact: bool = True
    manual_review_threshold_cents: int = 500_000
    informal_grn_ceiling_cents: int = 200_000
    per_vendor_overrides: dict[str, "ToleranceConfig"] | None = None
