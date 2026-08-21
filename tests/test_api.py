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
    assert len(cases) == 22
    assert all("vendor_name" in c and "total_cents" in c for c in cases)


def test_metrics_shape():
    m = Service().metrics()
    assert m["total"] == 22
    assert set(m["distribution"]) == {"APPROVE", "HOLD", "EMAIL", "ESCALATE"}
    assert 0 <= m["stp_pct"] <= 100


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


def test_email_vendor_only_sends_the_system_message():
    """Vendor email exists only when the decision carries an outbound
    message — there is no free-text path."""
    svc = Service()
    entry = svc.email_vendor("INV-V006-3019", actor="Norman")  # HOLD with outbound
    assert entry["kind"] == "vendor_query"
    assert entry["to"] == "billing@v006.example.com"
    with pytest.raises(ValueError):
        svc.email_vendor("INV-V001-3001")  # clean APPROVE, no outbound message


def test_confirm_writes_the_payment_record():
    svc = Service()
    svc.confirm_payment("INV-V001-3001", actor="Norman")
    plan = svc.schedule()
    rec = plan["payment_record"]
    assert len(rec) == 1
    assert rec[0]["invoice_id"] == "INV-V001-3001"
    assert rec[0]["confirmed_by"] == "Norman"
    assert rec[0]["currency"] == "SGD"


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
    saved = json.loads((tmp_path / "decisions.json").read_text())
    assert "INV-V001-9999" not in saved  # session state, not committed
    with pytest.raises(ValueError):  # same invoice number again -> refused
        svc.upload_invoice("sample.pdf", b"%PDF-fake")


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
