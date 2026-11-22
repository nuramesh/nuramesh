"""The public errors of the `nuramesh` library (stdlib only; `docs/python-api.md`).

Every error the public API raises is a `NuraMeshError`. A `PolicyDenied` carries the `Outcome`
that refused the action; it is raised only when the caller asked for it (`raise_on_deny=True`,
the decorator's default), otherwise a refusal is a returned `Outcome`. `CredentialError` is raised
at `bind()` time, before any action: no credential, a human token or a session value in its place,
or an unreadable server address. The engine's own `PolicyDenied` (`nuramesh.engine.models`) is
re-parented under this one in a later change so one `except` catches both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nuramesh.api.binding import Outcome

__all__ = [
    "ApprovalRequired",
    "ApprovalTimeout",
    "CredentialError",
    "NuraMeshError",
    "PolicyDenied",
    "ServerUnavailable",
]


class NuraMeshError(Exception):
    """Base of every error the public API raises."""


class CredentialError(NuraMeshError):
    """The binding cannot be made: no agent credential, a credential that is not an agent's (a
    person's token or session), or a server address that cannot carry one. Never quotes the
    value."""


class PolicyDenied(NuraMeshError):
    """The action was not allowed; `outcome` says why (decision, reason code, approval id)."""

    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        super().__init__(f"{outcome.decision.value}: {outcome.reason_code}")


class ApprovalRequired(PolicyDenied):
    """The action needs a person's approval and none is recorded yet; `approval_id` is the open
    request. The agent process cannot answer it: a person does, in the app or through the API."""

    @property
    def approval_id(self) -> str | None:
        return self.outcome.approval_id


class ApprovalTimeout(PolicyDenied):
    """The wait for a person's decision ran out: the action is denied (fail closed)."""


class ServerUnavailable(PolicyDenied):
    """The server could not be reached or answered badly: the action is denied (fail closed)."""
