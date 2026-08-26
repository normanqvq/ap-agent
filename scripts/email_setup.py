"""Prove the mail credentials work, without ever printing one.

Every failure this checks for is one that would otherwise surface much later,
inside a background poller, as silence. An app password with the spaces left
in, IMAP credentials that are fine but SMTP ones that are not, a provider key
that was never actually saved -- all of them look identical from the console:
the feature simply does nothing.

Nothing here echoes a secret. Addresses are masked, passwords are reported
only as a length, and the LLM check sends two words and prints whether an
answer came back. That is deliberate: the whole point of putting credentials
in .env rather than in a chat is that they stay out of transcripts, and a
setup script that helpfully prints them back defeats it.

    .venv/Scripts/python.exe scripts/email_setup.py
    .venv/Scripts/python.exe scripts/email_setup.py --send-test

--send-test sends one real message to the vendor address registered for V005,
so the round trip can be tried end to end before any of the product code
exists.
"""

import imaplib
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
VENDORS = ROOT / "data" / "email" / "vendors.json"

REQUIRED = [
    "IMAP_HOST",
    "IMAP_USER",
    "IMAP_PASSWORD",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "APAGENT_MAIL_FROM",
]


def mask(address: str) -> str:
    """someone@example.com -> so***@example.com. Enough to spot a typo in
    the domain, not enough to hand the address to anyone reading a log.

    The example is invented on purpose: this file is committed, and
    data/email/vendors.json is gitignored precisely because a real address
    names a real person."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    return f"{local[:2]}***@{domain}"


def secret(name: str) -> str:
    """Read a credential and strip whitespace.

    Gmail displays an app password as four space-separated groups, and it is
    copied that way more often than not. The spaces are not part of it, and
    the resulting login failure says only "invalid credentials".
    """
    raw = os.getenv(name, "")
    return "".join(raw.split())


def check_env() -> bool:
    missing = [name for name in REQUIRED if not os.getenv(name, "").strip()]
    if missing:
        print(f"  MISSING in .env: {', '.join(missing)}")
        return False
    for name in ("IMAP_PASSWORD", "SMTP_PASSWORD"):
        raw = os.getenv(name, "")
        if raw != "".join(raw.split()):
            print(f"  note: {name} contains spaces; they are being stripped")
        print(f"  {name}: {len(secret(name))} characters")
    print(f"  IMAP_USER: {mask(os.getenv('IMAP_USER', ''))}")
    print(f"  SMTP_USER: {mask(os.getenv('SMTP_USER', ''))}")
    print(f"  APAGENT_MAIL_FROM: {mask(os.getenv('APAGENT_MAIL_FROM', ''))}")
    return True


def check_imap() -> bool:
    host = os.getenv("IMAP_HOST", "")
    try:
        with imaplib.IMAP4_SSL(host) as imap:
            imap.login(os.getenv("IMAP_USER", ""), secret("IMAP_PASSWORD"))
            status, data = imap.select("INBOX", readonly=True)
            if status != "OK":
                print(f"  logged in, but INBOX did not open: {status}")
                return False
            print(f"  OK -- {host}, INBOX holds {int(data[0])} messages")
            return True
    except Exception as exc:  # noqa: BLE001 - a setup script should say what broke
        print(f"  FAILED -- {type(exc).__name__}: {exc}")
        return False


def _smtp_connect() -> smtplib.SMTP:
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    server = smtplib.SMTP(host, port, timeout=30)
    server.starttls()
    server.login(os.getenv("SMTP_USER", ""), secret("SMTP_PASSWORD"))
    return server


def check_smtp() -> bool:
    try:
        with _smtp_connect() as server:
            server.noop()
        print(f"  OK -- {os.getenv('SMTP_HOST')}:{os.getenv('SMTP_PORT')}, STARTTLS, authenticated")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED -- {type(exc).__name__}: {exc}")
        return False


def check_llm() -> bool:
    """Two words to the configured provider. Catches a key that was pasted
    into the wrong variable, which otherwise only shows up when the decision
    cache is regenerated."""
    provider = os.getenv("LLM_PROVIDER", "anthropic")
    try:
        from apagent.llm.client import call_model

        reply = call_model(
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            tools=[],
            system="You are a connectivity check.",
        )
        print(f"  OK -- {provider} answered: {(reply.get('text') or '').strip()[:40]!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED -- {provider}: {type(exc).__name__}: {exc}")
        return False


def send_test() -> bool:
    """One real message to the registered vendor address."""
    if not VENDORS.exists():
        print(f"  no vendor directory at {VENDORS.relative_to(ROOT)}")
        return False
    directory = json.loads(VENDORS.read_text(encoding="utf-8"))
    entry = directory.get("V005")
    if not entry:
        print("  V005 is not in the vendor directory")
        return False
    to = entry["email"]

    message = EmailMessage()
    message["From"] = os.getenv("APAGENT_MAIL_FROM", "")
    message["To"] = to
    message["Subject"] = "ap-agent connectivity test"
    message.set_content(
        "This is a connectivity test from ap-agent's setup script.\n"
        "Reply to it and the reply should land in the configured inbox."
    )
    try:
        with _smtp_connect() as server:
            server.send_message(message)
        print(f"  sent to {mask(to)} -- reply to it, then re-run with --check-reply")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED -- {type(exc).__name__}: {exc}")
        return False


def check_reply() -> bool:
    """Look for anything in the inbox from the registered vendor address.

    Crude on purpose: this only proves mail arrives. Correlating a reply to
    an invoice is the product code's job, by message headers rather than by
    who sent it.
    """
    if not VENDORS.exists():
        print(f"  no vendor directory at {VENDORS.relative_to(ROOT)}")
        return False
    to = json.loads(VENDORS.read_text(encoding="utf-8"))["V005"]["email"]
    try:
        with imaplib.IMAP4_SSL(os.getenv("IMAP_HOST", "")) as imap:
            imap.login(os.getenv("IMAP_USER", ""), secret("IMAP_PASSWORD"))
            imap.select("INBOX", readonly=True)
            status, data = imap.search(None, "FROM", f'"{to}"')
            ids = data[0].split() if status == "OK" and data and data[0] else []
            print(f"  {len(ids)} message(s) from {mask(to)} in the inbox")
            return bool(ids)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED -- {type(exc).__name__}: {exc}")
        return False


def main() -> None:
    load_dotenv(ROOT / ".env")
    print("\n.env")
    if not check_env():
        sys.exit(1)
    print("\nIMAP")
    imap_ok = check_imap()
    print("\nSMTP")
    smtp_ok = check_smtp()
    print("\nLLM")
    llm_ok = check_llm()

    if "--send-test" in sys.argv:
        print("\nTest send")
        send_test()
    if "--check-reply" in sys.argv:
        print("\nReply check")
        check_reply()

    print()
    if imap_ok and smtp_ok and llm_ok:
        print("All three are good. Nothing here was printed that is worth hiding.")
    else:
        print("Something above failed -- fix it in .env, then run this again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
