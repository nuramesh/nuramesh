"""MCP tool pinning (spec/mcp-gateway rules 10 to 20, #910): hash, diff, static checks, pins.

Pure (stdlib and pydantic). A **pin** is the organization's approval of one exact tool definition
(the whole object the server lists: name, description, schemas, annotations). The gateway compares
the listed definition with the pin on every call and at every session review; a difference blocks
the tool until a human decides on the exact candidate hash. Static checks flag instruction-like
text for the approver; a finding names a rule and a JSON pointer, never the text.

Errors never carry a value of the definition.
"""

from __future__ import annotations

import hashlib
import re
import threading
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nuramesh.core.canonical import canonical_json
from nuramesh.core.integrations import definition_hash_of
from nuramesh.core.people import is_human_id

MAX_DEFINITION_BYTES: Final = 262_144
MAX_DIFF_ENTRIES: Final = 200
MAX_TEXT: Final = 4_000
CONFIG_APPROVER: Final = "config"
"""`decided_by` of a pin seeded from a hash in reviewed configuration (rule 15)."""


class DefinitionUnavailable(ValueError):
    """A listed tool object has no definition hash (not a mapping, no name, a float, too big)."""


class PinDecisionError(ValueError):
    """A pin decision is not allowed: not a human, or not the candidate the pin holds."""


class PinConflict(Exception):
    """The pin changed since it was read (compare-and-set lost)."""


def definition_hash(tool_object: Any) -> str:
    """The definition hash of one listed tool object: `sha256` of its canonical JSON
    (`core.integrations.definition_hash_of`), or `DefinitionUnavailable`."""
    if not isinstance(tool_object, Mapping) or not isinstance(tool_object.get("name"), str):
        raise DefinitionUnavailable("the tool object is not a mapping with a text name")
    try:
        size = len(canonical_json(dict(tool_object)))
    except (ValueError, TypeError, RecursionError):
        size = -1
    if size < 0:
        raise DefinitionUnavailable("the tool object has no canonical JSON")
    if size > MAX_DEFINITION_BYTES:
        raise DefinitionUnavailable("the tool object is too large")
    return definition_hash_of(tool_object)


# --- diff (rule 19) --------------------------------------------------------------------------


class DiffEntry(BaseModel):
    """One changed place of a definition. Hashes are plain (a definition is configuration)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    change: Literal["added", "removed", "changed"]
    old_hash: str | None
    new_hash: str | None


def _value_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _pointer(path: str, key: str | int) -> str:
    return f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"


_ABSENT: Final = object()


def _diff(old: Any, new: Any, path: str, out: list[DiffEntry]) -> None:
    if old is _ABSENT:
        out.append(DiffEntry(path=path, change="added", old_hash=None, new_hash=_value_hash(new)))
    elif new is _ABSENT:
        out.append(DiffEntry(path=path, change="removed", old_hash=_value_hash(old), new_hash=None))
    elif isinstance(old, Mapping) and isinstance(new, Mapping):
        for key in sorted({*old, *new}):
            _diff(old.get(key, _ABSENT), new.get(key, _ABSENT), _pointer(path, key), out)
    elif _is_list(old) and _is_list(new):
        for index in range(max(len(old), len(new))):
            _diff(
                old[index] if index < len(old) else _ABSENT,
                new[index] if index < len(new) else _ABSENT,
                _pointer(path, index),
                out,
            )
    elif type(old) is not type(new) or old != new:
        out.append(
            DiffEntry(
                path=path, change="changed", old_hash=_value_hash(old), new_hash=_value_hash(new)
            )
        )


def _is_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def diff_definitions(
    old: Mapping[str, Any], new: Mapping[str, Any]
) -> tuple[tuple[DiffEntry, ...], bool]:
    """The places where two definitions differ, sorted by JSON pointer, and whether the list was
    cut at `MAX_DIFF_ENTRIES`. Both must have a definition hash (canonical JSON exists)."""
    out: list[DiffEntry] = []
    _diff(old, new, "", out)
    out.sort(key=lambda e: e.path)
    return tuple(out[:MAX_DIFF_ENTRIES]), len(out) > MAX_DIFF_ENTRIES


# --- static checks (rule 20) -----------------------------------------------------------------

FindingRule = Literal[
    "encoded_blob",
    "hidden_characters",
    "instruction_phrase",
    "markdown_link",
    "mixed_script",
    "name_shadow",
    "oversize_text",
    "sensitive_reference",
    "url",
    "url_scheme",
]


class Finding(BaseModel):
    """A flag for the approver: which rule, where. Never the text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: FindingRule
    path: str


_INSTRUCTION: Final = re.compile(
    "|".join(
        (
            r"ignore (all |any )?(the )?(previous|prior|above|earlier) "
            r"(instructions|messages|rules)",
            r"disregard (all |any )?(the )?(previous|prior|above|earlier)",
            r"do not (tell|inform|mention|reveal|show).{0,40}(user|human)",
            r"(system|developer) prompt",
            r"you (must|should) (always|first|now)",
            r"before (using|calling|you use|you call) (this|any|the|another) tool",
            r"(always|must|first) (call|use|invoke|run) ",
            r"instead of (calling|using)",
            r"<(important|system|instructions?|secret)>",
        )
    )
)
_SENSITIVE: Final = re.compile(
    r"\.ssh|id_rsa|\.env\b|/etc/passwd|private key|api[ _-]?key|password|credential"
)
_URL: Final = re.compile(r"https?://")
_SCHEME: Final = re.compile(
    r"\b(?:file|ftp|wss?|gopher):/"
    r"|\b(?:javascript|vbscript):"
    r"|\bdata:(?:[a-z0-9.+-]+/[a-z0-9.+-]+|;base64)"
)
# Linear-time by construction (a hostile server controls this text), and it accepts everything the
# earlier backtracking pattern `\[[^\]\n]*\]\([^)\n]*\)|^\s*\[[^\]\n]+\]:\s*\S+` (MULTILINE) did.
# Inline form: no "[" in the label (a label holding "[" also matches from the inner "["), and one
# rfind per line decides whether a ")" closes the target. Reference form: read with str methods,
# not a regex; leading blanks are Unicode whitespace (`\s`), the label is the text up to the first
# "]" (at least one character), and the blanks after the colon may cross newlines.
_LINK_OPEN: Final = re.compile(r"\[[^\[\]\n]*\]\(")


def _is_reference(line: str, after_line: int, content_end: int) -> bool:
    """`[x]: y` at the start of a line, blanks allowed; `y` may start on a later line."""
    body = line.lstrip()
    if not body.startswith("["):
        return False
    close = body.find("]", 1)
    if close < 2 or body[close + 1 : close + 2] != ":":
        return False
    # Offset in the whole text just after the colon; some non-blank character must follow it.
    colon_end = after_line - len(body) + close + 2
    return colon_end < content_end


def _has_markdown_link(text: str) -> bool:
    """An inline link `[x](y)` or a reference definition `[x]: y`."""
    content_end = len(text.rstrip())
    offset = 0
    for line in text.split("\n"):
        offset += len(line) + 1
        # `offset - 1` is the end of this line in the whole text (just before its newline).
        if _is_reference(line, offset - 1, content_end):
            return True
        last = line.rfind(")")
        if last >= 0 and any(m.end() <= last for m in _LINK_OPEN.finditer(line)):
            return True
    return False


_WORD: Final = re.compile(r"[^\W\d_]+")
_LOOKALIKE_SCRIPTS: Final = ("CYRILLIC", "GREEK")
_BLOB: Final = re.compile(r"[A-Za-z0-9+/=_-]{120,}")
_URL_FREE_KEYS: Final = frozenset({"$schema", "$ref", "$id"})
_PLAIN_CONTROLS: Final = frozenset("\t\n\r")


def _hidden(text: str) -> bool:
    return any(
        ch not in _PLAIN_CONTROLS
        and (unicodedata.category(ch) in ("Cf", "Cc") or 0xE0000 <= ord(ch) <= 0xE007F)
        for ch in text
    )


def _mixed_script(text: str) -> bool:
    """A word that mixes Latin letters with Cyrillic or Greek ones (a look-alike spelling)."""
    for word in _WORD.findall(text):
        if word.isascii():
            continue
        scripts = {unicodedata.name(ch, "").split(" ", 1)[0] for ch in word}
        if "LATIN" in scripts and scripts & set(_LOOKALIKE_SCRIPTS):
            return True
    return False


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _scan_text(text: str, key: str | int | None, path: str, out: set[tuple[str, str]]) -> None:
    folded = _fold(text)
    if _hidden(text):
        out.add(("hidden_characters", path))
    if _INSTRUCTION.search(folded):
        out.add(("instruction_phrase", path))
    if _SENSITIVE.search(folded):
        out.add(("sensitive_reference", path))
    if key not in _URL_FREE_KEYS and _URL.search(folded):
        out.add(("url", path))
    if key not in _URL_FREE_KEYS and _SCHEME.search(folded):
        out.add(("url_scheme", path))
    if _has_markdown_link(text):
        out.add(("markdown_link", path))
    if not text.isascii() and _mixed_script(text):
        out.add(("mixed_script", path))
    if _BLOB.search(text):
        out.add(("encoded_blob", path))
    if len(text) > MAX_TEXT:
        out.add(("oversize_text", path))


def _scan(value: Any, key: str | int | None, path: str, out: set[tuple[str, str]]) -> None:
    if isinstance(value, str):
        _scan_text(value, key, path, out)
    elif isinstance(value, Mapping):
        for name, item in value.items():
            _scan(item, name, _pointer(path, name), out)
    elif _is_list(value):
        for index, item in enumerate(value):
            _scan(item, index, _pointer(path, index), out)


def scan_definition(tool_object: Mapping[str, Any]) -> tuple[Finding, ...]:
    """The static-check findings of a definition: every text value, sorted by rule then path."""
    out: set[tuple[str, str]] = set()
    for name, item in tool_object.items():
        if name != "name":  # the name is checked against the tool pattern, not as prose
            _scan(item, name, _pointer("", name), out)
    return tuple(Finding.model_validate({"rule": r, "path": p}) for r, p in sorted(out))


_LOOKALIKES: Final = {
    **dict(zip("аеорсхуіјѕԁӏ", "aeopcxyijsdl", strict=True)),  # Cyrillic
    **dict(zip("αεικνορτυχ", "aeikvoptux", strict=True)),  # Greek
}
_SEPARATORS: Final = re.compile(r"[-_.\s]+")
_MIN_FUZZY_LENGTH: Final = 6


def _name_key(name: str) -> str:
    """The comparison form of a tool name: folded, separators removed, look-alike letters mapped
    to the Latin letter they imitate (a small table of the common Cyrillic and Greek ones)."""
    out = [_LOOKALIKES.get(ch, ch) for ch in unicodedata.normalize("NFKC", name).casefold()]
    return _SEPARATORS.sub("", "".join(out))


def _within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def name_shadows(server: str, tool: str, others: Sequence[tuple[str, str]]) -> bool:
    """True when `tool` of `server` imitates another registered `(server, tool)`: the same name on
    another server, the same name apart from case, separators or a look-alike letter, or a name
    one edit away from a longer one (shorter than `_MIN_FUZZY_LENGTH` is only compared exactly).
    A tool never shadows itself."""
    key = _name_key(tool)
    for other_server, other_tool in others:
        if (other_server, other_tool) == (server, tool):
            continue
        other_key = _name_key(other_tool)
        if other_tool == tool or key == other_key:
            return True
        if (
            min(len(key), len(other_key)) >= _MIN_FUZZY_LENGTH
            and other_server == server
            and _within_one_edit(key, other_key)
        ):
            return True
    return False


# --- pins (rules 10 to 16) -------------------------------------------------------------------


class PinState(StrEnum):
    """Where a pin stands."""

    PENDING = "pending"  # seen, never approved
    APPROVED = "approved"
    BLOCKED = "blocked"  # approved once; a different definition was listed


class PinVerdict(StrEnum):
    """What a listed definition hash means for a pin."""

    MATCH = "match"
    FIRST_SEEN = "first_seen"
    PENDING = "pending"
    CHANGED = "changed"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class ToolPin(BaseModel):
    """The pin of one tool of one server of one organization. The two definitions are kept for the
    approver's diff view; they never go into a ledger entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=200)
    server: str
    tool: str
    state: PinState
    approved_hash: str | None = None
    approved_definition: Mapping[str, Any] | None = None
    candidate_hash: str | None = None
    candidate_definition: Mapping[str, Any] | None = None
    rejected_hash: str | None = None
    approval_id: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    version: int = Field(default=0, ge=0)


def check_pin(pin: ToolPin | None, listed_hash: str) -> PinVerdict:
    """Compare a listed definition hash with the pin (rules 10 to 14)."""
    if pin is None:
        return PinVerdict.FIRST_SEEN
    if pin.state is PinState.APPROVED and listed_hash == pin.approved_hash:
        return PinVerdict.MATCH
    if pin.state is PinState.BLOCKED and listed_hash in (pin.candidate_hash, pin.approved_hash):
        return PinVerdict.BLOCKED  # rule 12: listing the approved definition again unblocks nothing
    if pin.state is PinState.PENDING and listed_hash == pin.candidate_hash:
        return PinVerdict.PENDING
    if listed_hash == pin.rejected_hash:
        return PinVerdict.REJECTED
    return PinVerdict.CHANGED


def first_seen_pin(
    org_id: str,
    server: str,
    tool: str,
    definition: Mapping[str, Any],
    approval_id: str | None = None,
) -> ToolPin:
    """The `pending` pin of a tool seen for the first time (rule 10)."""
    return ToolPin(
        org_id=org_id,
        server=server,
        tool=tool,
        state=PinState.PENDING,
        candidate_hash=definition_hash(definition),
        candidate_definition=dict(definition),
        approval_id=approval_id,
    )


def with_candidate(
    pin: ToolPin, definition: Mapping[str, Any], approval_id: str | None = None
) -> ToolPin:
    """The pin after a different definition was listed: `blocked` if one was approved, else still
    `pending`, with the new candidate and its approval (rules 11 and 13)."""
    return pin.model_copy(
        update={
            "state": PinState.PENDING if pin.approved_hash is None else PinState.BLOCKED,
            "candidate_hash": definition_hash(definition),
            "candidate_definition": dict(definition),
            "approval_id": approval_id,
        }
    )


def _check_decision(pin: ToolPin, candidate_hash: str, by: str) -> None:
    if not is_human_id(by):
        raise PinDecisionError("only a human decides a pin")
    if pin.candidate_hash is None or pin.candidate_hash != candidate_hash:
        raise PinDecisionError("the decision is not for the definition the pin is waiting on")


def approved_pin(pin: ToolPin, candidate_hash: str, by: str, at: datetime) -> ToolPin:
    """The pin after a human approved exactly `candidate_hash` (rule 14)."""
    _check_decision(pin, candidate_hash, by)
    return pin.model_copy(
        update={
            "state": PinState.APPROVED,
            "approved_hash": pin.candidate_hash,
            "approved_definition": pin.candidate_definition,
            "candidate_hash": None,
            "candidate_definition": None,
            "approval_id": None,
            "decided_by": by,
            "decided_at": at,
        }
    )


def rejected_pin(pin: ToolPin, candidate_hash: str, by: str, at: datetime) -> ToolPin:
    """The pin after a human rejected exactly `candidate_hash`: the approved definition (if any)
    stays in force and the rejected hash is remembered (rule 14)."""
    _check_decision(pin, candidate_hash, by)
    return pin.model_copy(
        update={
            "state": PinState.PENDING if pin.approved_hash is None else PinState.APPROVED,
            "rejected_hash": pin.candidate_hash,
            "candidate_hash": None,
            "candidate_definition": None,
            "approval_id": None,
            "decided_by": by,
            "decided_at": at,
        }
    )


def seeded_pin(
    org_id: str, server: str, tool: str, definition: Mapping[str, Any], at: datetime
) -> ToolPin:
    """An `approved` pin from a hash pinned in reviewed configuration (rule 15)."""
    return ToolPin(
        org_id=org_id,
        server=server,
        tool=tool,
        state=PinState.APPROVED,
        approved_hash=definition_hash(definition),
        approved_definition=dict(definition),
        decided_by=CONFIG_APPROVER,
        decided_at=at,
    )


class PinStore(Protocol):
    """The port. Every method takes the organization id; there is no listing across them."""

    def get(self, org_id: str, server: str, tool: str) -> ToolPin | None:
        """The pin, or None."""
        ...

    def save(self, pin: ToolPin, *, expected_version: int | None) -> ToolPin:
        """Create (`expected_version=None`) or replace the pin read at `expected_version`; the
        saved pin has the next version. `PinConflict` when the stored pin is not that one."""
        ...

    def list_for(self, org_id: str, server: str) -> list[ToolPin]:
        """The pins of one server of one organization, by tool name."""
        ...


class InMemoryPinStore:
    """`PinStore` in memory (tests and single-process use)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pins: dict[tuple[str, str, str], ToolPin] = {}

    def get(self, org_id: str, server: str, tool: str) -> ToolPin | None:
        with self._lock:
            return self._pins.get((org_id, server, tool))

    def save(self, pin: ToolPin, *, expected_version: int | None) -> ToolPin:
        key = (pin.org_id, pin.server, pin.tool)
        with self._lock:
            stored = self._pins.get(key)
            current = None if stored is None else stored.version
            if current == expected_version:
                saved = pin.model_copy(update={"version": (current or 0) + 1})
                self._pins[key] = saved
                return saved
        raise PinConflict("the pin changed since it was read")

    def list_for(self, org_id: str, server: str) -> list[ToolPin]:
        with self._lock:
            found = [p for (o, s, _t), p in self._pins.items() if o == org_id and s == server]
        return sorted(found, key=lambda p: p.tool)
