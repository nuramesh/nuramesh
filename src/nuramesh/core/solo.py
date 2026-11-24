"""Solo approval, the pure rules (#942).

A **solo decision** is a decision by the organization's only active person who holds `decide`:
nobody else could have decided, so nobody else reads the request. This module holds what can be
computed without a store: the digest that binds an approve to the exact action the person was
shown (SA-6), "within 600 s" (section 1), the fresh proof a high-risk approve needs (SA-7, table
3.2) and the wait a rate-limited person is told (SA-9). The engine (`engine.solo`) reads the
facts inside the decision transaction and calls these. It also holds the report of SA-18 item 4:
which organization entries removed an active decide-holder, and the counts of the organization
status (`solo_status`).

Nothing here grants anything: a solo approve is the strictest allowed path (SA-21).
"""

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from nuramesh.core.auth import ROLE_PERMISSIONS, Permission, Role
from nuramesh.core.canonical import JsonValue, canonical_json, decimal_text
from nuramesh.core.ledger import EventType, LedgerEvent, format_time
from nuramesh.core.policy import (
    SOLO_APPROVALS_PER_HOUR_MAX,
    ActionKind,
    Approval,
    ApprovalMode,
    ApprovalStatus,
)
from nuramesh.core.scorecard import solo_share

__all__ = [
    "FRESH_SECONDS",
    "HIGH_RISK_KINDS",
    "PROOF_TEXTS",
    "RATE_WINDOW_SECONDS",
    "REMOVAL_WINDOW_SECONDS",
    "SOLO_LIMIT_MAX",
    "STATUS_WINDOW_DAYS",
    "ProofFacts",
    "ProofRefused",
    "SoloProof",
    "accepted_proofs",
    "action_digest",
    "fresh_proof",
    "removal_times",
    "solo_retry_after",
    "solo_status",
    "within_fresh_window",
]

HIGH_RISK_KINDS = frozenset(
    {
        ActionKind.SPEND,
        ActionKind.EXTERNAL_MESSAGE,
        ActionKind.MERGE,
        ActionKind.RUN_COMMAND,
        ActionKind.NETWORK,
        ActionKind.USE_TOOL,
    }
)
"""The kinds whose solo approve needs a fresh proof (section 1, "HR")."""

FRESH_SECONDS = 600
RATE_WINDOW_SECONDS = 3600
SOLO_LIMIT_MAX = SOLO_APPROVALS_PER_HOUR_MAX
"""Solo approves per person in any hour; configuration may only lower it (SA-9)."""

STATUS_WINDOW_DAYS = 30
"""The organization status counts approves decided in the last 30 days (SA-18 item 4)."""
REMOVAL_WINDOW_SECONDS = 7 * 86400
"""A solo approve less than 7 days after a decide-holder was removed is reported (D7)."""

PASSWORD_LOGIN = "password"


class SoloProof(StrEnum):
    """What made a solo approve fresh (`solo_proof` of `approval.decided`, SA-14)."""

    MFA = "mfa"
    PASSWORD = "password"  # noqa: S105 (a proof word, not a secret)
    SIGN_IN = "sign_in"
    NONE = "none"  # a kind that is not high-risk: no proof is consulted


PROOF_TEXTS = {
    "mfa_unavailable": "a solo approval of this kind needs MFA, which is not available",
    "mfa_enrollment_required": "a solo approval of this kind needs MFA enrollment",
    "mfa_required": "sign in again with your second factor",
    "step_up_required": "sign in again or confirm your password",
}
"""The fixed detail of each fresh-proof refusal: never a time, a session or an address."""


class ProofRefused(Exception):
    """SA-7 refuses: `code` is one of `PROOF_TEXTS`. The message is fixed text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(PROOF_TEXTS[code])


@dataclass(frozen=True)
class ProofFacts:
    """What SA-7 reads, all from the decision transaction: the deployment, the approver's factor
    and the three times of the session the request came in on."""

    hosted: bool
    enrolled: bool
    mfa_available: bool
    mfa_verified_at: datetime | None
    password_confirmed_at: datetime | None
    signed_in_at: datetime | None
    login_via: str | None


def action_digest(approval: Approval) -> str:
    """SA-6: lowercase hex SHA-256 of the canonical JSON of exactly nine keys. The target is the
    stored text (never its keyed hash, never trimmed); a spend's amount is the ledger's decimal
    text. Unkeyed, so it is shown only by the approval API: never ledgered or logged."""
    action = approval.action
    body: dict[str, JsonValue] = {
        "v": 1,
        "approval": approval.id,
        "run": approval.run_id,
        "task": approval.task_id,
        "agent": approval.agent_id,
        "kind": action.kind.value,
        "target": action.target,
        "amount": None if action.amount is None else decimal_text(action.amount.amount),
        "requested_at": format_time(approval.requested_at),
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def within_fresh_window(at: datetime | None, now: datetime) -> bool:
    """Section 1: `0 s <= now - at <= 600 s`. A missing time and a time later than `now` (a clock
    set back, a restored row) never hold: a time from the future is invalid, not fresh."""
    if at is None:
        return False
    return timedelta(0) <= now - at <= timedelta(seconds=FRESH_SECONDS)


def _factor_branch(facts: ProofFacts) -> bool:
    return facts.hosted or facts.enrolled


def fresh_proof(facts: ProofFacts, now: datetime) -> SoloProof:
    """SA-7 for a high-risk kind: the proof word, or `ProofRefused`. First match answers."""
    if _factor_branch(facts) and not facts.mfa_available:
        raise ProofRefused("mfa_unavailable")  # branch 1: never falls through to a password
    if facts.hosted and not facts.enrolled:
        raise ProofRefused("mfa_enrollment_required")  # branch 2
    if _factor_branch(facts):  # branch 3: only the factor; a password never replaces it
        if within_fresh_window(facts.mfa_verified_at, now):
            return SoloProof.MFA
        raise ProofRefused("mfa_required")
    # branch 4: self-hosted, no active factor
    confirmed = (
        facts.password_confirmed_at
        if within_fresh_window(facts.password_confirmed_at, now)
        else None
    )
    signed_in = (
        facts.signed_in_at
        if facts.login_via == PASSWORD_LOGIN and within_fresh_window(facts.signed_in_at, now)
        else None
    )
    if confirmed is None and signed_in is None:
        raise ProofRefused("step_up_required")
    if signed_in is None or (confirmed is not None and confirmed >= signed_in):
        return SoloProof.PASSWORD  # the later time names the proof; password on a tie
    return SoloProof.SIGN_IN


def accepted_proofs(facts: ProofFacts) -> list[str]:
    """What SA-7 would accept from this caller (`caller_terms.proofs`, SA-16): nothing in
    branches 1 and 2 (no proof helps there), the factor in branch 3, else password or sign-in."""
    if _factor_branch(facts):
        if not facts.mfa_available or not facts.enrolled:
            return []
        return [SoloProof.MFA.value]
    return [SoloProof.PASSWORD.value, SoloProof.SIGN_IN.value]


def solo_retry_after(decided_at: Sequence[datetime], limit: int, now: datetime) -> int | None:
    """SA-9. `decided_at` are the times of the person's solo approves; the counted ones are those
    with `now - decided_at < 3600 s`. None when fewer than `limit` are counted (an approve is
    possible); else the whole seconds (rounded up, at least 1) until the `(count - limit + 1)`-th
    oldest counted row leaves the window, which is when the count falls to `limit - 1`."""
    window = timedelta(seconds=RATE_WINDOW_SECONDS)
    counted = sorted(at for at in decided_at if now - at < window)
    if len(counted) < limit:
        return None
    leaves = counted[len(counted) - limit] + window
    return max(1, math.ceil((leaves - now).total_seconds()))


def _holds_decide(role: object) -> bool:
    """Whether a role word of a ledger payload holds `decide`. An unknown word holds nothing."""
    if not isinstance(role, str):
        return False
    try:
        return Permission.DECIDE in ROLE_PERMISSIONS[Role(role)]
    except (ValueError, KeyError):
        return False


def removal_times(org_events: Iterable[LedgerEvent]) -> list[datetime]:
    """The ledger times of the **removal entries** of an organization stream, read in ledger
    order (SA-18 item 4): the entries, of any age, that removed an active decide-holder.

    That is a `user.disabled`, a `user.erased`, or a `user.role_changed` from a role with
    `decide` to one without, of a person who was active and held `decide` at that entry. The
    payloads of the first two name only the person, so the role as of the entry is rebuilt from
    the person's earlier `user.created` and `user.role_changed` entries; whether they were still
    active is rebuilt from `user.disabled`, `user.enabled` and `user.erased`. A person with no
    recorded role, an unknown role word and an unreadable payload give no removal (the count
    reports what the ledger positively says)."""
    role: dict[str, object] = {}
    inactive: set[str] = set()
    times: list[datetime] = []
    for event in org_events:
        user = event.payload.get("user")
        if not isinstance(user, str):
            continue
        held = user not in inactive and _holds_decide(role.get(user))
        if event.type == EventType.USER_CREATED.value:
            role[user] = event.payload.get("role")
            inactive.discard(user)
        elif event.type == EventType.USER_ROLE_CHANGED.value:
            new = event.payload.get("to")
            if held and not _holds_decide(new):
                times.append(event.at)
            role[user] = new
        elif event.type == EventType.USER_ENABLED.value:
            inactive.discard(user)
        elif event.type in (EventType.USER_DISABLED.value, EventType.USER_ERASED.value):
            if held:
                times.append(event.at)
            inactive.add(user)
    return times


def solo_status(
    approvals: Iterable[Approval],
    removals: Sequence[datetime],
    now: datetime,
    *,
    solo_now: bool,
    limit_per_hour: int,
) -> dict[str, Any]:
    """`solo_approval` of the organization status (SA-18 item 4). `approvals` are the
    organization's rows; the counted ones are approved (consumed included) with
    `0 s <= now - decided_at < 30 days`. A row with no mode (decided before modes) is in the
    total and never solo. `approved_solo_after_removal` counts each solo approve once when at
    least one removal has `0 s <= decided_at - removal_at < 7 days`."""
    window = timedelta(days=STATUS_WINDOW_DAYS)
    after_window = timedelta(seconds=REMOVAL_WINDOW_SECONDS)
    approved = approved_solo = after_removal = 0
    for row in approvals:
        if row.status not in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED):
            continue
        decided = row.decided_at
        if decided is None or not timedelta(0) <= now - decided < window:
            continue
        approved += 1
        if row.approval_mode is not ApprovalMode.SOLO:
            continue
        approved_solo += 1
        if any(timedelta(0) <= decided - removal < after_window for removal in removals):
            after_removal += 1
    return {
        "solo_now": solo_now,
        "limit_per_hour": limit_per_hour,
        "window_days": STATUS_WINDOW_DAYS,
        "approved": approved,
        "approved_solo": approved_solo,
        "solo_share": solo_share(approved_solo, approved),
        "approved_solo_after_removal": after_removal,
    }
