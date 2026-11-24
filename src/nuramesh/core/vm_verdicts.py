"""Merge Gate binding evaluator and GT-10 verdict check (spec SV-5,
SV-7, SV-8 b-d, SV-9, SV-12, GT-10, GT-15, 7.1; Fable fixes F-7, F-16, F-18).

Pure functions over records and a **gate view** (`GateView`: what the gate recomputed itself
from the forge and the repository). Nothing here reads the forge, the ledger or a key; the
caller supplies:

- `verify(public_key_hex, message, sig_hex) -> bool`: strict Ed25519 verification (engine);
- `carry_forward(record_head) -> str`: BR (a) and (b) of GT-15 for a record bound to another
  head, `"ok"` or the BR reason (`vm_carry.BrResult.reason`), computed by the caller with
  `vm_carry.carry_forward` and its git callables;
- `rebind(record) -> VerdictRecord | None`: ask the signer once to rebind an ACCEPT whose BR
  failed (SV-8 d); None when the signer refused, or pass `None` for no signer.

`judge_verdict` says whether one record is valid for the view (`ok`) or why not (one of the
GT-10 codes); `evaluate_verdicts` is step 6 of 7.1: candidate selection (the ledger test of
SV-12, the change hash, reset voiding by ledger order, the carry-forward switch), the 100-record
cap over distinct verdict ids newest first, REJECT precedence, conflict, and the newest failing
code. `EVALUATION_ORDER` is the 7.1 table as data with trust roots at step 3b (F-18), and
`resolve_order` applies GT-3 (the first check that does not pass decides; pending stops like a
failure; `gate_error` wins over everything).

Per-record checks, in the order their code is reported: trusted key (present, role
`verdict-signer`, a well-formed entry: `verdict_key_untrusted`), strict signature
(`verdict_bad_signature`), compromise or revocation by ledger order (`verdict_revoked`), the
key's window at the record's ledger time (`verdict_key_untrusted`), every exactly-bound field
plus F-7 holder, the
ledgered bundle, the F-16 ttl maximum and ledger-time skew (`verdict_wrong_binding`), the
validity window (`verdict_not_yet_valid`, `verdict_expired`), and for a rebound verdict its
whole `rebound_from` chain under the same tests except head binding (SV-7, SV-8 d).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from nuramesh.core.vm_formats import (
    VERDICT_DOMAIN,
    VERDICT_TTL_MAX_SECONDS,
    SignedVerdict,
    TrustList,
    VerdictPayload,
    parse_time,
    payload_hash,
    signed_bytes,
)

State = Literal["success", "failure", "pending"]

OK = "ok"
RESET = "reset"
"""`judge_verdict` result for a record (or an origin in its chain) voided by a `merge.review_reset`:
listed as ignored `reset`, never a failure code."""

VERDICT_FAILURE_CODES = (
    "verdict_rejected",
    "verdict_conflict",
    "verdict_bad_signature",
    "verdict_key_untrusted",
    "verdict_wrong_binding",
    "verdict_expired",
    "verdict_not_yet_valid",
    "verdict_revoked",
)
MAX_CANDIDATES = 100
MAX_CHAIN = 64
MAX_COPIES = 8
"""GC-15 b: distinct copies of one verdict_id that are verified."""


@dataclass(frozen=True)
class GateView:
    """What the gate computed itself for one (org, repo, PR, live head) at `now`."""

    org_id: str
    repo: str
    repo_id: str
    pr: int
    base_ref: str
    head_sha: str
    change_hash: str
    policy_sha256: str
    tier: int
    now: datetime
    ledgered_bundle_hashes: frozenset[str]
    """Bundles the ledger records for this PR and change hash (EB-7, SV-5 `bundle_hash`)."""
    task_key: str | None = None
    """The task a `merge.review_reset` with scope `task` is matched against (SV-10), or None."""
    carry_forward: bool = True
    skew_seconds: int = 300
    verdict_ttl_max_seconds: int = VERDICT_TTL_MAX_SECONDS


@dataclass(frozen=True)
class VerdictRecord:
    """A stored verdict with what the ledger says about it (LA-5): the sequence and time of its
    `merge.verdict_issued` entry and the payload hash that entry carries (None: not found)."""

    verdict: SignedVerdict
    ledger_seq: int | None
    ledger_at: datetime | None
    ledger_payload_hash: str | None
    source_at: datetime | None = None
    """When the forge showed the copy (the comment's created time); None for the signer's copy
    (GC-15 b: forge-only copies are taken oldest first)."""
    comment_id: int | None = None
    """The forge comment id: the tie-break after `source_at` (None sorts first)."""

    def __post_init__(self) -> None:
        if self.source_at is not None and self.source_at.tzinfo is None:
            raise ValueError("source_at must be timezone-aware (GC-15 b)")

    @property
    def payload(self) -> VerdictPayload:
        return self.verdict.payload

    @property
    def id(self) -> str:
        return self.verdict.payload.verdict_id

    def in_ledger(self) -> bool:
        """SV-12: a candidate only if its entry exists and carries this payload's hash."""
        return (
            self.ledger_seq is not None
            and self.ledger_at is not None
            and self.ledger_payload_hash == payload_hash(self.verdict.payload)
        )


@dataclass(frozen=True)
class ResetRecord:
    """A ledgered `merge.review_reset` (SV-10): its sequence and scope."""

    seq: int
    kind: Literal["change_hash", "task", "mode_flip"]
    value: str | None


@dataclass(frozen=True)
class UnparsedRecord:
    """A forge comment the gate could not turn into a `VerdictRecord` (GC-15 b, f): a pointer
    (`VERDICT_ID=<id>`, no signature) or a record that fails the schema. The caller puts them in
    the one `records` stream with `source_at`; `evaluate_verdicts` sorts it (oldest first)."""

    kind: Literal["pointer", "malformed"]
    verdict_id: str
    ledger_seq: int | None = None
    change_hash: str | None = None
    ledgered_decision: Literal["ACCEPT", "REJECT"] | None = None
    copy: bool = False
    """Malformed only: it carries the `verdict_id` and a payload hash the ledger holds, so it is
    a copy that uses one of the 8 (GC-15 b, case GC-88)."""
    source_at: datetime | None = None
    """The comment's created time; see `VerdictRecord.source_at`."""
    comment_id: int | None = None
    """The forge comment id; see `VerdictRecord.comment_id`."""

    def __post_init__(self) -> None:
        if self.source_at is not None and self.source_at.tzinfo is None:
            raise ValueError("source_at must be timezone-aware (GC-15 b)")


Verify = Callable[[str, bytes, str], bool]
CarryForward = Callable[[str], str]
"""`"ok"` when BR holds, else the reason; `unknown_commit` fails closed (GC-15 e)."""
Rebind = Callable[[VerdictRecord], "VerdictRecord | None"]


@dataclass(frozen=True)
class VerdictOutcome:
    """Step 6 of 7.1 for one evaluation."""

    state: State
    reason: str
    counted: tuple[str, ...]
    ignored: tuple[tuple[str, str], ...]
    candidates_checked: int
    rebind_asked: tuple[str, ...] = ()
    detail: str = ""
    """One of `decision_duplicate`, `accept_and_reject`, `verdict_reuse` for a conflict, or
    `over_cap` when more than `MAX_CANDIDATES` candidates made the check fail closed."""

    @property
    def state_reason(self) -> tuple[State, str]:
        return self.state, self.reason


def voided_by_reset(record: VerdictRecord, view: GateView, resets: Iterable[ResetRecord]) -> bool:
    """SV-10: a reset voids every record of its scope whose `merge.verdict_issued` entry has a
    lower ledger sequence; a record without a sequence is not judged here (not a candidate)."""
    seq = record.ledger_seq
    if seq is None:
        return False
    change_hash = record.payload.change_hash
    for reset in resets:
        if reset.seq <= seq:
            continue
        if reset.kind == "mode_flip":
            return True
        if reset.kind == "change_hash" and reset.value == change_hash:
            return True
        if reset.kind == "task" and view.task_key is not None and reset.value == view.task_key:
            return True
    return False


def _voided(
    seq: int | None, change_hash: str | None, view: GateView, resets: Iterable[ResetRecord]
) -> bool:
    """`voided_by_reset` for a record known only by ledger sequence and change hash."""
    if seq is None:
        return False
    for reset in resets:
        if reset.seq <= seq:
            continue
        if reset.kind == "mode_flip":
            return True
        if reset.kind == "change_hash" and reset.value == change_hash:
            return True
        if reset.kind == "task" and view.task_key is not None and reset.value == view.task_key:
            return True
    return False


def _once(pairs: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """`ignored` holds one entry per (id, reason), first occurrence order."""
    return tuple(dict.fromkeys(pairs))


def judge_verdict(
    record: VerdictRecord,
    view: GateView,
    trust: TrustList,
    verify: Verify,
    records_by_id: Mapping[str, VerdictRecord],
    resets: Sequence[ResetRecord],
    *,
    head_check: bool = True,
    _seen: frozenset[str] = frozenset(),
) -> str:
    """`ok` when the record counts for the view (its decision is then read by the caller), else
    the GT-10 code, or `reset` when an origin in its chain was voided. With `head_check` False
    the record's `head_sha` is not compared (BR holds for it, or it is a chain origin)."""
    p = record.payload
    key = trust.key(p.key_id)
    if key is None or key.role != "verdict-signer" or key.problem() is not None:
        return "verdict_key_untrusted"
    if not verify(key.public_key, signed_bytes(VERDICT_DOMAIN, p), record.verdict.sig):
        return "verdict_bad_signature"
    if key.compromised:
        return "verdict_revoked"
    if key.revoked_seq is not None and (
        record.ledger_seq is None or record.ledger_seq > key.revoked_seq
    ):
        return "verdict_revoked"
    now = view.now
    # GC-10: the key window is judged at the record's ledger time, never at `now`
    window_at = record.ledger_at
    if (
        window_at is None
        or parse_time(key.valid_from) > window_at
        or (key.valid_to is not None and window_at >= parse_time(key.valid_to))
    ):
        return "verdict_key_untrusted"
    bound = (
        p.org_id == view.org_id,
        p.repo == view.repo,
        p.repo_id == view.repo_id,
        p.pr == view.pr,
        p.base_ref == view.base_ref,
        p.change_hash == view.change_hash,
        p.policy_sha256 == view.policy_sha256,
        p.tier == view.tier,
        (not head_check) or p.head_sha == view.head_sha,
        p.bundle_hash in view.ledgered_bundle_hashes,
        key.holder == "service:" + p.reviewer_id,  # F-7
    )
    if not all(bound):
        return "verdict_wrong_binding"
    if trust.org_id != view.org_id:  # TN-1, TN-2: a trust list of another org counts for nothing
        return "verdict_key_untrusted"
    issued, expires = parse_time(p.issued_at), parse_time(p.expires_at)
    skew = timedelta(seconds=view.skew_seconds)
    ttl = expires - issued
    if ttl <= timedelta(0) or ttl > timedelta(seconds=view.verdict_ttl_max_seconds):  # F-16
        return "verdict_wrong_binding"
    if record.ledger_at is None or abs(issued - record.ledger_at) > skew:  # F-16
        return "verdict_wrong_binding"
    if now < issued - skew:
        return "verdict_not_yet_valid"
    if now >= expires:
        return "verdict_expired"
    if p.rebound_from is None:
        return OK
    # SV-8 d, SV-7: the whole chain passes the same tests except head binding
    if len(_seen) + 2 > MAX_CHAIN or p.verdict_id in _seen or p.rebound_from in _seen:
        return "verdict_wrong_binding"
    origin = records_by_id.get(p.rebound_from)
    if origin is None or not origin.in_ledger() or origin.id != p.rebound_from:
        return "verdict_wrong_binding"
    if voided_by_reset(origin, view, resets):
        return RESET
    if origin.payload.decision != "ACCEPT":
        return "verdict_wrong_binding"
    if origin.payload.reviewer_id != p.reviewer_id:  # GC-93 c: a chain never changes reviewer
        return "verdict_wrong_binding"
    if expires > parse_time(origin.payload.expires_at):  # rebinding never extends validity
        return "verdict_wrong_binding"
    return judge_verdict(
        origin,
        view,
        trust,
        verify,
        records_by_id,
        resets,
        head_check=False,
        _seen=_seen | {p.verdict_id},
    )


def _signature_ok(record: VerdictRecord, trust: TrustList, verify: Verify) -> bool:
    p = record.payload
    key = trust.key(p.key_id)
    return key is not None and verify(
        key.public_key, signed_bytes(VERDICT_DOMAIN, p), record.verdict.sig
    )


def _newest_first(records: Iterable[VerdictRecord]) -> list[VerdictRecord]:
    indexed = list(enumerate(records))
    indexed.sort(key=lambda item: (-(item[1].ledger_seq or 0), item[0]))
    return [record for _, record in indexed]


class _JudgedCopies(Mapping[str, VerdictRecord]):
    """verdict_id -> the copy that is judged. The copy of an id is chosen on first use, because
    choosing verifies signatures: an id nobody asks for costs no verification (#1122)."""

    def __init__(self, pick: Callable[[str], VerdictRecord | None], ids: Iterable[str]) -> None:
        self._pick = pick
        self._ids = tuple(ids)
        self._rebound: dict[str, VerdictRecord] = {}

    def __getitem__(self, verdict_id: str) -> VerdictRecord:
        found = self._pick(verdict_id) or self._rebound.get(verdict_id)
        if found is None:
            raise KeyError(verdict_id)
        return found

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys((*self._ids, *self._rebound)))

    def __len__(self) -> int:
        return len(dict.fromkeys((*self._ids, *self._rebound)))

    def add_rebound(self, record: VerdictRecord) -> None:
        """A verdict the signer just rebound; a presented copy of its id keeps its place."""
        if self._pick(record.id) is None:
            self._rebound.setdefault(record.id, record)


_COPY_FAILED = "\x00copy-failed"
"""Placeholder in the working `ignored` list at the place where an id's `verdict_bad_signature`
entry goes if, once its copies are verified, some verify and some do not."""

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _arrival_key(item: VerdictRecord | UnparsedRecord) -> tuple[object, ...]:
    """GC-15 b: oldest first by (source_at, comment_id) (None first), then a total,
    input-independent tie-break: verdict_id, signature bytes, then every remaining field."""
    at = item.source_at
    cid = item.comment_id
    head = (at is not None, at or _EPOCH, cid is not None, cid if cid is not None else -1)
    if isinstance(item, UnparsedRecord):
        return (
            *head,
            item.verdict_id,
            "",
            item.kind,
            item.ledger_seq if item.ledger_seq is not None else -1,
            item.change_hash or "",
            item.ledgered_decision or "",
            item.copy,
        )
    return (
        *head,
        item.id,
        item.verdict.sig,
        "",
        item.ledger_seq if item.ledger_seq is not None else -1,
        item.ledger_payload_hash or "",
        "",
        False,
    )


def evaluate_verdicts(
    records: Iterable[VerdictRecord | UnparsedRecord],
    view: GateView,
    trust: TrustList,
    verify: Verify,
    resets: Sequence[ResetRecord],
    *,
    ai_required: bool,
    carry_forward: CarryForward,
    rebind: Rebind | None,
    max_candidates: int = MAX_CANDIDATES,
    ai_quorum: int,
) -> VerdictOutcome:
    """GT-10 (step 6 of 7.1). With `ai_required` False (the tier's quorum has `ai` 0) verdicts
    are ignored and the step passes."""
    if not ai_required:
        return VerdictOutcome("success", OK, (), (), 0)
    # forge-only: oldest comment first by source time, so the input order never matters
    stream = sorted(records, key=_arrival_key)
    ordered = _newest_first(r for r in stream if isinstance(r, VerdictRecord))
    ledgered_hash: dict[str, str | None] = {}
    seq_of_id: dict[str, int | None] = {}
    id_of_seq: dict[int | None, str] = {}
    for record in ordered:  # one ledger entry per verdict_id
        if record.in_ledger():
            seen_hash = ledgered_hash.setdefault(record.id, record.ledger_payload_hash)
            # #1163: one ledger entry has one sequence; two ids never share one (fail closed;
            # extends SV-8 (b): two ids with one seq also fail as verdict_reuse)
            seen_seq = seq_of_id.setdefault(record.id, record.ledger_seq)
            owner = id_of_seq.setdefault(record.ledger_seq, record.id)
            if (
                seen_hash != record.ledger_payload_hash
                or seen_seq != record.ledger_seq
                or owner != record.id
            ):  # verdict_reuse (signer fault)
                return VerdictOutcome("failure", "verdict_conflict", (), (), 0, (), "verdict_reuse")
    ignored: list[tuple[str, str]] = []
    # GC-15 b: copies of one verdict_id in arrival order; at most MAX_COPIES distinct ones are
    # verified, the rest are ignored `duplicate_id` unverified
    groups: dict[str, list[VerdictRecord | UnparsedRecord]] = {}
    for item in stream:
        if isinstance(item, UnparsedRecord):
            if item.kind == "pointer":
                ignored.append((item.verdict_id, "pointer_unresolved"))
                continue
            ignored.append((item.verdict_id, "malformed"))
            if item.copy:
                groups.setdefault(item.verdict_id, []).append(item)
        elif not item.in_ledger():
            ignored.append((item.id, "not_in_ledger"))
        else:
            groups.setdefault(item.id, []).append(item)
    # #1122 (GT-10, GC-15 c): nothing is verified until the cap is known to hold. Signatures
    # are checked only for the candidates within the cap and for the chain origins they name,
    # each (key, message, signature) once.
    verified: dict[tuple[str, bytes, str], bool] = {}

    def verify_once(public_key: str, message: bytes, sig: str) -> bool:
        call = (public_key, message, sig)
        if call not in verified:
            verified[call] = verify(public_key, message, sig)
        return verified[call]

    copies: dict[str, tuple[list[VerdictRecord], list[VerdictRecord]]] = {}  # slots, all parsed
    failed_malformed: list[tuple[int, str]] = []  # a ledgered REJECT that has no parsed copy
    for vid, group in groups.items():
        slots: list[VerdictRecord | UnparsedRecord] = []
        seen_sigs: set[str] = set()
        for item in group:
            if isinstance(item, VerdictRecord) and item.verdict.sig in seen_sigs:
                ignored.append((vid, "duplicate_id"))
            elif len(slots) >= MAX_COPIES:
                ignored.append((vid, "duplicate_id"))
            else:
                slots.append(item)
                if isinstance(item, VerdictRecord):
                    seen_sigs.add(item.verdict.sig)
        parsed_all = [i for i in group if isinstance(i, VerdictRecord)]
        if parsed_all:
            copies[vid] = ([i for i in slots if isinstance(i, VerdictRecord)], parsed_all)
            ignored.append((vid, _COPY_FAILED))
        else:
            first = group[0]
            assert isinstance(first, UnparsedRecord)  # no parsed copy in this group
            unknown = first.change_hash is None or first.ledgered_decision is None
            if unknown or (
                first.ledgered_decision == "REJECT"
                and first.change_hash == view.change_hash
                and not _voided(first.ledger_seq, first.change_hash, view, resets)
            ):
                # a copy that cannot be told from a REJECT fails closed (GC-15 b)
                failed_malformed.append((first.ledger_seq or 0, "verdict_bad_signature"))
    chosen: dict[str, VerdictRecord] = {}
    precode: dict[str, str] = {}  # verdict_id -> code the candidate fails with before GC-9
    some_copy_failed: set[str] = set()

    def pick(vid: str) -> VerdictRecord | None:
        """The copy of `vid` that is judged (GC-15 b); the first call verifies its slots."""
        if vid in chosen:
            return chosen[vid]
        if vid not in copies:
            return None
        parsed_slots, parsed_all = copies[vid]
        verifying = [i for i in parsed_slots if _signature_ok(i, trust, verify_once)]
        if verifying:
            chosen[vid] = verifying[0]
            if len(verifying) < len(parsed_slots):
                some_copy_failed.add(vid)
        else:
            chosen[vid] = parsed_slots[0] if parsed_slots else parsed_all[0]
            # no copy verifies (or the slots hold only malformed copies): fail closed without
            # verifying again; an untrusted key still reports verdict_key_untrusted
            precode[vid] = judge_verdict(
                chosen[vid], view, trust, lambda *_: False, {}, resets, head_check=False
            )
        return chosen[vid]

    def listed() -> tuple[tuple[str, str], ...]:
        """`ignored` as reported: a copy placeholder becomes its entry or is dropped."""
        return _once(
            (vid, "verdict_bad_signature") if reason == _COPY_FAILED else (vid, reason)
            for vid, reason in ignored
            if reason != _COPY_FAILED or vid in some_copy_failed
        )

    by_id = _JudgedCopies(pick, copies)
    candidates: list[VerdictRecord] = []
    wrong_head: list[VerdictRecord] = []
    wrong_head_failed: list[tuple[VerdictRecord, str]] = []

    def judge(record: VerdictRecord, *, head_check: bool = True) -> str:
        return precode.get(record.id) or judge_verdict(
            record, view, trust, verify_once, by_id, resets, head_check=head_check
        )

    # Which ids are candidates is read from the ledgered payload and the ledger sequence alone
    # (every copy of an id carries the one ledgered payload), so the cap is settled first.
    plan: list[tuple[str, str]] = []  # newest first: (verdict_id, what happens to it)
    classified: set[str] = set()
    counted_ids: list[str] = []  # newest first: the ids that count toward the cap (GC-15 c)
    for record in ordered:
        if record.id in classified or record.id not in copies or not record.in_ledger():
            continue
        classified.add(record.id)
        if record.payload.change_hash != view.change_hash:
            plan.append((record.id, "change_hash"))
        elif voided_by_reset(record, view, resets):
            plan.append((record.id, RESET))
        elif (
            not view.carry_forward
            and record.payload.decision == "ACCEPT"  # BR is not applied to a REJECT (GT-10)
            and record.payload.head_sha != view.head_sha
        ):
            counted_ids.append(record.id)
            plan.append((record.id, "wrong_head"))
        else:
            counted_ids.append(record.id)
            plan.append((record.id, "candidate"))
    if len(counted_ids) > max_candidates:
        # GC-15 c: fail closed, with no signature verified; the candidates beyond the cap are
        # listed as ignored
        ignored.extend(step for step in plan if step[1] in ("change_hash", RESET))
        ignored.extend((vid, "candidate_cap") for vid in counted_ids[max_candidates:])
        return VerdictOutcome("failure", "verdict_bad_signature", (), listed(), 0, (), "over_cap")
    for vid, step in plan:
        if step in ("change_hash", RESET):
            ignored.append((vid, step))
            continue
        record = by_id[vid]
        if step == "candidate":
            candidates.append(record)
            continue
        # GC-7 a: only a record that passes every GC-9 check but the head is set aside
        code = judge(record, head_check=False)
        if code == OK:
            wrong_head.append(record)
            ignored.append((record.id, "wrong_head"))
        elif code == RESET:
            ignored.append((record.id, RESET))
            wrong_head_failed.append((record, RESET))
        else:
            wrong_head_failed.append((record, code))
    rebound_origins = {c.payload.rebound_from for c in candidates if c.payload.rebound_from}
    accepts: list[str] = []
    rejects: list[str] = []
    failures: list[tuple[int, str, bool, str]] = []  # (ledger seq, code, is a REJECT, detail)
    asked: list[str] = []
    br_failed_ok: list[str] = []  # valid old-head ACCEPTs whose BR failed (GC-7 f)

    def settle(record: VerdictRecord, code: str, detail: str = "") -> None:
        if code == OK:
            (accepts if record.payload.decision == "ACCEPT" else rejects).append(record.id)
        elif code == RESET:
            ignored.append((record.id, RESET))
        else:
            failures.append(
                (record.ledger_seq or 0, code, record.payload.decision == "REJECT", detail)
            )
            ignored.append((record.id, code))

    for record in candidates:
        p = record.payload
        if p.decision == "REJECT":  # blocks its change hash whatever its head; never rebound
            settle(record, judge(record, head_check=False))
            continue
        if p.head_sha == view.head_sha:
            settle(record, judge(record))
            continue
        if p.verdict_id in rebound_origins:
            ignored.append((record.id, "rebound"))  # its rebound verdict is judged instead
            continue
        br = carry_forward(p.head_sha)
        if br == "ok":
            settle(record, judge(record, head_check=False))
            continue
        if br == "unknown_commit":
            # GC-15 e: git cannot explain the chain: no rebind is asked, fail closed; a record
            # that is valid otherwise still counts for the duplicate test
            own = judge(record, head_check=False)
            if own != OK:
                settle(record, own)
            else:
                br_failed_ok.append(record.id)
                settle(record, "verdict_wrong_binding", "unknown_commit")
            continue
        # BR failed. A record that fails its own checks is not a decision and is not rebound
        # (GC-32); a valid one still counts for the duplicate test (GC-7 f, GC-30).
        own = judge(record, head_check=False)
        if own != OK:
            settle(record, own)
            continue
        if rebind is None:
            br_failed_ok.append(record.id)
            settle(record, "verdict_wrong_binding")
            continue
        asked.append(record.id)
        fresh = rebind(record)
        if (
            fresh is None
            or not fresh.in_ledger()
            or fresh.payload.head_sha != view.head_sha
            or fresh.payload.rebound_from != p.verdict_id
            or fresh.payload.change_hash != view.change_hash
            or fresh.payload.decision != "ACCEPT"
        ):
            br_failed_ok.append(record.id)
            settle(record, "verdict_wrong_binding")
            continue
        by_id.add_rebound(fresh)
        settle(fresh, judge_verdict(fresh, view, trust, verify_once, by_id, resets))
    for stale, code in wrong_head_failed:  # GC-7 a: an invalid old-head ACCEPT is not a decision
        if code != RESET:
            failures.append((stale.ledger_seq or 0, code, False, ""))
            ignored.append((stale.id, code))
    failures.extend((seq, code, True, "") for seq, code in failed_malformed)
    checked = len(candidates)
    # a wrong-head ACCEPT (carry-forward off) still is a decision for this change hash
    extra = [
        r.id
        for r in wrong_head
        if r.payload.verdict_id not in rebound_origins  # a rebound verdict and its origin: one
    ]
    extra.extend(br_failed_ok)
    per_reviewer: dict[str, int] = {}  # one decision per reviewer_id and change hash (owner)
    for decision_id in (*accepts, *rejects, *extra):
        decided = by_id.get(decision_id)
        who = decided.payload.reviewer_id if decided is not None else decision_id
        per_reviewer[who] = per_reviewer.get(who, 0) + 1
    out_ignored = listed()
    if any(count > 1 for count in per_reviewer.values()):
        return VerdictOutcome(
            "failure",
            "verdict_conflict",
            (),
            out_ignored,
            checked,
            tuple(asked),
            "decision_duplicate",
        )
    if accepts and rejects:
        return VerdictOutcome(
            "failure",
            "verdict_conflict",
            (),
            out_ignored,
            checked,
            tuple(asked),
            "accept_and_reject",
        )
    if rejects:
        return VerdictOutcome("failure", "verdict_rejected", (), out_ignored, checked, tuple(asked))
    # GC-15 b: a failed REJECT cannot be outvoted; order: conflict, rejected, failed REJECT,
    # a met quorum, failed non-REJECT, missing
    failed_rejects = [f for f in failures if f[2]]
    if failed_rejects:
        newest_reject = max(failed_rejects, key=lambda item: item[0])
        return VerdictOutcome("failure", newest_reject[1], (), out_ignored, checked, tuple(asked))
    failed_others = [f for f in failures if not f[2]]
    if accepts and (len(accepts) >= ai_quorum or not failed_others):
        return VerdictOutcome("success", OK, tuple(accepts), out_ignored, checked, tuple(asked))
    if failed_others:
        newest = max(failed_others, key=lambda item: item[0])
        return VerdictOutcome(
            "failure", newest[1], (), out_ignored, checked, tuple(asked), newest[3]
        )
    return VerdictOutcome("pending", "verdict_missing", (), out_ignored, checked, tuple(asked))


# --- 7.1: the evaluation order as data (F-18: trust roots at step 3b) -----------------------------


@dataclass(frozen=True)
class Step:
    id: str
    name: str
    pending: tuple[str, ...]
    failure: tuple[str, ...]


EVALUATION_ORDER: tuple[Step, ...] = (
    Step("1", "flag", ("not_enforced",), ()),
    Step(
        "2",
        "eligibility",
        ("pr_draft",),
        ("pr_not_eligible", "fork_head", "hold_label", "head_shared_unverified"),
    ),
    Step(
        "3",
        "bundle",
        ("bundle_missing",),
        ("bundle_invalid", "bundle_wrong_head", "diff_mismatch", "tier_mismatch"),
    ),
    Step("3b", "trust_roots", (), ("trust_root_needs_admin",)),
    Step("4", "required_checks", ("ci_missing", "ci_pending"), ("ci_failed", "ci_wrong_producer")),
    Step("5", "full_tier", ("full_ci_missing",), ("full_ci_failed", "full_ci_unbound")),
    Step("6", "verdict", ("verdict_missing",), VERDICT_FAILURE_CODES),
    Step("7", "quorum", ("quorum_unmet", "codeowner_uncovered"), ()),
    Step("9", "fresh_base", ("behind_main",), ()),
    Step("10", "ledger", (), ("ledger_unavailable",)),
)
"""7.1 with F-18: step 8 (trust roots) moved to 3b so the status says why the PR cannot
auto-merge instead of `pending quorum_unmet`; 9 and 10 keep the spec's numbers."""

STEP_IDS: tuple[str, ...] = tuple(step.id for step in EVALUATION_ORDER)
GATE_ERROR = "gate_error"
SUCCESS_REASONS = ("ok", "tier_not_enforced")
QUEUE_CODES = {
    "failure": ("group_member_unverified", "group_head_mismatch", "group_ci_failed"),
    "pending": ("group_ci_missing", "group_ci_pending"),
}


@dataclass(frozen=True)
class StepResult:
    step: str
    """A step id of `STEP_IDS`, or `any` for an exception (`gate_error`)."""
    state: State
    reason: str


def resolve_order(results: Iterable[StepResult]) -> tuple[State, str]:
    """GT-3: the first step (in 7.1 order) that did not pass decides state and reason; a
    pending result stops the evaluation like a failure; an exception is `failure gate_error`
    whatever else was found. A reason that does not belong to its step and state is refused."""
    by_step = {step.id: step for step in EVALUATION_ORDER}
    ordered: list[tuple[int, StepResult]] = []
    for result in results:
        if result.state not in ("success", "failure", "pending"):
            raise ValueError(f"unknown state {result.state!r}")
        if result.step == "any":
            if result.state != "failure" or result.reason != GATE_ERROR:
                raise ValueError("step 'any' carries only failure gate_error")
            return "failure", GATE_ERROR
        step = by_step.get(result.step)
        if step is None:
            raise ValueError(f"unknown step {result.step!r}")
        if result.state == "success":
            if result.reason != OK:
                raise ValueError("a passing step's reason is ok")
            continue
        allowed = step.pending if result.state == "pending" else step.failure
        if result.reason not in allowed:
            raise ValueError(
                f"reason {result.reason!r} is not a {result.state} code of step {step.id}"
            )
        ordered.append((STEP_IDS.index(step.id), result))
    if not ordered:
        return "success", OK
    first = min(ordered, key=lambda item: item[0])[1]
    return first.state, first.reason
