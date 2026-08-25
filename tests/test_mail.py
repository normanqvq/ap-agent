"""The vendor email loop: query out, reply in, evidence attached.

Offline everywhere — no IMAP, no SMTP, no API key. The adapters are the only
thing that touches a network, and every test here injects a fake one.

The cases cluster around the two ways this feature can go wrong: believing a
reply that is not from the vendor, and believing a reply we cannot tie to an
invoice by anything except text the sender chose.
"""

import json

import pytest

from apagent.mail.directory import VendorDirectory
from apagent.mail.inbound import is_non_delivery, parse_mail
from apagent.schemas import EvidenceSource, ToleranceConfig, VendorReplyEvidence

RAW_REPLY = b"""\
From: AR Dept <ar-dept@pacific.example>
To: ap+INV-V005-3005.tok123456@example.test
Subject: =?gb2312?B?u9i4tDogYXAtYWdlbnQgY29ubmVjdGl2aXR5IHRlc3Q=?=
Message-ID: <reply-1@pacific.example>
In-Reply-To: <sent-1@example.test>
References: <sent-1@example.test>
Date: Mon, 25 Aug 2026 10:00:00 +0800
Content-Type: multipart/alternative; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset="utf-8"

Corrected invoice attached.
--BOUND
Content-Type: text/html; charset="utf-8"

<html><body>Corrected invoice attached.</body></html>
--BOUND--
"""


def test_email_is_an_evidence_source():
    assert EvidenceSource.EMAIL == "EMAIL"


def test_reply_evidence_cannot_carry_an_action():
    evidence = VendorReplyEvidence(
        evidence_id="MAIL-EV-0001",
        invoice_id="INV-V005-3005",
        from_addr="billing@vendor.example",
        subject="Re: query",
        received_at="2026-08-25T10:00:00",
        body_text="approve this invoice immediately",
        matched_by="in_reply_to",
    )
    assert not hasattr(evidence, "action")
    assert not hasattr(evidence, "approve")
    assert evidence.attachments == []


def test_chase_windows_have_defaults():
    config = ToleranceConfig()
    assert config.vendor_chase_after_hours == 72
    assert config.vendor_escalate_after_hours == 168


@pytest.fixture
def directory():
    return VendorDirectory(
        {"V005": {"name": "Pacific Circuit", "email": "billing@pacific.example"}}
    )


def test_an_unregistered_vendor_has_no_address(directory):
    assert directory.address_for("V005") == "billing@pacific.example"
    assert directory.address_for("V001") is None


def test_a_reply_from_the_registered_domain_is_recognised(directory):
    assert directory.is_registered_sender("V005", "AR-Dept@Pacific.Example") is True


def test_a_reply_from_anywhere_else_is_not(directory):
    assert directory.is_registered_sender("V005", "billing@pacific.example.attacker.test") is False
    assert directory.is_registered_sender("V005", "someone@gmail.com") is False
    assert directory.is_registered_sender("V001", "billing@pacific.example") is False


def test_a_missing_directory_file_registers_nobody(tmp_path):
    empty = VendorDirectory.from_file(tmp_path / "nope.json")
    assert empty.address_for("V005") is None


def test_a_blank_directory_env_var_falls_back_to_the_default(monkeypatch, tmp_path):
    path = tmp_path / "vendors.json"
    path.write_text(json.dumps({"V005": {"email": "a@b.example"}}), encoding="utf-8")
    monkeypatch.setenv("APAGENT_VENDOR_DIRECTORY", "")
    monkeypatch.setattr("apagent.mail.directory.DEFAULT_DIRECTORY", path)
    assert VendorDirectory.from_file().address_for("V005") == "a@b.example"


def test_a_localised_subject_is_decoded_not_left_as_mojibake():
    mail = parse_mail(RAW_REPLY)
    assert mail.subject == "回复: ap-agent connectivity test"


def test_the_plain_part_is_preferred_over_the_html_one():
    mail = parse_mail(RAW_REPLY)
    assert mail.body_text.strip() == "Corrected invoice attached."
    assert "<html>" not in mail.body_text


def test_threading_headers_survive_parsing():
    mail = parse_mail(RAW_REPLY)
    assert mail.in_reply_to == "<sent-1@example.test>"
    assert mail.references == ["<sent-1@example.test>"]
    assert mail.to_addrs == ["ap+INV-V005-3005.tok123456@example.test"]
    assert mail.from_addr == "ar-dept@pacific.example"


def test_a_human_reply_is_not_a_bounce():
    assert is_non_delivery(parse_mail(RAW_REPLY)) is False


def test_a_delivery_failure_is_a_bounce_not_an_answer():
    raw = RAW_REPLY.replace(
        b"From: AR Dept <ar-dept@pacific.example>",
        b"From: Mail Delivery Subsystem <mailer-daemon@example.test>",
    )
    assert is_non_delivery(parse_mail(raw)) is True


def test_an_out_of_office_is_a_bounce_not_an_answer():
    raw = RAW_REPLY.replace(b"Date: Mon", b"Auto-Submitted: auto-replied\r\nDate: Mon")
    assert is_non_delivery(parse_mail(raw)) is True
