"""Who we may write to, and whose reply counts as the vendor's.

One file doing two jobs on purpose. Outbound it is an allowlist: an invoice
whose vendor has no registered address is never mailed, it goes to a human.
Inbound it is the sender check: a reply from outside the registered domain is
kept as evidence but takes no automatic path.

Keeping both in one place means the question "can this vendor be automated
with" has one answer in one file, the way roster.json answers "may this
person confirm a delivery". Two separate lists would drift, and the drift
would be invisible until a reply from an address we happily write to was
quietly refused.

The check is on the DOMAIN, not the full address. An AP query goes to
billing@ and gets answered by whoever picks it up — ar-dept@, a named person,
a ticketing system. Demanding an exact match would refuse ordinary replies.
Matching a suffix instead would be worse than useless: `pacific.example`
would match `pacific.example.attacker.test`, so the comparison is on the
whole domain, split at the last @.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_DIRECTORY = ROOT / "data" / "email" / "vendors.json"


def domain_of(address: str) -> str:
    """The domain half, lowercased. Empty string if there is not one."""
    _, _, domain = address.strip().rpartition("@")
    return domain.strip().strip(">").lower()


class VendorDirectory:
    """vendor_id -> where we write and who may answer, loaded from JSON."""

    def __init__(self, entries: dict[str, dict]) -> None:
        self._entries = {
            str(k): v for k, v in entries.items() if isinstance(v, dict) and v.get("email")
        }

    @classmethod
    def from_file(cls, path: Path | None = None) -> "VendorDirectory":
        """Load it, or an empty directory if the file is missing.

        Failing closed: an install that never configured this sends no mail
        at all, rather than sending to whatever address a document happened
        to carry. Same reasoning as Roster.from_file.
        """
        # `or`, not getenv's default: .env.example ships
        # APAGENT_VENDOR_DIRECTORY= with an empty value, and an empty string
        # IS set as far as getenv is concerned — the bug already fixed once
        # for the chat roster and once for the model overrides.
        path = path or Path(os.getenv("APAGENT_VENDOR_DIRECTORY") or DEFAULT_DIRECTORY)
        if not path.is_file():
            return cls({})
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls({k: v for k, v in raw.items() if not k.startswith("_")})

    def address_for(self, vendor_id: str) -> str | None:
        entry = self._entries.get(str(vendor_id))
        return entry["email"] if entry else None

    def is_registered_sender(self, vendor_id: str, address: str) -> bool:
        registered = self.address_for(vendor_id)
        if not registered:
            return False
        return domain_of(address) == domain_of(registered)
