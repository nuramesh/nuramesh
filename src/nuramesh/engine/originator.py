"""Who started the work being approved, and the no-self-approval rule (R2.13 3a, owner Q2).

The **originator** of a run is the actor of its first ledger entry (`run.started`). When that actor
is an API token (`service:<id>`) it is the person who issued the token. An operator, a schedule,
`service:cli`, an agent, a token whose issuer is disabled, erased or unknown, a missing run or an
unreadable or unexpected first entry gives **no originator**: the rule then matches nobody, so it
can never block every approver. It refuses a person only when the ledger positively names them.

`originator_of` is called inside the transaction that records an approval or a gate decision, so it
reads the values that transaction will write over. Since solo approval
 the two callers (`engine.policy`, `engine.human_decisions`) read the
originator themselves and raise `SelfApproval` unless the decision is a solo one;
`refuse_self_approval` is the rule without the solo exception, kept for callers that have no solo
path (the design notes names it).
"""

from typing import Protocol

from nuramesh.core.auth import ApiToken, User
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.people import is_human_id
from nuramesh.stores.base import UnitOfWork, run_stream

__all__ = ["SelfApproval", "originator_of", "refuse_self_approval"]

SERVICE_PREFIX = "service:"


class SelfApproval(Exception):
    """The person who started the work tried to approve it. The message is fixed text."""

    def __init__(self) -> None:
        super().__init__("you cannot approve your own work")


class OriginatorReads(Protocol):
    """What reading the originator needs: a unit of work has it, and so does a read-only adapter
    over the store (the approval view, SA-16)."""

    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]: ...

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None: ...

    def get_user(self, org_id: str, user_id: str) -> User | None: ...


def originator_of(uow: OriginatorReads, org_id: str, run_id: str) -> str | None:
    """The person (`human:...`) behind the run, or None when no person is positively named."""
    first = uow.events_after(run_stream(run_id), 0, 1)
    if not first or first[0].type != EventType.RUN_STARTED.value:
        return None
    actor = first[0].actor
    if is_human_id(actor):
        return actor  # named in the ledger; the person acts as themselves, so no status check
    if actor.startswith(SERVICE_PREFIX):
        token: ApiToken | None = uow.get_api_token(org_id, actor.removeprefix(SERVICE_PREFIX))
        if token is None or not is_human_id(token.created_by):
            return None  # an unknown token, or `service:cli`: no person
        return _live_person(uow, org_id, token.created_by)
    return None


def _live_person(uow: OriginatorReads, org_id: str, person: str) -> str | None:
    """A token's issuer, if they are an active user of the organization: a token whose issuer is
    erased, disabled or unknown has no person behind it (design decision)."""
    user = uow.get_user(org_id, person)
    if user is None or user.erased_at is not None or user.disabled_at is not None:
        return None
    return person


def refuse_self_approval(uow: UnitOfWork, org_id: str, run_id: str, approver: str) -> None:
    """Raise `SelfApproval` when `approver` is the originator of the run (whatever their role)."""
    if originator_of(uow, org_id, run_id) == approver:
        raise SelfApproval
