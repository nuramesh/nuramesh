"""The committed-value reads of the solo approval view and `solo_approval` of the organization
status (SA-16, SA-18 item 4), shared by the server and the CLI."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from nuramesh.core.auth import ApiToken, AuthSession, User
from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.mfa import MfaFactor
from nuramesh.core.policy import ApprovalStatus
from nuramesh.core.solo import removal_times, solo_status
from nuramesh.engine.org_halt import read_org_events
from nuramesh.engine.solo import decide_holders
from nuramesh.stores.base import Store

__all__ = ["StoreReads", "solo_status_view"]


class StoreReads:
    """The committed values the approval view reads, with the names a unit of work has. A view
    takes no write lock; the decision itself reads again inside its transaction."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def list_users(self, org_id: str) -> list[User]:
        return self._store.list_users(org_id)

    def get_user(self, org_id: str, user_id: str) -> User | None:
        return self._store.get_user(org_id, user_id)

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        return self._store.get_api_token(org_id, token_id)

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        return self._store.get_mfa_factor(org_id, user_id)

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        return self._store.get_auth_session(org_id, session_id)

    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]:
        return list(self._store.read_stream(stream, after_seq=after_seq, limit=limit))


def solo_status_view(
    store: Store,
    org_id: str,
    limit_per_hour: int,
    in_org: Callable[[str], bool],
    now: datetime,
    events: Sequence[LedgerEvent] | None = None,
) -> dict[str, Any]:
    """`solo_approval` of `GET /v1/org/status` (SA-18 item 4): whether the organization has
    exactly one active decide-holder now, the limit, and the 30-day counts of its approved
    requests with the solo share and the solo approves made within 7 days of a removal. An
    approval row carries no organization, so `in_org` says whether a run is the organization's
    (the rule of the approvals list). Counts only: no person, action or time is shown. A read of
    committed values; it writes nothing. `events` are the organization stream's entries when the
    caller already read them (the status route reads the stream once); None reads them here."""
    rows = [
        stored.approval
        for status in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED)
        for stored in store.approvals(status=status)
        if in_org(stored.approval.run_id)
    ]
    return solo_status(
        rows,
        removal_times(read_org_events(store, org_id) if events is None else events),
        now,
        solo_now=len(decide_holders(StoreReads(store), org_id)) == 1,
        limit_per_hour=limit_per_hour,
    )
