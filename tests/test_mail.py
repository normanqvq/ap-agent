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
from apagent.schemas import EvidenceSource, ToleranceConfig, VendorReplyEvidence


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
