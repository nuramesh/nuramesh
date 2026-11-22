"""Security events written outside the session service (API tokens; ND60).

One place for the two things every security event needs: the **identifier key** a new hash is made
with (the user's own when the identifier belongs to a known, not-erased user, else the
organization's key of the current epoch) and the **pair** of writes, the minimal ledger fact and
its one security-log row, in the caller's transaction.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from nuramesh.core.auth import (
    ApiToken,
    SecurityDetail,
    SecurityLogEntry,
    identifier_epoch,
    identifier_hash,
)
from nuramesh.core.ledger import EventType, format_time
from nuramesh.stores import EventDraft, UnitOfWork


class SecurityRecorder:
    """Writes security events of one organization with the clock it is given."""

    def __init__(self, org_id: str, clock: Callable[[], datetime]) -> None:
        self.org_id, self._clock = org_id, clock

    def key(self, uow: UnitOfWork, user_id: str | None) -> bytes:
        """The key for a new hash: the user's, else the organization's of this epoch."""
        key = None if user_id is None else uow.user_identifier_key(self.org_id, user_id)
        if key is None:
            key = uow.org_identifier_key(self.org_id, identifier_epoch(self._clock()))
        if key is None:  # the epoch's key was destroyed: only if the clock went backwards
            raise RuntimeError("the identifier key of this epoch was destroyed")
        return key

    def hash_of(self, uow: UnitOfWork, text: str, user_id: str | None) -> str:
        """The keyed identifier hash of an opaque id (a token or session id)."""
        return identifier_hash(self.key(uow, user_id), self.org_id, text)

    def record(
        self,
        uow: UnitOfWork,
        kind: EventType,
        actor: str,
        payload: dict[str, Any],
        *,
        subject: str | None = None,
        details: dict[str, SecurityDetail] | None = None,
    ) -> None:
        """The ledger fact and exactly one security-log row, in `uow`'s transaction."""
        uow.append(EventDraft(stream=f"org:{self.org_id}", type=kind, actor=actor, payload=payload))
        uow.append_security_log(
            SecurityLogEntry.new(
                self.org_id,
                self._clock(),
                kind.value,
                actor=actor,
                subject=subject,
                details=details,
            )
        )


def owner_of(token: ApiToken) -> str | None:
    """The user who issued a token (its owner), or None for one made by the CLI, by `system` or an
    operator, or whose issuer was erased (`erased-user`)."""
    return token.created_by if token.created_by.startswith("human:") else None


def token_created(rec: SecurityRecorder, uow: UnitOfWork, token: ApiToken) -> None:
    """`auth.token_created`: actor the issuer, subject the owner (ND60)."""
    owner = owner_of(token)
    rec.record(
        uow,
        EventType.AUTH_TOKEN_CREATED,
        token.created_by,
        {
            "token_hash": rec.hash_of(uow, token.id, owner),
            "scopes": sorted(s.value for s in token.scopes),
            "expires_at": format_time(token.expires_at),
        },
        subject=owner,
    )


def token_revoked(rec: SecurityRecorder, uow: UnitOfWork, token: ApiToken, by: str) -> None:
    """`auth.token_revoked`: actor the person (or `system`) that revoked it, subject the owner."""
    owner = owner_of(token)
    rec.record(
        uow,
        EventType.AUTH_TOKEN_REVOKED,
        by,
        {"token_hash": rec.hash_of(uow, token.id, owner)},
        subject=owner,
    )


def token_renamed(rec: SecurityRecorder, uow: UnitOfWork, token: ApiToken, by: str) -> None:
    """`auth.token_renamed`: actor the person (or `service:cli`) who renamed it, subject the owner
    when the issuer is a person; the payload and the log details are the keyed hash only, never a
    name (#634 R12, R44)."""
    _token_event(rec, uow, EventType.AUTH_TOKEN_RENAMED, token, by)


def token_deleted(rec: SecurityRecorder, uow: UnitOfWork, token: ApiToken, by: str) -> None:
    """`auth.token_deleted`: as `token_renamed`, for the removal of an inactive token."""
    _token_event(rec, uow, EventType.AUTH_TOKEN_DELETED, token, by)


def _token_event(
    rec: SecurityRecorder, uow: UnitOfWork, kind: EventType, token: ApiToken, by: str
) -> None:
    owner = owner_of(token)
    token_hash = rec.hash_of(uow, token.id, owner)
    rec.record(
        uow, kind, by, {"token_hash": token_hash}, subject=owner, details={"token_hash": token_hash}
    )


def token_names_erased(
    rec: SecurityRecorder, uow: UnitOfWork, count: int, *, actor: str, origin: str | None = None
) -> None:
    """`auth.token_names_erased`: how many token rows lost a personal name or issuer, nothing
    else (no name, id or hash); written only when `count` is above zero."""
    if count <= 0:
        return
    payload: dict[str, Any] = {"count": count}
    if origin is not None:
        payload["origin"] = origin
    rec.record(uow, EventType.AUTH_TOKEN_NAMES_ERASED, actor, payload, details=dict(payload))


def token_issuers_erased(rec: SecurityRecorder, uow: UnitOfWork, user_id: str) -> None:
    """Erasure step 1c (#634 R1 to R5): the person's tokens lose their personal names and the
    issuer fields become `erased-user`, in the erasing transaction, after step 1 (which wrote the
    revocation hashes while the identifier key still exists)."""
    token_names_erased(rec, uow, uow.erase_api_token_issuer(rec.org_id, user_id), actor="system")


def revoke_issued_tokens(
    rec: SecurityRecorder,
    uow: UnitOfWork,
    tokens: list[ApiToken],
    user_id: str,
    at: datetime,
    by: str = "system",
) -> None:
    """Erasure step 1 for tokens (ND60): revoke every live token `user_id` issued, as `system`
    (or as the owner `by` who disabled the person, E1 D1), each with its event and log row,
    while the user's identifier key still exists."""
    for token in tokens:
        if token.created_by == user_id and token.active(at):
            if uow.revoke_api_token(token.org_id, token.id, at, by):
                token_revoked(rec, uow, token, by)


def revoke_all_agent_credentials(
    rec: SecurityRecorder, uow: UnitOfWork, at: datetime, reason: str
) -> int:
    """End every live agent credential of the organization (every team) as `system`, oldest
    first, each with its `agent.credential_revoked` entry (organization-keyed hash, no subject,
    ND60) and log row, in `uow`'s transaction. Returns how many."""
    count = 0
    for c in uow.list_agent_credentials(rec.org_id):
        if c.active(at) and uow.revoke_agent_credential(rec.org_id, c.id, at, "system"):
            rec.record(
                uow,
                EventType.AGENT_CREDENTIAL_REVOKED,
                "system",
                {
                    "agent": c.agent_id,
                    "credential_hash": rec.hash_of(uow, c.id, None),
                    "reason": reason,
                },
            )
            count += 1
    return count


def agent_credentials_issuer_erased(
    rec: SecurityRecorder, uow: UnitOfWork, user_id: str, at: datetime
) -> None:
    """Erasure step 1b (E2, Q4): the person's agent credentials lose their issuer (`erased-user`)
    but are not revoked; if any was live, one `agent.credential_issuer_erased` entry (a count and
    the agents' slugs, nothing of the person) and its one security-log row (subject none) are
    written in the erasing transaction."""
    live = uow.erase_agent_credential_user(rec.org_id, user_id, at)
    if live:
        rec.record(
            uow,
            EventType.AGENT_CREDENTIAL_ISSUER_ERASED,
            "system",
            {"count": len(live), "agents": sorted({c.agent_id for c in live})},
        )
