"""The chat-harvesting path: message in, goods receipt out (or a refusal).

The LLM is stubbed everywhere — these tests are about what WE do with a
model's reading, not about the model. The cases cluster around the two ways
this can go wrong: trusting the wrong person, and trusting a claim we cannot
tie to our own records.

Offline: no API key, no network, no Telegram.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import apagent.chat.harvest as harvest_module
from apagent.chat.buffer import MessageBuffer
from apagent.chat.extract import ChatExtractionError, _strip_fences, render_window
from apagent.chat.harvest import ChatHarvester
from apagent.chat.resolve import resolve_grn
from apagent.chat.roster import Roster
from apagent.schemas import ChatMessage, EvidenceSource
from apagent.store import DocumentStore

DATA = Path(__file__).resolve().parent.parent / "data" / "synthetic"
CHAT = "-1001234567890"
PO_ID = "PO-2026-1019"
CONFIRMER = "88888888"


def msg(message_id, text, sender_id=CONFIRMER, name="Ah Seng", at="2026-08-12T14:30:00"):
    return ChatMessage(
        message_id=message_id,
        chat_id=CHAT,
        sender_id=sender_id,
        sender_name=name,
        text=text,
        sent_at=at,
    )


@pytest.fixture
def roster():
    return Roster({CHAT: "Ops group"}, {"telegram:88888888": "Li Wei (EMP-003)"})


@pytest.fixture
def store():
    return DocumentStore.from_dir(DATA)


def harvester_with(monkeypatch, store, roster, claim):
    """A harvester whose extraction step returns a canned claim."""
    monkeypatch.setattr(harvest_module, "extract_delivery_claim", lambda w, provider=None: claim)
    return ChatHarvester(store, roster=roster)


ALL_ARRIVED = {
    "is_delivery_confirmation": True,
    "po_reference": PO_ID,
    "items": [],
    "everything_arrived": True,
    "notes": None,
}


# --- the roster is the security boundary ----------------------------------


def test_display_name_cannot_impersonate_a_confirmer(roster):
    """The reason the roster keys on a numeric id. Anyone can set their
    Telegram display name to a colleague's; nobody can take their user id."""
    assert roster.confirmer_label("telegram", CONFIRMER) == "Li Wei (EMP-003)"
    assert roster.confirmer_label("telegram", "99999999") is None


def test_a_missing_roster_file_authorises_nobody(tmp_path):
    """An install that never configured the roster gets no automation, not
    open automation."""
    empty = Roster.from_file(tmp_path / "does-not-exist.json")
    assert empty.is_bound(CHAT) is False
    assert empty.confirmer_label("telegram", CONFIRMER) is None


def test_unbound_group_is_ignored_entirely(monkeypatch, store, roster):
    """Anyone can add a bot to a group. That must not be a way in."""
    h = harvester_with(monkeypatch, store, roster, ALL_ARRIVED)
    stranger = msg("1", "@apbot the goods arrived").model_copy(update={"chat_id": "-100999"})
    h.observe(stranger)
    assert h.buffer.messages("-100999") == []  # not even retained
    result = h.on_mention(stranger)
    assert result.receipt is None


def test_unauthorised_sender_still_produces_evidence(monkeypatch, store, roster):
    """The tiering in one test: a confirmation from someone off the roster is
    recorded so a reviewer can see it, but comes out unauthorised so the gate
    will not release money on it. Refusing to look would throw away the most
    useful thing on the hold screen — what was said, and by whom."""
    h = harvester_with(monkeypatch, store, roster, ALL_ARRIVED)
    result = h.on_mention(msg("1", "@apbot confirm", sender_id="99999999"))
    assert result.receipt is not None
    assert result.receipt.confirmed_by is None
    assert result.receipt.source == EvidenceSource.CHAT
    assert "reviewer still needs to accept" in result.reply


def test_authorised_sender_is_recorded_as_proof(monkeypatch, store, roster):
    h = harvester_with(monkeypatch, store, roster, ALL_ARRIVED)
    result = h.on_mention(msg("1", "@apbot confirm"))
    assert result.receipt.confirmed_by == "Li Wei (EMP-003)"
    assert result.invoice_ids == ["INV-V006-3019"]
    assert "proof of delivery" in result.reply


# --- resolution fails closed ----------------------------------------------


@pytest.mark.parametrize(
    "label,claim",
    [
        (
            "no PO reference",
            {"is_delivery_confirmation": True, "po_reference": None, "items": []},
        ),
        (
            "a PO we do not have",
            {"is_delivery_confirmation": True, "po_reference": "PO-9999", "items": []},
        ),
        (
            "not a confirmation at all",
            {"is_delivery_confirmation": False, "po_reference": PO_ID, "items": []},
        ),
        (
            "an item we cannot match to the order",
            {
                "is_delivery_confirmation": True,
                "po_reference": PO_ID,
                "items": [{"description": "a pallet of something", "qty": "3"}],
                "everything_arrived": False,
            },
        ),
        (
            "quantities nobody stated",
            {
                "is_delivery_confirmation": True,
                "po_reference": PO_ID,
                "items": [{"description": "Nitrile gloves size L", "qty": "a few boxes"}],
                "everything_arrived": False,
            },
        ),
        (
            "goods arrived, but no idea how many",
            {
                "is_delivery_confirmation": True,
                "po_reference": PO_ID,
                "items": [],
                "everything_arrived": False,
            },
        ),
    ],
)
def test_ambiguous_claims_record_nothing(monkeypatch, store, roster, label, claim):
    """Refusing is the normal outcome. Guessing which order a bare
    "the stuff arrived" meant is how a receipt confirms a delivery that never happened."""
    h = harvester_with(monkeypatch, store, roster, claim)
    result = h.on_mention(msg("1", "@apbot confirm"))
    assert result.receipt is None, label
    assert result.reply


def test_refusal_does_not_name_our_purchase_orders(monkeypatch, store, roster):
    """The reply goes to a group that often contains the supplier. Listing
    the references we were expecting tells them what the system responds to."""
    claim = {"is_delivery_confirmation": True, "po_reference": None, "items": []}
    h = harvester_with(monkeypatch, store, roster, claim)
    reply = h.on_mention(msg("1", "@apbot confirm")).reply
    assert PO_ID not in reply
    assert "CleanPro" not in reply


def test_receipt_lines_always_carry_the_po_sku(monkeypatch, store, roster):
    """The trap this whole module is shaped around. build_discrepancies reads
    a receipt BY SKU and treats a missing sku as ZERO RECEIVED, so a receipt
    assembled from free-text descriptions would invent a shortfall on every
    line. Lines only ever come from PO lines, copied whole."""
    h = harvester_with(monkeypatch, store, roster, ALL_ARRIVED)
    receipt = h.on_mention(msg("1", "@apbot confirm")).receipt
    po = store.get_po(PO_ID)
    assert [line.sku for line in receipt.lines] == [line.sku for line in po.lines]
    assert all(line.sku for line in receipt.lines)


def test_a_partial_confirmation_records_only_what_was_confirmed(store):
    """ "Only the gloves came" must not silently confirm the whole order."""
    claim = {
        "is_delivery_confirmation": True,
        "po_reference": PO_ID,
        "items": [{"description": "Nitrile gloves size L, box of 100", "qty": "60"}],
        "everything_arrived": False,
    }
    receipt, reason = resolve_grn(claim, store, [], "Li Wei", "2026-08-12T14:30:00", "CHAT-EV-0001")
    assert reason is None
    assert len(receipt.lines) == 1
    assert receipt.lines[0].qty == 60


def test_generated_ids_never_come_from_chat_text(monkeypatch, store, roster):
    """Receipt and evidence ids are interpolated into the UI and into tool
    results, so they must be ours. api/web/app.js prints a receipt id without
    escaping it — safe only because ids are code-generated."""
    h = harvester_with(monkeypatch, store, roster, ALL_ARRIVED)
    result = h.on_mention(msg("1", "@apbot <script>alert(1)</script>"))
    assert result.receipt.doc_id == "GRN-CHAT-1019-1"
    assert result.evidence.evidence_id == "CHAT-EV-0001"


def test_extraction_failure_does_not_blame_the_group(monkeypatch, store, roster):
    """A model or parsing failure is our problem, and must not read as a
    judgement on what someone wrote."""

    def boom(window, provider=None):
        raise ChatExtractionError("model returned no text")

    monkeypatch.setattr(harvest_module, "extract_delivery_claim", boom)
    result = ChatHarvester(store, roster=roster).on_mention(msg("1", "@apbot confirm"))
    assert result.receipt is None
    assert "try again" in result.reply


# --- the buffer -----------------------------------------------------------


def test_window_covers_the_messages_around_the_mention():
    """The useful sentence is usually a few messages before the @mention."""
    buffer = MessageBuffer()
    for i in range(10):
        buffer.add(msg(str(i), f"line {i}"))
    window = buffer.window(CHAT, "7", before=3, after=1)
    assert [m.message_id for m in window] == ["4", "5", "6", "7", "8"]


def test_window_is_bounded_by_time_not_only_count():
    """Someone who can post in the group can otherwise flood the real
    confirmation out of a count-bounded window."""
    buffer = MessageBuffer()
    old = datetime(2026, 8, 12, 2, 0, 0)
    buffer.add(msg("old", "only 8 of the 10 arrived", at=old.isoformat()))
    for i in range(5):
        buffer.add(msg(f"f{i}", "chatter", at=(old + timedelta(hours=10)).isoformat()))
    buffer.add(msg("mention", "@apbot confirm", at=(old + timedelta(hours=10)).isoformat()))
    window = buffer.window(CHAT, "mention", before=30, after=0, within_seconds=3600)
    assert "old" not in [m.message_id for m in window]


def test_window_is_empty_when_the_anchor_is_unknown():
    assert MessageBuffer().window(CHAT, "never-seen") == []


def test_prune_drops_messages_past_the_ttl():
    """Privacy mode off means we see every message in a bound group, so we
    keep as little as we can get away with."""
    buffer = MessageBuffer(ttl_seconds=3600)
    buffer.add(msg("1", "old", at="2026-08-12T02:00:00"))
    buffer.prune(now=datetime(2026, 8, 12, 20, 0, 0))
    assert buffer.messages(CHAT) == []


def test_buffer_survives_an_unreadable_timestamp():
    """A platform quirk must not crash the bot mid-conversation."""
    buffer = MessageBuffer()
    buffer.add(msg("1", "hi", at="not-a-date"))
    buffer.add(msg("2", "@apbot confirm"))
    assert buffer.window(CHAT, "2")
    buffer.prune()


# --- extraction plumbing --------------------------------------------------


def test_strip_fences_handles_a_json_code_block():
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_rendered_window_labels_each_speaker():
    rendered = render_window([msg("1", "the goods arrived")])
    assert "Ah Seng" in rendered and "the goods arrived" in rendered


# --- the poller, joined to the running service ----------------------------


class _FakeAdapter:
    """A platform that yields one conversation, then nothing."""

    platform = "telegram"

    def __init__(self, messages):
        self._messages = messages
        self.sent = []

    def poll(self, timeout=30):
        out, self._messages = self._messages, []
        return out

    def mentions_bot(self, message):
        return "@apbot" in message.text

    def reply(self, chat_id, text):
        self.sent.append(text)


def test_a_mention_flips_the_invoice_in_the_running_service(monkeypatch, roster):
    """The whole feature end to end, and the reason the poller runs inside
    the web app's process: the harvester shares the service's in-memory
    store, so a receipt recorded from chat is visible to the console
    immediately. A separate process would record into a different store and
    the page would keep showing the hold."""
    import json

    import apagent.api.service as service_module
    from apagent.chat.runner import ChatRunner

    monkeypatch.setattr(
        "apagent.agent.loop.call_model",
        lambda messages, tools, system, provider=None: {
            "text": json.dumps(
                {"action": "APPROVE", "hold_reason": None, "confidence": 0.9, "reasoning": "ok"}
            ),
            "tool_calls": [],
        },
    )
    monkeypatch.setattr(
        harvest_module, "extract_delivery_claim", lambda w, provider=None: ALL_ARRIVED
    )

    svc = service_module.Service()
    monkeypatch.setattr(svc, "_save_cache", lambda: None)  # never touch the committed file
    harvester = svc.chat_harvester()
    harvester.roster = roster

    assert svc.get_case("INV-V006-3019")["decision"]["action"] == "HOLD"

    adapter = _FakeAdapter([msg("1", "PO-2026-1019 all arrived"), msg("2", "@apbot confirm")])
    ChatRunner(adapter, harvester, on_receipt=svc.on_chat_receipt).tick()

    case = svc.get_case("INV-V006-3019")
    assert case["decision"]["action"] == "APPROVE"
    gate = next(g for g in case["guardrails"] if g["key"] == "grn")
    assert gate["passed"] and "chat-confirmed" in gate["label"]
    assert adapter.sent  # the group was told what was recorded


def test_a_flipped_invoice_never_moves_the_measured_benchmark(monkeypatch, roster):
    """The headline metric is measured over the committed ERP dataset. A
    session's chat evidence sits outside that ground truth, so it must not
    show up as a false approve on the analytics page mid-demo — nor rewrite
    the committed cache."""
    import json

    import apagent.api.service as service_module
    from apagent.chat.runner import ChatRunner

    monkeypatch.setattr(
        "apagent.agent.loop.call_model",
        lambda messages, tools, system, provider=None: {
            "text": json.dumps(
                {"action": "APPROVE", "hold_reason": None, "confidence": 0.9, "reasoning": "ok"}
            ),
            "tool_calls": [],
        },
    )
    monkeypatch.setattr(
        harvest_module, "extract_delivery_claim", lambda w, provider=None: ALL_ARRIVED
    )

    svc = service_module.Service()
    monkeypatch.setattr(svc, "_save_cache", lambda: None)
    svc.chat_harvester().roster = roster
    adapter = _FakeAdapter([msg("1", "PO-2026-1019 all arrived"), msg("2", "@apbot confirm")])
    ChatRunner(adapter, svc.chat_harvester(), on_receipt=svc.on_chat_receipt).tick()

    assert svc.metrics()["false_approve"] == 0
    assert svc.analytics()["metrics"]["false_approve_count"] == 0
    # Present with its committed value, NOT dropped: it has a manifest entry,
    # and a missing key would make the harness report it under `missing`.
    assert svc._benchmark_view()["INV-V006-3019"]["action"] == "HOLD"


def test_the_poller_survives_a_platform_outage(roster, store):
    """A daemon thread that dies takes the feature down for the life of the
    process, so tick must not propagate."""
    from apagent.chat.runner import ChatRunner

    class Broken:
        platform = "telegram"

        def poll(self, timeout=30):
            raise ConnectionError("network down")

        def mentions_bot(self, m):
            return False

        def reply(self, c, t):
            pass

    runner = ChatRunner(Broken(), ChatHarvester(store, roster=roster))
    with pytest.raises(ConnectionError):
        runner.tick()  # tick itself is honest about failing...
    runner._stop.set()
    runner.run_forever()  # ...and run_forever is what swallows it


# --- real confirmations mix states ----------------------------------------


def _resolve(store, items, everything=False):
    return resolve_grn(
        {
            "is_delivery_confirmation": True,
            "po_reference": PO_ID,
            "items": items,
            "everything_arrived": everything,
        },
        store,
        [],
        "Li Wei",
        "2026-08-12T14:30:00",
        "CHAT-EV-0001",
    )


def test_one_message_can_confirm_some_items_and_not_others(store):
    """How people actually write: "the detergent all came, gloves only 60,
    still waiting on the bags". Three items, three different answers.

    An earlier version resolved completeness for the whole message, so an
    item mentioned without a number failed the lot — throwing away the two
    lines the sender had been perfectly clear about."""
    receipt, reason = _resolve(
        store,
        [
            {"description": "detergent", "qty": None, "complete": True},
            {"description": "nitrile gloves", "qty": "60", "complete": False},
            {"description": "trash bag", "qty": None, "complete": False},
        ],
    )
    assert reason is None
    assert {line.sku: line.qty for line in receipt.lines} == {"CP-DET-5L": 10, "CP-GLOVE-L": 60}
    # The pending item is ABSENT rather than recorded as zero. Absent is what
    # build_discrepancies reads as nothing received, so the invoice holds on
    # that line — the safe direction, and what "still waiting" means.
    assert "CP-BAG-120" not in {line.sku for line in receipt.lines}


def test_an_item_named_with_no_quantity_and_no_completeness_is_skipped(store):
    """ "Still short on the gloves" states a shortfall, not a quantity."""
    receipt, reason = _resolve(store, [{"description": "gloves", "qty": None, "complete": False}])
    assert receipt is None
    assert reason == "no_quantity"


def test_a_partial_word_still_identifies_the_line(store):
    """People type a fragment of what the order calls something. Against
    "Nitrile gloves size L, box of 100", the phrase "nitrile gloves" scores
    only 0.60 on whole-string similarity and "trash bag" 0.51 — both would
    have been rejected as unrecognisable before containment was added."""
    receipt, reason = _resolve(
        store,
        [
            {"description": "gloves", "qty": "100", "complete": True},
            {"description": "trash bag", "qty": "24", "complete": True},
        ],
    )
    assert reason is None
    assert {line.sku for line in receipt.lines} == {"CP-GLOVE-L", "CP-BAG-120"}


def test_a_word_common_to_two_lines_is_refused_not_guessed(store):
    """Containment alone would let a bare "box" match anything with a box in
    it. The ambiguity margin is what stops that becoming a coin flip."""
    receipt, reason = _resolve(store, [{"description": "of", "qty": "5", "complete": True}])
    assert receipt is None
    assert reason == "unmatched_item"


def test_completeness_falls_back_to_the_whole_delivery(store):
    """An item with no per-item verdict inherits "everything arrived", which
    is how a plain "all received, 3 items" reads."""
    receipt, reason = _resolve(store, [{"description": "detergent", "qty": None}], everything=True)
    assert reason is None
    assert receipt.lines[0].qty == 10


def test_a_blank_roster_env_var_falls_back_to_the_default(monkeypatch, tmp_path):
    """.env.example ships APAGENT_CHAT_ROSTER= with an empty value, and an
    empty string is SET as far as os.getenv is concerned. Passing it as
    getenv's default meant Path("") -> Path(".") -> PermissionError on
    opening a directory, at app startup, for anyone who copied the example."""
    monkeypatch.setenv("APAGENT_CHAT_ROSTER", "")
    roster = Roster.from_file()  # must not raise
    assert isinstance(roster, Roster)


def test_a_roster_path_that_is_a_directory_authorises_nobody(monkeypatch, tmp_path):
    monkeypatch.setenv("APAGENT_CHAT_ROSTER", str(tmp_path))
    assert Roster.from_file().is_bound(CHAT) is False


def test_buffer_handles_timezone_aware_and_naive_stamps_together():
    """Telegram stamps messages with an offset ("...+00:00"); fixtures and
    other platforms may not. Mixing the two is a TypeError, and it killed
    the poller before it fetched anything — prune() raised on every tick, so
    it retried forever, silently, doing nothing. Only running against real
    Telegram surfaced it, because every test stamp here was naive."""
    buffer = MessageBuffer()
    buffer.add(msg("1", "naive stamp", at="2026-08-12T14:30:00"))
    buffer.add(msg("2", "aware stamp", at="2026-08-12T14:31:00+00:00"))
    buffer.add(msg("3", "@apbot confirm", at="2026-08-12T14:32:00+00:00"))
    assert len(buffer.window(CHAT, "3")) == 3  # must not raise
    buffer.prune()  # must not raise


def test_a_bot_token_never_reaches_the_logs(caplog):
    """Telegram puts the token in the URL PATH, so anything logging a request
    URL logs the credential — httpx does exactly that at INFO level. An
    ordinary log file became a leaked bot: whoever reads it can impersonate
    us, read every bound group, and post as the company.

    Found the only way these things are: by reading a log file and seeing the
    token sitting in it."""
    import logging

    from apagent.chat.adapters import redact_tokens_from_logs

    redact_tokens_from_logs()
    url = "https://api.telegram.org/bot8874647777:AAH_EmUxAJ8XGk6ss9LADjxKTn63G7lKuOY/getUpdates"
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("HTTP Request: GET %s", url)
        logging.getLogger("httpx").info("plain message with %s inside", url)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "AAH_EmU" not in joined
    assert "bot<REDACTED>" in joined
