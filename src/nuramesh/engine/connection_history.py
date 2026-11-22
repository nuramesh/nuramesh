"""The connection history read (R1.33 spec 2.2): newest page first, audit view of one organization.

It folds the connection entries of the organization stream in `seq` order (the service's own
`apply_connection_event`), judges every row with the resolver's checks and keeps only the newest
`limit + 1` matching rows in memory. It reads the ledger and writes nothing; the caller reveals the
reason texts of the returned rows (the content store is not touched here).

**A row that is not valid never echoes a ledger value.** It carries the ledger header (`seq`,
`type`, `actor`, `at`), `valid: false`, `reason: null` and the fixed placeholder `(invalid)` for
`provider`. Every value of a valid row is from a closed set (a supported provider, an `auth`, a
status), a validated endpoint, key name or endpoint id, or the reason text read later.

On a hosted server (or with an invalid hosted flag) **every** `openai-compatible` row, whatever its
type (created, updated, removed, tested), is a placeholder (Q4).

**Key entries (R1.14c spec 9).** `model.key_stored`, `key_test_failed`, `key_activated`,
`key_revoked` and `key_read_failed` are rows of the connection their `connection` names; a
`key_destroyed` names only a key id and is the row of the connection whose valid `key_stored` named
that id first (none: no row, e.g. an orphan). A key row shows ids, versions, actor and time plus a
closed-set `code` or `cause`, the activation's `previous_version` and `grace_seconds`, and a reason
text; never the fingerprint, its markers, `via` or `mfa`. The organization-wide erasure records
(`key_erased`, `key_erasure_verified`, `key_erasure_pending`) are no connection's row.
"""

import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError

from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.model_catalog import (
    ConnectionFacts,
    ConnectionsState,
    ProviderConnection,
    apply_connection_event,
    is_repoint,
)
from nuramesh.core.org import OPENAI_COMPATIBLE, EndpointId
from nuramesh.core.secret_store import KEY_ID_PATTERN
from nuramesh.engine import anthropic as anthropic_adapter
from nuramesh.engine import azure_openai as azure_adapter
from nuramesh.engine import openai_compatible as oac
from nuramesh.engine.connection_rules import SUPPORTED_PROVIDERS, auth_of, connection_problem
from nuramesh.engine.key_ops import TEST_CODES
from nuramesh.engine.key_sweeper import DESTROYABLE
from nuramesh.engine.org_ledger import PAGE, org_stream
from nuramesh.stores.base import Store

PLACEHOLDER: Final = "(invalid)"
"""The `provider` of a row that is not valid (never the raw value, even a real provider name)."""

PROBE_CODES: Final[frozenset[str]] = frozenset(
    {*azure_adapter.PROBE_TEXTS, *anthropic_adapter.PROBE_TEXTS, *oac.PROBE_CODES}
    | {"endpoint_invalid"}
)
"""The closed set of probe codes a `model.connection_tested` entry may carry (built from the
adapters' own data, plus the `endpoint_invalid` the tester writes without a probe)."""

STATUS_CLASSES: Final = frozenset({"2xx", "3xx", "4xx", "5xx"})
_ROW_TYPES: Final = {
    EventType.MODEL_CONNECTION_CREATED.value,
    EventType.MODEL_CONNECTION_UPDATED.value,
    EventType.MODEL_CONNECTION_REMOVED.value,
    EventType.MODEL_CONNECTION_TESTED.value,
}
_WRITES: Final = {
    EventType.MODEL_CONNECTION_CREATED.value,
    EventType.MODEL_CONNECTION_UPDATED.value,
}
_FORBIDDEN_KEY_PARTS: Final = ("file", "path")
_ENDPOINT_ID: Final = TypeAdapter(EndpointId)


@dataclass(frozen=True)
class HistoryRow:
    """One row of the answer. `reason_hash` is for the caller's reveal and is never shown."""

    data: dict[str, Any]
    reason_hash: str | None


@dataclass(frozen=True)
class HistoryPage:
    """The newest `limit` matching rows (ascending `seq`) and the `seq` to page older with."""

    rows: list[HistoryRow]
    next_before: int | None


def _endpoint_id_ok(value: object) -> bool:
    try:
        _ENDPOINT_ID.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True


def _connection_facts(payload: Mapping[str, Any]) -> ConnectionFacts | None:
    """The facts of a created/updated entry if the resolver would accept the entry, else None:
    `ConnectionFacts`, the provider/auth pairing, the fold's forbidden-key rule and
    `connection_problem` (the one function the resolver and the PUT use)."""
    provider = payload.get("provider")
    if provider not in SUPPORTED_PROVIDERS:
        return None
    try:
        facts = ConnectionFacts.model_validate(dict(payload))
    except (ValidationError, ValueError):
        return None
    if provider == OPENAI_COMPATIBLE:
        if any(part in str(k).lower() for k in payload for part in _FORBIDDEN_KEY_PARTS):
            return None
    elif facts.auth not in (auth_of(facts.provider), "stored"):
        return None
    problem = connection_problem(
        facts.provider,
        facts.endpoint,
        endpoint_id=facts.endpoint_id,
        auth=facts.auth,
        key=facts.key,
    )
    return facts if problem is None else None


def _id_rule_ok(provider: str, endpoint_id: object) -> bool:
    """A valid `EndpointId` for `openai-compatible`; absent or null for every other provider."""
    if provider == OPENAI_COMPATIBLE:
        return _endpoint_id_ok(endpoint_id)
    return endpoint_id is None


def _tested_ok(payload: Mapping[str, Any]) -> bool:
    provider, ok, code, klass = (payload.get(k) for k in ("provider", "ok", "code", "status_class"))
    return (
        provider in SUPPORTED_PROVIDERS
        and isinstance(ok, bool)
        and isinstance(code, str)
        and code in PROBE_CODES
        and (klass is None or (isinstance(klass, str) and klass in STATUS_CLASSES))
        and _id_rule_ok(str(provider), payload.get("endpoint_id"))
    )


def _header(event: LedgerEvent) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "type": event.type,
        "actor": event.actor,
        "at": event.at.isoformat(),
    }


def _placeholder(event: LedgerEvent) -> HistoryRow:
    return HistoryRow(
        {**_header(event), "provider": PLACEHOLDER, "valid": False, "reason": None}, None
    )


def _reason_hash(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("reason_hash")
    return value if isinstance(value, str) else None


def _row(event: LedgerEvent, before: ConnectionsState) -> HistoryRow:
    """The row of one entry, judged with the state of the stream before it."""
    payload: Mapping[str, Any] = event.payload
    kind = event.type
    if kind in _WRITES:
        facts = _connection_facts(payload)
        if facts is None:
            return _placeholder(event)
        state = before.providers.get(facts.conn_key, ProviderConnection())
        data: dict[str, Any] = {
            **_header(event),
            "provider": facts.provider,
            "valid": True,
            "endpoint": facts.endpoint,
            "status": facts.status,
            "auth": facts.auth,
            "repoint": is_repoint(facts.provider, state, facts.endpoint, facts.auth, facts.key),
            "reason": None,
        }
        if facts.endpoint_id is not None:
            data["endpoint_id"] = facts.endpoint_id
        if facts.key is not None:
            data["key"] = facts.key
        return HistoryRow(data, _reason_hash(payload))
    provider = payload["provider"]
    if provider == OPENAI_COMPATIBLE and oac.hosted_problem() is not None:
        return _placeholder(event)  # hosted (or a bad flag): every row of this provider (Q4)
    if kind == EventType.MODEL_CONNECTION_REMOVED.value:
        if provider not in SUPPORTED_PROVIDERS or not _id_rule_ok(
            provider, payload.get("endpoint_id")
        ):
            return _placeholder(event)
        data = {**_header(event), "provider": provider, "valid": True, "reason": None}
        if payload.get("endpoint_id") is not None:
            data["endpoint_id"] = payload["endpoint_id"]
        return HistoryRow(data, _reason_hash(payload))
    if not _tested_ok(payload):
        return _placeholder(event)
    data = {
        **_header(event),
        "provider": provider,
        "valid": True,
        "ok": payload["ok"],
        "code": payload["code"],
        "status_class": payload.get("status_class"),
    }
    if payload.get("endpoint_id") is not None:
        data["endpoint_id"] = payload["endpoint_id"]
    return HistoryRow(data, None)


_KEY_ROW_TYPES: Final = frozenset(
    {
        EventType.MODEL_KEY_STORED.value,
        EventType.MODEL_KEY_TEST_FAILED.value,
        EventType.MODEL_KEY_ACTIVATED.value,
        EventType.MODEL_KEY_REVOKED.value,
        EventType.MODEL_KEY_READ_FAILED.value,
    }
)
"""The key entries that name their connection (spec r114c 9: the History drawer shows them)."""
_KEY_STORED: Final = EventType.MODEL_KEY_STORED.value
_KEY_DESTROYED: Final = EventType.MODEL_KEY_DESTROYED.value
"""Names no connection: the row of the connection whose `key_stored` first named the key id."""
_TEST_FAILED: Final = EventType.MODEL_KEY_TEST_FAILED.value
_READ_FAILED: Final = EventType.MODEL_KEY_READ_FAILED.value
_ACTIVATED: Final = EventType.MODEL_KEY_ACTIVATED.value
_REVOKED: Final = EventType.MODEL_KEY_REVOKED.value
KEY_TEST_CODES: Final = frozenset({*TEST_CODES, "test_error"})
KEY_READ_CODES: Final = frozenset({"key_missing", "key_mismatch"})
KEY_REVOKE_CAUSES: Final = frozenset({"connection_removed"})
KEY_DESTROY_CAUSES: Final = frozenset({*DESTROYABLE, "orphan", "unreferenced", "rotation_lost"})
_KEY_ID: Final = re.compile(KEY_ID_PATTERN)

Where = tuple[str, str | None]
"""(provider, endpoint_id) of a connection."""


def _count(value: object, low: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= low


def _key_connection(value: object) -> Where | None:
    """(provider, endpoint_id) of a key entry's `connection` (`connection_key`), else None."""
    if not isinstance(value, str):
        return None
    provider, slash, endpoint_id = value.partition("/")
    if provider not in SUPPORTED_PROVIDERS:
        return None
    if not slash:
        return None if provider == OPENAI_COMPATIBLE else (provider, None)
    if provider != OPENAI_COMPATIBLE or not _endpoint_id_ok(endpoint_id):
        return None
    return provider, endpoint_id


def _key_extra(kind: str, payload: Mapping[str, Any]) -> tuple[bool, dict[str, Any], str | None]:
    """The type's own fields of a key row: (valid, fields, reason hash)."""
    if kind in (_TEST_FAILED, _READ_FAILED):
        code = payload.get("code")
        codes = KEY_TEST_CODES if kind == _TEST_FAILED else KEY_READ_CODES
        return code in codes, {"code": code}, None
    if kind == _ACTIVATED:
        previous, grace = payload.get("previous_version"), payload.get("grace_seconds", 0)
        ok = (previous is None or _count(previous, 1)) and _count(grace, 0)
        fields = {"previous_version": previous, "grace_seconds": grace, "reason": None}
        return ok, fields, _reason_hash(payload)
    if kind == _REVOKED:
        cause = payload.get("cause")
        ok = cause is None or cause in KEY_REVOKE_CAUSES
        return ok, {"cause": cause, "reason": None}, _reason_hash(payload)
    if kind == _KEY_DESTROYED:
        cause = payload.get("cause")
        return cause in KEY_DESTROY_CAUSES, {"cause": cause}, None
    return True, {}, None


def _key_row(event: LedgerEvent, owner: Where | None) -> HistoryRow:
    """The row of one key entry (`owner` is the connection of a destroy's key id). Every value
    shown is a validated id, a count or from a closed set; the fingerprint, its markers, `via` and
    `mfa` never are. A malformed entry is the usual placeholder."""
    payload: Mapping[str, Any] = event.payload
    kind = event.type
    where = owner if kind == _KEY_DESTROYED else _key_connection(payload.get("connection"))
    key_id, version = payload.get("key_id"), payload.get("version")
    ok, extra, reason = _key_extra(kind, payload)
    ok = ok and isinstance(key_id, str) and _KEY_ID.fullmatch(key_id) is not None
    ok = ok and (_count(version, 1) or (kind == _REVOKED and version == "all"))
    if not ok or where is None:
        return _placeholder(event)
    provider, endpoint_id = where
    if provider == OPENAI_COMPATIBLE and oac.hosted_problem() is not None:
        return _placeholder(event)  # hosted (or a bad flag): every row of this provider (Q4)
    data: dict[str, Any] = {
        **_header(event),
        "provider": provider,
        "valid": True,
        "key_id": key_id,
        "version": version,
        **extra,
    }
    if endpoint_id is not None:
        data["endpoint_id"] = endpoint_id
    return HistoryRow(data, reason)


def _next_row(
    event: LedgerEvent, state: ConnectionsState, owners: dict[str, Where]
) -> HistoryRow | None:
    """The row of one entry of the stream, or None when it is no connection's row; keeps the
    connection fold and the key id owners up to date."""
    kind = event.type
    if kind in _KEY_ROW_TYPES:
        row = _key_row(event, None)
        if kind == _KEY_STORED and row.data["valid"]:
            key_id = row.data["key_id"]
            owners.setdefault(key_id, (row.data["provider"], row.data.get("endpoint_id")))
        return row
    if kind == _KEY_DESTROYED:
        key_id = event.payload.get("key_id")
        owner = owners.get(key_id) if isinstance(key_id, str) else None
        return None if owner is None else _key_row(event, owner)
    if kind not in _ROW_TYPES or not isinstance(event.payload.get("provider"), str):
        return None
    row = _row(event, state)
    if kind != EventType.MODEL_CONNECTION_TESTED.value:
        apply_connection_event(state, event)
    return row


def read_history(
    store: Store,
    org_id: str,
    *,
    provider: str | None = None,
    endpoint_id: str | None = None,
    before: int | None = None,
    limit: int = 50,
) -> HistoryPage:
    """The `limit` newest matching rows with `seq < before` (absent: the newest).

    Linear in the organization stream (the fold needs the earlier entries for `repoint`), in pages
    of `PAGE`, with a small state per connection and a buffer of the newest `limit + 1` matching
    rows. With `before` the scan stops there. A store failure propagates (never an empty list)."""
    stream = org_stream(org_id)
    state = ConnectionsState()
    owners: dict[str, Where] = {}
    newest: deque[HistoryRow] = deque(maxlen=limit + 1)
    after, finished = 0, False
    while not finished:
        page = list(store.read_stream(stream, after_seq=after, limit=PAGE))
        for event in page:
            after = event.seq
            if before is not None and event.seq >= before:
                finished = True
                break
            row = _next_row(event, state, owners)
            if row is None:
                continue
            shown = row.data
            if not shown["valid"]:
                if provider is None and endpoint_id is None:
                    newest.append(row)
                continue
            if provider is not None and shown["provider"] != provider:
                continue
            if endpoint_id is not None and shown.get("endpoint_id") != endpoint_id:
                continue
            newest.append(row)
        if len(page) < PAGE:
            break
    rows = list(newest)
    more = len(rows) > limit
    if more:
        rows = rows[1:]
    return HistoryPage(rows, rows[0].data["seq"] if more else None)
