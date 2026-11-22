"""Idempotency keys (R1.35, ND112): the pure rules.

Header rules, the request fingerprint, the outcome of `begin`, the sealed-response envelope and its
associated data, and the route-mode types. Stdlib and pydantic only. The store holds the rows, the
engine seals, the server connects them (`spec/idempotency/README.md`).
"""

import hashlib
import hmac
import json
import re
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal

from nuramesh.core.canonical import MAX_SAFE_INTEGER, canonical_json

# The lifetime is fixed at 24 hours: no setting, no environment variable.
IDEMPOTENCY_TTL: Final = 86_400
LEASE_SECONDS: Final = 120
SWEEP_INTERVAL: Final = 3_600
MAX_STORED_BODY: Final = 262_144
MAX_STORED_HEADERS: Final = 1_024
MAX_SEALED: Final = MAX_STORED_BODY + 4 + MAX_STORED_HEADERS + 16
FINGERPRINT_VERSION: Final = 1
LOCAL_RING_NAME: Final = "local-inspector"
SEAL_INFO: Final = "nuramesh idempotency response v1"
# headers stored and replayed; everything else (Set-Cookie, Date, Content-Length, ...) never is
STORED_HEADERS: Final = ("content-type", "cache-control", "location")
REPLAY_HEADER: Final = "Idempotent-Replayed"

_KEY = re.compile(r"[A-Za-z0-9._-]{16,128}")
_HTTP_WS = " \t"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class IdempotencyError(Exception):
    """Base of the errors raised by the idempotency rules."""


class InvalidKey(IdempotencyError):
    """The `Idempotency-Key` header is malformed (400 `idempotency_key_invalid`)."""


class IdempotencyFenced(IdempotencyError):
    """The caller's fence no longer matches the row: another attempt owns it."""


class IdempotencyContractError(IdempotencyError):
    """An exactly-once handler finished with a key and never called `respond`."""


class Delivery(StrEnum):
    """What an honored route guarantees (section 6)."""

    EXACTLY_ONCE = "exactly_once"
    AT_LEAST_ONCE = "at_least_once"


class Mode(StrEnum):
    """How a mutating route treats the header (section 8)."""

    HONORED = "honored"
    EXCLUDED_READONLY = "excluded_readonly"
    EXCLUDED_SECRET = "excluded_secret"
    IGNORED = "ignored"  # the header is not read: no 400, nothing stored (mfa.md section 4)


@dataclass(frozen=True)
class RouteMode:
    """One row of `IDEMPOTENCY_ROUTES`: the mode, and the delivery for an honored route only."""

    mode: Mode
    delivery: Delivery | None = None

    def __post_init__(self) -> None:
        if (self.mode is Mode.HONORED) != (self.delivery is not None):
            raise ValueError("only an honored route has a delivery")


def honored(delivery: Delivery) -> RouteMode:
    """An honored route with the given delivery class."""
    return RouteMode(Mode.HONORED, delivery)


EXCLUDED_READONLY: Final = RouteMode(Mode.EXCLUDED_READONLY)
EXCLUDED_SECRET: Final = RouteMode(Mode.EXCLUDED_SECRET)
IGNORED: Final = RouteMode(Mode.IGNORED)


# ---------------------------------------------------------------- header


def parse_key(values: Sequence[str]) -> str:
    """The key from the raw `Idempotency-Key` header lines. Exactly one line, optional leading and
    trailing whitespace stripped, 16 to 128 characters of `A-Za-z0-9._-`; anything else raises
    `InvalidKey` (the message never repeats the value)."""
    if len(values) != 1:
        raise InvalidKey("send the Idempotency-Key header exactly once")
    key = values[0].strip(_HTTP_WS)
    if not _KEY.fullmatch(key):
        raise InvalidKey(
            "the Idempotency-Key must be 16 to 128 characters of letters, digits, '.', '_' or '-'"
        )
    return key


def key_hash(key: str) -> str:
    """SHA-256 of the key as lower hex: the only form of the key that is ever stored."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- scope


@dataclass(frozen=True)
class Scope:
    """Where a key means something: organization, actor, method and route template."""

    org_id: str
    actor: str
    method: str
    route: str


# ---------------------------------------------------------------- fingerprint


class _Unsafe(Exception):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _Unsafe("repeated key")
        out[key] = value
    return out


def _int(text: str) -> int:
    value = int(text)
    if abs(value) > MAX_SAFE_INTEGER:
        raise _Unsafe("integer too large")
    return value


def _refuse(text: str) -> Any:
    raise _Unsafe("float or constant")


def _body(data: bytes) -> tuple[Literal["empty", "json", "raw"], Any]:
    if not data:
        return "empty", None
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_int,
            parse_float=_refuse,
            parse_constant=_refuse,
        )
        canonical_json(value)  # refuses lone surrogates and anything that cannot be hashed
    except (_Unsafe, ValueError, UnicodeError, RecursionError):
        return "raw", hashlib.sha256(data).hexdigest()
    return "json", value


def _media(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def fingerprint(
    method: str,
    path: str,
    query: Sequence[tuple[str, str]],
    content_type: str | None,
    body: bytes,
) -> str:
    """The request fingerprint (section 4). The key, credentials, host, client and the other
    headers are not part of it; a request with no body has an empty media type."""
    kind, value = _body(body)
    document = {
        "v": FINGERPRINT_VERSION,
        "method": method,
        "path": path,
        "query": [[k, v] for k, v in sorted(query)],
        "media": "" if kind == "empty" else _media(content_type),
        "body_kind": kind,
        "body": value,
    }
    return hashlib.sha256(canonical_json(document)).hexdigest()


def same_fingerprint(a: str, b: str) -> bool:
    """Constant-time comparison."""
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------- begin


class State(StrEnum):
    """The state of a stored row."""

    IN_PROGRESS = "in_progress"
    DONE = "done"


@dataclass(frozen=True)
class Sealed:
    """An encrypted response: ciphertext (with the GCM tag), nonce and the id of the sealing key."""

    payload: bytes
    nonce: bytes
    key_id: str


@dataclass(frozen=True)
class Row:
    """The facts `decide` needs from a stored row."""

    state: State
    fingerprint: str
    attempt: int
    lease_until: datetime
    expires_at: datetime
    status: int | None = None
    sealed: Sealed | None = None


@dataclass(frozen=True)
class Started:
    """The route runs. `fence` authorizes every later call of this attempt."""

    attempt: int
    fence: str


@dataclass(frozen=True)
class Replay:
    """The stored response."""

    status: int
    sealed: Sealed


@dataclass(frozen=True)
class InProgress:
    """Another attempt is running (409 `idempotency_in_progress`, `Retry-After: 1`)."""


@dataclass(frozen=True)
class Mismatch:
    """The key was used with a different request (422 `idempotency_key_reused`)."""


type Outcome = Started | Replay | InProgress | Mismatch
type Action = Literal["insert", "takeover", "replay", "in_progress", "mismatch"]


def decide(row: Row | None, fingerprint_: str, now: datetime) -> Action:
    """What `begin` does given the row (section 5). A row with `expires_at <= now` is absent;
    a different fingerprint is a mismatch before anything else."""
    if row is None or row.expires_at <= now:
        return "insert"
    if not same_fingerprint(row.fingerprint, fingerprint_):
        return "mismatch"
    if row.state is State.DONE:
        return "replay"
    return "takeover" if row.lease_until <= now else "in_progress"


def lease_end(now: datetime) -> datetime:
    """When a lease granted at `now` lapses."""
    return now + timedelta(seconds=LEASE_SECONDS)


SWEEP_BATCH: Final = 1000
"""Rows one sweep call deletes at most."""
SWEEP_MAX_BATCHES: Final = 100
"""Batches one sweep runs at most (100,000 rows), so a sweep cannot run forever (ND148)."""

_PERSON: Final = re.compile(r"human:[A-Za-z0-9_-]{1,64}")
_PERSON_EXACT: Final = re.compile(r"human:u_[0-9a-f]{20}")
"""The real shape of a person id (`core.auth`). Text may run on after one (`-x`, `_x`, `z9`), which
the loose pattern would read as a longer, different token; the exact one still finds the id."""


def sweep_expired(purge: Callable[[int], int]) -> int:
    """Run `purge(limit)` while a batch deletes the full `SWEEP_BATCH`, at most
    `SWEEP_MAX_BATCHES` times; the total removed."""
    total = 0
    for _ in range(SWEEP_MAX_BATCHES):
        removed = purge(SWEEP_BATCH)
        total += removed
        if removed < SWEEP_BATCH:
            break
    return total


def human_subjects(*texts: str) -> frozenset[str]:
    """Every person id (`human:...`) that appears in the texts (ND148). A text that is JSON is
    also read as data, so an escaped colon (`\\u003a`) cannot hide an id."""
    found: set[str] = set()

    def scan(value: str) -> None:
        found.update(_PERSON.findall(value))
        found.update(_PERSON_EXACT.findall(value))

    def walk(root: Any) -> None:
        # iterative: a recursive walk fails near depth 2,000, json.loads itself near 10,000
        stack = [root]
        while stack:
            value = stack.pop()
            if isinstance(value, str):
                scan(value)
            elif isinstance(value, Mapping):
                for k, v in value.items():
                    stack.append(k)
                    stack.append(v)
            elif isinstance(value, Sequence):
                stack.extend(value)

    for text in texts:
        scan(text)
        try:
            walk(json.loads(text))
        except (ValueError, RecursionError):
            pass  # not readable as JSON (or nested too deep): the plain-text scan above stands
    return frozenset(found)


def expiry(now: datetime, ttl: int = IDEMPOTENCY_TTL) -> datetime:
    """When a row written (or completed) at `now` expires."""
    return now + timedelta(seconds=ttl)


# ---------------------------------------------------------------- response envelope


def stored_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The allowlisted headers of a response, lower-cased names."""
    return {k.lower(): v for k, v in headers.items() if k.lower() in STORED_HEADERS}


def storable(
    status: int, headers: Mapping[str, str], body: bytes, *, set_cookie: bool = False
) -> bool:
    """Whether a response may be stored: 2xx, no cookie, within the size limits."""
    if not 200 <= status <= 299 or set_cookie:
        return False
    if any(k.lower() == "set-cookie" for k in headers):
        return False
    if len(body) > MAX_STORED_BODY:
        return False
    return len(_headers_json(stored_headers(headers))) <= MAX_STORED_HEADERS


def _headers_json(headers: Mapping[str, str]) -> bytes:
    return json.dumps(dict(sorted(headers.items())), separators=(",", ":")).encode("utf-8")


def pack_response(headers: Mapping[str, str], body: bytes) -> bytes:
    """4 bytes big-endian length H, H bytes of header JSON, then the body."""
    head = _headers_json(stored_headers(headers))
    return struct.pack(">I", len(head)) + head + body


def unpack_response(data: bytes) -> tuple[dict[str, str], bytes]:
    """The inverse of `pack_response`; raises `ValueError` on a malformed envelope."""
    if len(data) < 4:
        raise ValueError("short envelope")
    (size,) = struct.unpack(">I", data[:4])
    if size > MAX_STORED_HEADERS or 4 + size > len(data):
        raise ValueError("bad envelope")
    headers = json.loads(data[4 : 4 + size].decode("utf-8"))
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k in STORED_HEADERS
        for k, v in headers.items()
    ):
        raise ValueError("bad envelope headers")
    return headers, data[4 + size :]


def _frame(*parts: str) -> bytes:
    out = bytearray()
    for part in parts:
        raw = part.encode("utf-8")
        out += struct.pack(">I", len(raw)) + raw
    return bytes(out)


def associated_data(scope: Scope, key_hash_: str, fingerprint_: str, status: int) -> bytes:
    """Authenticated with the ciphertext, so a row copied to another scope fails to open."""
    return _frame(
        SEAL_INFO,
        scope.org_id,
        scope.actor,
        scope.method,
        scope.route,
        key_hash_,
        fingerprint_,
        str(status),
    )


def is_hex64(text: str) -> bool:
    """Whether `text` is a 64-character lower-hex string."""
    return bool(_HEX64.fullmatch(text))
