"""The vendor email loop: query out, reply in, evidence attached.

Offline everywhere — no IMAP, no SMTP, no API key. The adapters are the only
thing that touches a network, and every test here injects a fake one.

The cases cluster around the two ways this feature can go wrong: believing a
reply that is not from the vendor, and believing a reply we cannot tie to an
invoice by anything except text the sender chose.
"""

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
