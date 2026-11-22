"""Verified Merge signer core (spec `spec/verified-merge` SV-1 to SV-11, SV-14, 7.2, LA-1, LA-5;
Fable fixes F-6 requester, F-7 holder, F-8 actor table, F-12 compromised keys).

The signer is the only principal that touches the signing key: it holds the `SignerBackend` port
(`sign(domain, message)`, `public_key_hex()`, `key_id`; SV-6) and nothing else in the process
needs it. The reviewer step is keyless and its output is parsed **as data** (`parse_proposal`,
SV-3): line 1 must be exactly `VERDICT: ACCEPT|REJECT PR=<n> HEAD=<sha>` for the requested values
and no other line may start with `VERDICT:`; anything else is `malformed_proposal`, not a verdict.

A verdict binds the head AND the change hash **the signer computed itself** (`ReviewFacts`, F-2):
nothing from the request but repo, PR number, head and requester is used, and the head is re-read
after the review (`head_moved`). One decision per (repo, PR, change hash) is kept (SV-8 c) with
REJECT memory across PRs (`reject_memory_days`) and resets by ledger order (SV-10, F-12 c). The
ledger entry `merge.verdict_issued` is appended **before** the decision is stored (LA-1), through
the F-8 actor table (`check_merge_actor`), so a verdict without a ledger entry can never exist in
the signer's own store (LA-5). Rebinding (SV-8 d) issues a fresh verdict without a model call iff
every condition holds, with the mechanical-rebase test supplied as a callable the signer owns
(never a flag from the gate's request).

Failure is closed and loud: a `Refusal` is a value with a 7.2 code (and `merge.review_failed` is
written, except for `decision_exists`); anything the signer cannot do safely (the ledger down,
the backend failing, the key not usable under the current trust list, a trust-list rollback) is
a `SignerFailure` with a code and nothing stored. No error carries key material, a path, a hash
or a line of input: codes only.

The two-step split (reviewer step keyless, signer step with the key) is `core.vm_review`, which
drives `screen`, `admit`, `record_cost` and `issue` in order; the SV-10 limits are evaluated
over the ledger by `core.vm_limits` behind the `Limits` port. What stays behind ports for the
engine (Sonnet's part of VM-2): the decision store and ledger adapters, the diff fetch and size
checks (SV-3), the budget check and the model call (SV-2).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from nuramesh.core.ledger import EventType
from nuramesh.core.vm_events import GATE_ACTOR, SIGNER_ACTOR, check_merge_actor
from nuramesh.core.vm_formats import (
    VERDICT_DOMAIN,
    VERDICT_VERSION,
    KeyIdCollision,
    SignedVerdict,
    TrustKey,
    TrustList,
    VerdictPayload,
    canonical_bytes,
    format_time,
    parse_time,
    payload_hash,
    public_key_id,
    refuse_key_id_collisions,
)
from nuramesh.core.vm_trust import accept_trust, signer_key_status
from nuramesh.core.vm_verdicts import ResetRecord

REFUSAL_CODES: tuple[str, ...] = (
    "malformed_proposal",
    "head_moved",
    "decision_exists",
    "diff_too_large",
    "diff_binary",
    "diff_header_mismatch",
    "rate_limited",
    "reject_limit",
    "task_ask_limit",
    "run_in_flight",
    "org_mismatch",
    "model_failed",
    "budget_exceeded",
    "rebind_refused",
    "requester_forbidden",  # F-6
)
"""7.2 plus F-6. None is a verdict; each writes `merge.review_failed` except `decision_exists`."""
REBIND_DETAILS: tuple[str, ...] = (
    "tree_mismatch",
    "change_hash",
    "policy",
    "rejected",
    "expired",
    "revoked",
    "voided",
    "reviewer",  # GC-93 c
)
HEAD_MOVED_DETAILS: tuple[str, ...] = ("before_review", "during_review")
"""`head_moved` says when: before any review started (the facts were gathered for another head)
or during the review (the re-read after the reviewer ran). `core.vm_limits` ends a run on the
second only."""
LIMIT_CODES = frozenset({"rate_limited", "reject_limit", "task_ask_limit", "run_in_flight"})
FAILURE_CODES = (
    "ledger_unavailable",
    "sign_failed",
    "actor_forbidden",
    "trust_rollback",
    "trust_org",
    "key_id_collision",
    "key_id_mismatch",
    "limits_unavailable",
    "side_unavailable",
    "task_key_missing",
)
DEFAULT_VERDICT_TTL_SECONDS = 604800
DEFAULT_REJECT_MEMORY_DAYS = 30

_PROPOSAL_LINE = re.compile(r"^VERDICT: (ACCEPT|REJECT) PR=([1-9][0-9]*) HEAD=([0-9a-f]{40})$")


# --- ports ----------------------------------------------------------------------------------------


class SignerBackend(Protocol):
    """SV-6: the key behind a port. `sign` returns the 64 raw signature bytes over
    `domain + message`; the private half is never returned by anything."""

    @property
    def key_id(self) -> str: ...

    def public_key_hex(self) -> str: ...

    def sign(self, domain: bytes, message: bytes) -> bytes: ...


@dataclass(frozen=True)
class LedgerRef:
    """Where an appended entry landed: its sequence number and the ledger's time."""

    seq: int
    at: datetime


class Ledger(Protocol):
    """Append to the org stream (LA-2); raises on any failure."""

    def append(self, event_type: str, actor: str, payload: Mapping[str, Any]) -> LedgerRef: ...


@dataclass(frozen=True)
class StoredDecision:
    """A verdict the signer issued, with where its `merge.verdict_issued` entry landed (LA-5)
    and the task key it was issued under (SV-10: the linked issue, else the PR author's id)."""

    verdict: SignedVerdict
    task_key: str
    ledger_seq: int
    ledger_at: datetime

    @property
    def payload(self) -> VerdictPayload:
        return self.verdict.payload

    @property
    def id(self) -> str:
        return self.verdict.payload.verdict_id

    @property
    def org_id(self) -> str:
        return self.verdict.payload.org_id

    @property
    def repo(self) -> str:
        return self.verdict.payload.repo

    @property
    def pr(self) -> int:
        return self.verdict.payload.pr

    @property
    def change_hash(self) -> str:
        return self.verdict.payload.change_hash

    @property
    def decision(self) -> str:
        return self.verdict.payload.decision


class DecisionStore(Protocol):
    """The signer's store (not the source of truth, LA-5): the current decision per
    (org, repo, PR, change hash, reviewer) (SV-8 c, GC-7 e, GC-93), every record by id (chain
    origins), all records of a change hash in a repository (reject memory)."""

    def get(
        self, org_id: str, repo: str, pr: int, change_hash: str, reviewer_id: str
    ) -> StoredDecision | None: ...

    def by_id(self, verdict_id: str) -> StoredDecision | None: ...

    def for_change(self, org_id: str, repo: str, change_hash: str) -> Sequence[StoredDecision]: ...

    def put(self, decision: StoredDecision) -> None: ...


class Limits(Protocol):
    """SV-10 limits: `None` to proceed, else one of `LIMIT_CODES`."""

    def check_run(self, request: ReviewRequest, task_key: str) -> str | None: ...

    def check_rebind(self, request: ReviewRequest) -> str | None: ...


Clock = Callable[[], datetime]
NewId = Callable[[], str]
Mechanical = Callable[[StoredDecision, "ReviewFacts"], bool]
"""SV-8 d: whether the live head is a mechanical rebase of the stored decision's head, computed
by the signer's own pinned `mt-1` run (engine) from the origin's bundle and the live repository."""


# --- values ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRequest:
    """What a caller may say (SV-3: only repo, PR and head) plus who asks (F-6)."""

    org_id: str
    repo: str
    repo_id: str
    pr: int
    head_sha: str
    requester: str


@dataclass(frozen=True)
class ReviewFacts:
    """What the signer established itself for the requested head: the base, the change hash of
    the diff it rendered (F-2), the bundle it read, the tier, the policy hash, the author's
    enrolled id (F-6), the task key (SV-10) and the model it ran."""

    head_sha: str
    base_ref: str
    base_sha: str
    change_hash: str
    bundle_hash: str
    tier: int
    policy_sha256: str
    author_id: str | None
    task_key: str | None
    model_id: str
    model_family: str | None


@dataclass(frozen=True)
class Refusal:
    """A 7.2 refusal: not a verdict, nothing signed, nothing stored."""

    code: str
    detail: str | None = None


@dataclass(frozen=True)
class Issued:
    """A decision the caller gets: freshly issued (`new`) or the stored one (`decision_exists`)."""

    decision: StoredDecision
    new: bool


class SignerFailure(Exception):
    """The signer cannot continue safely; `code` is one of `FAILURE_CODES` or a key status code.
    The message is the code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"signer: {code}")


class SignerKeyUnusable(SignerFailure):
    """The signer's key may not sign under the current trust list (F-7, F-12, SV-7)."""


# --- proposal parsing (SV-3) ----------------------------------------------------------------------


def parse_proposal(text: str, pr: int, head_sha: str) -> str | None:
    """`ACCEPT` or `REJECT` when line 1 is exactly the verdict line for `pr` and `head_sha` and
    no other line starts with `VERDICT:`; else None (`malformed_proposal`, SV-11). The text is
    data: nothing in it is interpreted beyond this shape."""
    lines = text.split("\n")
    first = lines[0][:-1] if lines[0].endswith("\r") else lines[0]
    match = _PROPOSAL_LINE.fullmatch(first)
    if match is None:
        return None
    if int(match.group(2)) != pr or match.group(3) != head_sha:
        return None
    if any(line.startswith("VERDICT:") for line in lines[1:]):
        return None
    return match.group(1)


# --- the signer -----------------------------------------------------------------------------------


def task_key_of(facts: ReviewFacts) -> str:
    """SV-10: the task a request is counted under is the issue linked at the base; with no
    linked issue it is the PR author's id. A request with neither cannot be counted, so it is
    never reviewed: `task_key_missing` (loud, nothing requested, nothing signed)."""
    key = facts.task_key or facts.author_id
    if not key:
        raise SignerFailure("task_key_missing")
    return key


def _signer_key_of(key: TrustKey, reviewer_id: str) -> bool:
    """The gate's two origin-key tests (`vm_verdicts.judge_verdict`), mirrored so the signer never
    signs a rebind the gate would refuse: role exactly `verdict-signer` (the gate's
    `verdict_key_untrusted`) and holder exactly `service:<the record's reviewer id>` (F-7, the
    gate's `verdict_wrong_binding`). Fail closed: an entry without either field, or with one
    that is not text, is not a signer key. Read before `problem()`, which needs both fields."""
    role = getattr(key, "role", None)
    holder = getattr(key, "holder", None)
    if not isinstance(role, str) or not isinstance(holder, str):
        return False
    return role == "verdict-signer" and holder == "service:" + reviewer_id


def _voided(decision: StoredDecision, resets: Sequence[ResetRecord]) -> bool:
    """SV-10 by ledger order: a reset with a higher sequence voids the records of its scope.

    Reset scope: `ResetRecord` carries no repository, so a `task`
    or `change_hash` reset voids its value in every repository of the org, and `mode_flip`
    voids everything; the caller passes one org's resets only. Org-wide voiding is accepted
    for now (it only ever voids more, never less); a per-repository scope is VM-3's."""
    for reset in resets:
        if reset.seq <= decision.ledger_seq:
            continue
        if reset.kind == "mode_flip":
            return True
        if reset.kind == "change_hash" and reset.value == decision.change_hash:
            return True
        if (
            reset.kind == "task"
            and decision.task_key is not None
            and reset.value == decision.task_key
        ):
            return True
    return False


class Signer:
    """One signer for one org and one reviewer id (F-7: one key per reviewer id)."""

    def __init__(
        self,
        *,
        backend: SignerBackend,
        reviewer_id: str,
        org_id: str,
        trust: TrustList,
        ledger: Ledger,
        store: DecisionStore,
        limits: Limits,
        clock: Clock,
        new_id: NewId | None = None,
        reserved_key_ids: Mapping[str, str] | None = None,
        verdict_ttl_seconds: int = DEFAULT_VERDICT_TTL_SECONDS,
        reject_memory_days: int = DEFAULT_REJECT_MEMORY_DAYS,
    ) -> None:
        self._backend = backend
        self._reviewer_id = reviewer_id
        self._org_id = org_id
        self._ledger = ledger
        self._store = store
        self._limits = limits
        self._clock = clock
        self._new_id = new_id or (lambda: secrets.token_hex(16))
        self._reserved = dict(reserved_key_ids or {})
        self._ttl = timedelta(seconds=verdict_ttl_seconds)
        self._reject_memory = timedelta(days=reject_memory_days)
        self._trust: TrustList | None = None
        self.load_trust(trust)
        self._require_key_usable(self._clock())

    def __repr__(self) -> str:
        return "Signer(key hidden)"

    __str__ = __repr__

    @property
    def trust(self) -> TrustList:
        assert self._trust is not None
        return self._trust

    @property
    def key_id(self) -> str:
        return self._backend.key_id

    @property
    def org_id(self) -> str:
        return self._org_id

    @property
    def reviewer_id(self) -> str:
        return self._reviewer_id

    def load_trust(self, trust: TrustList) -> None:
        """Take a newer trust list (by serial, never a rollback: SV-7, 6.7); the signer's key
        status is read from it at every signing."""
        if trust.org_id != self._org_id:
            raise SignerFailure("trust_org")
        problem = accept_trust(self._trust, trust)
        if problem is not None:
            raise SignerFailure(problem)
        self._trust = trust

    def replace_backend(self, backend: SignerBackend) -> None:
        """After a rotation: the new key must be usable under the loaded trust list."""
        previous = self._backend
        self._backend = backend
        try:
            self._require_key_usable(self._clock())
        except SignerFailure:
            self._backend = previous
            raise

    def _require_key_usable(self, now: datetime) -> None:
        """The key the backend holds is the one the trust list names for this reviewer, usable
        at `now`, with a key id in no other key space (anchor, licence, any the caller names);
        else loud."""
        try:
            public = self._backend.public_key_hex()
            key_id = self._backend.key_id
        except Exception as exc:
            raise SignerFailure("sign_failed") from exc
        if public_key_id(public) != key_id:
            raise SignerKeyUnusable("key_id_mismatch")
        if key_id in self._reserved:
            raise SignerKeyUnusable("key_id_collision")
        try:
            refuse_key_id_collisions(self.trust, self._reserved)
        except KeyIdCollision as exc:
            raise SignerKeyUnusable("key_id_collision") from exc
        status = signer_key_status(self.trust, key_id, self._reviewer_id, now)
        if status is not None:
            raise SignerKeyUnusable(status)
        key = self.trust.key(key_id)
        if key is None or key.public_key != public:
            raise SignerKeyUnusable("key_not_in_trust_list")

    # --- ledger -----------------------------------------------------------------------------------

    def _append(self, event_type: EventType, payload: Mapping[str, Any]) -> LedgerRef:
        problem = check_merge_actor(event_type.value, SIGNER_ACTOR)
        if problem is not None:
            raise SignerFailure("actor_forbidden")
        try:
            return self._ledger.append(event_type.value, SIGNER_ACTOR, payload)
        except Exception as exc:
            raise SignerFailure("ledger_unavailable") from exc

    def _base_payload(self, request: ReviewRequest) -> dict[str, Any]:
        return {
            "org_id": request.org_id,
            "repo": request.repo,
            "repo_id": request.repo_id,
            "pr": request.pr,
            "head_sha": request.head_sha,
            "reviewer_id": self._reviewer_id,
        }

    def refuse(self, request: ReviewRequest, code: str, detail: str | None = None) -> Refusal:
        """Record a refusal (`merge.review_failed`, SV-11) and return it. `decision_exists` is
        not a failure and is never recorded here; an unknown code is a programming error."""
        if code not in REFUSAL_CODES or code == "decision_exists":
            raise ValueError("not a signer refusal code")
        if code == "rebind_refused" and detail not in REBIND_DETAILS:
            raise ValueError("rebind_refused needs one of its details")
        if code == "head_moved" and detail is not None and detail not in HEAD_MOVED_DETAILS:
            raise ValueError("head_moved takes one of its details or none")
        self._append(
            EventType.MERGE_REVIEW_FAILED,
            {**self._base_payload(request), "reason": code, "detail": detail},
        )
        return Refusal(code, detail)

    def record_cost(
        self,
        request: ReviewRequest,
        *,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
        price_version: str | None,
        cost: Decimal | None,
    ) -> LedgerRef:
        """SV-2 / S-12: `merge.review_cost` with the cost as a decimal string, or `unknown` when
        the price is unknown (never guessed; never a float)."""
        if cost is not None and not isinstance(cost, Decimal):
            raise TypeError("cost is a Decimal or None")
        return self._append(
            EventType.MERGE_REVIEW_COST,
            {
                **self._base_payload(request),
                "model_id": model_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "price_version": price_version,
                "cost": "unknown" if cost is None else format(cost, "f"),
            },
        )

    # --- the review path --------------------------------------------------------------------------

    def screen(self, request: ReviewRequest) -> Refusal | None:
        """S-07, before anything is fetched for the request: this signer serves one org."""
        if request.org_id != self._org_id:
            return self.refuse(request, "org_mismatch")
        return None

    def _limit(self, check: Callable[[], str | None], request: ReviewRequest) -> Refusal | None:
        """Ask the limits port. Its failure is loud (`limits_unavailable`): nothing is requested
        and nothing is signed on a limit the signer could not evaluate."""
        try:
            limit = check()
        except Exception as exc:
            raise SignerFailure("limits_unavailable") from exc
        if limit is None:
            return None
        if limit not in LIMIT_CODES:
            raise SignerFailure("limits_unavailable")
        return self.refuse(request, limit)

    def _existing(
        self, request: ReviewRequest, facts: ReviewFacts, resets: Sequence[ResetRecord]
    ) -> StoredDecision | None:
        """SV-8 c: the stored decision for (repo, PR, change hash), or a remembered REJECT of the
        same change hash from any PR of the repository within `reject_memory_days`; voided
        records (SV-10) do not count."""
        now = self._clock()
        for other in self._store.for_change(request.org_id, request.repo, facts.change_hash):
            if other.decision != "REJECT" or _voided(other, resets):
                continue
            # GC-93 b: a REJECT of any reviewer answers every later request (same PR or another)
            if other.pr == request.pr or now - other.ledger_at <= self._reject_memory:
                return other
        current = self._store.get(
            request.org_id, request.repo, request.pr, facts.change_hash, self._reviewer_id
        )
        if current is not None and not _voided(current, resets):
            return current
        return None

    def admit(
        self, request: ReviewRequest, facts: ReviewFacts, resets: Sequence[ResetRecord]
    ) -> Refusal | Issued | None:
        """Before any budget or model call (the facts are the signer's own, already gathered):
        org (S-07), requester (F-6), the head the facts were gathered for, the stored decision
        (`decision_exists`, returned as `Issued`), then the limits port. None means proceed;
        `merge.review_requested` is then written."""
        if request.org_id != self._org_id:
            return self.refuse(request, "org_mismatch")
        allowed = {GATE_ACTOR}
        if facts.author_id is not None and not facts.author_id.startswith("unmapped:"):
            allowed.add(facts.author_id)
        if request.requester not in allowed:
            return self.refuse(request, "requester_forbidden")
        if facts.head_sha != request.head_sha:
            return self.refuse(request, "head_moved", "before_review")
        existing = self._existing(request, facts, resets)
        if existing is not None:
            return Issued(existing, new=False)
        task_key = task_key_of(facts)
        limited = self._limit(lambda: self._limits.check_run(request, task_key), request)
        if limited is not None:
            return limited
        self._append(
            EventType.MERGE_REVIEW_REQUESTED,
            {
                **self._base_payload(request),
                "requester": request.requester,
                "task_key": task_key,
                "change_hash": facts.change_hash,
            },
        )
        return None

    def issue(
        self,
        request: ReviewRequest,
        facts: ReviewFacts,
        *,
        proposal_text: str,
        verdict_text_sha256: str,
        live_head: str,
        resets: Sequence[ResetRecord] = (),
    ) -> Issued | Refusal:
        """After the reviewer ran: parse the proposal as data (S-01, S-02), refuse a moved head
        (S-04), then sign, ledger and store one verdict bound to `facts`. A decision stored
        meanwhile (and not voided) is returned instead (SV-8 c)."""
        decision = parse_proposal(proposal_text, request.pr, request.head_sha)
        if decision is None:
            return self.refuse(request, "malformed_proposal")
        if live_head != request.head_sha or facts.head_sha != request.head_sha:
            return self.refuse(request, "head_moved", "during_review")
        existing = self._existing(request, facts, resets)
        if existing is not None:
            return Issued(existing, new=False)
        now = self._clock()
        return self._sign_and_record(
            request,
            facts,
            decision=decision,
            verdict_text_sha256=verdict_text_sha256,
            issued_at=now,
            expires_at=now + self._ttl,
            rebound_from=None,
        )

    def rebind(
        self,
        request: ReviewRequest,
        facts: ReviewFacts,
        resets: Sequence[ResetRecord],
        mechanical: Mechanical,
    ) -> Issued | Refusal:
        """SV-8 d: a fresh ACCEPT for the live head without a model call, iff the stored ACCEPT
        for (repo, PR, change hash) is valid, unrevoked at ledger order, not voided, has no
        REJECT beside it, matches tier and policy, and the live head is a mechanical rebase of
        it (computed by `mechanical`, the signer's own run). Else `rebind_refused` with its
        detail (7.2). A REJECT is never rebound."""
        if request.org_id != self._org_id:
            return self.refuse(request, "org_mismatch")
        limited = self._limit(lambda: self._limits.check_rebind(request), request)
        if limited is not None:
            return limited
        if facts.head_sha != request.head_sha:
            return self.refuse(request, "head_moved", "before_review")
        stored = self._store.get(
            request.org_id, request.repo, request.pr, facts.change_hash, self._reviewer_id
        )
        if stored is None:
            # GC-93 c: a signer rebinds only its own reviewer's ACCEPT, never another's
            foreign = any(
                other.pr == request.pr and other.decision == "ACCEPT"
                for other in self._store.for_change(request.org_id, request.repo, facts.change_hash)
            )
            return self.refuse(request, "rebind_refused", "reviewer" if foreign else "change_hash")
        if stored.decision != "REJECT":
            for other in self._store.for_change(request.org_id, request.repo, facts.change_hash):
                if other.decision == "REJECT" and not _voided(other, resets):
                    return self.refuse(request, "rebind_refused", "rejected")
        now = self._clock()
        problem = self._chain_problem(stored, facts, resets, now)
        if problem is not None:
            return self.refuse(request, "rebind_refused", problem)
        if not mechanical(stored, facts):
            return self.refuse(request, "rebind_refused", "tree_mismatch")
        return self._sign_and_record(
            request,
            facts,
            decision="ACCEPT",
            verdict_text_sha256=stored.payload.verdict_text_sha256,
            issued_at=now,
            expires_at=parse_time(stored.payload.expires_at),
            rebound_from=stored.id,
            task_key=stored.task_key,
            model_id=stored.payload.model_id,
            model_family=stored.payload.model_family,
        )

    def _chain_problem(
        self,
        stored: StoredDecision,
        facts: ReviewFacts,
        resets: Sequence[ResetRecord],
        now: datetime,
    ) -> str | None:
        """The rebind conditions over the stored decision and its whole `rebound_from` chain
        (SV-7, SV-8 d), as a `REBIND_DETAILS` code or None. A chain key that is unknown, invalid,
        not a `verdict-signer` key, not held by the record's own reviewer (F-7), compromised,
        revoked by ledger order or outside its validity window at the record's own ledger time
        (GC-10, as the gate) is `revoked`:
        7.2 has no narrower detail for a key the gate would not trust."""
        if stored.decision != "ACCEPT":
            return "rejected"
        p = stored.payload
        if p.change_hash != facts.change_hash or p.tier != facts.tier:
            return "change_hash"
        if p.policy_sha256 != facts.policy_sha256:
            return "policy"
        if now >= parse_time(p.expires_at):
            return "expired"
        seen: set[str] = set()
        record: StoredDecision | None = stored
        while record is not None:
            if record.id in seen or len(seen) > 64:
                return "revoked"
            seen.add(record.id)
            if record.decision != "ACCEPT":
                return "rejected"
            if record.payload.reviewer_id != self._reviewer_id:
                return "reviewer"  # GC-93 c: the chain never changes reviewer
            if _voided(record, resets):
                return "voided"
            key = self.trust.key(record.payload.key_id)
            if key is None or not _signer_key_of(key, record.payload.reviewer_id):
                return "revoked"
            if key.problem() is not None or key.compromised:
                return "revoked"
            if key.revoked_seq is not None and record.ledger_seq > key.revoked_seq:
                return "revoked"
            # GC-10 (answer c): each chain key's window is judged at that
            # record's own ledger time, never at `now`, exactly as the gate's
            # `vm_verdicts.judge_verdict` (no skew, `valid_to` exclusive), so a rotation never
            # voids a verdict ledgered inside its key's window; `now` is tested by `expires_at`
            window_at = record.ledger_at
            if parse_time(key.valid_from) > window_at or (
                key.valid_to is not None and window_at >= parse_time(key.valid_to)
            ):
                return "revoked"
            origin = record.payload.rebound_from
            if origin is None:
                break
            record = self._store.by_id(origin)
            if record is None:
                return "revoked"
        return None

    def _sign_and_record(
        self,
        request: ReviewRequest,
        facts: ReviewFacts,
        *,
        decision: str,
        verdict_text_sha256: str,
        issued_at: datetime,
        expires_at: datetime,
        rebound_from: str | None,
        task_key: str | None = None,
        model_id: str | None = None,
        model_family: str | None = None,
    ) -> Issued:
        self._require_key_usable(issued_at)
        # a rebind stays under its origin's task key; a review is counted under its own (SV-10)
        counted_under = task_key if rebound_from is not None and task_key else task_key_of(facts)
        payload = VerdictPayload.model_validate(
            {
                "version": VERDICT_VERSION,
                "org_id": request.org_id,
                "repo": request.repo,
                "repo_id": request.repo_id,
                "pr": request.pr,
                "head_sha": facts.head_sha,
                "base_ref": facts.base_ref,
                "base_sha": facts.base_sha,
                "change_hash": facts.change_hash,
                "bundle_hash": facts.bundle_hash,
                "tier": facts.tier,
                "policy_sha256": facts.policy_sha256,
                "decision": decision,
                "reviewer_id": self._reviewer_id,
                "model_id": model_id or facts.model_id,
                "model_family": facts.model_family if model_id is None else model_family,
                "verdict_text_sha256": verdict_text_sha256,
                "verdict_id": self._new_id(),
                "rebound_from": rebound_from,
                "key_id": self._backend.key_id,
                "issued_at": format_time(issued_at),
                "expires_at": format_time(expires_at),
            }
        )
        try:
            signature = self._backend.sign(VERDICT_DOMAIN, canonical_bytes(payload))
        except Exception as exc:
            raise SignerFailure("sign_failed") from exc
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise SignerFailure("sign_failed")
        verdict = SignedVerdict(payload=payload, sig=signature.hex())
        ref = self._append(
            EventType.MERGE_VERDICT_ISSUED,
            {
                **self._base_payload(request),
                "change_hash": payload.change_hash,
                "task_key": counted_under,
                "verdict_id": payload.verdict_id,
                "payload_sha256": payload_hash(payload),
                "key_id": payload.key_id,
                "decision": payload.decision,
                "rebound_from": payload.rebound_from,
                "issued_at": payload.issued_at,
                "expires_at": payload.expires_at,
            },
        )
        stored = StoredDecision(
            verdict=verdict,
            task_key=counted_under,
            ledger_seq=ref.seq,
            ledger_at=ref.at,
        )
        self._store.put(stored)
        return Issued(stored, new=True)
