"""Licence file format 1 and the pure verifier (ND389, tier 3).

A licence file is `{"claims": {...}, "sig": "<128 hex>"}`; the signed bytes are the prefix
`nuramesh-licence-v1\\n` followed by the RFC 8785 text of the claims, and `v`, `alg` and `key_id`
sit inside those bytes. This module is **pure** (standard library only): it parses, checks the
claims, and asks an injected function to check the Ed25519 signature. It opens no file, reads no
clock and fetches nothing; `nuramesh.licence` (outside `nuramesh.core`) does the wiring.

`verify_licence` returns a result for every input and never raises on the file's account: every
refusal is one named problem from `PROBLEMS`. A licence can only switch paid features on; nothing
here, and nothing that reads this module, may switch an open feature off.

`NOT_BEFORE_SKEW` and `EXPIRY_GRACE` are placeholder values (spec section 9): code constants,
never a setting, a flag or a claim.
"""

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final

from nuramesh.core.canonical import canonical_json_jcs

VERSION: Final = 1
ALG: Final = "ed25519"
PREFIX: Final = b"nuramesh-licence-v1\n"
MAX_LICENCE_BYTES: Final = 16384
MAX_FEATURES: Final = 64
MAX_CUSTOMER_CHARS: Final = 200
NOT_BEFORE_SKEW: Final = timedelta(minutes=5)  # placeholder (spec section 9, item 1)
EXPIRY_GRACE: Final = timedelta(0)  # placeholder (spec section 9, item 2)

CLAIM_NAMES: Final = (
    "v",
    "alg",
    "key_id",
    "licence_id",
    "customer_id",
    "customer",
    "issued_at",
    "not_before",
    "expires_at",
    "features",
)
PROBLEMS: Final = frozenset(
    {
        "too_large",
        "malformed",
        "unsupported_version",
        "bad_claims",
        "unsupported_alg",
        "unknown_key_id",
        "bad_signature",
        "not_yet_valid",
        "expired",
        # the loader's own problems (nuramesh.licence), listed here so the set is one
        "unreadable",
        "refused_hosted",
        "verifier_unavailable",
    }
)

_HEX32 = re.compile(r"[0-9a-f]{32}", re.ASCII)
_HEX128 = re.compile(r"[0-9a-f]{128}", re.ASCII)
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}", re.ASCII)
_FEATURE = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", re.ASCII)
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", re.ASCII)

SignatureCheck = Callable[[bytes, bytes, bytes], bool]
"""`check(raw_public_key, message, signature) -> bool`: True only for a valid Ed25519 signature."""


@dataclass(frozen=True)
class LicenceClaims:
    """The ten claims of format 1, parsed and checked (spec section 2.1)."""

    v: int
    alg: str
    key_id: str
    licence_id: str
    customer_id: str
    customer: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    features: tuple[str, ...]


@dataclass(frozen=True)
class LicenceResult:
    """What the verifier says about one file. `claims` is set when the file was authentic (`ok`,
    `not_yet_valid` and `expired`); every other refusal carries none."""

    ok: bool
    problem: str | None
    detail: str
    claims: LicenceClaims | None


def licence_key_id(raw: bytes) -> str:
    """The first 32 lower-case hex digits of SHA-256 of the 32 raw public key bytes."""
    return sha256(raw).hexdigest()[:32]


def licence_message(claims: Mapping[str, Any]) -> bytes:
    """The signed bytes: the prefix, then the canonical JSON of the claims."""
    return PREFIX + canonical_json_jcs(claims)


def parse_licence_time(text: str) -> datetime:
    """`YYYY-MM-DDTHH:MM:SSZ` only (UTC, whole seconds); anything else is a `ValueError`."""
    if not _TIME.fullmatch(text) or int(text[17:19]) > 59:
        raise ValueError("not a licence time")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _aware(value: datetime, what: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be an aware datetime")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate member")
        out[key] = value
    return out


def _refuse(problem: str, detail: str, claims: LicenceClaims | None = None) -> LicenceResult:
    return LicenceResult(False, problem, detail, claims)


def _kind(value: object) -> str:
    """A type word for a detail line; never the value itself."""
    return "null" if value is None else type(value).__name__


def _parse_claims(claims: Mapping[str, Any]) -> LicenceClaims | str:
    """The parsed claims, or the detail of the first offence in section 2.1 order."""
    for name in CLAIM_NAMES:
        if name not in claims:
            return f"missing claim '{name}'"
    extra = sorted(set(claims) - set(CLAIM_NAMES))
    if extra:
        return f"extra claim '{extra[0]}'"
    if not isinstance(claims["alg"], str):
        return f"claim 'alg' is a {_kind(claims['alg'])}, not a string"
    key_id = claims["key_id"]
    if not isinstance(key_id, str) or not _HEX32.fullmatch(key_id):
        return "claim 'key_id' is not 32 lower-case hex digits"
    for name in ("licence_id", "customer_id"):
        value = claims[name]
        if not isinstance(value, str) or not _SLUG.fullmatch(value):
            return f"claim '{name}' is not an id of 1 to 63 lower-case letters, digits, '_' or '-'"
    customer = claims["customer"]
    if not isinstance(customer, str):
        return f"claim 'customer' is a {_kind(customer)}, not a string"
    if not 1 <= len(customer) <= MAX_CUSTOMER_CHARS:
        return f"claim 'customer' has {len(customer)} characters (1 to {MAX_CUSTOMER_CHARS})"
    if any(unicodedata.category(ch) == "Cc" for ch in customer):
        return "claim 'customer' has a control character"
    times: dict[str, datetime] = {}
    for name in ("issued_at", "not_before", "expires_at"):
        value = claims[name]
        try:
            if not isinstance(value, str):
                raise ValueError("not a string")
            times[name] = parse_licence_time(value)
        except ValueError:
            return f"claim '{name}' is not a UTC time of the form YYYY-MM-DDTHH:MM:SSZ"
    if times["not_before"] < times["issued_at"]:
        return "claim 'not_before' is before issued_at"
    if times["expires_at"] <= times["not_before"]:
        return "claim 'expires_at' is not after not_before"
    features = claims["features"]
    if not isinstance(features, list):
        return f"claim 'features' is a {_kind(features)}, not an array"
    if len(features) > MAX_FEATURES:
        return f"claim 'features' has {len(features)} entries (at most {MAX_FEATURES})"
    for item in features:
        if not isinstance(item, str) or not _FEATURE.fullmatch(item):
            return "claim 'features' has an entry that is not a feature key"
    if features != sorted(set(features)):
        return "claim 'features' is not sorted and unique"
    return LicenceClaims(
        v=VERSION,
        alg=claims["alg"],
        key_id=key_id,
        licence_id=claims["licence_id"],
        customer_id=claims["customer_id"],
        customer=customer,
        issued_at=times["issued_at"],
        not_before=times["not_before"],
        expires_at=times["expires_at"],
        features=tuple(features),
    )


def verify_licence(
    data: bytes,
    trusted_keys: Mapping[str, bytes],
    signature_check: SignatureCheck,
    now: datetime,
) -> LicenceResult:
    """Check one licence file (spec section 3, steps 1 to 10, first problem wins).

    `trusted_keys` maps a key id to the 32 raw public key bytes; `now` is an aware UTC time (a
    naive one is a programming error and raises). Nothing in `data` can make this raise."""
    _aware(now, "now")
    if len(data) > MAX_LICENCE_BYTES:
        return _refuse("too_large", f"{len(data)} bytes; the limit is {MAX_LICENCE_BYTES}")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _refuse("malformed", "not UTF-8 JSON, or a member appears twice")
    if not isinstance(document, dict) or set(document) != {"claims", "sig"}:
        return _refuse("malformed", "not one object with exactly the members claims and sig")
    claims, sig = document["claims"], document["sig"]
    if not isinstance(claims, dict):
        return _refuse("malformed", "claims is not an object")
    if not isinstance(sig, str) or not _HEX128.fullmatch(sig):
        return _refuse("malformed", "sig is not 128 lower-case hex digits")
    version = claims.get("v")
    if type(version) is not int or version != VERSION:
        return _refuse("unsupported_version", f"v is {_kind(version)}, not the integer 1")
    parsed = _parse_claims(claims)
    if isinstance(parsed, str):
        return _refuse("bad_claims", parsed)
    if parsed.alg != ALG:
        return _refuse("unsupported_alg", f"alg is not '{ALG}'")
    raw = trusted_keys.get(parsed.key_id)
    if raw is None:
        return _refuse("unknown_key_id", f"key id {parsed.key_id} is not a trusted licence key")
    try:
        valid = signature_check(raw, licence_message(claims), bytes.fromhex(sig))
    except Exception:  # the check is a library call; any failure is a refusal, never a crash
        valid = False
    if valid is not True:
        return _refuse("bad_signature", f"the signature does not match key {parsed.key_id}")
    stamp = parsed.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if now + NOT_BEFORE_SKEW < parsed.not_before:
        starts = parsed.not_before.strftime("%Y-%m-%dT%H:%M:%SZ")
        return _refuse("not_yet_valid", f"{parsed.licence_id} is not valid before {starts}", parsed)
    if now >= parsed.expires_at + EXPIRY_GRACE:
        return _refuse("expired", f"{parsed.licence_id} expired at {stamp}", parsed)
    return LicenceResult(True, None, "", parsed)
