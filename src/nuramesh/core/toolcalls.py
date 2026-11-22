"""The pure rules of the adapter tool-call contract (`spec/adapters/README.md`, S4).

What every adapter and both transports of the seam (`nuramesh.adapters._toolcheck`) share and
what needs no store: the tool name (AD-7), the call id (AD-8), which arguments are readable
(AD-9), the arguments digest (AD-10), the manifest and `tool_set_hash` (AD-14 to AD-16), the
reason codes with their kind (section 9.1, AD-41), the two fixed texts a model may see (AD-38)
and the ranges of the two options (AD-42, AD-46).

Nothing here writes, logs or raises the arguments, a tool's text or the plain digest's input:
errors carry fixed words only (AD-65). The plain `digest` is for the party that writes the ledger
to fold into the keyed `args_hash`; it is never itself recorded (AD-26).
"""

import hashlib
import json
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nuramesh.core.canonical import MAX_SAFE_INTEGER, canonical_json_jcs

ADAPTERS = frozenset({"langchain", "langgraph", "openai_agents", "claude_agent_sdk"})
"""The `adapter` values of ledger payloads (section 2); the set only grows."""

_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_HEX64 = re.compile(r"[0-9a-f]{64}")

MAX_ARGUMENTS_TEXT_BYTES = 4_194_304
MAX_CANONICAL_BYTES = 1_048_576
MAX_DEPTH = 64
MAX_TOOLS = 256

DENIAL_LIMIT = 3
DEADLINE_WAIT_SECONDS = 600
DEADLINE_DURABLE_SECONDS = 86_400
DEADLINE_MAX_SECONDS = 604_800


class ReasonCode(StrEnum):
    """The stable reason codes of section 9.1. Codes are only added, never renamed."""

    ALLOW = "allow"
    ASK_PENDING = "ask_pending"
    DENIED_BY_RULE = "denied_by_rule"
    REJECTED_BY_PERSON = "rejected_by_person"
    APPROVAL_TIMEOUT = "approval_timeout"
    APPROVAL_EXPIRED = "approval_expired"
    SERVER_UNAVAILABLE = "server_unavailable"
    ROLE_MISMATCH = "role_mismatch"
    UNREADABLE_ARGUMENTS = "unreadable_arguments"
    UNKNOWN_ACTION_KIND = "unknown_action_kind"
    CREDENTIAL_REJECTED = "credential_rejected"
    UNREADABLE_TOOL_NAME = "unreadable_tool_name"
    UNKNOWN_DELEGATE = "unknown_delegate"
    ARGUMENTS_CHANGED = "arguments_changed"
    TOOL_NOT_FROZEN = "tool_not_frozen"
    TOOL_SET_CHANGED = "tool_set_changed"
    TOOL_REPLACED = "tool_replaced"
    ORG_HALTED = "org_halted"
    ROLE_PAUSED = "role_paused"
    RUN_STOPPED = "run_stopped"
    NO_BUDGET = "no_budget"
    BUDGET_STOP = "budget_stop"
    DENIAL_LIMIT = "denial_limit"
    LEDGER_UNAVAILABLE = "ledger_unavailable"
    ADAPTER_ERROR = "adapter_error"
    STOPPED = "stopped"
    MODEL_MISMATCH = "model_mismatch"
    GUARDED_RUN_STOPPED = "guarded_run_stopped"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    SHELL_TEXT = "shell_text"


_SOFT = frozenset(
    {
        ReasonCode.DENIED_BY_RULE,
        ReasonCode.ASK_PENDING,
        ReasonCode.REJECTED_BY_PERSON,
        ReasonCode.APPROVAL_TIMEOUT,
        ReasonCode.APPROVAL_EXPIRED,
        ReasonCode.UNREADABLE_ARGUMENTS,
        ReasonCode.UNREADABLE_TOOL_NAME,
        ReasonCode.UNKNOWN_DELEGATE,
        ReasonCode.PATH_OUTSIDE_WORKSPACE,
    }
)
_STOP = frozenset(
    {
        ReasonCode.TOOL_NOT_FROZEN,
        ReasonCode.TOOL_SET_CHANGED,
        ReasonCode.TOOL_REPLACED,
        ReasonCode.ARGUMENTS_CHANGED,
        ReasonCode.ORG_HALTED,
        ReasonCode.ROLE_PAUSED,
        ReasonCode.RUN_STOPPED,
        ReasonCode.GUARDED_RUN_STOPPED,
        ReasonCode.NO_BUDGET,
        ReasonCode.BUDGET_STOP,
        ReasonCode.DENIAL_LIMIT,
        ReasonCode.LEDGER_UNAVAILABLE,
        ReasonCode.SERVER_UNAVAILABLE,
        ReasonCode.ADAPTER_ERROR,
        ReasonCode.CREDENTIAL_REJECTED,
        ReasonCode.ROLE_MISMATCH,
        ReasonCode.MODEL_MISMATCH,
    }
)
_NEVER_SHOWN = frozenset({ReasonCode.ALLOW, ReasonCode.SHELL_TEXT})


def is_soft_denial(code: ReasonCode) -> bool:
    """Whether an outcome with this code is a soft denial (AD-41): the run goes on."""
    return code in _SOFT


def is_stop(code: ReasonCode) -> bool:
    """Whether an outcome with this code stops the guarded run (AD-41)."""
    return code in _STOP


def deny_text(code: ReasonCode) -> str:
    """The one text a model sees for a refused call (AD-38). Nothing else is ever added."""
    code = ReasonCode(code)
    if code in _NEVER_SHOWN:
        raise ValueError(f"'{code.value}' is not a refusal a model sees")
    return f"Blocked by the team's rules: {code.value}."


def ask_text(approval_id: str) -> str:
    """The one text a model sees for an `ask` that is returned instead of awaited (AD-38)."""
    return f"Not done: this action needs a person's approval (approval {approval_id})."


def check_denial_limit(value: object) -> int:
    """The denial limit of a guarded run: 1 to `DENIAL_LIMIT`. It can be lowered, never raised
    and never switched off (AD-42, AD-69)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= DENIAL_LIMIT:
        raise ValueError(f"the denial limit is a whole number from 1 to {DENIAL_LIMIT}")
    return value


def check_deadline_seconds(value: object) -> int:
    """An approval deadline in seconds: 1 to 604800 (AD-46)."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= DEADLINE_MAX_SECONDS
    ):
        raise ValueError(
            f"the approval deadline is a whole number of seconds from 1 to {DEADLINE_MAX_SECONDS}"
        )
    return value


class UnreadableToolName(ValueError):
    """A tool name that fails AD-7. The message never holds the name (agent-chosen text)."""

    def __init__(self) -> None:
        super().__init__("the tool name is not a readable name (AD-7)")


class UnreadableArguments(ValueError):
    """Arguments that are not one readable JSON object (AD-9). The message is fixed: it never
    holds the arguments, a key, a value or a position in them."""

    def __init__(self) -> None:
        super().__init__("the arguments are not one readable JSON object (AD-9)")


def check_tool_name(name: object) -> str:
    """The name exactly as the framework dispatches it, or `UnreadableToolName` (AD-7): no
    trimming, no case folding."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise UnreadableToolName()
    return name


def call_id_for(framework_id: str | None) -> str:
    """The call id (AD-8): the framework's own id when it matches the name pattern, `h-` plus 32
    hex characters of its SHA-256 when it does not, `c-` plus 32 random hex characters when the
    framework gives none."""
    if framework_id is None:
        return "c-" + secrets.token_hex(16)
    if _NAME.fullmatch(framework_id) is not None:
        return framework_id
    digest = hashlib.sha256(framework_id.encode("utf-8", "surrogatepass")).hexdigest()
    return "h-" + digest[:32]


@dataclass(frozen=True, repr=False)
class ToolArguments:
    """Readable arguments: the object rebuilt from what was read (what a body may receive, AD-11),
    its RFC 8785 canonical bytes and their SHA-256 (AD-10). Never printed (AD-26, AD-65)."""

    value: dict[str, Any]
    canonical: bytes
    digest: str

    def __repr__(self) -> str:
        return "ToolArguments(<not shown>)"


class _Unreadable(Exception):
    """Internal: carries nothing, so no argument text can travel with it."""


def _refuse_constant(_: str) -> Any:
    raise _Unreadable


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Unreadable
        result[key] = value
    return result


def _parse(text: str | bytes) -> Any:
    """The object arguments text stands for; empty text is the empty object."""
    if len(text) > MAX_ARGUMENTS_TEXT_BYTES:  # characters never outnumber bytes
        raise _Unreadable
    if isinstance(text, bytes):
        raw = text
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _Unreadable from None
    else:
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:
            raise _Unreadable from None
    if len(raw) > MAX_ARGUMENTS_TEXT_BYTES:
        raise _Unreadable
    if text == "":
        return {}
    try:
        return json.loads(text, object_pairs_hook=_unique, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise _Unreadable from None


def _rebuild(value: Any, level: int) -> Any:
    """A copy of a JSON value made only of dict, list, str, int, float, bool and None, or
    `_Unreadable`. `level` is the level of `value` if it is a container (the top object is 1)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _Unreadable from None
        return value
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise _Unreadable
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _Unreadable
        if value.is_integer() and not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise _Unreadable
        return value
    if isinstance(value, dict):
        if level > MAX_DEPTH:
            raise _Unreadable
        rebuilt: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _Unreadable
            rebuilt[_rebuild(key, level)] = _rebuild(item, level + 1)
        return rebuilt
    if isinstance(value, list):
        if level > MAX_DEPTH:
            raise _Unreadable
        return [_rebuild(item, level + 1) for item in value]
    raise _Unreadable  # bytes, a tuple, a set, any other object: nothing is coerced


def _read(arguments: object) -> ToolArguments:
    value = _parse(arguments) if isinstance(arguments, str | bytes) else arguments
    if not isinstance(value, dict):
        raise _Unreadable
    rebuilt = _rebuild(value, 1)
    try:
        canonical = canonical_json_jcs(rebuilt)
    except (ValueError, TypeError, RecursionError):
        raise _Unreadable from None
    if len(canonical) > MAX_CANONICAL_BYTES:
        raise _Unreadable
    return ToolArguments(rebuilt, canonical, hashlib.sha256(canonical).hexdigest())


def read_arguments(arguments: object) -> ToolArguments:
    """Read a tool call's arguments (AD-9, AD-10): text (parsed once; empty text is `{}`) or the
    object a framework built. Anything that is not one readable JSON object raises
    `UnreadableArguments`, which carries no part of the arguments and no chained error."""
    try:
        return _read(arguments)
    except _Unreadable:
        pass
    raise UnreadableArguments()


def schema_hash(schema: object) -> str:
    """The `schema_hash` of a tool's parameter JSON Schema (AD-14): the hex SHA-256 of its RFC
    8785 canonical JSON."""
    return hashlib.sha256(canonical_json_jcs(schema)).hexdigest()


class ManifestError(ValueError):
    """A manifest that cannot be frozen (AD-15). It names the tool, never a schema's content."""


@dataclass(frozen=True)
class ManifestEntry:
    """One tool or handoff the framework could dispatch: its dispatch name and its `schema_hash`,
    or `None` when the framework exposes no schema for it (AD-14). `network` declares the tool
    network-capable (C-3 of `spec/network-guard`, #1119): only a declared tool may be handed the
    guarded network client; an undeclared one is governed by its name only (AD-12)."""

    name: str
    schema: str | None = None
    network: bool = False


def _hashed_entry(entry: ManifestEntry) -> dict[str, Any]:
    """One `tools` element of the hash body and the freeze record (AD-16, AD-29): `network` is
    written only when declared, so a set without a declaration hashes as before the field."""
    element: dict[str, Any] = {"name": entry.name, "schema": entry.schema}
    if entry.network:
        element["network"] = True
    return element


@dataclass(frozen=True)
class FrozenToolSet:
    """The frozen tool set of one guarded run (section 5): entries sorted by name, and
    `tool_set_hash` (AD-16)."""

    tools: tuple[ManifestEntry, ...]
    tool_set_hash: str
    _schemas: dict[str, str | None] = field(repr=False, compare=False, default_factory=dict)
    _network: frozenset[str] = field(repr=False, compare=False, default=frozenset())

    def __contains__(self, name: object) -> bool:
        return name in self._schemas

    def schema_of(self, name: str) -> str | None:
        """The frozen `schema_hash` of a tool in the set (`KeyError` outside it)."""
        return self._schemas[name]

    def network_capable(self, name: object) -> bool:
        """True only for a tool of this frozen set that the manifest declared network-capable
        (C-3, AD-14). Any other name, frozen or not, is False (default deny)."""
        return isinstance(name, str) and name in self._network

    def ledger_tools(self) -> list[dict[str, Any]]:
        """The `tools` list of the freeze record (AD-29): names, hashes and the network
        declaration (`network: true` only when declared)."""
        return [_hashed_entry(e) for e in self.tools]


def freeze_manifest(entries: Iterable[ManifestEntry]) -> FrozenToolSet:
    """Check a manifest and compute its `tool_set_hash` (AD-15, AD-16). Refused with a
    `ManifestError` that names the tool (a bad name only by its entry index): a name that fails
    AD-7, a schema that is not a `schema_hash`, a `network` flag that is not exactly `True` or
    `False`, two entries with one name, more than 256 entries."""
    listed = list(entries)
    if len(listed) > MAX_TOOLS:
        raise ManifestError(f"a manifest holds at most {MAX_TOOLS} tools (AD-15)")
    schemas: dict[str, str | None] = {}
    network: set[str] = set()
    for index, entry in enumerate(listed):
        if not isinstance(entry.name, str) or _NAME.fullmatch(entry.name) is None:
            raise ManifestError(f"manifest entry {index}: the tool name fails AD-7")
        if entry.name in schemas:
            raise ManifestError(f"tool '{entry.name}' is listed twice (AD-15)")
        if entry.schema is not None and (
            not isinstance(entry.schema, str) or _HEX64.fullmatch(entry.schema) is None
        ):
            raise ManifestError(f"tool '{entry.name}': the schema is not a schema hash (AD-14)")
        if type(entry.network) is not bool:
            raise ManifestError(
                f"tool '{entry.name}': the network flag is not true or false (AD-14)"
            )
        schemas[entry.name] = entry.schema
        if entry.network:
            network.add(entry.name)
    tools = tuple(
        ManifestEntry(name, schemas[name], network=name in network) for name in sorted(schemas)
    )
    body = {"v": 1, "tools": [_hashed_entry(e) for e in tools]}
    digest = hashlib.sha256(canonical_json_jcs(body)).hexdigest()
    return FrozenToolSet(tools, digest, schemas, frozenset(network))


__all__ = [
    "ADAPTERS",
    "DEADLINE_DURABLE_SECONDS",
    "DEADLINE_MAX_SECONDS",
    "DEADLINE_WAIT_SECONDS",
    "DENIAL_LIMIT",
    "MAX_ARGUMENTS_TEXT_BYTES",
    "MAX_CANONICAL_BYTES",
    "MAX_DEPTH",
    "MAX_TOOLS",
    "FrozenToolSet",
    "ManifestEntry",
    "ManifestError",
    "ReasonCode",
    "ToolArguments",
    "UnreadableArguments",
    "UnreadableToolName",
    "ask_text",
    "call_id_for",
    "check_deadline_seconds",
    "check_denial_limit",
    "check_tool_name",
    "deny_text",
    "freeze_manifest",
    "is_soft_denial",
    "is_stop",
    "read_arguments",
    "schema_hash",
]
