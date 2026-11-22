"""The content store's data model: keyed hashes and encrypted, expiring records (ND25, T46).

The ledger is append-only and hash-chained, so it can hold **no content** (A7): no prompts, outputs,
and, since T46, no human-written text such as the reason for an approval. Such **content** goes
to a separate store that can forget:

- **Per-organization key.** Everything about an organization's content is derived from one secret
  key held *outside* the store (a key ring, `engine.content`). `hkdf` derives a hashing key and an
  encryption key from it, so neither is ever the master key itself.
- **Keyed hashes.** The ledger records `keyed_hash(...)`, an HMAC-SHA256 under the organization's
  hashing key over the content's kind and text. Without the key a hash can't be tested against a
  guess (a plain SHA-256 of a short reason could be brute-forced), and the same text in two
  organizations gives different hashes. The hash is also the record's id: "show the reason for
  ledger entry N" means "fetch the content with this hash".
- **Encrypted, expiring records.** `ContentRecord` holds the ciphertext (AES-GCM, `engine.content`),
  when it expires and whether a legal hold keeps it. Deleting the records, or **destroying the
  key** (crypto-shredding, which also makes copies and backups unreadable), forgets the content
  while every ledger hash stays valid: the chain never contained the content.

This module has only models and pure functions (stdlib + pydantic). Encryption and key storage need
a library and the file system, so they live in `nuramesh.engine.content`.
"""

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from nuramesh.core.org import Slug

Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", strict=True)]

MAX_TEXT_BYTES = 256 * 1024
"""The most plaintext one record may hold (UTF-8 bytes). Reasons are far smaller; this is the cap
for gate evidence and task outputs (ND25: capped)."""

KEY_BYTES = 32
"""An organization's master key is 256 random bits."""

DEFAULT_RETENTION_DAYS = 30
"""ND25/ND52: how long a task output is kept after its task is final, unless the organization says
otherwise."""

HOSTED_MAX_DAYS = 90
"""ND25: the longest hosted content is kept (and, ND55, the longest a task waits for a human)."""

DEFAULT_REASON_DAYS = 396
"""How long a person's reason is kept on a self-hosted deployment unless the organization says
otherwise (13 months, ND28 D). Hosted deployments cap it at `HOSTED_MAX_DAYS` (ND195 b)."""


class ContentSettings(BaseModel):
    """An organization's content setting (`content:` in its configuration, ND52).

    `retention_days` is how long a task output is kept **after its task is final**: the default is
    30 days; **0 means delete it when the task is final**. Hosted deployments cap it at the 90-day
    hosted maximum (`problems`); self-hosted ones set their own value.

    `reason_days` is how long a person's reason (an approval answer, a stop reason, an unblock or
    override reason) is kept: unset means the default, 396 days (13 months) self-hosted and the
    90-day maximum hosted (ND195 b); a value above 90 is refused on a hosted deployment. It can
    only raise the default: a value below 396 is refused (R11b).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    retention_days: StrictInt = Field(default=DEFAULT_RETENTION_DAYS, ge=0, le=36_500)
    reason_days: StrictInt | None = Field(default=None, ge=1, le=36_500)

    @field_validator("reason_days")
    @classmethod
    def _only_raise(cls, value: int | None) -> int | None:
        if value is not None and value < DEFAULT_REASON_DAYS:
            raise ValueError(
                f"reason_days can only raise the 13-month default of {DEFAULT_REASON_DAYS} days"
            )
        return value

    def effective_reason_days(self) -> int:
        """The days a reason is kept self-hosted: the default unless a value at or above it is
        set (a stored value below the default is read as the default, R11b)."""
        value = self.reason_days
        return DEFAULT_REASON_DAYS if value is None or value < DEFAULT_REASON_DAYS else value

    def problems(self, *, hosted: bool) -> list[str]:
        """Why this setting can't be used (empty if it can)."""
        problems: list[str] = []
        if hosted and self.retention_days > HOSTED_MAX_DAYS:
            problems.append(f"hosted content is kept at most {HOSTED_MAX_DAYS} days")
        if hosted and self.reason_days is not None and self.reason_days > HOSTED_MAX_DAYS:
            problems.append(f"hosted reasons are kept at most {HOSTED_MAX_DAYS} days")
        return problems


class ContentKind(StrEnum):
    """What a piece of content is. The kind is part of the hash, so a reason and an output with the
    same text have different ids."""

    REASON = "reason"  # a person's reason for an approval, denial, unblock or override
    GATE_EVIDENCE = "gate_evidence"  # what a verifier or human cited
    TASK_OUTPUT = "task_output"  # what a worker produced (durable runs, R1.20)
    CONFIG = "config"  # the full configuration a `config_loaded` entry names (ND118)
    PLAN_TEXT = "plan_text"  # a title or definition of done from an API run plan (R1.20, OD-1)
    TASK_TEMPLATE = "task_template"  # an org task template an API plan may reference (R1.20)
    # the arguments of an adapter tool call that asked for approval (S4 AD-24a, ND171)
    TOOL_ARGUMENTS = "tool_arguments"


def hkdf(key: bytes, info: str, length: int = 32) -> bytes:
    """HKDF-SHA256 (RFC 5869) with no salt: a derived key for one purpose. `info` names it."""
    if length < 1 or length > 255 * 32:
        raise ValueError("length out of range")
    prk = hmac.new(b"\x00" * 32, key, hashlib.sha256).digest()  # extract (the default salt)
    okm, block = b"", b""
    for counter in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info.encode() + bytes([counter]), hashlib.sha256).digest()
        okm += block
    return okm[:length]


def hash_key(master: bytes) -> bytes:
    """The key content hashes are made with, derived from the organization's master key."""
    _check_master(master)
    return hkdf(master, "nuramesh content hash v2")


def encryption_key(master: bytes) -> bytes:
    """The AES-256 key content is encrypted with, derived from the organization's master key."""
    _check_master(master)
    return hkdf(master, "nuramesh content encryption v2")


def _check_master(master: bytes) -> None:
    if len(master) != KEY_BYTES:
        raise ValueError(f"an organization key is exactly {KEY_BYTES} bytes")


def key_id(master: bytes) -> str:
    """A short public fingerprint of the organization's key (16 hex digits of an HMAC), stored with
    each record so a record sealed under an older key (before a shred) is recognised as such
    instead of looking like tampering. Reveals nothing usable about the key."""
    _check_master(master)
    return hkdf(master, "nuramesh content key id v2", 8).hex()


def keyed_hash(master: bytes, org_id: str, kind: ContentKind, text: str) -> str:
    """HMAC-SHA256 hex of the content under the organization's hashing key. The organization id
    and the kind are part of the message, with unambiguous framing (lengths, not separators)."""
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"content is over {MAX_TEXT_BYTES} bytes")
    parts = (org_id.encode(), kind.value.encode(), data)
    message = b"".join(len(p).to_bytes(8, "big") + p for p in parts)
    return hmac.new(hash_key(master), message, hashlib.sha256).hexdigest()


class LedgerHashKind(StrEnum):
    """What a ledger hash covers (PH-KEY, R2). The kind is part of the message, so the same bytes
    under two kinds give two hashes. These are hash kinds only, never content-store records, so
    they can't collide with `ContentKind`."""

    MODEL_PROMPT = "model_prompt"
    MODEL_PREFIX = "model_prefix"
    MODEL_RESPONSE = "model_response"
    EVAL_RESPONSE = "eval_response"
    ACTION_TARGET = "action_target"
    GATE_RESULT = "gate_result"
    PLAN = "plan"
    MODEL_KEY = "model_key"
    TOOL_ARGS = "tool_args"  # the 32 bytes of a tool call's arguments digest (S4 AD-26)


HASH_SCHEME = "hmac-v1"
"""The fixed marker (`hash_scheme`) every ledger payload with a PH-KEY hash carries (R4)."""

LedgerHasher = Callable[[LedgerHashKind, bytes], str]
"""What core functions take to make a ledger hash: the engine binds one to an organization."""


class MarkedLedgerHasher(Protocol):
    """A hasher bound to one organization that also states the marker for its rows (R5)."""

    def __call__(self, kind: LedgerHashKind, data: bytes) -> str: ...

    def marker(self) -> dict[str, str]: ...

    def use(self, kind: LedgerHashKind, data: bytes) -> str:
        """Hash with the key already in use (never creates one), checked against the key of the
        first digest (R6, the response hash)."""
        ...


def ledger_hash(master: bytes, org_id: str, kind: LedgerHashKind, data: bytes) -> str:
    """HMAC-SHA256 hex of `data` under the organization's hashing key (PH-KEY R2): the framing of
    `keyed_hash` (8-byte big-endian lengths), without the stored-record size cap."""
    parts = (org_id.encode(), kind.value.encode(), data)
    message = b"".join(len(p).to_bytes(8, "big") + p for p in parts)
    return hmac.new(hash_key(master), message, hashlib.sha256).hexdigest()


class KeyedRowMismatch(ValueError):
    """A keyed row that can't be attributed to the loaded key (no `hash_org`, or another key id)."""


def legacy_aware_result_match(
    recorded_value: object,
    marker: Mapping[str, object],
    data: bytes,
    keyed_digest: Callable[[], str],
    *,
    key_id_of_loaded: Callable[[], str] | None = None,
) -> bool:
    """Whether a recorded `result_hash` is this result's (PH-KEY R10, section 4 point 2).

    A row without `hash_scheme` is legacy: compared with the plain SHA-256 of `data` (the result's
    canonical bytes, hashed here and only for such a row). A row with it is
    compared with the keyed digest (a thunk, called only for such a row). A keyed row without
    `hash_org`, or whose `hash_key_id` differs from the key loaded, raises `KeyedRowMismatch`
    (fail closed: never a non-match, or the result would be recorded twice). Returns a bool,
    never a digest."""
    if "hash_scheme" not in marker:
        return recorded_value == _plain_digest_of(data)  # lazy: only a legacy row needs it (R10)
    org = marker.get("hash_org")
    if not isinstance(org, str) or not org:
        raise KeyedRowMismatch("a keyed row without hash_org can't be compared")
    digest = keyed_digest()
    if key_id_of_loaded is not None and marker.get("hash_key_id") != key_id_of_loaded():
        raise KeyedRowMismatch("the key id of a keyed row differs from the loaded key")
    return recorded_value == digest


class ContentRecord(BaseModel):
    """One stored piece of content: ciphertext and when it goes. Never holds plaintext."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: Slug
    id: Hash  # the keyed hash of the plaintext (what the ledger holds)
    kind: ContentKind
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")  # which key sealed it (`key_id(master)`)
    nonce: bytes = Field(min_length=12, max_length=12)
    ciphertext: bytes = Field(min_length=16, max_length=MAX_TEXT_BYTES + 64)  # includes the GCM tag
    size: int = Field(ge=0, le=MAX_TEXT_BYTES)  # plaintext bytes
    created_at: datetime
    expires_at: datetime | None = None  # None: kept until the organization removes it

    @model_validator(mode="after")
    def _utc(self) -> Self:
        for name in ("created_at", "expires_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} needs a time zone")
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("expires_at is before created_at")
        return self


TOMBSTONE_REASONS = ("shred", "erasure", "backfilled", "backfilled_approximate", "reconciled")
"""Why an organization key was destroyed: a fixed code, never free text (R1.37; the record is
permanent and cannot be erased)."""

_ACTOR = re.compile(r"[a-z][a-z0-9_]*(:[a-z0-9_.-]{1,64})?")
MAX_ACTOR = 80


def valid_tombstone_actor(actor: str) -> bool:
    """True if `actor` is an opaque id (`human:u_...`, `service:tok_...`, `cli`, `system`): never an
    email, a name or an address."""
    return len(actor) <= MAX_ACTOR and _ACTOR.fullmatch(actor) is not None


def normalise_tombstone_actor(actor: str) -> str:
    """The backfill's rule: a legacy actor that is not an opaque id is stored as `system`."""
    return actor if valid_tombstone_actor(actor) else "system"


class KeyTombstone(BaseModel):
    """The permanent record that an organization's content key was destroyed (R1.37, K1 to K4).
    Holds no key material, no content and no personal data. Written once, never changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: Slug
    destroyed_at: datetime
    actor: str
    reason: str
    key_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    backfilled: bool = False
    version: int = 1

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.destroyed_at.tzinfo is None:
            raise ValueError("destroyed_at needs a time zone")
        if self.destroyed_at.microsecond:
            raise ValueError("destroyed_at is whole seconds")
        if not valid_tombstone_actor(self.actor):
            raise ValueError("a tombstone actor is an opaque id, never an email or a name")
        if self.reason not in TOMBSTONE_REASONS:
            raise ValueError(f"a tombstone reason is one of {TOMBSTONE_REASONS}")
        return self

    @property
    def at_approximate(self) -> bool:
        """True if `destroyed_at` is the migration clock, not the ledger's date (the original
        date was unreadable, T258): every export and message must say so."""
        return self.reason == "backfilled_approximate"


def _plain_digest_of(data: bytes) -> str:
    """The plain SHA-256 of `data`: ONLY for comparing against a legacy ledger row written before
    PH-KEY (R8, R10), and only called by `legacy_aware_result_match` when a candidate row has no
    `hash_scheme`. Private on purpose: no engine module can reach a plain hash (K19)."""
    return hashlib.sha256(data).hexdigest()


def _has_value(value: object) -> bool:
    return value is not None and value != ""


def holds_ph_key_hash(event_type: str, payload: Mapping[str, object]) -> bool:
    """Whether a ledger payload of this type holds at least one non-null PH-KEY hash field."""
    if event_type == "model.called":
        return _has_value(payload.get("prompt_hash")) or _has_value(payload.get("response_hash"))
    if event_type == "eval.scored":
        outcomes = payload.get("outcomes")
        return isinstance(outcomes, Sequence) and any(
            isinstance(o, Mapping) and _has_value(o.get("response_hash")) for o in outcomes
        )
    if event_type in ("policy.decided", "approval.requested", "approval.decided"):
        return _has_value(payload.get("target_hash"))
    if event_type == "gate.completed":
        return _has_value(payload.get("result_hash"))
    if event_type == "task.transitioned":
        verification = payload.get("verification")
        return isinstance(verification, Sequence) and any(
            isinstance(g, Mapping) and bool(g.get("deciding_results")) for g in verification
        )
    return False


def classify_hash_row(event_type: str, payload: Mapping[str, object]) -> str | None:
    """PH-KEY R9: `legacy` (a PH-KEY hash field with a value and no `hash_scheme`), `no_org` (a
    `hash_scheme` row with no `hash_org`, which counts for nobody, R4) or None."""
    if "hash_scheme" in payload:
        return None if _has_value(payload.get("hash_org")) else "no_org"
    return "legacy" if holds_ph_key_hash(event_type, payload) else None
