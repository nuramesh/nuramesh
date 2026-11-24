"""Agent credentials: the service behind the owner routes, the CLI and authentication (E2).

The agent-identity spec is the contract. An agent principal is an agent of the team
configuration; its credentials (`nma_...`, scopes `read` and `run`) are issued, rotated and revoked
by an owner in a human session; the CLI can only list and revoke. Everything a credential does is
recorded as `agent:<agent id>`, set by the server from the credential (the binding rule).

Every write here is one store transaction: the row change, its ledger entry on `org:<org>` and its
one security-log row. The secret, its hash and the credential id never reach the ledger or the
security log; the entries carry a keyed hash of the id (ND60), so erasing the issuer makes it
unmatchable.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nuramesh.core.auth import (
    AGENT_CREDENTIAL_CALLS_PER_MINUTE,
    AGENT_CREDENTIAL_LIFETIME,
    AGENT_CREDENTIAL_MAX_LIVE,
    AGENT_ROTATION_GRACE_MAX,
    ERASED_ACTOR,
    AgentCredential,
    IssuedAgentCredential,
    Role,
    TokenScope,
    agent_fingerprint,
    hash_agent_credential,
    is_agent_credential_id,
    is_agent_slug,
    issue_agent_credential,
    looks_like_agent_credential,
)
from nuramesh.core.breakers import counts_for_halt, counts_for_role, halt_state, role_states
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.org import OrgGraph
from nuramesh.core.people import is_human_id
from nuramesh.engine.org_halt import org_stream_id
from nuramesh.server.secevents import SecurityRecorder
from nuramesh.server.sessions import AuthProblem
from nuramesh.stores import AlreadyExistsError, StoreBusyError, UnitOfWork
from nuramesh.stores.base import Store

SUSPENDED_DETAIL = (
    "the organization is halted or this agent's role is paused; "
    "credentials can be revoked but not issued or renewed"
)
_PAGE = 500


class AgentProblem(AuthProblem):
    """An error answer from the agent credential service (same shape as every auth error)."""


@dataclass(frozen=True)
class AgentEntry:
    """What the service knows about an agent of the configuration: its role id and fingerprint."""

    role: str
    fingerprint: str
    name: str = ""


class SettingsError(ValueError):
    """A bad `NURAMESH_AGENT_CREDENTIAL_*` value; the server does not start (spec 14)."""


@dataclass(frozen=True)
class AgentSettings:
    """The three agent credential settings (spec 14). The defaults are the spec's."""

    days: int = AGENT_CREDENTIAL_LIFETIME.days
    max_live: int = AGENT_CREDENTIAL_MAX_LIVE
    calls_per_minute: int = AGENT_CREDENTIAL_CALLS_PER_MINUTE

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "AgentSettings":
        """Read the settings; unset is the default, anything else (empty, not an integer, out of
        range) raises `SettingsError` naming the variable and the range."""
        found: dict[str, int] = {}
        for field, name, low, high in (
            ("days", "NURAMESH_AGENT_CREDENTIAL_DAYS", 1, 90),
            ("max_live", "NURAMESH_AGENT_CREDENTIAL_MAX_LIVE", 1, 20),
            ("calls_per_minute", "NURAMESH_AGENT_CREDENTIAL_CALLS_PER_MINUTE", 10, 6000),
        ):
            if name not in environ:
                continue
            raw = environ[name].strip()
            if not re.fullmatch(r"[0-9]{1,6}", raw) or not low <= int(raw) <= high:
                raise SettingsError(f"{name} must be a whole number from {low} to {high}")
            found[field] = int(raw)
        return cls(**found)


def agent_directory(org: OrgGraph) -> dict[str, AgentEntry]:
    """The agents of a configuration with their role and fingerprint (spec 2.6)."""
    return {
        a.id: AgentEntry(
            role=a.role, fingerprint=agent_fingerprint(a, org.role_of(a.id)), name=a.name
        )
        for a in org.agents
    }


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def _no_agent() -> AgentProblem:
    return AgentProblem(404, "not_found", "no such agent")


def _no_credential() -> AgentProblem:
    return AgentProblem(404, "not_found", "no such credential")


def _not_owner() -> AgentProblem:
    return AgentProblem(403, "forbidden", "the acting person is no longer an owner")


class AgentCredentialService:
    """Issue, rotate, revoke, list and look up agent credentials of one organization and team."""

    def __init__(
        self,
        store: Store,
        org_id: str,
        team: str,
        agents: Mapping[str, AgentEntry],
        clock: Callable[[], datetime],
        *,
        default_days: int = AGENT_CREDENTIAL_LIFETIME.days,
        max_live: int = AGENT_CREDENTIAL_MAX_LIVE,
    ) -> None:
        self.store, self.org_id, self.team, self.agents = store, org_id, team, agents
        self._clock, self.default_days, self.max_live = clock, default_days, max_live
        self.recorder = SecurityRecorder(org_id, clock)

    def now(self) -> datetime:
        return self._clock()

    # -- reading ---------------------------------------------------------------------------

    def view(self, c: AgentCredential) -> dict[str, Any]:
        """The public view of a credential now: never the secret, never the hash."""
        now = self.now()
        active = c.active(now)
        return {
            "id": c.id,
            "agent": c.agent_id,
            "actor": c.actor,
            "scopes": sorted(s.value for s in c.scopes),
            "created_by": c.created_by,
            "created_at": _iso(c.created_at),
            "expires_at": _iso(c.expires_at),
            "revoked_at": None if c.revoked_at is None else _iso(c.revoked_at),
            "revoked_by": c.revoked_by,
            "active": active,
            "in_config": c.agent_id in self.agents,
            "team": c.team,
            "issuer_erased": c.created_by == ERASED_ACTOR and active,
        }

    def list_views(
        self,
        agent: str | None = None,
        *,
        active: bool | None = None,
        issuer_erased: bool | None = None,
    ) -> list[dict[str, Any]]:
        """The credentials of this server's team (views), oldest first, with the optional
        filters. Another team's credentials are not listed: this server cannot revoke or rotate
        them (404), so the lists show exactly what the caller may act on."""
        found = [
            self.view(c)
            for c in self.store.list_agent_credentials(self.org_id)
            if c.team == self.team
        ]
        return [
            v
            for v in found
            if (agent is None or v["agent"] == agent)
            and (active is None or v["active"] is active)
            and (issuer_erased is None or v["issuer_erased"] is issuer_erased)
        ]

    def issued_views(self, person_id: str) -> list[dict[str, Any]]:
        """The live credentials the person issued, in every team of the organization (E1 D5: the
        same set the issuer-disabled alert counts), oldest first, as views."""
        now = self.now()
        return [
            self.view(c)
            for c in self.store.list_agent_credentials(self.org_id)
            if c.created_by == person_id and c.active(now)
        ]

    def agent_views(self) -> list[dict[str, Any]]:
        """The agents of the configuration for the Agents page: id, name, role and how many of
        their credentials are live now (never a fingerprint), ordered by id."""
        live: dict[str, int] = {}
        for c in self.store.list_agent_credentials(self.org_id):
            if c.team == self.team and c.active(self.now()):
                live[c.agent_id] = live.get(c.agent_id, 0) + 1
        return [
            {"id": i, "name": e.name or i, "role": e.role, "active_credentials": live.get(i, 0)}
            for i, e in sorted(self.agents.items())
        ]

    def lookup(self, presented: str) -> AgentCredential | None:
        """The active credential behind a presented secret for this organization and team whose
        agent is in the configuration with the same fingerprint; else None (steps 1 to 4 of the
        spec's section 3: every failure looks the same)."""
        if not looks_like_agent_credential(presented):
            return None
        try:
            digest = hash_agent_credential(presented)
        except UnicodeEncodeError:
            return None
        found = self.store.get_agent_credential_by_hash(self.org_id, self.team, digest)
        if found is None or not found.active(self.now()):
            return None
        return self.current(found)

    def current(self, c: AgentCredential) -> AgentCredential | None:
        """`c` if its agent is in the configuration with the stored fingerprint, else None."""
        known = self.agents.get(c.agent_id)
        if known is None or known.fingerprint != c.fingerprint:
            return None
        return c

    # -- the kill switch (E19) -----------------------------------------------------------------

    def suspended(self, uow: UnitOfWork, agent_id: str) -> bool:
        """Whether the organization is halted or the agent's role is paused, read through `uow`.
        Raises `StoreBusyError` when busy; any other failure is treated as suspended (fail
        closed)."""
        try:
            return self._suspended(uow, agent_id)
        except StoreBusyError:
            raise
        except Exception:
            return True

    def _suspended(self, uow: UnitOfWork, agent_id: str) -> bool:
        stream, after = org_stream_id(self.org_id), 0
        events: list[LedgerEvent] = []
        while True:
            batch = uow.events_after(stream, after, _PAGE)
            events.extend(e for e in batch if counts_for_halt(e) or counts_for_role(e))
            if len(batch) < _PAGE:
                break
            after = batch[-1].seq
        return self._suspended_by(events, agent_id)

    def _suspended_by(self, events: list[LedgerEvent], agent_id: str) -> bool:
        if halt_state(events).halted:
            return True
        role = self.agents[agent_id].role
        state = role_states(events).get(role)
        return state is not None and state.paused

    def suspended_now(self, agent_id: str) -> bool:
        """Whether a `run` call of this agent is suspended, read at this request (no cache) from
        committed entries, without a write transaction (a read must not take the writer lock).
        The write's own recheck inside its transaction (8.1) stays the authority. Busy raises
        `StoreBusyError` (503); any other failure counts as suspended."""
        try:
            events = [
                e
                for e in self.store.read_stream(org_stream_id(self.org_id))
                if counts_for_halt(e) or counts_for_role(e)
            ]
            return self._suspended_by(events, agent_id)
        except StoreBusyError:
            raise
        except Exception:
            return True

    # -- hashing for the ledger ------------------------------------------------------------

    def _hash(self, uow: UnitOfWork, c: AgentCredential, *, org_key: bool = False) -> str:
        """The keyed hash of the credential id: the issuer's key while they exist (so the issued
        and the revoked entries match), else the organization's key of the current epoch."""
        owner = None if org_key or not is_human_id(c.created_by) else c.created_by
        return self.recorder.hash_of(uow, c.id, owner)

    @staticmethod
    def _subject(c: AgentCredential) -> str | None:
        return c.created_by if is_human_id(c.created_by) else None

    # -- writing ---------------------------------------------------------------------------

    def _owner(self, uow: UnitOfWork, by: str) -> None:
        user = uow.get_user(self.org_id, by)
        if user is None or user.erased or user.disabled or user.role is not Role.OWNER:
            raise _not_owner()

    def check_agent(self, agent_id: str) -> None:
        """404 `no such agent` unless the agent is a slug of this configuration."""
        self._agent(agent_id)

    def _agent(self, agent_id: str) -> AgentEntry:
        known = self.agents.get(agent_id) if is_agent_slug(agent_id) else None
        if known is None:
            raise _no_agent()
        return known

    def _scopes(self, scopes: list[str]) -> frozenset[TokenScope]:
        try:
            found = frozenset(TokenScope(s) for s in scopes)
        except (ValueError, TypeError):
            found = frozenset()
        if not 1 <= len(found) <= 2:
            raise AgentProblem(422, "invalid_request", "scopes must be 1 to 2 of read, run")
        return found

    def _days(self, days: int | None) -> int:
        if days is None:
            return self.default_days
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            raise AgentProblem(422, "invalid_request", "expires_in_days must be from 1 to 90")
        return days

    def _write_issued(
        self,
        uow: UnitOfWork,
        issued: IssuedAgentCredential,
        by: str,
        replaces: AgentCredential | None,
        replaces_until: datetime | None,
        keep_old: bool,
    ) -> None:
        rec = issued.record
        agent_id = rec.agent_id
        live_after = uow.count_live_agent_credentials(self.org_id, agent_id, self.now()) + 1
        if replaces is not None and not keep_old:
            live_after -= 1
        if live_after > self.max_live:
            raise AgentProblem(
                409,
                "credential_limit",
                "this agent already has the maximum number of live credentials",
            )
        try:
            uow.put_agent_credential(rec)
        except AlreadyExistsError:  # pragma: no cover - 256 random bits
            raise AgentProblem(503, "store_busy", "the database is busy; retry") from None
        self.recorder.record(
            uow,
            EventType.AGENT_CREDENTIAL_ISSUED,
            by,
            {
                "agent": agent_id,
                "credential_hash": self.recorder.hash_of(uow, rec.id, by),
                "scopes": sorted(s.value for s in rec.scopes),
                "expires_at": _iso(rec.expires_at),
                "replaces": None if replaces is None else self._hash(uow, replaces),
                "replaces_until": None if replaces_until is None else _iso(replaces_until),
            },
            subject=by,
        )

    def issue(
        self, agent_id: str, scopes: list[str], days: int | None, *, by: str
    ) -> IssuedAgentCredential:
        """Issue a credential to an agent (an owner in a session). Order of the refusals: 404
        (agent), 422 (body), then in the transaction 403 (owner), 409 `agent_suspended`, 409
        `credential_limit`."""
        known = self._agent(agent_id)
        wanted, lifetime = self._scopes(scopes), self._days(days)
        now = self.now()
        issued = issue_agent_credential(
            self.org_id,
            agent_id,
            team=self.team,
            fingerprint=known.fingerprint,
            scopes=wanted,
            created_by=by,
            now=now,
            lifetime=timedelta(days=lifetime),
        )
        with self.store.transaction() as uow:
            self._owner(uow, by)
            if self.suspended(uow, agent_id):
                raise AgentProblem(409, "agent_suspended", SUSPENDED_DETAIL)
            self._write_issued(uow, issued, by, None, None, False)
        return issued

    def rotate(
        self,
        agent_id: str,
        credential_id: str,
        days: int | None,
        grace_seconds: int | None,
        *,
        by: str,
    ) -> tuple[IssuedAgentCredential, datetime]:
        """Replace a credential without a gap (spec 2.5). Returns the new one and the instant the
        old one stops working."""
        known = self._agent(agent_id)
        if not is_agent_credential_id(credential_id):
            raise _no_credential()
        lifetime = self._days(days)
        grace = 0 if grace_seconds is None else grace_seconds
        if (
            isinstance(grace, bool)
            or not isinstance(grace, int)
            or not 0 <= grace <= AGENT_ROTATION_GRACE_MAX
        ):
            raise AgentProblem(422, "invalid_request", "grace_seconds must be from 0 to 86400")
        now = self.now()
        with self.store.transaction() as uow:
            old = uow.get_agent_credential(self.org_id, credential_id)
            if old is None or old.agent_id != agent_id or old.team != self.team:
                raise _no_credential()
            self._owner(uow, by)
            if self.suspended(uow, agent_id):
                raise AgentProblem(409, "agent_suspended", SUSPENDED_DETAIL)
            if not old.active(now):
                raise AgentProblem(409, "credential_not_active", "the credential is not active")
            if old.fingerprint != known.fingerprint:
                raise AgentProblem(
                    409,
                    "credential_not_active",
                    "the agent's definition changed; issue a new credential",
                )
            issued = issue_agent_credential(
                self.org_id,
                agent_id,
                team=self.team,
                fingerprint=known.fingerprint,
                scopes=old.scopes,
                created_by=by,
                now=now,
                lifetime=timedelta(days=lifetime),
            )
            until = now if grace == 0 else min(old.expires_at, now + timedelta(seconds=grace))
            self._write_issued(uow, issued, by, old, until, grace > 0)
            if grace == 0:
                self._revoke_row(uow, old, by, "rotated", at=now)
            else:
                uow.shorten_agent_credential(self.org_id, old.id, until)
        return issued, until

    def _revoke_row(
        self,
        uow: UnitOfWork,
        c: AgentCredential,
        by: str,
        reason: str,
        *,
        at: datetime | None = None,
    ) -> bool:
        if not uow.revoke_agent_credential(self.org_id, c.id, at or self.now(), by):
            return False
        sweep_or_cli = by in ("system", "service:cli")
        self.recorder.record(
            uow,
            EventType.AGENT_CREDENTIAL_REVOKED,
            by,
            {
                "agent": c.agent_id,
                "credential_hash": self._hash(uow, c, org_key=sweep_or_cli),
                "reason": reason,
            },
            subject=None if sweep_or_cli else self._subject(c),
        )
        return True

    def revoke(self, agent_id: str, credential_id: str, *, by: str) -> dict[str, Any]:
        """Revoke one credential of the agent (never suspended). A revoked one answers its record
        and writes nothing; unknown, foreign, other-agent and other-team ids are the same 404."""
        with self.store.transaction() as uow:
            return self.revoke_in(uow, agent_id, credential_id, by=by)

    def revoke_in(
        self, uow: UnitOfWork, agent_id: str, credential_id: str, *, by: str
    ) -> dict[str, Any]:
        """`revoke` inside the caller's unit of work (R1.35: a keyed request stores its answer in
        the same transaction)."""
        self._agent(agent_id)
        if not is_agent_credential_id(credential_id):
            raise _no_credential()
        current = uow.get_agent_credential(self.org_id, credential_id)
        if current is None or current.agent_id != agent_id or current.team != self.team:
            raise _no_credential()
        self._owner(uow, by)
        self._revoke_row(uow, current, by, "admin")
        stored = uow.get_agent_credential(self.org_id, credential_id)
        assert stored is not None
        return self.view(stored)

    def revoke_all(self, agent_id: str, *, by: str) -> int:
        """End every live credential of the agent in this team, oldest first. Returns the count."""
        with self.store.transaction() as uow:
            return self.revoke_all_in(uow, agent_id, by=by)

    def revoke_all_in(self, uow: UnitOfWork, agent_id: str, *, by: str) -> int:
        """`revoke_all` inside the caller's unit of work (R1.35)."""
        self._agent(agent_id)
        now = self.now()
        self._owner(uow, by)
        live = [
            c
            for c in uow.list_agent_credentials(self.org_id)
            if c.agent_id == agent_id and c.team == self.team and c.active(now)
        ]
        return sum(self._revoke_row(uow, c, by, "admin") for c in live)

    # -- the CLI (store access, actor `service:cli`; revoke only) ---------------------------------

    def cli_revoke(self, credential_id: str) -> str | None:
        """Revoke any credential of the organization by id, whatever its team or agent. Returns
        `revoked`, `already` or None when there is no such credential."""
        with self.store.transaction() as uow:
            current = (
                uow.get_agent_credential(self.org_id, credential_id)
                if is_agent_credential_id(credential_id)
                else None
            )
            if current is None:
                return None
            return (
                "revoked" if self._revoke_row(uow, current, "service:cli", "admin") else "already"
            )

    def cli_revoke_all(self, agent_id: str) -> int:
        """Revoke the live credentials of the agent of this team as `service:cli`."""
        now = self.now()
        with self.store.transaction() as uow:
            live = [
                c
                for c in uow.list_agent_credentials(self.org_id)
                if c.agent_id == agent_id and c.team == self.team and c.active(now)
            ]
            return sum(self._revoke_row(uow, c, "service:cli", "admin") for c in live)

    # -- the start sweep (spec 2.6) ---------------------------------------------------------

    def revoke_absent_agents(self) -> int:
        """At every server start, before the port opens: in one transaction revoke (as `system`)
        the active credentials of this organization and team whose agent is not in the
        configuration (`agent_removed`) or whose fingerprint changed (`agent_changed`). Nothing
        of another organization or team is touched. Raises whatever fails: the caller must not
        start the server."""
        now = self.now()
        with self.store.transaction() as uow:
            count = 0
            for c in uow.list_agent_credentials(self.org_id):
                if c.team != self.team or not c.active(now):
                    continue
                known = self.agents.get(c.agent_id)
                if known is None:
                    reason = "agent_removed"
                elif known.fingerprint != c.fingerprint:
                    reason = "agent_changed"
                else:
                    continue
                count += self._revoke_row(uow, c, "system", reason)
            return count
