"""API tokens for programs (R1.22 PR 3; ND53 C, ND56; `docs/architecture-platform.md` §11).

A token is 256 random bits, shown **once** (in the answer that creates it, or on the terminal for
`nuramesh auth tokens create`), stored only as its SHA-256, scoped `read` and/or `run`, expiring
after 90 days by default (a token made by the CLI: 7 days by default, at most 30), revocable
at once. Everything a token does is recorded as `service:<token id>` (T85: never the name, which
is free text and lives only in the token table; `service:cli` is the CLI's own actor class, ND67).
A token is never a `human:` actor, and **no scope lets it approve, lift a budget block, read a
person's reason or manage tokens** (those need a human session, ND56). An owner in a human
session (or the CLI) can rename a token and delete an inactive one (#634): a renamed or deleted
name stays reserved for a bounded time as a keyed hash (`api_token_retired_names`), and the
erasure of the issuer replaces the name.

The secret never reaches a log, a later answer or the ledger: the ledger records
`auth.token_created` and `auth.token_revoked` as minimal facts (ND60: a keyed hash of the token id,
the scopes and the expiry), each with one security-log row; never the secret, its SHA-256, the id
or the name.
Tokens are accepted only in the `Authorization: Bearer` header, never in a query string (which
access logs would keep).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from nuramesh.core.auth import (
    API_TOKEN_LIFETIME,
    IDENTIFIER_KEY_OVERLAP,
    MAX_CLI_TOKEN_LIFETIME,
    ROLE_PERMISSIONS,
    ApiToken,
    IssuedApiToken,
    Permission,
    Role,
    TokenScope,
    address_key,
    epoch_start,
    hash_api_token,
    identifier_epoch,
    issue_api_token,
    looks_like_api_token,
    new_token_name_problem,
    next_erased_token_name,
    token_name_hash,
)
from nuramesh.core.people import is_human_id
from nuramesh.server.limits import RateLimiter
from nuramesh.server.secevents import (
    SecurityRecorder,
    token_created,
    token_deleted,
    token_names_erased,
    token_renamed,
    token_revoked,
)
from nuramesh.server.sessions import AuthProblem
from nuramesh.stores import AlreadyExistsError, Store, UnitOfWork

CLI_ISSUER = "service:cli"
NAME_TAKEN = "a token with this name exists or was used lately; pick another"
BUSY = "the database is busy; retry"
ACTIVE = "revoke the token first; an active token is not deleted"
CLI_DEFAULT_LIFETIME = timedelta(days=7)
FAILED_BEARER_LIMIT = 30  # failed token presentations per client per window, then 429
FAILED_BEARER_WINDOW = 60.0


class TokenProblem(AuthProblem):
    """An error answer from the token endpoints (same shape as every auth error)."""


@dataclass(frozen=True)
class TokenView:
    """What the API shows about a token: never the secret, never its hash."""

    id: str
    name: str
    actor: str
    scopes: list[str]
    created_by: str
    created_at: str
    expires_at: str
    revoked_at: str | None
    active: bool

    @classmethod
    def of(cls, token: ApiToken, now: datetime) -> "TokenView":
        """The public view of a stored token at `now`."""
        return cls(
            id=token.id,
            name=token.name,
            actor=token.actor,
            scopes=sorted(s.value for s in token.scopes),
            created_by=token.created_by,
            created_at=token.created_at.isoformat(),
            expires_at=token.expires_at.isoformat(),
            revoked_at=None if token.revoked_at is None else token.revoked_at.isoformat(),
            active=token.active(now),
        )


class TokenService:
    """Create, list, revoke and check API tokens for one organization."""

    def __init__(
        self,
        store: Store,
        org_id: str,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
    ) -> None:
        self.store, self.org_id, self._clock = store, org_id, clock
        self.recorder = SecurityRecorder(org_id, clock)
        self.failures = RateLimiter(FAILED_BEARER_LIMIT, FAILED_BEARER_WINDOW, monotonic)

    def now(self) -> datetime:
        return self._clock()

    def create(
        self,
        name: str,
        scopes: set[TokenScope],
        *,
        created_by: str,
        lifetime: timedelta | None = None,
    ) -> IssuedApiToken:
        """Issue and store a token, ledgering `auth.token_created` in the same
        transaction. 422 for a bad name, scope set or lifetime; 409 if a live token holds the
        name or it is still reserved after a rename or delete (one answer, no oracle); 503 when
        the organization has no usable identifier key (fail closed)."""
        problem = new_token_name_problem(name)
        if problem is not None:
            raise TokenProblem(422, "invalid_request", problem)
        if not scopes:
            raise TokenProblem(422, "invalid_request", "a token needs at least one scope")
        if lifetime is None:  # 90 days for a person, 7 for the CLI
            lifetime = CLI_DEFAULT_LIFETIME if created_by == CLI_ISSUER else API_TOKEN_LIFETIME
        if created_by == CLI_ISSUER and lifetime > MAX_CLI_TOKEN_LIFETIME:
            raise TokenProblem(
                422,
                "invalid_request",
                f"a token made by the CLI lives at most {MAX_CLI_TOKEN_LIFETIME.days} days",
            )
        now = self.now()
        try:
            issued = issue_api_token(
                self.org_id, name, scopes, created_by=created_by, now=now, lifetime=lifetime
            )
        except ValueError as exc:
            raise TokenProblem(422, "invalid_request", str(exc)) from None
        record = issued.record
        try:
            with self.store.transaction() as uow:
                if is_human_id(created_by):  # the issuer may have been erased or demoted since
                    issuer = uow.get_user(self.org_id, created_by)
                    if (
                        issuer is None
                        or issuer.erased
                        or issuer.disabled
                        or issuer.role is not Role.OWNER
                    ):
                        raise TokenProblem(
                            403, "forbidden", "the issuing person is no longer an owner"
                        )
                if self._retired(uow, name, now):  # the 503 check is inside, before any 409
                    raise TokenProblem(409, "token_name_taken", NAME_TAKEN)
                uow.put_api_token(record)  # AlreadyExistsError: a live clash (ids are random)
                token_created(self.recorder, uow, record)
        except AlreadyExistsError:
            raise TokenProblem(409, "token_name_taken", NAME_TAKEN) from None
        return issued

    def view(self, token: ApiToken) -> TokenView:
        """The public view of one token now."""
        return TokenView.of(token, self.now())

    def list(self) -> list[TokenView]:
        """Every token of the organization, oldest first (views only)."""
        now = self.now()
        return [TokenView.of(t, now) for t in self.store.list_api_tokens(self.org_id)]

    def revoke(self, token_id: str, *, by: str) -> TokenView:
        """Revoke a token at once (the next request with it fails), ledgering `auth.token_revoked`.
        Revoking a revoked token answers its record and writes nothing. 404 if unknown."""
        with self.store.transaction() as uow:
            return self.revoke_in(uow, token_id, by=by)

    def revoke_in(self, uow: UnitOfWork, token_id: str, *, by: str) -> TokenView:
        """`revoke` inside the caller's unit of work (R1.35): opens no transaction. The view is
        built from the token this call read, with `revoked_at` set to the transaction time when
        this call revoked it (a read after the write would miss it on the Memory store)."""
        now = self.now()
        current = uow.get_api_token(self.org_id, token_id)
        if current is None:
            raise TokenProblem(404, "not_found", "no such token")
        if uow.revoke_api_token(self.org_id, token_id, now, by):
            token_revoked(self.recorder, uow, current, by)
            current = current.model_copy(update={"revoked_at": now, "revoked_by": by})
        return TokenView.of(current, now)

    # -- names (#634) ---------------------------------------------------------------------

    def _name_keys(self, uow: UnitOfWork, now: datetime) -> tuple[tuple[int, bytes], ...]:
        """The organization keys a retired name is checked under, current epoch first: the key of
        the current epoch and, only during the lockout window after it began, the previous
        epoch's key (read, never made; R16). 503 `store_busy` when the current key was destroyed
        and tombstoned (no usable key): nothing can be hashed, so the answer never depends on a
        name (fail closed)."""
        epoch = identifier_epoch(now)
        key = uow.org_identifier_key(self.org_id, epoch)
        if key is None:
            raise TokenProblem(503, "store_busy", BUSY)
        keys: tuple[tuple[int, bytes], ...] = ((epoch, key),)
        if now < epoch_start(epoch) + IDENTIFIER_KEY_OVERLAP:
            previous = uow.get_org_identifier_key(self.org_id, epoch - 1)
            if previous is not None:
                keys += ((epoch - 1, previous),)
        return keys

    def _retired(self, uow: UnitOfWork, name: str, now: datetime) -> bool:
        keys = self._name_keys(uow, now)
        return uow.api_token_name_retired(
            self.org_id, [(e, token_name_hash(k, self.org_id, name)) for e, k in keys]
        )

    def _retire(self, uow: UnitOfWork, name: str, now: datetime) -> None:
        epoch, key = self._name_keys(uow, now)[0]
        uow.retire_api_token_name(self.org_id, token_name_hash(key, self.org_id, name), epoch, now)

    def _normalised(self, uow: UnitOfWork, current: ApiToken) -> ApiToken:
        """R9: the row with a gone person's id and personal name replaced, before the caller's
        own change. Reads the row again only when something changed."""
        if uow.normalise_api_token_issuer(self.org_id, current.id) == 0:
            return current
        after = uow.get_api_token(self.org_id, current.id)
        assert after is not None
        return after

    @staticmethod
    def _worth_reserving(before: ApiToken, after: ApiToken) -> bool:
        """The old name is reserved unless R9 just replaced it or it already is an erasure
        replacement name (R43: neither was a person's choice)."""
        if after.name != before.name:
            return False
        return next_erased_token_name(before.id, frozenset(), current=before.name) != before.name

    def rename(self, token_id: str, name: str, *, by: str) -> TokenView:
        """Give a token a new name (any state). See `rename_in`."""
        with self.store.transaction() as uow:
            return self.rename_in(uow, token_id, name, by=by)

    def rename_in(self, uow: UnitOfWork, token_id: str, name: str, *, by: str) -> TokenView:
        """`rename` inside the caller's unit of work. Order (R12): 404 for no such row of this
        organization; the current name is 200 and writes nothing; 422 for a bad name; 503 when no
        key can hash; 409 for a name held by another row or still reserved; then R9, the change,
        the old name reserved (never a name R9 or the erasure replaced) and one
        `auth.token_renamed` with its log row."""
        current = uow.get_api_token(self.org_id, token_id)
        if current is None:
            raise TokenProblem(404, "not_found", "no such token")
        now = self.now()
        if name == current.name:
            return TokenView.of(current, now)
        problem = new_token_name_problem(name)
        if problem is not None:
            raise TokenProblem(422, "invalid_request", problem)
        if self._retired(uow, name, now):
            raise TokenProblem(409, "token_name_taken", NAME_TAKEN)
        row = self._normalised(uow, current)
        try:
            renamed = uow.rename_api_token(self.org_id, token_id, name)
        except AlreadyExistsError:
            raise TokenProblem(409, "token_name_taken", NAME_TAKEN) from None
        if not renamed:  # pragma: no cover - the row was read in this transaction
            raise TokenProblem(404, "not_found", "no such token")
        if self._worth_reserving(current, row):
            self._retire(uow, current.name, now)
        token_renamed(self.recorder, uow, row, by)
        return TokenView.of(row.model_copy(update={"name": name}), now)

    def delete(self, token_id: str, *, by: str) -> TokenView:
        """Delete an inactive token. See `delete_in`."""
        with self.store.transaction() as uow:
            return self.delete_in(uow, token_id, by=by)

    def delete_in(self, uow: UnitOfWork, token_id: str, *, by: str) -> TokenView:
        """`delete` inside the caller's unit of work. Order (R17): 404; 409 `token_active` for a
        token not revoked and not past `expires_at` (the instant itself still counts as active);
        503 when no key can hash; then R9, the delete (PostgreSQL decides on `least(now,
        database now)`: a 0 there is 409 and the transaction rolls back), the name reserved and
        one `auth.token_deleted` with its log row. The answer is the row as R9 left it."""
        current = uow.get_api_token(self.org_id, token_id)
        if current is None:
            raise TokenProblem(404, "not_found", "no such token")
        now = self.now()
        if current.revoked_at is None and now <= current.expires_at:
            raise TokenProblem(409, "token_active", ACTIVE)
        self._name_keys(uow, now)  # 503 before anything is written
        row = self._normalised(uow, current)
        if not uow.delete_api_token(self.org_id, token_id, now):
            raise TokenProblem(409, "token_active", ACTIVE)
        if self._worth_reserving(current, row):
            self._retire(uow, current.name, now)
        token_deleted(self.recorder, uow, row, by)
        return TokenView.of(row, now)

    def scrub_erased(self, *, by: str = CLI_ISSUER) -> int:
        """Operator command (R10): the erasure rewrite for every row of this organization whose
        `created_by` or `revoked_by` is a person who is erased or has no user row. Never revokes.
        Returns the rows changed; above zero it writes one `auth.token_names_erased` (`origin:
        cli`) with its log row."""
        with self.store.transaction() as uow:
            gone: dict[str, set[str]] = {}
            # the unit of work has no token list primitive; the read is by the store and every
            # write below is one transaction, which the one-writer store serializes (MINOR-4).
            for row in self.store.list_api_tokens(self.org_id):
                for who in (row.created_by, row.revoked_by):
                    if who is not None and is_human_id(who):
                        user = uow.get_user(self.org_id, who)
                        if user is None or user.erased:
                            gone.setdefault(who, set()).add(row.id)
            rows: set[str] = set().union(*gone.values()) if gone else set()
            for who in sorted(gone):
                uow.erase_api_token_issuer(self.org_id, who)
            token_names_erased(self.recorder, uow, len(rows), actor=by, origin="cli")
            return len(rows)

    def authenticate_with_rights(
        self, presented: str, client: str
    ) -> tuple[ApiToken, frozenset[Permission]]:
        """The active token behind a bearer value and its effective permissions now: its scopes
        intersected with its issuing person's current role (`spec/auth/README.md` 4a), or 401
        `token_invalid` (unknown, expired, revoked and issuer gone look the same; more than 30
        failures a minute from one client answer 429). An issuer who is gone or erased makes the
        token invalid. Only `service:cli` has no person: exactly the token's scopes. Any other
        `created_by` is refused."""
        token: ApiToken | None = None
        if looks_like_api_token(presented):
            try:
                digest = hash_api_token(presented)
            except UnicodeEncodeError:
                digest = ""
            token = self.store.get_api_token_by_hash(self.org_id, digest) if digest else None
        if token is not None and token.active(self.now()):
            rights = token.permissions()
            if is_human_id(token.created_by):
                issuer = self.store.get_user(self.org_id, token.created_by)
                if issuer is not None and not issuer.erased and not issuer.disabled:
                    return token, rights & ROLE_PERMISSIONS[issuer.role]
            elif token.created_by == CLI_ISSUER:
                return token, rights
        wait = self.failures.allow(address_key(client))
        if wait > 0:
            raise TokenProblem(
                429,
                "rate_limited",
                "too many failed token attempts; slow down",
                {"Retry-After": str(max(1, int(wait + 0.999)))},
            )
        raise TokenProblem(
            401,
            "token_invalid",
            "the API token is unknown, expired or revoked",
            {"WWW-Authenticate": 'Bearer realm="nuramesh", error="invalid_token"'},
        )
