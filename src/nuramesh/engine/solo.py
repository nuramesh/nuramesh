"""Solo approval inside the decision transaction (#942 PR 1).

The **solo condition** is `D(org) == {approver}`: the approver is the organization's only active
person whose role holds `decide`, read inside the transaction that records the decision. It does
not depend on who started the run. While it holds, a decision sent through the server is a solo
decision, and an **approve** must meet the terms of SA-12 step 7, in this order: the
acknowledgement (7a), the action digest present (7b) and equal (7c), a fresh proof for a high-risk
kind (7d) and the rate limit (7e). A reject is free (SA-4).

The terms are a keyword that defaults to "never solo" (SA-2): only the server route passes
`SoloTerms`, and only for a person in a cookie session. Every other caller (the CLI, in-run
`--approve-as`, the `dev_human` harness, tests) keeps the strict rule: the originator's answer is
`SelfApproval` even in a solo organization, and the mode they write is `peer` or `no_originator`.

Every value is read through the unit of work; every write transaction takes the store's one global
write lock, so the read of `D(org)`, the count of SA-9 and the decision are serialized with every
other write (SA-9 invariant, SA-13). A refusal here writes nothing.
"""

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from nuramesh.core.auth import ROLE_PERMISSIONS, ApiToken, AuthSession, Permission, User
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.mfa import MfaFactor
from nuramesh.core.policy import Approval, ApprovalMode, ApprovalStatus
from nuramesh.core.solo import (
    HIGH_RISK_KINDS,
    PROOF_TEXTS,
    RATE_WINDOW_SECONDS,
    SOLO_LIMIT_MAX,
    ProofFacts,
    ProofRefused,
    SoloProof,
    accepted_proofs,
    action_digest,
    fresh_proof,
    solo_retry_after,
)
from nuramesh.stores.base import UnitOfWork, run_stream

__all__ = [
    "SoloReads",
    "SoloRefused",
    "SoloTerms",
    "Standing",
    "caller_terms",
    "check_solo_approve",
    "decide_holders",
    "mode_without_solo",
    "proof_facts",
    "standing_of",
]


class SoloReads(Protocol):
    """The reads the solo rules need. A unit of work has them (a decision reads inside its own
    transaction); the approval view passes a read-only adapter over the store."""

    def list_users(self, org_id: str) -> list[User]: ...

    def get_user(self, org_id: str, user_id: str) -> User | None: ...

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None: ...

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None: ...

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None: ...

    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]: ...


REFUSAL_TEXTS = {
    "acknowledge_required": (
        "you are the only person who can approve this: acknowledge the solo approval "
        "and send the action digest you were shown"
    ),
    "action_changed": "the action is not the one you were shown; read the approval again",
    "solo_rate_limited": "too many solo approvals in the last hour; try again later",
    **PROOF_TEXTS,
}
"""The fixed detail of each solo refusal (table 3.3). Never a digest, a time or a session id."""


class SoloRefused(Exception):
    """A solo approve that does not meet SA-12 step 7. `code` is the error code of table 3.3;
    `retry_after` is set only for `solo_rate_limited`. The message is fixed text."""

    def __init__(self, code: str, retry_after: int | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(REFUSAL_TEXTS[code])


@dataclass(frozen=True)
class SoloTerms:
    """What the server route passes for a person in a cookie session: the session the request
    came in on, the two body fields of an approve, the deployment facts SA-7 reads and the
    configured limit. Passing it never loosens anything: it is the only way to the solo path,
    and the solo path asks for more than any other."""

    session_id: str
    hosted: bool
    mfa_available: bool
    limit_per_hour: int = SOLO_LIMIT_MAX
    acknowledge: bool = False
    action_digest: str | None = None

    def __post_init__(self) -> None:
        if type(self.limit_per_hour) is not int or not 1 <= self.limit_per_hour <= SOLO_LIMIT_MAX:
            raise ValueError(f"the solo limit is an integer from 1 to {SOLO_LIMIT_MAX}")


@dataclass(frozen=True)
class Standing:
    """Where the approver stands, read in the transaction: whether they are an active
    decide-holder and whether anyone else is."""

    holder: bool
    others: bool

    @property
    def solo(self) -> bool:
        """The solo condition: `D(org) == {approver}`."""
        return self.holder and not self.others


def decide_holders(uow: SoloReads, org_id: str) -> frozenset[str]:
    """`D(org)`: the active persons (not disabled, not erased; locked, unverified or signed out
    still counts) of the organization whose role holds `decide`. A pending invite is no person."""
    return frozenset(
        user.id
        for user in uow.list_users(org_id)
        if user.erased_at is None
        and user.disabled_at is None
        and Permission.DECIDE in ROLE_PERMISSIONS.get(user.role, frozenset())
    )


def standing_of(uow: SoloReads, org_id: str, approver: str) -> Standing:
    """The approver's standing in the run's organization."""
    holders = decide_holders(uow, org_id)
    return Standing(holder=approver in holders, others=bool(holders - {approver}))


def mode_without_solo(originator: str | None) -> ApprovalMode:
    """SA-14 for a decision that is not solo: `peer` when a person started the run (the approver
    is then another person), else `no_originator`."""
    return ApprovalMode.PEER if originator is not None else ApprovalMode.NO_ORIGINATOR


def proof_facts(uow: SoloReads, org_id: str, approver: str, terms: SoloTerms) -> ProofFacts:
    """What SA-7 reads. A session that is missing, ended or not the approver's gives no time at
    all, so no proof can come from it (fail closed)."""
    factor = uow.get_mfa_factor(org_id, approver)
    session = uow.get_auth_session(org_id, terms.session_id)
    if session is not None and (session.user_id != approver or session.revoked_at is not None):
        session = None
    return ProofFacts(
        hosted=terms.hosted,
        enrolled=factor is not None and factor.active,
        mfa_available=terms.mfa_available,
        mfa_verified_at=None if session is None else session.mfa_verified_at,
        password_confirmed_at=None if session is None else session.password_confirmed_at,
        signed_in_at=None if session is None else session.created_at,
        login_via=None if session is None else session.login_via,
    )


def _run_org(uow: UnitOfWork, run_id: str) -> str | None:
    first = uow.events_after(run_stream(run_id), 0, 1)
    if not first or first[0].type != EventType.RUN_STARTED.value:
        return None
    org = first[0].payload.get("org")
    return org if isinstance(org, str) else None


def counted_solo_approves(
    uow: UnitOfWork, org_id: str, approver: str, now: datetime
) -> list[datetime]:
    """The decision times SA-9 counts: this person's solo approves (consumed included) of the
    organization's runs with `now - decided_at < 3600 s`."""
    rows = uow.solo_approvals_after(approver, now - timedelta(seconds=RATE_WINDOW_SECONDS))
    orgs: dict[str, str | None] = {}
    times: list[datetime] = []
    for row in rows:
        if row.run_id not in orgs:
            orgs[row.run_id] = _run_org(uow, row.run_id)
        if orgs[row.run_id] == org_id and row.decided_at is not None:
            times.append(row.decided_at)
    return times


def check_solo_approve(
    uow: UnitOfWork, org_id: str, approval: Approval, approver: str, terms: SoloTerms
) -> SoloProof:
    """SA-12 step 7 for a solo approve; the first failure raises `SoloRefused`. Returns the
    proof word the ledger entry carries (`none` for a kind that is not high-risk)."""
    if terms.acknowledge is not True:
        raise SoloRefused("acknowledge_required")  # 7a
    if terms.action_digest is None:
        raise SoloRefused("acknowledge_required")  # 7b
    expected = action_digest(approval)
    if not hmac.compare_digest(terms.action_digest.encode("utf-8"), expected.encode("ascii")):
        raise SoloRefused("action_changed")  # 7c
    proof = SoloProof.NONE
    if approval.action.kind in HIGH_RISK_KINDS:  # 7d
        try:
            proof = fresh_proof(proof_facts(uow, org_id, approver, terms), uow.at)
        except ProofRefused as refused:
            raise SoloRefused(refused.code) from None
    wait = solo_retry_after(
        counted_solo_approves(uow, org_id, approver, uow.at), terms.limit_per_hour, uow.at
    )
    if wait is not None:
        raise SoloRefused("solo_rate_limited", retry_after=wait)  # 7e
    return proof


def caller_terms(
    reads: SoloReads,
    org_id: str,
    approval: Approval,
    caller: str,
    originator: str | None,
    terms: SoloTerms,
    now: datetime,
) -> dict[str, Any] | None:
    """`caller_terms` of the approval view (SA-16), from the values an approve sent now would
    read; never stored. None when the approval is not pending. It never looks at the
    acknowledgement, the digest, the rate limit or the `approvers` list (steps 7a to 7c, 7e, 8).
    `originator` is the run's (`engine.originator.originator_of`, read by the caller).
    `caller_is_originator` (#1458) is on every answer: display only, and false when the run
    names no originator."""
    if approval.status is not ApprovalStatus.PENDING:
        return None
    mode, needed, proofs = _terms_of(reads, org_id, approval, caller, originator, terms, now)
    return {
        "mode": mode,
        "fresh_proof_needed": needed,
        "proofs": proofs,
        "caller_is_originator": originator is not None and originator == caller,
    }


def _terms_of(
    reads: SoloReads,
    org_id: str,
    approval: Approval,
    caller: str,
    originator: str | None,
    terms: SoloTerms,
    now: datetime,
) -> tuple[str, bool, list[str]]:
    """`(mode, fresh_proof_needed, proofs)` of a pending approval for `caller` (SA-16)."""
    standing = standing_of(reads, org_id, caller)
    if not standing.holder:
        return "refused", False, []
    if not standing.solo:
        if originator == caller:
            return "refused", False, []  # step 6 would refuse them
        return mode_without_solo(originator).value, False, []
    if approval.action.kind not in HIGH_RISK_KINDS:
        return ApprovalMode.SOLO.value, False, []
    facts = proof_facts(reads, org_id, caller, terms)
    try:
        fresh_proof(facts, now)
        needed = False
    except ProofRefused:
        needed = True
    return ApprovalMode.SOLO.value, needed, accepted_proofs(facts)
