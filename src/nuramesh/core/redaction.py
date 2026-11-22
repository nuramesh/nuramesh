"""Which ledger text a read shows (R1.27-H): a person's reason or note is never shown.

Only the exact actor `system` writes engine wording, so only its `reason` and `note` values may be
shown. Every other actor (a person, an agent, a service, a named system part, an unknown value) is
hidden, which fails closed. Pure functions, no I/O; the input is never changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HIDDEN_KEYS = ("reason", "note")
INCIDENT_KIND = "engine_incident"
_DECISION_RECORDED = "decision.recorded"


def reason_text_visible(actor: str | None) -> bool:
    """True exactly when the actor is `system` (case-sensitive; no prefix or other branch)."""
    return actor == "system"


def _shown(value: object) -> bool:
    """A value that counts as text to hide: anything other than None or the empty string."""
    return value is not None and value != ""


def _walk(value: Any, hide: bool, hidden: set[str]) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if hide and key in HIDDEN_KEYS and _shown(item):
                hidden.add(key)
                out[key] = None
            else:
                out[key] = _walk(item, hide, hidden)
        return out
    if isinstance(value, list | tuple):
        return [_walk(item, hide, hidden) for item in value]
    return value


def redact_payload(
    event_type: str, actor: str | None, payload: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The payload as a read may show it, and the sorted names of the keys it hid (R2, R3)."""
    hidden: set[str] = set()
    source: Mapping[str, Any] = payload
    extra: dict[str, Any] = {}
    if event_type == _DECISION_RECORDED and payload.get("kind") == INCIDENT_KIND:
        note = payload.get("note")
        source = {k: v for k, v in payload.items() if k != "note"}
        extra["note_present"] = isinstance(note, str) and note != ""
        if _shown(note):
            hidden.add("note")
    out = _walk(source, not reason_text_visible(actor), hidden)
    out.update(extra)
    return out, sorted(hidden)
