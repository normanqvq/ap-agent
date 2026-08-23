"""Which groups we listen to, and whose word counts as a delivery confirmation.

This is the security boundary of the whole chat feature, and it is a plain
allowlist on purpose: the question "may this person release money" should be
answerable by reading a file, not by tracing logic.

Two separate checks, because they fail differently:

- A chat we were never bound to is ignored outright. Anyone can add a bot to
  a group; that must not be a way to get our attention.
- A sender who is not a listed confirmer still produces evidence -- a reviewer
  should see what was said and by whom -- but the receipt comes out with
  confirmed_by=None, which pipeline.grn_gate refuses to act on by itself.
  Not an error, just weaker evidence.

Everything is keyed on the platform's NUMERIC user id. Display names and
@usernames are the obvious thing to key on and the wrong one: on Telegram a
display name is free text the user edits at will, and a released @username can
be re-registered by somebody else. Keying on either would mean anyone in the
group could become the warehouse manager by renaming themselves. The numeric
id is the only handle the platform will not hand to a different human.

This matters more than it looks, because of who is in these groups. An SME's
delivery chat routinely includes the supplier. A vendor confirming their own
delivery is exactly the conflict of interest a three-way match exists to catch,
so vendors simply never appear in `confirmers`.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_ROSTER = ROOT / "data" / "chat" / "roster.json"
# Committed alongside it as roster.example.json. The live file holds real
# chat and user ids, so it is gitignored for the same reason .env is:
# it is per-deployment configuration, and those ids identify real people.


class Roster:
    """Bound chats and authorised confirmers, loaded from JSON.

    Held in memory and never written back. The roster is an operational
    decision about who may release money, so it changes by editing a file
    someone reviews -- the same reasoning that keeps the tolerance limits out
    of the web UI.
    """

    def __init__(self, bound_chats: dict[str, str], confirmers: dict[str, str]) -> None:
        # chat_id -> a human label for the group, only ever used in logs.
        self._bound_chats = bound_chats
        # "platform:numeric_user_id" -> the canonical employee label. That
        # label is what lands on the receipt and in messages; the display
        # name from chat never does.
        self._confirmers = confirmers

    @classmethod
    def from_file(cls, path: Path | None = None) -> "Roster":
        """Load the roster, or an empty one if the file does not exist.

        A missing file is not an error: it means nothing is bound and nobody
        is authorised, so every message is ignored and no receipt is ever
        created. Failing closed is the right default for a file whose job is
        granting permission -- an install that forgot to configure it gets no
        automation rather than open automation.
        """
        # `or`, not getenv's default argument: .env.example ships
        # APAGENT_CHAT_ROSTER= with an empty value, and an empty string IS
        # set as far as getenv is concerned. That turned into Path("") ->
        # Path(".") -> PermissionError on opening a directory, at app
        # startup, for anyone who copied the example file.
        path = path or Path(os.getenv("APAGENT_CHAT_ROSTER") or DEFAULT_ROSTER)
        if not path.is_file():
            return cls({}, {})
        raw = json.loads(path.read_text(encoding="utf-8"))
        bound = {str(k): str(v.get("label", k)) for k, v in raw.get("bound_chats", {}).items()}
        confirmers = {
            str(k): str(v.get("employee_label") or v.get("employee_id") or k)
            for k, v in raw.get("confirmers", {}).items()
        }
        return cls(bound, confirmers)

    def is_bound(self, chat_id: str) -> bool:
        """Whether we listen to this group at all."""
        return str(chat_id) in self._bound_chats

    def chat_label(self, chat_id: str) -> str | None:
        return self._bound_chats.get(str(chat_id))

    def confirmer_label(self, platform: str, sender_id: str) -> str | None:
        """The internal label for a sender, or None if they may not confirm.

        None is the common case and not a failure -- it is what turns a
        message into evidence-for-a-human instead of grounds to pay.
        """
        return self._confirmers.get(f"{platform}:{sender_id}")
