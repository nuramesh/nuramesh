"""Verified Merge wire formats (spec `spec/verified-merge` section 6; SV-4, SV-14, QA-8; Fable
fixes F-2 `pins`, F-7 `holder`, F-8 formats 6.8 to 6.10, F-11 `roles`, F-15 `repo_id`).

Every object is canonical JSON as the ledger writes it (`core.canonical`, LCF-1: UTF-16 key
order, no floats, decimals as strings): `*_hash` and `*_sha256` are 64 lower-case hex SHA-256
of canonical bytes, times are `YYYY-MM-DDTHH:MM:SS.ffffffZ`, shas 40 lower-case hex. Models are
pydantic with `extra="forbid"`, frozen, and strict scalar types; a signed payload has no
defaults, so its canonical bytes are exactly the bytes that were signed. Fields are never
renamed within a version; a format change is a new version (`vm-verdict-2`) with a new domain
string, never an edit of these constants.

Signed bytes are `<domain> + canonical(payload)`; the domain strings below are distinct per
object kind so a signature is useless for any other message type (SV-4).

`parse_canonical` is the SV-14 hardened parser for stored signed objects and bundles: it refuses
duplicate object keys, integers outside the I-JSON range (+/- 2^53 - 1), floats, non-canonical
bytes and an unsupported `version` (reported before anything else, so a verifier stops there
before touching a signature). Error messages never carry the input.

Signing and verifying (Ed25519, strict) live in the engine; this module holds only the formats
and the key-id formula (`public_key_id`: 16 hex of the SHA-256 of the raw public key, as anchors).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    model_validator,
)

from nuramesh.core.canonical import MAX_SAFE_INTEGER, canonical_json
from nuramesh.core.people import HUMAN_ID_BODY

EVIDENCE_VERSION = "vm-evidence-1"
VERDICT_VERSION = "vm-verdict-1"
APPROVAL_VERSION = "vm-approval-1"
DECISION_VERSION = "vm-decision-1"
POLICY_VERSION = "vm-policy-1"
TRUST_VERSION = "vm-trust-1"
RESET_VERSION = "vm-reset-1"
WITHDRAWAL_VERSION = "vm-withdrawal-1"
OVERRIDE_VERSION = "vm-override-1"

VERDICT_DOMAIN = b"nuramesh-vm-verdict-v1\n"
APPROVAL_DOMAIN = b"nuramesh-vm-approval-v1\n"
RESET_DOMAIN = b"nuramesh-vm-reset-v1\n"
WITHDRAWAL_DOMAIN = b"nuramesh-vm-withdrawal-v1\n"
OVERRIDE_DOMAIN = b"nuramesh-vm-override-v1\n"

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
VERDICT_TTL_MAX_SECONDS = 30 * 86400
"""SV-9: the hard maximum of `expires_at - issued_at` (F-16 makes the gate check it)."""


def _strict(pattern: str, max_length: int | None = None) -> StringConstraints:
    return StringConstraints(pattern=pattern, strict=True, max_length=max_length)


Sha = Annotated[str, _strict(r"^[0-9a-f]{40}$")]
Hex64 = Annotated[str, _strict(r"^[0-9a-f]{64}$")]
KeyId = Annotated[str, _strict(r"^[0-9a-f]{16}$")]
SigHex = Annotated[str, _strict(r"^[0-9a-f]{128}$")]
RecordId = Annotated[str, _strict(r"^[0-9a-f]{32}$")]
"""128 random bits as 32 hex (SV-8 b)."""


def _real_time(text: str) -> str:
    try:
        datetime.strptime(text, TIME_FORMAT)
    except ValueError as exc:
        raise ValueError("not a real calendar time") from exc
    return text


Time = Annotated[
    str, _strict(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"), AfterValidator(_real_time)
]
OrgId = Annotated[str, _strict(r"^[a-z0-9][a-z0-9_-]{0,62}$")]
Repo = Annotated[str, _strict(r"^[a-z0-9.-]+(/[A-Za-z0-9._-]+){2,}$", 512)]
"""`<forge host>/<owner>/<name>` (deeper groups allowed), for example `github.com/acme/widgets`."""
RepoId = Annotated[str, _strict(r"^[0-9]{1,20}$")]
"""The forge's numeric repository id as a decimal string (F-15: names can be reused)."""
Ref = Annotated[str, _strict(r"^[A-Za-z0-9._/-]{1,255}$")]
HumanId = Annotated[str, _strict(rf"^human:{HUMAN_ID_BODY}$")]
Identity = Annotated[
    str,
    _strict(
        rf"^(human:{HUMAN_ID_BODY}|agent:[A-Za-z0-9._@-]{{1,128}}"
        r"|unmapped:([0-9a-f]{64}|unavailable))$"
    ),
]
"""EB-3 / F-15: an enrolled `human:` or `agent:` id, or `unmapped:` plus the SHA-256 of the
forge's numeric user id, or `unmapped:unavailable` when the push event is gone. Never a login."""
ServiceId = Annotated[str, _strict(r"^service:[A-Za-z0-9._@-]{1,128}$")]
Holder = Annotated[str, _strict(rf"^(human:{HUMAN_ID_BODY}|service:[A-Za-z0-9._@-]{{1,128}})$")]
Name = Annotated[str, _strict(r"^[A-Za-z0-9._:@/ \[\]-]{1,128}$")]
"""A check name, producer id, reviewer id, model id or model family."""
Code = Annotated[str, _strict(r"^[a-z][a-z0-9_]{0,63}$")]
GitVersion = Annotated[str, _strict(r"^\d+\.\d+(\.\d+)?$")]
Short = Annotated[str, _strict(r"^.{1,256}$")]
Path = Annotated[str, _strict(r"^.{1,4096}$")]
Pr = Annotated[int, Field(strict=True, ge=1, le=MAX_SAFE_INTEGER)]
Tier = Annotated[int, Field(strict=True, ge=1, le=3)]
Seq = Annotated[int, Field(strict=True, ge=1, le=MAX_SAFE_INTEGER)]
Count = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]
Seconds = Annotated[int, Field(strict=True, ge=1, le=MAX_SAFE_INTEGER)]

Conclusion = Literal[
    "success",
    "failure",
    "neutral",
    "cancelled",
    "timed_out",
    "action_required",
    "skipped",
    "stale",
    "pending",
    "in_progress",
    "queued",
    "error",
]


class VmModel(BaseModel):
    """Base of every wire object: unknown fields refused, frozen after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WireObject(VmModel):
    """A top-level object with a `version` field; `WIRE_VERSION` is the one this code reads."""

    WIRE_VERSION: ClassVar[str]


# --- 6.1 evidence bundle ----------------------------------------------------------------------


class Author(VmModel):
    """EB-9: who made the change, on positive evidence only."""

    kind: Literal["agent", "human", "unknown"]
    id: Identity | None
    model_family: Name | None
    source: Literal["ledger_run", "human_evidence", "unknown"]
    run_ref: Short | None


class PathEntry(VmModel):
    path: Path
    status: Literal["A", "D", "M"]


class CommitEntry(VmModel):
    """EB-3: one commit of `merge-base..head`; `tree` and `merge_tree` are what the collector
    saw and are never trusted by the gate (GT-15 b recomputes)."""

    sha: Sha
    parents: list[Sha]
    tree: Sha
    merge_tree: Sha | None
    author_id: Identity
    pusher_id: Identity


class CheckEntry(VmModel):
    """EB-5: a CI result by name, producer and sha."""

    name: Name
    kind: Literal["check_run", "status"]
    producer_id: Name
    head_sha: Sha
    conclusion: Conclusion
    run_id: Short | None
    completed_at: Time | None


class FullTierBinding(VmModel):
    """M-4 / GT-9: what the full-tier run said it ran on, from its own event payload."""

    pr: Pr
    base_ref: Ref
    head_sha: Sha


class FullTierEntry(VmModel):
    name: Name
    binding: FullTierBinding
    kind: Literal["check_run", "status"]
    producer_id: Name
    conclusion: Conclusion
    run_id: Short | None
    started_at: Time | None
    completed_at: Time | None


class ReviewEntry(VmModel):
    reviewer_id: Name
    model_id: Name
    model_family: Name | None
    verdict: Literal["ACCEPT", "REJECT"]
    verdict_text_sha256: Hex64
    reviewed_change_hash: Hex64


class Pins(VmModel):
    """F-2: the pinned recomputations the bundle's hashes were made with. `diff` accepts the
    frozen `diff-1` so that an object written with it still parses (format 1 is never edited);
    writers record `diff-2` (#944) and the gate refuses a `diff-1` bundle (README section 4)."""

    merge_tree: Literal["mt-1"]
    diff: Literal["diff-1", "diff-2"]


class EvidenceBundle(WireObject):
    """`vm-evidence-1` (6.1): the hashed facts about one (repo, PR, head). `bundle_hash` is the
    SHA-256 of the canonical bytes of the object without that field (EB-1)."""

    WIRE_VERSION: ClassVar[str] = EVIDENCE_VERSION

    version: Literal["vm-evidence-1"]
    org_id: OrgId
    repo: Repo
    repo_id: RepoId
    pr: Pr
    head_sha: Sha
    tree_sha: Sha
    base_ref: Ref
    base_sha: Sha
    merge_base: Sha
    change_hash: Hex64
    diff_hash: Hex64
    git_version: GitVersion
    pins: Pins
    author: Author
    paths: list[PathEntry]
    commits: list[CommitEntry]
    tier: Tier
    tier_reasons: list[Code]  # TL-17: codes only, a path or free text never enters the ledger
    tier_list_sha256: Hex64
    policy_sha256: Hex64
    codeowners_sha256: Hex64
    canon_commit: Sha | None
    task_sha256: Hex64 | None
    """F-5 (reserved): the hash of the exact task and definition-of-done bytes the reviewer got."""
    checks: list[CheckEntry]
    full_tier: list[FullTierEntry]
    reviews: list[ReviewEntry]
    labels: list[Short]
    collected_at: Time
    collector: ServiceId
    bundle_hash: Hex64

    @model_validator(mode="after")
    def _tier_reasons_sorted_and_unique(self) -> EvidenceBundle:
        """TL-17: `tier_reasons` is sorted by code point and has no duplicates. Whether the codes
        fit the tier is the gate's own recomputation (`tier_mismatch`, TL-20), not a wire rule."""
        if self.tier_reasons != sorted(set(self.tier_reasons)):
            raise ValueError("tier_reasons must be sorted and unique")
        return self

    def body_hash(self) -> str:
        """The hash the `bundle_hash` field must carry."""
        body = self.model_dump(mode="json")
        body.pop("bundle_hash")
        return hashlib.sha256(canonical_json(body)).hexdigest()

    def hash_ok(self) -> bool:
        return self.body_hash() == self.bundle_hash


# --- 6.2 verdict, 6.3 approval ------------------------------------------------------------------


class VerdictPayload(WireObject):
    """`vm-verdict-1` (6.2). Bound exactly or informational per SV-5; `rebound_from` names the
    origin of a rebound verdict (SV-8 d) and is not a bound field."""

    WIRE_VERSION: ClassVar[str] = VERDICT_VERSION

    version: Literal["vm-verdict-1"]
    org_id: OrgId
    repo: Repo
    repo_id: RepoId
    pr: Pr
    head_sha: Sha
    base_ref: Ref
    base_sha: Sha
    change_hash: Hex64
    bundle_hash: Hex64
    tier: Tier
    policy_sha256: Hex64
    decision: Literal["ACCEPT", "REJECT"]
    reviewer_id: Name
    model_id: Name
    model_family: Name | None
    verdict_text_sha256: Hex64
    verdict_id: RecordId
    rebound_from: RecordId | None
    key_id: KeyId
    issued_at: Time
    expires_at: Time


class SignedVerdict(VmModel):
    """The stored object: `{"payload": ..., "sig": "<128 hex>"}` (signature in hex, as anchors)."""

    payload: VerdictPayload
    sig: SigHex


class ApprovalPayload(WireObject):
    """`vm-approval-1` (6.3, QA-7)."""

    WIRE_VERSION: ClassVar[str] = APPROVAL_VERSION

    version: Literal["vm-approval-1"]
    org_id: OrgId
    repo: Repo
    repo_id: RepoId
    pr: Pr
    head_sha: Sha
    change_hash: Hex64
    bundle_hash: Hex64 | None
    policy_sha256: Hex64
    approver_id: HumanId
    role: Literal["code_owner", "trust_root_admin", "reviewer"]
    key_id: KeyId
    approval_id: RecordId
    issued_at: Time
    expires_at: Time


class SignedApproval(VmModel):
    payload: ApprovalPayload
    sig: SigHex


# --- 6.4 gate decision ----------------------------------------------------------------------------


class Counted(VmModel):
    verdicts: list[RecordId]
    approvals: list[RecordId]


class Ignored(VmModel):
    id: Short
    reason: Code


class DecisionInputs(VmModel):
    bundle_hash: Hex64 | None
    status_ids: list[Short]


class GateDecision(WireObject):
    """`vm-decision-1` (6.4, GT-13): the object that is ledgered and attested."""

    WIRE_VERSION: ClassVar[str] = DECISION_VERSION

    version: Literal["vm-decision-1"]
    org_id: OrgId
    repo: Repo
    repo_id: RepoId
    pr: Pr
    head_sha: Sha
    group_head_sha: Sha | None
    shared_head_prs: list[Pr]
    mode: Literal["shadow", "enforce"]
    state: Literal["success", "failure", "pending"]
    reason: Code
    counted: Counted
    ignored: list[Ignored]
    inputs: DecisionInputs
    override: StrictBool
    decided_at: Time
    gate_version: Short
    decision_hash: Hex64


# --- 6.6 policy -----------------------------------------------------------------------------------


class Quorum(VmModel):
    """QA-1: one tier's quorum."""

    ai: Count
    human: Count
    codeowner: StrictBool = False
    min_model_families: Annotated[int, Field(strict=True, ge=1, le=MAX_SAFE_INTEGER)] = 1
    author_family_differs: StrictBool = False


class Producer(VmModel):
    kind: Literal["check_run", "status"]
    id: Name


class Policy(WireObject):
    """`vm-policy-1` (6.6): per repository, read at the base tip, a trust root. Every top-level
    key but `extra_trust_roots` is required (GC-11 (c)); only the three quorum fields default
    (GC-11 (a), TM-21)."""

    WIRE_VERSION: ClassVar[str] = POLICY_VERSION

    version: Literal["vm-policy-1"]
    enforce_tiers: list[Tier]
    required_checks: dict[str, list[Name]]
    full_tier: list[Name]
    full_tier_tiers: list[Tier]
    producers: dict[str, Producer]
    quorum: dict[str, Quorum]
    hold_labels: list[Short]
    carry_forward: StrictBool
    verdict_ttl_seconds: Annotated[int, Field(strict=True, ge=1, le=VERDICT_TTL_MAX_SECONDS)]
    approval_ttl_seconds: Seconds
    bundle_ttl_seconds: Seconds
    last_pusher_may_approve: StrictBool
    sole_codeowner_exception: StrictBool
    approval_binds_bundle: StrictBool
    approver_custody_min: Literal["software", "hardware"]
    review_budget_scope: Short
    # GC-11 (b), TL-29: the one optional key; an invalid value is carried as written (hashed as
    # written) and the tier decision gives `trust_roots_invalid`, never `policy_invalid`.
    extra_trust_roots: Any = Field(default_factory=list)
    max_diff_bytes: Seconds
    reject_memory_days: Seconds
    task_reject_limit: Seconds
    task_ask_limit: Seconds
    gate_secret_names: list[Short]

    @model_validator(mode="before")
    @classmethod
    def _tier3_quorum_defaults(cls, data: Any) -> Any:
        """GC-11 (a): omitted quorum fields of tier "3" default to the strict values."""
        if isinstance(data, dict) and isinstance(data.get("quorum"), dict):
            entry = data["quorum"].get("3")
            if isinstance(entry, dict):
                strict = {"codeowner": True, "min_model_families": 1, "author_family_differs": True}
                data = {**data, "quorum": {**data["quorum"], "3": {**strict, **entry}}}
        return data

    @model_validator(mode="after")
    def _tier_keys(self) -> Policy:
        for name, table in (("quorum", self.quorum), ("required_checks", self.required_checks)):
            if set(table) != {"1", "2", "3"}:
                raise ValueError(f"{name} must have exactly the keys 1, 2 and 3")
        return self


# --- 6.7 trust list -------------------------------------------------------------------------------


class TrustKey(VmModel):
    """One public key of the trust list. `holder` (F-7) names who holds the private key:
    `service:<reviewer id>` for a verdict signer, `human:<id>` for an approver. `roles` (F-11)
    grants `trust_root_admin` beside the key's role."""

    key_id: KeyId
    role: Literal["verdict-signer", "attestor", "approver"]
    holder: Holder
    roles: list[Literal["approver", "trust_root_admin"]] = Field(default_factory=list)
    public_key: Hex64
    custody: Literal["hardware", "software", "service"]
    valid_from: Time
    valid_to: Time | None
    revoked_at: Time | None
    revoked_seq: Seq | None
    compromised: StrictBool

    def problem(self) -> str | None:
        """Why this entry is not a usable key, or None. 6.7: `revoked_at` without `revoked_seq`
        is invalid (the gate treats the key as untrusted); a `key_id` that is not the formula's
        value for `public_key`, or a holder of the wrong kind for the role, is invalid too."""
        if self.revoked_at is not None and self.revoked_seq is None:
            return "revoked_without_seq"
        if self.key_id != public_key_id(self.public_key):
            return "key_id"
        if self.role == "approver" and not self.holder.startswith("human:"):
            return "holder_kind"
        if self.role != "approver" and not self.holder.startswith("service:"):
            return "holder_kind"
        return None

    def has_role(self, name: str) -> bool:
        return name == self.role or name in self.roles


class TrustList(WireObject):
    """`vm-trust-1` (6.7): the public keys per role with validity windows and revocations,
    versioned by a `serial` that only increases."""

    WIRE_VERSION: ClassVar[str] = TRUST_VERSION

    version: Literal["vm-trust-1"]
    org_id: OrgId
    serial: Seq
    keys: list[TrustKey]

    @model_validator(mode="after")
    def _unique_key_ids(self) -> TrustList:
        ids = [key.key_id for key in self.keys]
        if len(set(ids)) != len(ids):
            raise ValueError("trust list has a duplicate key_id")
        return self

    def key(self, key_id: str) -> TrustKey | None:
        for key in self.keys:
            if key.key_id == key_id:
                return key
        return None


# --- 6.8 reset, 6.9 withdrawal, 6.10 override (F-8) ---------------------------------------------


class ResetScope(VmModel):
    """SV-10 / F-12: a reset voids the records of one change hash, one task, or (at a mode flip)
    every stored decision; `value` is the change hash or the task key, null for `mode_flip`."""

    kind: Literal["change_hash", "task", "mode_flip"]
    value: Short | None

    @model_validator(mode="after")
    def _value_matches_kind(self) -> ResetScope:
        if (self.kind == "mode_flip") != (self.value is None):
            raise ValueError("a reset scope value is required unless the kind is mode_flip")
        if self.kind == "change_hash" and (len(self.value or "") != 64):
            raise ValueError("a change_hash scope value is a 64-hex hash")
        return self


class ResetPayload(WireObject):
    """`vm-reset-1` (6.8): an admin's signed `merge.review_reset`."""

    WIRE_VERSION: ClassVar[str] = RESET_VERSION

    version: Literal["vm-reset-1"]
    org_id: OrgId
    repo: Repo
    repo_id: RepoId
    scope: ResetScope
    reason_sha256: Hex64
    approver_id: HumanId
    key_id: KeyId
    reset_id: RecordId
    issued_at: Time


class SignedReset(VmModel):
    payload: ResetPayload
    sig: SigHex


class WithdrawalPayload(WireObject):
    """`vm-withdrawal-1` (6.9): a signed `merge.approval_withdrawn` (QA-9)."""

    WIRE_VERSION: ClassVar[str] = WITHDRAWAL_VERSION

    version: Literal["vm-withdrawal-1"]
    org_id: OrgId
    repo: Repo
    repo_id: RepoId
    pr: Pr
    approval_id: RecordId
    approver_id: HumanId
    key_id: KeyId
    withdrawal_id: RecordId
    issued_at: Time


class SignedWithdrawal(VmModel):
    payload: WithdrawalPayload
    sig: SigHex


class OverridePayload(WireObject):
    """`vm-override-1` (6.10): the break-glass `merge.override` (GT-16), binding the head and
    change hash the admin saw (F-9)."""

    WIRE_VERSION: ClassVar[str] = OVERRIDE_VERSION

    version: Literal["vm-override-1"]
    org_id: OrgId
    repo: Repo
    repo_id: RepoId
    pr: Pr
    head_sha: Sha
    change_hash: Hex64
    reason_sha256: Hex64
    approver_id: HumanId
    key_id: KeyId
    override_id: RecordId
    issued_at: Time


class SignedOverride(VmModel):
    payload: OverridePayload
    sig: SigHex


# --- canonical bytes, hashes, signed bytes --------------------------------------------------------


def canonical_bytes(model: BaseModel) -> bytes:
    """The canonical JSON of a wire object (LCF-1)."""
    return canonical_json(model.model_dump(mode="json"))


def payload_hash(model: BaseModel) -> str:
    """SHA-256 (hex) of the canonical bytes of a wire object (the ledger's payload hash)."""
    return hashlib.sha256(canonical_bytes(model)).hexdigest()


def signed_bytes(domain: bytes, payload: BaseModel) -> bytes:
    """The bytes a signer signs and a verifier checks: the domain string then the payload."""
    return domain + canonical_bytes(payload)


def parse_time(text: str) -> datetime:
    """A `Time` field as an aware UTC datetime."""
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def format_time(when: datetime) -> str:
    """A `Time` field from an aware datetime (converted to UTC, microseconds kept)."""
    return when.astimezone(UTC).strftime(TIME_FORMAT)


def public_key_id(public_key_hex: str) -> str:
    """The key id of a raw 32-byte Ed25519 public key: 16 hex of its SHA-256 (as anchors)."""
    try:
        raw = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise ValueError("public key is not hex") from exc
    if len(raw) != 32:
        raise ValueError("public key is not 32 bytes")
    return hashlib.sha256(raw).hexdigest()[:16]


class KeyIdCollision(ValueError):
    """A trust-list key id equals a key id of another space (anchor keys, licence keys, ...):
    the same 16-hex formula, the same collision space, so the entry is refused."""

    def __init__(self, key_id: str, space: str) -> None:
        self.key_id = key_id
        self.space = space
        super().__init__(f"trust-list key id collides with a {space} key id")


def refuse_key_id_collisions(trust: TrustList, reserved: Mapping[str, str]) -> None:
    """Raise `KeyIdCollision` when any trust-list key id is in `reserved` (key id to the name
    of the space it belongs to, supplied by the caller who can see those stores)."""
    for key in trust.keys:
        space = reserved.get(key.key_id)
        if space is not None:
            raise KeyIdCollision(key.key_id, space)


# --- SV-14 hardened parsing -----------------------------------------------------------------------


class FormatError(ValueError):
    """A stored object this code refuses. `code` is one of `not_json`, `duplicate_key`,
    `unsafe_integer`, `not_canonical_number`, `not_object`, `unsupported_version`, `schema`,
    `not_canonical`. The message is the code only: never the input, a key or a value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"format: {code}")

    def public_message(self) -> str:
        return str(self)


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _Refused("duplicate_key")
        out[key] = value
    return out


def _int(text: str) -> int:
    if len(text.lstrip("-")) > 16:  # at least 10^16 > 2^53 - 1; never int() a huge literal
        raise _Refused("unsafe_integer")
    value = int(text)
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise _Refused("unsafe_integer")
    return value


def _refuse_number(text: str) -> Any:
    raise _Refused("not_canonical_number")


def read_hardened_json(data: bytes) -> Any:
    """JSON with duplicate keys, floats, `NaN`/`Infinity` and integers beyond +/- 2^53 - 1
    refused (SV-14); raises `FormatError`."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError("not_json") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_int=_int,
            parse_float=_refuse_number,
            parse_constant=_refuse_number,
        )
    except _Refused as exc:
        raise FormatError(exc.code) from None
    except (ValueError, RecursionError) as exc:
        raise FormatError("not_json") from exc


def parse_canonical[W: WireObject](data: bytes, model: type[W]) -> W:
    """Read a stored wire object: hardened JSON, the expected `version` (else
    `unsupported_version`, reported before anything else), byte-for-byte canonical input, then
    the schema. The returned object's canonical bytes equal `data`."""
    parsed = read_hardened_json(data)
    if not isinstance(parsed, dict):
        raise FormatError("not_object")
    if parsed.get("version") != model.WIRE_VERSION:
        raise FormatError("unsupported_version")
    try:
        if canonical_json(parsed) != data:
            raise FormatError("not_canonical")
    except (ValueError, TypeError) as exc:
        raise FormatError("not_canonical") from exc
    try:
        value = model.model_validate(parsed)
    except ValidationError as exc:
        raise FormatError("schema") from exc
    if canonical_bytes(value) != data:
        raise FormatError("not_canonical")
    return value
