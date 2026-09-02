"""Tests for the API service layer.

Offline: these exercise the deterministic case-bundle assembly (facts,
guardrail pass/fail, ordering), not the LLM. The decision field may be
present or absent depending on whether the cache exists — the tests never
depend on it.
"""

import pytest

from apagent.api.service import Service


def test_headline_case_all_guardrails_pass():
    """INV-V005-3018: 4% within the contract's 5% — every code gate passes,
    and the bundle exposes the code-parsed allowance."""
    c = Service().get_case("INV-V005-3018")
    assert c["contract_allowance_pct"] == 5.0
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert all(gates.values()), gates
    price = [d for d in c["match"]["discrepancies"] if d["field"] == "UNIT_PRICE"]
    assert len(price) == 1


def test_missing_grn_case_fails_the_grn_gate():
    c = Service().get_case("INV-V006-3019")
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert gates["grn"] is False
    assert c["grn"] is None


def test_injection_case_fails_the_price_gate():
    """INV-V002-3020: 10% overcharge, no contract allowance — price gate fails."""
    c = Service().get_case("INV-V002-3020")
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert gates["price"] is False


def test_duplicate_case_fails_the_duplicate_gate():
    c = Service().get_case("INV-V003-3901")
    gates = {g["key"]: g["passed"] for g in c["guardrails"]}
    assert gates["duplicate"] is False
    assert "INV-V003-3003" in c["duplicates"]


def test_list_is_ordered_headline_first_and_complete():
    cases = Service().list_cases()
    assert cases[0]["invoice_id"] == "INV-V005-3018"
    # 22 graded invoices plus the held-out bank-swap demo showcase row.
    assert len(cases) == 23
    assert all("vendor_name" in c and "total_cents" in c for c in cases)


def test_metrics_shape():
    m = Service().metrics()
    assert m["total"] == 22
    assert set(m["distribution"]) == {"APPROVE", "HOLD", "EMAIL", "ESCALATE"}
    assert 0 <= m["stp_pct"] <= 100


def test_performance_reports_the_six_metrics():
    p = Service().performance()
    assert p["schema_pass"]["ok"] == p["schema_pass"]["total"] == 22
    assert p["completion_pct"] == 100  # nothing hit the round cap
    assert p["false_approve"] == 0
    assert p["defects_handled"] == p["defects_total"] == 7  # all planted defects handled
    assert p["avg_rounds"] > 0
    # token cost is None over the committed cache (predates the field), and a
    # real number once a live run records usage.
    assert p["avg_tokens_per_run"] is None or p["avg_tokens_per_run"] > 0


def test_performance_metrics_can_show_a_failure():
    """schema_pass and tool_success are measured, not asserted: a parse-failure
    decision and an Error: tool result count as misses."""
    svc = Service()
    svc._cache["INV-V001-3001"] = {
        "invoice_id": "INV-V001-3001",
        "action": "ESCALATE",
        "hold_reason": None,
        "confidence": 0.0,
        "reasoning": "Failed to parse agent response as JSON. Raw text: ...",
        "tool_calls": [
            {"round": 1, "tool_name": "nope", "args": {}, "result": "Error: Tool 'nope'"}
        ],
        "rounds_used": 1,
    }
    p = svc.performance()
    assert p["schema_pass"]["ok"] < p["schema_pass"]["total"]  # the parse failure counts
    assert p["tool_success_pct"] < 100  # the Error: result counts


def test_performance_scores_the_benchmark_view_not_the_raw_cache():
    """An uploaded (session) invoice must not inflate the panel's population —
    performance() scores _benchmark_view(), same as the other scorecards."""
    svc = Service()
    base_total = svc.performance()["schema_pass"]["total"]
    svc._cache["INV-UPLOAD-1"] = {
        "invoice_id": "INV-UPLOAD-1",
        "action": "APPROVE",
        "hold_reason": None,
        "confidence": 0.9,
        "reasoning": "ok",
        "tool_calls": [],
        "rounds_used": 1,
    }
    svc._uploaded.add("INV-UPLOAD-1")
    assert svc.performance()["schema_pass"]["total"] == base_total  # upload excluded


# --- PO sanity screen surfaces --------------------------------------------

FATFINGER_ID = "PO-DEMO-FATFINGER"
OVERORDER_ID = "PO-DEMO-OVERORDER"


def test_pos_list_flags_exactly_the_two_seeded_pos():
    """The PO screening list carries a flag count per PO: every real PO is
    clean (0), and only the two seeded fat-finger POs are flagged."""
    rows = Service().pos()
    by_id = {r["po_id"]: r for r in rows}
    assert by_id[FATFINGER_ID]["flag_count"] >= 1
    assert by_id[OVERORDER_ID]["flag_count"] >= 1
    flagged = sorted(r["po_id"] for r in rows if r["flag_count"] > 0)
    assert flagged == [FATFINGER_ID, OVERORDER_ID], flagged


def test_po_detail_exposes_the_arithmetic_flag_on_the_mistyped_line():
    flags = Service().po_detail(FATFINGER_ID)["sanity_flags"]
    assert len(flags) == 1
    assert flags[0]["signal"] == "ARITHMETIC"
    assert flags[0]["line_no"] == 2  # the A4 paper line
    assert flags[0]["hint"]


def test_po_detail_exposes_the_history_flag_on_the_overorder():
    flags = Service().po_detail(OVERORDER_ID)["sanity_flags"]
    assert len(flags) == 1
    assert flags[0]["signal"] == "HISTORY"
    assert flags[0]["line_no"] == 1  # the toilet-roll line
    assert "usual order" in flags[0]["hint"]


def test_po_detail_unknown_id_raises_keyerror():
    with pytest.raises(KeyError):
        Service().po_detail("PO-DOES-NOT-EXIST")


def test_case_bundle_carries_po_sanity_flags_field():
    """Every case bundle exposes the matched PO's flags; a clean PO gives an
    empty list rather than omitting the field."""
    c = Service().get_case("INV-V005-3018")
    assert c["po_sanity_flags"] == []


def test_analytics_scorecard_covers_every_planted_defect():
    a = Service().analytics()
    assert len(a["defects"]) == 7
    assert all(c["verdict"] == "pass" for c in a["defects"])
    assert a["clean_total"] == 15
    assert a["metrics"]["false_approve_count"] == 0
    assert len(a["vendors"]) == 6


def test_confirm_payment_refused_unless_agent_approved():
    """The human sign-off endpoint re-checks the precondition in code:
    a HOLD invoice cannot be confirmed no matter what the UI sends."""
    svc = Service()
    with pytest.raises(ValueError):
        svc.confirm_payment("INV-V005-3005")  # HOLD · price variance
    case = svc.confirm_payment("INV-V001-3001")  # clean APPROVE
    assert case["human_review"] == "confirmed"


def test_send_to_human_works_for_any_state():
    svc = Service()
    case = svc.send_to_human("INV-V005-3005")
    assert case["human_review"] == "sent_to_human"
    with pytest.raises(KeyError):
        svc.send_to_human("INV-NOPE-0000")


def test_confirmation_is_void_when_the_decision_changes():
    """A sign-off certifies one decision. If the cached decision stops
    being APPROVE, the 'confirmed' state must not be shown against it."""
    svc = Service()
    svc.confirm_payment("INV-V001-3001")
    svc._cache["INV-V001-3001"]["action"] = "HOLD"  # simulate a re-decide
    assert svc.get_case("INV-V001-3001")["human_review"] is None
    listed = {c["invoice_id"]: c for c in svc.list_cases()}
    assert listed["INV-V001-3001"]["human_review"] is None


def test_rerun_resets_human_state(monkeypatch):
    """A re-run is a new decision: any prior sign-off or routing is void."""
    import apagent.api.service as service_module

    svc = Service()
    svc.confirm_payment("INV-V001-3001")

    class FakeDecision:
        def model_dump(self):
            return dict(svc._cache["INV-V001-3001"])

    monkeypatch.setattr(service_module, "decide_invoice", lambda *a, **k: FakeDecision())
    monkeypatch.setattr(svc, "_save_cache", lambda: None)  # keep the repo file untouched
    svc.run_case("INV-V001-3001")
    assert svc._human == {}


def test_send_to_human_records_the_handoff_in_the_outbox():
    """The outbox is the answer to "where did Send go": the code-templated
    hand-off email is recorded with who sent it."""
    svc = Service()
    svc.send_to_human("INV-V006-3019", actor="Norman")
    sent = svc.outbox()
    assert len(sent) == 1
    assert sent[0]["kind"] == "handoff"
    assert sent[0]["sent_by"] == "Norman"
    assert "INV-V006-3019" in sent[0]["subject"]
    assert "rendered by code" in sent[0]["body"]


def test_send_outbound_routes_internal_notes_to_operations():
    """A HOLD's message is an internal ops note — it must go to operations,
    never to the vendor's billing address (no internal-status leak)."""
    svc = Service()
    svc.send_outbound("INV-V006-3019", actor="Norman")  # HOLD/AWAITING_GRN
    sent = svc.outbox()[0]
    assert sent["kind"] == "ops_note"
    assert sent["to"] == "ap-supervisor@demo.local"
    assert "billing@" not in sent["to"]
    assert svc.outbound_recipient("INV-V006-3019") == "ap-supervisor@demo.local"


def test_send_outbound_requires_a_system_message():
    """No free-text path: an invoice whose decision carries no outbound
    message cannot send anything."""
    svc = Service()
    with pytest.raises(ValueError):
        svc.send_outbound("INV-V001-3001")  # clean APPROVE, no outbound message
    assert svc.outbound_recipient("INV-V001-3001") is None


def test_confirm_writes_the_payment_record():
    svc = Service()
    svc.confirm_payment("INV-V001-3001", actor="Norman")
    plan = svc.schedule()
    rec = plan["payment_record"]
    assert len(rec) == 1
    assert rec[0]["invoice_id"] == "INV-V001-3001"
    assert rec[0]["confirmed_by"] == "Norman"
    assert rec[0]["currency"] == "SGD"
    assert rec[0]["voided"] is False


def test_confirm_is_idempotent():
    """A double-POST must not log a second payment for the same sign-off."""
    svc = Service()
    svc.confirm_payment("INV-V001-3001")
    svc.confirm_payment("INV-V001-3001")
    svc.confirm_payment("INV-V001-3001")
    assert len(svc.schedule()["payment_record"]) == 1


def test_confirm_stays_single_row_across_send_to_human():
    """The idempotency guard keys on the record, not the (clobberable)
    human badge: confirm -> send-to-human -> confirm is still one payment."""
    svc = Service()
    svc.confirm_payment("INV-V001-3001")
    svc.send_to_human("INV-V001-3001")  # overwrites _human to sent_to_human
    svc.confirm_payment("INV-V001-3001")
    assert len(svc.schedule()["payment_record"]) == 1


def test_rerun_voids_the_payment_record(monkeypatch):
    """When a re-run changes the decision, the payment record stops
    asserting 'Paid' — it is marked voided, not silently kept."""
    import apagent.api.service as service_module

    svc = Service()
    svc.confirm_payment("INV-V001-3001")

    class HoldDecision:
        def model_dump(self):
            return {"invoice_id": "INV-V001-3001", "action": "HOLD", "hold_reason": "AWAITING_GRN"}

    monkeypatch.setattr(service_module, "decide_invoice", lambda *a, **k: HoldDecision())
    monkeypatch.setattr(svc, "_save_cache", lambda: None)
    svc.run_case("INV-V001-3001")
    rec = svc.schedule()["payment_record"]
    assert len(rec) == 1 and rec[0]["voided"] is True


def test_outbox_endpoint_auth_and_ordering():
    client = _signed_in_client()
    assert client.get("/api/outbox").json() == []
    client.post("/api/invoices/INV-V006-3019/send-to-human")
    client.post("/api/invoices/INV-V006-3019/send-message")
    box = client.get("/api/outbox").json()
    assert [m["kind"] for m in box] == ["ops_note", "handoff"]  # newest first
    assert box[0]["sent_by"] == "Norman"  # session name threaded through


def test_send_message_http_mappings():
    client = _signed_in_client()
    assert client.post("/api/invoices/INV-V001-3001/send-message").status_code == 409  # no message
    assert client.post("/api/invoices/INV-NOPE-0000/send-message").status_code == 404


def test_outbox_and_send_message_need_a_session():
    from fastapi.testclient import TestClient

    from apagent.api.app import app

    client = TestClient(app)
    assert client.get("/api/outbox").status_code == 401
    assert client.post("/api/invoices/INV-V006-3019/send-message").status_code == 401


def test_schedule_marks_confirmed_invoices():
    """The Payments page shows which scheduled invoices a reviewer signed
    off; the annotation lives in the service, the scheduler stays pure."""
    svc = Service()
    svc.confirm_payment("INV-V001-3001")
    svc.send_to_human("INV-V005-3005")
    plan = svc.schedule()
    items = {i["invoice_id"]: i for r in plan["runs"] for p in r["payments"] for i in p["invoices"]}
    assert items["INV-V001-3001"]["confirmed"] is True
    assert items["INV-V005-3018"]["confirmed"] is False
    held = {n["invoice_id"]: n for n in plan["not_scheduled"]}
    assert held["INV-V005-3005"]["human_review"] == "sent_to_human"


def test_analytics_and_metrics_agree():
    svc = Service()
    a = svc.analytics()
    m = svc.metrics()
    assert a["metrics"]["stp_pct"] == m["stp_pct"]
    assert a["metrics"]["touchless_pct"] == m["touchless_pct"]
    assert a["metrics"]["false_approve_count"] == m["false_approve"]
    assert a["distribution"] == m["distribution"]


def _signed_in_client():
    from fastapi.testclient import TestClient

    from apagent.api.app import app

    client = TestClient(app)
    assert client.post("/api/login", json={"name": "Norman"}).status_code == 200
    return client


def test_http_confirm_maps_409_and_404():
    client = _signed_in_client()
    assert client.post("/api/invoices/INV-V005-3005/confirm").status_code == 409
    assert client.post("/api/invoices/INV-NOPE-0000/confirm").status_code == 404
    assert client.post("/api/invoices/INV-NOPE-0000/send-to-human").status_code == 404


def test_api_requires_a_session():
    """Every /api route except login/logout/me answers 401 to strangers —
    the state-changing POSTs are not open to the world."""
    from fastapi.testclient import TestClient

    from apagent.api.app import app

    client = TestClient(app)
    assert client.get("/api/invoices").status_code == 401
    assert client.post("/api/invoices/INV-V001-3001/confirm").status_code == 401
    assert client.get("/api/me").status_code == 401


def test_login_logout_cycle():
    client = _signed_in_client()
    assert client.get("/api/me").json()["name"] == "Norman"
    assert client.get("/api/invoices").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/invoices").status_code == 401


def _uploaded_doc(doc_id="INV-V001-9999"):
    from apagent.schemas import Document, LineItem

    return Document(
        doc_id=doc_id,
        doc_type="INVOICE",
        vendor_id="V001",
        vendor_name="Tan Hardware Supplies Pte Ltd",
        issue_date="2026-08-01",
        ref_doc_id="PO-2026-1001",
        currency="SGD",
        due_date="2026-08-31",
        total_cents=5000,
        lines=[
            LineItem(
                line_no=1,
                sku="X-1",
                description="widget",
                qty=1,
                uom="PCS",
                unit_price_cents=5000,
                line_total_cents=5000,
            )
        ],
    )


def test_upload_stays_out_of_the_committed_cache(monkeypatch, tmp_path):
    """Uploads are session state: decided live, listed in the queue, but
    never written into the committed decisions cache."""
    import json

    import apagent.api.service as service_module

    svc = Service()
    doc = _uploaded_doc()
    monkeypatch.setattr(service_module, "extract_invoice", lambda p, v: doc)

    class FakeDecision:
        def model_dump(self):
            return {"invoice_id": doc.doc_id, "action": "HOLD", "hold_reason": None}

    monkeypatch.setattr(service_module, "decide_invoice", lambda *a, **k: FakeDecision())
    monkeypatch.setattr(service_module, "CACHE", tmp_path / "decisions.json")

    case = svc.upload_invoice("sample.pdf", b"%PDF-fake")
    assert case["invoice_id"] == "INV-V001-9999"
    assert case["decision"]["action"] == "HOLD"
    assert any(c["invoice_id"] == "INV-V001-9999" for c in svc.list_cases())
    saved = json.loads((tmp_path / "decisions.json").read_text(encoding="utf-8"))
    assert "INV-V001-9999" not in saved  # session state, not committed
    with pytest.raises(ValueError):  # same invoice number again -> refused
        svc.upload_invoice("sample.pdf", b"%PDF-fake")


def test_live_rerun_keeps_the_demo_invoice_on_disk(monkeypatch, tmp_path):
    """INV-DEMO-BANKSWAP is held out of the scored rates, not of the file: it
    ships in the dataset so the console shows its ESCALATE on load. A live
    decision on any other invoice rewrites decisions.json, and saving the
    benchmark view alone dropped the demo with it, so the payout gate's
    showcase was gone at the next restart."""
    import json

    import apagent.api.service as service_module

    svc = Service()

    class FakeDecision:
        def model_dump(self):
            return dict(svc._cache["INV-V005-3018"])

    monkeypatch.setattr(service_module, "decide_invoice", lambda *a, **k: FakeDecision())
    monkeypatch.setattr(service_module, "CACHE", tmp_path / "decisions.json")

    svc.run_case("INV-V005-3018")
    saved = json.loads((tmp_path / "decisions.json").read_text(encoding="utf-8"))
    assert saved["INV-DEMO-BANKSWAP"]["action"] == "ESCALATE"
    assert list(saved)[-1] == "INV-DEMO-BANKSWAP"  # same key order as the committed file
    assert "INV-V001-9999" not in saved  # still no session uploads


def test_upload_guards():
    svc = Service()
    with pytest.raises(ValueError):
        svc.upload_invoice("big.pdf", b"x" * (5 * 1024 * 1024 + 1))


def test_handoff_draft_is_code_templated():
    """The internal hand-off email comes from a fixed code template with
    the facts filled in — never model text."""
    case = Service().get_case("INV-V006-3019")  # HOLD, missing GRN
    draft = case["handoff_draft"]
    assert draft["to"] == "ap-supervisor@demo.local"
    assert "INV-V006-3019" in draft["subject"]
    assert "Failed gates:" in draft["body"]
    assert "Goods received" in draft["body"]  # the gate that failed
    assert "rendered by code" in draft["body"]


def test_config_reports_the_enforced_policy():
    k = Service().config_info()
    assert k["tolerances"]["unit_price_pct"] == 2.0
    assert k["tolerances"]["manual_review_threshold_cents"] == 500_000
    by_id = {v["vendor_id"]: v["allowance_pct"] for v in k["contract_allowances"]}
    assert by_id["V005"] == 5.0  # the headline case's negotiated allowance
    assert by_id["V004"] == 3.0  # also parsed from its supply agreement
    assert by_id["V001"] is None  # no clause -> default applies
    assert k["actions"] == ["APPROVE", "HOLD", "EMAIL", "ESCALATE"]


# --- chat-confirmed deliveries on the detail page --------------------------


def _chat_case(monkeypatch, sender_id="88888888"):
    """A service with one chat confirmation harvested against PO-2026-1019."""
    import json as _json

    import apagent.api.service as service_module
    import apagent.chat.harvest as harvest_module
    from apagent.chat.roster import Roster
    from apagent.chat.runner import ChatRunner
    from apagent.schemas import ChatMessage

    chat_id = "-1001234567890"
    monkeypatch.setattr(
        "apagent.agent.loop.call_model",
        lambda messages, tools, system, provider=None: {
            "text": _json.dumps(
                {"action": "APPROVE", "hold_reason": None, "confidence": 0.9, "reasoning": "ok"}
            ),
            "tool_calls": [],
        },
    )
    monkeypatch.setattr(
        harvest_module,
        "extract_delivery_claim",
        lambda w, provider=None: {
            "is_delivery_confirmation": True,
            "po_reference": "PO-2026-1019",
            "items": [
                {"description": "detergent", "qty": None, "complete": True},
                {"description": "nitrile gloves", "qty": "100", "complete": True},
                {"description": "trash bag", "qty": None, "complete": False},
            ],
            "everything_arrived": False,
        },
    )
    svc = service_module.Service()
    monkeypatch.setattr(svc, "_save_cache", lambda: None)
    harvester = svc.chat_harvester()
    harvester.roster = Roster({chat_id: "Ops"}, {"telegram:88888888": "Li Wei (warehouse)"})

    def message(mid, text):
        return ChatMessage(
            message_id=mid,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name="whoever",
            text=text,
            sent_at="2026-08-12T14:30:00",
        )

    class _Adapter:
        platform = "telegram"

        def __init__(self):
            self.pending = [message("1", "PO-2026-1019 delivered"), message("2", "@apbot confirm")]
            self.sent = []

        def poll(self, timeout=30):
            out, self.pending = self.pending, []
            return out

        def mentions_bot(self, m):
            return "@apbot" in m.text

        def reply(self, chat_id, text):
            self.sent.append(text)

    ChatRunner(_Adapter(), harvester, on_receipt=svc.on_chat_receipt).tick()
    return svc


def test_detail_page_carries_the_conversation_and_what_code_read_from_it(monkeypatch):
    """Everything the reviewer needs to judge a chat confirmation is assembled
    server-side — CLAUDE.md keeps business logic out of the frontend, and
    "was this person allowed to confirm" is exactly that."""
    svc = _chat_case(monkeypatch)
    evidence = svc.get_case("INV-V006-3019")["chat_grn"]
    assert evidence["authorised"] is True
    assert evidence["confirmed_by"] == "Li Wei (warehouse)"
    assert [line["qty"] for line in evidence["lines"]] == [10, 100]
    # The item nobody gave a number for is listed as outstanding rather than
    # silently recorded as received.
    assert evidence["unconfirmed"] == ["Trash bag 120L, roll of 20"]
    assert [m["text"] for m in evidence["messages"]]  # verbatim, for the reviewer
    assert evidence["policy"] == "TIERED"


def test_an_ordinary_receipt_shows_no_chat_card(monkeypatch):
    import apagent.api.service as service_module

    svc = service_module.Service()
    monkeypatch.setattr(svc, "_save_cache", lambda: None)
    assert svc.get_case("INV-V001-3001")["chat_grn"] is None


def test_the_supplier_confirming_their_own_delivery_is_flagged(monkeypatch):
    """The case worth demoing. An SME delivery group usually contains the
    vendor; their confirmation is shown, marked unauthorised, and does not
    release payment."""
    svc = _chat_case(monkeypatch, sender_id="55555555")
    case = svc.get_case("INV-V006-3019")
    assert case["chat_grn"]["authorised"] is False
    assert case["decision"]["action"] == "HOLD"
    gate = next(g for g in case["guardrails"] if g["key"] == "grn")
    assert gate["passed"] is False
    assert "chat-confirmed" in gate["label"]


def test_a_reviewer_accepting_does_not_also_accept_the_bill(monkeypatch):
    """Accepting is recorded, and the invoice still holds — correctly.

    The confirmation covered the detergent and the gloves; nobody said the
    trash bags arrived, and the invoice bills for all three. So the reviewer
    vouched for a DELIVERY, and the facts gate keeps refusing the BILL. This
    is the line that keeps the escape hatch from being a rubber stamp."""
    svc = _chat_case(monkeypatch, sender_id="55555555")
    case = svc.accept_chat_grn("INV-V006-3019", actor="123")
    assert case["chat_grn"]["endorsed_by"] == "123"
    assert case["decision"]["action"] == "HOLD"


def test_a_reviewer_can_release_a_fully_confirmed_delivery(monkeypatch):
    """The escape hatch working: one click, not "go record a formal goods
    receipt" — which the businesses this serves do not do, which is why the
    delivery was confirmed in a chat group in the first place."""
    import apagent.chat.harvest as harvest_module

    svc = _chat_case(monkeypatch, sender_id="55555555")
    # Same unauthorised sender, but this time the whole delivery is confirmed.
    monkeypatch.setattr(
        harvest_module,
        "extract_delivery_claim",
        lambda w, provider=None: {
            "is_delivery_confirmation": True,
            "po_reference": "PO-2026-1019",
            "items": [],
            "everything_arrived": True,
        },
    )
    from apagent.chat.runner import ChatRunner
    from apagent.schemas import ChatMessage

    class _Adapter:
        platform = "telegram"

        def __init__(self):
            self.pending = [
                ChatMessage(
                    message_id="9",
                    chat_id="-1001234567890",
                    sender_id="55555555",
                    sender_name="whoever",
                    text="@apbot everything arrived",
                    sent_at="2026-08-12T15:00:00",
                )
            ]

        def poll(self, timeout=30):
            out, self.pending = self.pending, []
            return out

        def mentions_bot(self, m):
            return True

        def reply(self, chat_id, text):
            pass

    ChatRunner(_Adapter(), svc.chat_harvester(), on_receipt=svc.on_chat_receipt).tick()
    assert svc.get_case("INV-V006-3019")["decision"]["action"] == "HOLD"  # unauthorised sender

    case = svc.accept_chat_grn("INV-V006-3019", actor="123")
    assert case["decision"]["action"] == "APPROVE"
    assert case["chat_grn"]["endorsed_by"] == "123"


def test_accepting_is_refused_when_there_is_nothing_to_accept(monkeypatch):
    import apagent.api.service as service_module

    svc = service_module.Service()
    monkeypatch.setattr(svc, "_save_cache", lambda: None)
    with pytest.raises(ValueError, match="chat-confirmed"):
        svc.accept_chat_grn("INV-V001-3001")


def test_settings_reports_the_chat_policy_and_ceiling(monkeypatch):
    """Both decide whether money moves, so both belong on the read-only
    policy page rather than buried in code nobody reads."""
    import apagent.api.service as service_module

    info = service_module.Service().config_info()
    assert info["chat_grn"]["policy"] == "TIERED"
    assert "TRUSTED" in info["chat_grn"]["options"]
    assert info["tolerances"]["informal_grn_ceiling_cents"] == 200_000


def test_the_headline_numbers_are_all_measured_over_the_same_set(monkeypatch):
    """STP used to count this session's decisions while false approvals were
    scored against the committed benchmark — three tiles over one population
    and a fourth over another, presented as one scorecard. A chat
    confirmation pushed STP up without the defect it cleared being
    re-scored."""
    svc = _chat_case(monkeypatch)
    before = Service().metrics()
    after = svc.metrics()
    assert after["stp_pct"] == before["stp_pct"]
    assert after["touchless_pct"] == before["touchless_pct"]
    assert after["false_approve"] == 0
    # The flip is real and still visible — on the invoice's own page, next to
    # the conversation that caused it.
    assert svc.get_case("INV-V006-3019")["chat_grn"] is not None


def test_case_bundle_exposes_payout_account_mismatch():
    c = Service().get_case("INV-DEMO-BANKSWAP")
    pa = c["payout_account"]
    assert pa is not None
    assert pa["matches"] is False
    assert pa["invoice"].endswith("8765")
    assert pa["on_file"].endswith("2345")


def test_case_bundle_payout_account_matches_on_a_real_invoice():
    c = Service().get_case("INV-V001-3001")
    assert c["payout_account"]["matches"] is True


def test_upload_rejects_bytes_that_are_not_a_pdf():
    client = _signed_in_client()
    r = client.post(
        "/api/invoices/upload", files={"file": ("x.pdf", b"garbage", "application/pdf")}
    )
    assert r.status_code in (400, 422), r.text
    assert "PDF" in r.json()["detail"]


def test_currency_chip_fails_when_the_invoice_currency_is_unreadable():
    """The pipeline escalates an unreadable currency; the chip used to show it
    as passed when both sides were None."""
    svc = Service()
    inv = svc.store.get_invoice("INV-V001-3001").model_copy(
        update={"doc_id": "INV-NOCUR", "currency": None}
    )
    svc.store.add_invoice(inv)
    chips = {g["key"]: g["passed"] for g in svc.get_case("INV-NOCUR")["guardrails"]}
    assert chips["currency"] is False


def test_settings_expose_the_tax_cap():
    client = _signed_in_client()
    payload = client.get("/api/config").json()
    assert "'max_tax_pct': 25.0" in str(payload), payload
