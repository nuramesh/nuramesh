"""Login, sessions, refresh families, setup-token redemption (R1.22 PR 2; spec/auth/sessions.md).

`AuthService` holds the rules; `nuramesh.server.auth` wires them to HTTP, and
`nuramesh.server.authz` uses `authenticate` and `require_csrf` for every other route (PR 3).

Order of work on a login, so a flood cannot reach the expensive step:

1. per-client rate limit (memory, bounded);
2. stored lockout counters for the client and the account (a restart keeps them): in **one
   transaction** the lock is checked and one failure is **reserved** on both counters, so
   concurrent attempts cannot all pass the check before any of them is counted;
3. a free hashing slot (no waiting: 429 at once; the reservation is given back);
4. only then the password hash. A missing user and a wrong password run the same code
   (`verify_unknown` does one scrypt), answer with the same body, and keep the reserved failure.
   A success gives the client's reservation back and deletes the account's counter.

Security events (ND60): the ledger gets **minimal facts only**, the event type, its time, the
opaque actor when known and **keyed hashes** of identifiers (`core.auth.identifier_hash`, kind
`identifier`), keyed per subject: a known user's identifiers (email, session ids) with that user's
own identifier key, which erasure deletes; anything else (an unknown email, a client address)
with the organization's key of the current 90-day epoch, destroyed when the epoch ends. Events:
`auth.login_failed`, `auth.lockout`,
`auth.session_created`, `auth.session_revoked` (logout is one of its reasons) and
`auth.setup_token_used`. The raw details (the email as typed, the client address, the user
agent, counts) go to the mutable, expiring `security_log` table, written in the same transaction
as the ledger event. Refusals that change nothing (rate limit, busy, locked) write nothing.
"""

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from nuramesh.core.auth import (
    DEFAULT_PARAMS,
    IDENTIFIER_KEY_OVERLAP,
    LOGIN_VIA,
    MAX_AUTH_TOKEN_LENGTH,
    REFRESH_GRACE_MAX,
    REFRESH_SUCCESSOR_KEY_MIN,
    SESSION_ABSOLUTE,
    SESSION_RETENTION,
    SETUP_TOKEN_LIFETIME,
    AuthFailure,
    AuthSession,
    AuthToken,
    Credential,
    IssuedSession,
    IssuedSetupToken,
    LockoutPolicy,
    Role,
    ScryptParams,
    SecurityDetail,
    SecurityLogEntry,
    SetupTokenCheck,
    TokenKind,
    User,
    address_key,
    address_text,
    check_setup_token,
    csrf_matches,
    csrf_token,
    email_text,
    epoch_start,
    hash_auth_token,
    hash_password,
    hash_setup_token,
    identifier_epoch,
    identifier_hash,
    issue_setup_token,
    issue_tokens,
    needs_rehash,
    new_session_id,
    new_user_id,
    normalize_email,
    parse_hash,
    password_problem,
    successor_tokens,
    verify_password,
    verify_unknown,
)
from nuramesh.core.idempotency import sweep_expired
from nuramesh.core.identity import InviteStatus, invite_status
from nuramesh.core.ledger import EventType, format_time
from nuramesh.core.mfa import (
    MFA_ENROL_CHALLENGE_MAX,
    MfaChallenge,
    challenge_hash,
    new_challenge_token,
)
from nuramesh.engine.halt_refusal import fold_expired_pending
from nuramesh.server.limits import HashGate, RateLimiter
from nuramesh.server.mfa_keys import KeyRing
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.secevents import (
    SecurityRecorder,
    agent_credentials_issuer_erased,
    revoke_all_agent_credentials,
    revoke_issued_tokens,
    token_issuers_erased,
    token_revoked,
)
from nuramesh.stores import EventDraft, Store, UnitOfWork

if TYPE_CHECKING:
    from nuramesh.engine.content import ContentService

GENERIC_LOGIN_FAILURE = "the email or password is not right"
GENERIC_SETUP_FAILURE = "this setup token can't be used (wrong, used or expired)"
_LOG = logging.getLogger(__name__)
PURGE_EVERY_SECONDS = 3600.0
PURGE_RETRY_SECONDS = 60.0
"""After a failed retention purge the next attempt waits this long (ND148)."""


def _is_email(text: str) -> bool:
    """True when the text is a valid address (normalize_email accepts it)."""
    try:
        normalize_email(text)
    except ValueError:
        return False
    return True


# #1484 SU-2: method -> (sets `password_confirmed_at`, the factor written to `mfa_method` or None).
# A passkey sets only `step_up_at` until R2.16 decides its factor (SU-9).
STEP_UP_METHODS: dict[str, tuple[bool, str | None]] = {
    "password": (True, None),
    "password+totp": (True, "totp"),
    "password+recovery": (True, "recovery"),
    "totp": (False, "totp"),
    "recovery": (False, "recovery"),
    "passkey": (False, None),
}


class AuthProblem(Exception):
    """An auth error answer: HTTP status, stable code, message, optional extra headers."""

    def __init__(
        self, status: int, code: str, detail: str, headers: dict[str, str] | None = None
    ) -> None:
        self.status, self.code, self.detail = status, code, detail
        self.headers = headers or {}
        self.caller: object | None = None
        """Who was refused, when the refusal came after authentication (set by the authorizer)."""
        super().__init__(detail)


class LastOwnerRefused(AuthProblem):
    """A refusal because the target is the last active owner (E1 D4): the caller that catches it
    writes `auth.last_owner_refused` after the refused transaction has rolled back."""

    def __init__(self, status: int, code: str, detail: str, target: str) -> None:
        super().__init__(status, code, detail)
        self.target = target


@dataclass(frozen=True)
class AuthSettings:
    """Tunables of the auth endpoints; every field has the production default."""

    trusted_proxy: TrustedProxies | bool = (
        False  # forwarded headers, believed only from these peers
    )
    # memory the concurrent password hashes may use together: four default hashes (~128 MiB)
    hash_memory_budget: int = 4 * DEFAULT_PARAMS.memory_bytes
    ip_rate_limit: int = 30  # login/setup/refresh requests per client per window
    ip_rate_window: float = 60.0
    account_lockout: LockoutPolicy = field(default_factory=LockoutPolicy)
    ip_lockout: LockoutPolicy = field(default_factory=lambda: LockoutPolicy(threshold=20))
    clock: Callable[[], datetime] | None = None  # None: the store's clock (ND243)
    monotonic: Callable[[], float] = time.monotonic
    scrypt: ScryptParams = DEFAULT_PARAMS
    # a just-spent refresh token presented again inside this window gets the same new pair (T109);
    # 0 (the default here) is off. The deployment default of 30 s is set by `nuramesh serve`.
    refresh_grace: timedelta = timedelta(0)
    # server secret (at least 32 random bytes) the successor pair is derived under; required when
    # the window is on, never stored in the database, shared by every replica (T109, spec 11.7)
    refresh_successor_key: bytes | None = field(default=None, repr=False)
    # MFA (R1.38): the auth key files that seal TOTP secrets (None: enrolment is off, mfa.md 2a),
    # the name shown in the authenticator app and the life of a login challenge in seconds
    mfa_keys: KeyRing | None = None
    mfa_issuer: str = "NuraMesh"
    mfa_challenge_seconds: int = 300

    def __post_init__(self) -> None:
        if not timedelta(0) <= self.refresh_grace <= REFRESH_GRACE_MAX:
            raise ValueError("the refresh grace window must be between 0 and 30 seconds")
        if self.refresh_grace > timedelta(0) and (
            self.refresh_successor_key is None
            or len(self.refresh_successor_key) < REFRESH_SUCCESSOR_KEY_MIN
        ):
            raise ValueError(
                "refresh_successor_key (at least 32 bytes) is required when the refresh grace "
                "window is on"
            )


@dataclass(frozen=True)
class Principal:
    """Who a valid session belongs to."""

    session: AuthSession
    user: User


@dataclass(frozen=True)
class Started:
    """A session just started or refreshed: the records and the secrets for the cookies."""

    session: AuthSession
    user: User
    issued: IssuedSession


@dataclass(frozen=True)
class MfaPending:
    """A correct password that needs a second step (mfa.md section 3): no session yet, only a
    challenge. `methods` is None for an enrolment challenge."""

    status: str  # "mfa_required" or "mfa_enroll_required"
    challenge: str = field(repr=False)
    expires_in: int
    methods: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _Reserved:
    """One failure reserved on a lockout counter before hashing: the counter as it was and as
    the reservation wrote it, so a success (or a refusal) can give it back."""

    scope: str  # "client" or "account"
    key: str
    policy: LockoutPolicy
    previous: AuthFailure | None
    reserved: AuthFailure


@dataclass(frozen=True)
class SetupOutcome:
    """What `issue_setup_token` did: `issued` (token set, shown once), `token_open` (one exists
    that cannot be shown again), `owner_exists` (no token is needed) or `closed` (T128: the
    organization was closed)."""

    status: str
    issued: IssuedSetupToken | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    retired: int = 0
    """How many open (unused, unexpired) tokens issuing this one retired (SA-0.9)."""


OPERATOR_ACTOR = "service:cli"
RECOVERY_COMMANDS = ("erase-owner", "close-org", "mfa-reset", "auth-key-destroy")
"""The ledger actor of an operator action done on the server with the CLI (T128)."""


@dataclass(frozen=True)
class NewOwner:
    """A new owner the operator creates while transferring ownership (T128). The email, name and
    password are personal data or secrets: kept out of `repr`."""

    email: str = field(repr=False)
    display_name: str | None = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class OperatorErasure:
    """The result of `operator_erase_owner`: whether the user was erased now, and the id of the
    owner who took over (None when nothing was erased)."""

    erased: bool
    new_owner_id: str | None


class AuthService:
    """The session rules over a store, for one organization."""

    def __init__(
        self,
        store: Store,
        org_id: str,
        settings: AuthSettings | None = None,
        content: "ContentService | None" = None,
    ) -> None:
        self.store, self.org_id = store, org_id
        # with a content service, erasing a person also scrubs the operator free text linked to
        # them in the stored configuration (ND218); None where there is no content store
        self.content = content
        self.settings = settings or AuthSettings()
        self.gate = HashGate(self.settings.hash_memory_budget)
        verify_unknown("warm-up", self.settings.scrypt)  # build the dummy hash now, not at login
        self.limiter = RateLimiter(
            self.settings.ip_rate_limit, self.settings.ip_rate_window, self.settings.monotonic
        )
        self._next_purge = float("-inf")

    # -- helpers -------------------------------------------------------------------------

    def now(self) -> datetime:
        return (self.settings.clock or self.store.now)()

    def limit_client(self, client: str) -> None:
        """Rate limit per client address: 429 with `Retry-After` when over."""
        wait = self.limiter.allow(address_key(client))
        if wait > 0:
            raise AuthProblem(
                429,
                "rate_limited",
                "too many requests; slow down",
                {"Retry-After": str(max(1, math.ceil(wait)))},
            )

    @property
    def stream(self) -> str:
        """The organization's ledger stream, where the auth events go."""
        return f"org:{self.org_id}"

    def _event(self, uow: UnitOfWork, kind: EventType, actor: str, payload: dict[str, Any]) -> None:
        uow.append(EventDraft(stream=self.stream, type=kind, actor=actor, payload=payload))

    def _key(self, uow: UnitOfWork, user_id: str | None = None) -> bytes:
        """The identifier key for a new hash (ND60), inside the event's own transaction: the
        user's key when the identifier belongs to a known, not-erased user, else the
        organization's key of the current epoch (made at its first use)."""
        key = None if user_id is None else uow.user_identifier_key(self.org_id, user_id)
        if key is None:
            key = uow.org_identifier_key(self.org_id, identifier_epoch(self.now()))
        if key is None:  # the epoch's key was destroyed: only if the clock went backwards
            raise RuntimeError("the identifier key of this epoch was destroyed")
        return key

    def _email_hash(self, uow: UnitOfWork, email: str, user_id: str | None) -> str:
        return identifier_hash(self._key(uow, user_id), self.org_id, email_text(email))

    def _address_hash(self, uow: UnitOfWork, client: str) -> str:
        return identifier_hash(self._key(uow), self.org_id, address_text(client))

    def _session_hash(self, uow: UnitOfWork, session: AuthSession) -> str:
        return identifier_hash(self._key(uow, session.user_id), self.org_id, session.id)

    @staticmethod
    def _seen(client: str | None, user_agent: str | None) -> dict[str, SecurityDetail]:
        """The raw request facts for the security log: the canonical client address (the form
        that is hashed) and the user agent."""
        return {
            "client_address": None if client is None else address_text(client),
            "user_agent": user_agent,
        }

    def _record(
        self,
        uow: UnitOfWork,
        kind: EventType,
        actor: str,
        payload: dict[str, Any],
        *,
        subject: str | None = None,
        details: dict[str, SecurityDetail] | None = None,
    ) -> None:
        """A security event: the minimal fact in the ledger and **exactly one** security log row
        with the raw details (`email`, `client_address`, `user_agent` where they apply), in the
        same transaction (`uow`). The row's actor is the event's (`system` for system events);
        its `subject` is the user the event concerns, whenever that user is known (ND60), so
        erasure finds the row even when the actor is `system`."""
        self._event(uow, kind, actor, payload)
        uow.append_security_log(
            SecurityLogEntry.new(
                self.org_id,
                self.now(),
                kind.value,
                actor=actor,
                subject=subject,
                details=details,
            )
        )

    def _lockouts(
        self,
        uow: UnitOfWork,
        started: dict[str, AuthFailure],
        hashes: dict[str, str],
        seen: dict[str, SecurityDetail],
        user_id: str | None = None,
    ) -> None:
        """One `auth.lockout` per lock this failure started, the account's first (ND60), each
        after the failure's `auth.login_failed`."""
        for scope in ("account", "client", "mfa"):
            if scope not in started:
                continue
            details = dict(seen)
            if scope == "client":
                details.pop("email", None)
            until = started[scope].locked_until
            details["scope"] = scope
            details["locked_until"] = None if until is None else format_time(until)
            self._record(
                uow,
                EventType.AUTH_LOCKOUT,
                "system",
                {"scope": scope, "identifier_hash": hashes[scope]},
                subject=user_id if scope in ("account", "mfa") else None,
                details=details,
            )

    @staticmethod
    def _too_many(wait: int) -> AuthProblem:
        return AuthProblem(
            429,
            "too_many_attempts",
            "too many failed attempts; try again later",
            {"Retry-After": str(wait)},
        )

    def _locked(self, client: str, now: datetime) -> None:
        """The client lock only (setup redemption hashes nothing before its token check)."""
        with self.store.transaction() as uow:
            keys = self._counter_keys(uow, now, client, None, None)
            wait = max(
                (policy.retry_after(uow.get_auth_failure(key), now) for _, key, policy in keys),
                default=0,
            )
        if wait > 0:
            raise self._too_many(wait)

    def _count_failure(self, now: datetime, client: str, seen: dict[str, SecurityDetail]) -> None:
        """Count a failure that needed no hashing (a wrong setup token, client key only), with
        its ledger events and security log rows in the same transaction."""
        with self.store.transaction() as uow:
            keys = self._counter_keys(uow, now, client, None, None)
            started: dict[str, AuthFailure] = {}
            details: dict[str, SecurityDetail] = {**seen, "via": "setup_token"}
            for scope, key, policy in keys:
                failure = policy.after_failure(uow.get_auth_failure(key), key, now)
                uow.put_auth_failure(failure)
                details[f"{scope}_failures"] = failure.count
                if failure.locked_until is not None:  # counted only while unlocked: a new lock
                    started[scope] = failure
            hashes = {"client": self._address_hash(uow, client)}
            self._record(
                uow,
                EventType.AUTH_LOGIN_FAILED,
                "system",
                {"identifier_hash": hashes["client"], "via": "setup_token"},
                details=details,
            )
            self._lockouts(uow, started, hashes, details)
        self._maybe_purge()  # unknown-email rows go even if nobody ever logs in successfully

    def _reserve(
        self, now: datetime, client: str, email: str, user_id: str | None
    ) -> list[_Reserved]:
        """Check the locks and reserve one failure on every key in **one transaction** (the
        stores serialise writers), so of N concurrent attempts at most those below the threshold
        pass; the rest see the lock and are refused before hashing. Locked: 429, nothing
        written."""
        with self.store.transaction() as uow:
            keys = self._counter_keys(uow, now, client, email, user_id)
            current = [(s, k, policy, uow.get_auth_failure(k)) for s, k, policy in keys]
            wait = max((policy.retry_after(f, now) for _, _, policy, f in current), default=0)
            if wait > 0:
                raise self._too_many(wait)  # rolls back: a refused attempt is not counted
            reserved = []
            for scope, key, policy, previous in current:
                failure = policy.after_failure(previous, key, now)
                uow.put_auth_failure(failure)
                reserved.append(_Reserved(scope, key, policy, previous, failure))
        return reserved

    @staticmethod
    def _give_back(uow: UnitOfWork, reservation: _Reserved) -> None:
        """Undo one reserved failure. If nothing touched the counter since, it is restored
        exactly (a lock the reservation started goes too). If concurrent attempts changed it, one
        is taken off the count; a lock stays only while the remaining count is still at or above
        the threshold (otherwise no counted failure justifies it, and it is cleared)."""
        current = uow.get_auth_failure(reservation.key)
        if current is None:
            return
        if current == reservation.reserved:
            if reservation.previous is None:
                uow.delete_auth_failure(reservation.key)
            else:
                uow.put_auth_failure(reservation.previous)
            return
        count = current.count - 1
        if count < 1:
            uow.delete_auth_failure(reservation.key)
            return
        lock = current.locked_until if count >= reservation.policy.threshold else None
        uow.put_auth_failure(current.model_copy(update={"count": count, "locked_until": lock}))

    def _settle_failure(
        self,
        reserved: list[_Reserved],
        user_id: str | None,
        email: str,
        client: str,
        seen: dict[str, SecurityDetail],
    ) -> None:
        """Turn the reservations into a counted failure and record it in **one transaction**:
        `auth.login_failed` (the email's identifier hash, `via: password`), then an
        `auth.lockout` for each lock this failure started that is still there as this transaction
        sees the counters (a counter a concurrent success deleted meanwhile starts no lock). If
        the transaction fails, the reservations are given back: neither a counter nor an event is
        left."""
        try:
            with self.store.transaction() as uow:
                started: dict[str, AuthFailure] = {}
                details: dict[str, SecurityDetail] = {**seen, "via": "password"}
                raced = False
                if user_id is not None:  # read again: an erasure may have landed since the lookup
                    current_user = uow.get_user(self.org_id, user_id)
                    if current_user is None or current_user.erased:
                        user_id, raced = None, True  # an unknown email, with no email in the row
                if not raced and _is_email(email):  # a typed non-email is never kept (ND320.1)
                    details["email"] = email_text(email)
                for r in reserved:
                    if raced and r.scope == "account":
                        # the counter was reserved under the erased user's key, which is gone: give
                        # it back (that counter alone is never ledgered as a lockout) and count
                        # this failure as an unknown email, like any other: not at all while that
                        # counter is locked, and every lock it starts or lengthens is ledgered.
                        self._give_back(uow, r)
                        now = self.now()
                        unknown = self._counter_key(uow, now, "acct", email_text(email))
                        before = uow.get_auth_failure(unknown)
                        if r.policy.retry_after(before, now) > 0:
                            details["account_failures"] = 0 if before is None else before.count
                            continue
                        failure = r.policy.after_failure(before, unknown, now)
                        uow.put_auth_failure(failure)
                        details["account_failures"] = failure.count
                        if failure.locked_until is not None:
                            started["account"] = failure
                        continue
                    current = uow.get_auth_failure(r.key)
                    if current is None:  # a concurrent success cleared it: not counted here
                        continue
                    details[f"{r.scope}_failures"] = current.count
                    if r.reserved.locked_until is not None and current.locked_until is not None:
                        started[r.scope] = current
                hashes = {
                    "account": self._email_hash(uow, email, user_id),
                    "client": self._address_hash(uow, client),
                }
                self._record(
                    uow,
                    EventType.AUTH_LOGIN_FAILED,
                    "system",
                    {"identifier_hash": hashes["account"], "via": "password"},
                    subject=user_id,
                    details=details,
                )
                self._lockouts(uow, started, hashes, details, user_id)
        except BaseException:
            self._release(reserved)
            raise

    def _release(self, reserved: list[_Reserved]) -> None:
        with self.store.transaction() as uow:
            for reservation in reserved:
                self._give_back(uow, reservation)

    def _previous_org_key(self, uow: UnitOfWork, now: datetime) -> bytes | None:
        """The previous epoch's organization key, **only** during the lockout window after its
        epoch ended (`now < epoch start + 3600 s`), to find counters written under it; never
        afterwards, whether or not the expiry job has destroyed it yet, and never created."""
        epoch = identifier_epoch(now)
        if now >= epoch_start(epoch) + IDENTIFIER_KEY_OVERLAP:
            return None
        return uow.get_org_identifier_key(self.org_id, epoch - 1)

    def _counter_key(
        self, uow: UnitOfWork, now: datetime, prefix: str, text: str, user_id: str | None = None
    ) -> str:
        """A lockout counter key: `prefix:` and the identifier's keyed hash of `text` (already
        `email_text` or `address_text`; no address or email is stored). A known user's account
        uses the user's key (it does not rotate); anything else the organization's current key,
        and a counter written under the previous epoch's key is **carried over** to the new one
        during the lockout window, so counting does not reset at the boundary."""
        user_key = None if user_id is None else uow.user_identifier_key(self.org_id, user_id)
        if user_key is not None:
            return f"{prefix}:{identifier_hash(user_key, self.org_id, text)}"
        key = f"{prefix}:{identifier_hash(self._key(uow), self.org_id, text)}"
        previous = self._previous_org_key(uow, now)
        if previous is not None:
            old = f"{prefix}:{identifier_hash(previous, self.org_id, text)}"
            carried = uow.get_auth_failure(old)
            if carried is not None:
                if uow.get_auth_failure(key) is None:
                    uow.put_auth_failure(carried.model_copy(update={"key": key}))
                uow.delete_auth_failure(old)
        return key

    def _counter_keys(
        self,
        uow: UnitOfWork,
        now: datetime,
        client: str,
        email: str | None,
        user_id: str | None,
    ) -> list[tuple[str, str, LockoutPolicy]]:
        address = address_key(client)
        keys = [("client", self._counter_key(uow, now, "ip", address), self.settings.ip_lockout)]
        if email is not None:
            account = self._counter_key(uow, now, "acct", email_text(email), user_id)
            keys.append(("account", account, self.settings.account_lockout))
        return keys

    def lockout_key(self, scope: str, value: str, *, user_id: str | None = None) -> str:
        """The counter key a `client` address or an `account` email has now (for operators and
        tests; it may carry a counter over from the previous epoch, like a login would)."""
        with self.store.transaction() as uow:
            if scope == "client":
                return self._counter_key(uow, self.now(), "ip", address_key(value))
            return self._counter_key(uow, self.now(), "acct", email_text(value), user_id)

    def _maybe_purge(self) -> None:
        mono = self.settings.monotonic()
        if mono < self._next_purge:
            return
        try:
            self.purge()
            self._next_purge = mono + PURGE_EVERY_SECONDS
        except Exception:
            self._next_purge = mono + PURGE_RETRY_SECONDS  # not on every login, refresh, logout
            _LOG.warning("the retention purge failed; it will be retried", exc_info=True)
        self.sweep_idempotency()

    def sweep_idempotency(self) -> int:
        """Housekeeping for expired idempotency rows (batches of 1,000 while a batch is full, a
        bounded number of batches; R1.35 section 10, ND148). A failed sweep is logged and never
        affects a request."""
        try:
            return sweep_expired(
                lambda limit: self.store.purge_idempotency(self.now(), limit=limit)
            )
        except Exception:
            _LOG.warning("the idempotency sweep failed", exc_info=True)
            return 0

    def purge(self) -> int:
        """Retention: delete session records ended 90 days ago or more, stale counters, and
        security log rows at or past their expiry (written + 90 days, ND60). Rows removed."""
        now = self.now()
        with self.store.transaction() as uow:
            removed = uow.purge_auth_records(
                now - SESSION_RETENTION, now - self.settings.account_lockout.decay
            )
            # T154: pending halt-refusal counts enter the ledger (or the day row) before the purge
            fold_expired_pending(uow)
            removed += uow.purge_security_log(now)
            removed += uow.purge_agent_credentials(now)  # E2 (Q11): ended rows after 90 days
            removed += uow.purge_invites(now)  # R1.37 (Q8/Q9): ended invites after 90 days
            removed += uow.purge_mfa_records(now)  # R1.38 (Q9): challenges +1 h, pending +10 min
            removed += uow.purge_email_changes(now)  # R1.40 (Q9): 7 days after the end
            removed += uow.purge_password_reset(now)  # R1.41 (Q9): tokens and links, 7 days
            # an organization key is destroyed once its epoch and the 3600 s lockout window after
            # it have passed (crypto-shredding); it is never used after that even before this runs
            oldest_usable = identifier_epoch(now - IDENTIFIER_KEY_OVERLAP)
            return removed + uow.destroy_org_identifier_keys(oldest_usable)

    def _revoke(
        self,
        uow: UnitOfWork,
        session: AuthSession,
        now: datetime,
        reason: str,
        actor: str = "system",
        seen: dict[str, SecurityDetail] | None = None,
    ) -> bool:
        """Revoke one session and record `auth.session_revoked` in the same transaction. The
        actor (ND60): the session's person for `logout`, the admin for `admin`, `system` for
        `refresh_reuse`, `superseded` and `user_erased`. False (and nothing recorded) if it was
        revoked already: the first reason stays."""
        if not uow.revoke_auth_session(self.org_id, session.id, now, reason):
            return False
        who = {"logout": session.user_id, "admin": actor, "disabled": actor}.get(reason, "system")
        self._record(
            uow,
            EventType.AUTH_SESSION_REVOKED,
            who,
            {"session_hash": self._session_hash(uow, session), "reason": reason},
            subject=session.user_id,
            details={**(seen or {}), "reason": reason},
        )
        return True

    def _start(
        self,
        uow: UnitOfWork,
        user: User,
        now: datetime,
        login_via: str,
        mfa_method: str | None = None,
    ) -> Started:
        session = AuthSession(
            org_id=self.org_id,
            id=new_session_id(),
            user_id=user.id,
            created_at=now,
            last_active_at=now,
            absolute_expires_at=now + SESSION_ABSOLUTE,
            login_via=login_via,
            mfa_verified_at=None if mfa_method is None else now,
            mfa_method=mfa_method,
            # SU-3, the one sign-in writer (W1) of `step_up_at`: the insert carries it, there is
            # no second write. The SSO half of SU-3 (a provider sign-in sets it too) is #962's:
            # until then only password sessions are created here (F9).
            step_up_at=now if login_via == "password" else None,
        )
        issued = issue_tokens(session, now)
        uow.put_auth_session(session)
        uow.put_auth_token(issued.access)
        uow.put_auth_token(issued.refresh)
        return Started(session, user, issued)

    # -- login ---------------------------------------------------------------------------

    def login(
        self,
        email: str,
        password: str,
        client: str,
        previous_refresh: str | None,
        *,
        user_agent: str | None = None,
    ) -> Started:
        """`begin_login` for a caller that has no second step to offer: a login that needs one
        is a `RuntimeError` (the session was not made)."""
        got = self.begin_login(email, password, client, previous_refresh, user_agent=user_agent)
        if isinstance(got, MfaPending):
            raise RuntimeError("this login needs a second factor; use begin_login")
        return got

    def begin_login(
        self,
        email: str,
        password: str,
        client: str,
        previous_refresh: str | None,
        *,
        user_agent: str | None = None,
    ) -> Started | MfaPending:
        """Check the credentials and start a **new** session (or, when a second factor is needed,
        answer a challenge and start none: `MfaPending`). Any session the request already
        carried (`previous_refresh`) is revoked: an id set beforehand is never adopted.
        `user_agent` goes to the security log only (ND60)."""
        seen = self._seen(client, user_agent)
        self.limit_client(client)
        now = self.now()
        user_row = self.store.get_user_by_email(self.org_id, email)
        if user_row is not None and user_row.erased:
            user_row = None
        known = None if user_row is None else user_row.id
        reserved = self._reserve(now, client, email, known)  # 429 if locked; else counted now
        try:
            # R1.41 Q15: an owner or CLI reset refuses the old password until a reset is redeemed
            blocked = (
                user_row is not None
                and self.store.get_password_login_block(self.org_id, user_row.id) is not None
            )
            stored = None
            credential = (
                self.store.get_credential(self.org_id, user_row.id)
                if user_row is not None
                else None
            )
            # the slot is sized by the real cost: the stored hash's own parameters (a hash that
            # parses can need up to 1 GiB) or, for the rehash and the unknown user, the current
            parsed = None if credential is None else parse_hash(credential.password_hash)
            cost = max(
                self.settings.scrypt.memory_bytes,
                0 if parsed is None else parsed.params.memory_bytes,
            )
            with self.gate.slot(cost) as got:
                if not got:
                    raise AuthProblem(
                        429, "busy", "the server is busy; try again", {"Retry-After": "1"}
                    )
                if user_row is not None and credential is not None:
                    ok = verify_password(password, credential.password_hash)
                    stored = credential.password_hash
                else:
                    ok = verify_unknown(password, self.settings.scrypt)
                upgraded = (
                    hash_password(password, self.settings.scrypt)
                    if ok
                    and stored is not None
                    and (user_row is None or not user_row.disabled)
                    and not blocked
                    and needs_rehash(stored, self.settings.scrypt)
                    else None
                )
        except BaseException:
            self._release(reserved)  # busy or an error: not an attempt that counts
            raise
        if not ok or user_row is None or user_row.disabled or blocked:
            # a disabled person fails exactly like a wrong password (E1 D2, X-07): the hash was
            # checked above, so the work and the answer are the same
            self._settle_failure(reserved, known, email, client, seen)
            self._maybe_purge()  # unknown-email rows go even if nobody ever logs in successfully
            raise AuthProblem(401, "invalid_credentials", GENERIC_LOGIN_FAILURE)
        try:
            started = self._finish_login(reserved, user_row, upgraded, previous_refresh, now, seen)
        except BaseException:
            self._release(reserved)  # the password was right: the session failed, not the person
            raise
        self._maybe_purge()
        return started

    def confirm_password(
        self, user_id: str, password: str, client: str, *, user_agent: str | None = None
    ) -> None:
        """A signed-in person enters the password again (MFA enrol, regenerate and disable,
        mfa.md section 4): checked like a login password (client limiter, hashing budget, the
        failure counted on the account and client counters, 401 `invalid_credentials`). Success
        clears nothing: a stolen session must not reset the account's brute-force counter."""
        seen = self._seen(client, user_agent)
        self.limit_client(client)
        now = self.now()
        user_row = self.store.get_user(self.org_id, user_id)
        if user_row is None or user_row.erased or user_row.disabled or user_row.email is None:
            raise AuthProblem(401, "session_invalid", "sign in again")
        email = user_row.email
        reserved = self._reserve(now, client, email, user_row.id)
        try:
            credential = self.store.get_credential(self.org_id, user_row.id)
            parsed = None if credential is None else parse_hash(credential.password_hash)
            cost = max(
                self.settings.scrypt.memory_bytes,
                0 if parsed is None else parsed.params.memory_bytes,
            )
            with self.gate.slot(cost) as got:
                if not got:
                    raise AuthProblem(
                        429, "busy", "the server is busy; try again", {"Retry-After": "1"}
                    )
                if credential is not None:
                    ok = verify_password(password, credential.password_hash)
                else:
                    ok = verify_unknown(password, self.settings.scrypt)
        except BaseException:
            self._release(reserved)
            raise
        if not ok:
            self._settle_failure(reserved, user_row.id, email, client, seen)
            raise AuthProblem(401, "invalid_credentials", GENERIC_LOGIN_FAILURE)
        self._release(reserved)

    def confirm_session_password(
        self,
        user_id: str,
        session_id: str,
        password: str,
        client: str,
        *,
        user_agent: str | None = None,
    ) -> None:
        """`POST /v1/auth/password/confirm` (spec/rbac/solo-approval.md SA-8): the password is
        checked by `confirm_password`, unchanged (a wrong one is 401, counted and ledgered as a
        failed login; a locked account is 429; success clears no counter). On success, in one
        transaction: the calling session's `password_confirmed_at` is set to now (only that
        session) and `auth.password_confirmed` is appended (actor the person, payload `{}`). No
        security-log row is written and nothing portable is issued. The person and the session
        are read again inside the transaction: ended or disabled meanwhile is 401."""
        self.confirm_password(user_id, password, client, user_agent=user_agent)
        now = self.now()
        with self.store.transaction() as uow:
            user = uow.get_user(self.org_id, user_id)
            session = uow.get_auth_session(self.org_id, session_id)
            if (
                user is None
                or user.erased
                or user.disabled
                or session is None
                or session.user_id != user_id
                or not session.active(now)
            ):
                raise AuthProblem(401, "session_invalid", "sign in again")
            uow.set_auth_session_password_confirmed(self.org_id, session_id, now)
            self._event(uow, EventType.AUTH_PASSWORD_CONFIRMED, user_id, {})

    def step_up_session(self, user_id: str, session_id: str, method: str) -> None:
        """The one writer of `step_up_at` after sign-in (spec/auth/step-up-vs-password-confirmed.md
        SU-2, W2, option A of #1484). The caller has ALREADY verified the proof (this takes no
        credential). In one transaction: the person and the session are read again (ended, expired,
        another person's or another organization's session, or a disabled or erased person is 401
        `session_invalid`, nothing written); `step_up_at` is set to now, `password_confirmed_at` too
        when the method included the password, and the factor columns when it included a factor
        (`mfa_verified_at` = now: a stored later time is overwritten, SP-10); `auth.step_up
        {method}` is appended. A primitive that answers false (a concurrent revoke) raises and rolls
        everything back."""
        if method not in STEP_UP_METHODS:
            raise ValueError(f"unknown step-up method {method!r}")
        password, factor = STEP_UP_METHODS[method]
        now = self.now()
        with self.store.transaction() as uow:
            user = uow.get_user(self.org_id, user_id)
            session = uow.get_auth_session(self.org_id, session_id)
            if (
                user is None
                or user.erased
                or user.disabled
                or session is None
                or session.user_id != user_id
                or not session.active(now)
            ):
                raise AuthProblem(401, "session_invalid", "sign in again")
            done = uow.set_auth_session_step_up(self.org_id, session_id, now)
            if password:
                done = done and uow.set_auth_session_password_confirmed(
                    self.org_id, session_id, now
                )
            if factor is not None:
                done = done and uow.set_auth_session_mfa(self.org_id, session_id, now, factor)
            if not done:
                raise AuthProblem(401, "session_invalid", "sign in again")
            self._event(uow, EventType.AUTH_STEP_UP, user_id, {"method": method})

    def _finish_login(
        self,
        reserved: list[_Reserved],
        user_row: User,
        upgraded: str | None,
        previous_refresh: str | None,
        now: datetime,
        seen: dict[str, SecurityDetail],
    ) -> Started | MfaPending:
        """One transaction: clear the counters, store an upgraded hash, then read the factor, the
        policy and the role and either issue a challenge (no session) or end a carried session,
        start the session and record `auth.session_created`. The user is read again inside the
        transaction: if an erasure got in between (the password was checked outside it), this is a
        failed login, nothing personal is written and the reservations are given back by the
        caller."""
        with self.store.transaction() as uow:
            current = uow.get_user(self.org_id, user_row.id)
            if current is None or current.erased or current.disabled:
                raise AuthProblem(401, "invalid_credentials", GENERIC_LOGIN_FAILURE)
            user_row = current
            for reservation in reserved:
                if reservation.scope == "account":
                    uow.delete_auth_failure(reservation.key)  # the account's counter is cleared
                else:
                    self._give_back(uow, reservation)  # the client's earlier failures stay
            if upgraded is not None:
                uow.put_credential(
                    Credential(
                        org_id=self.org_id,
                        user_id=user_row.id,
                        password_hash=upgraded,
                        updated_at=now,
                    )
                )
            pending = self._mfa_gate(uow, user_row, now)
            if pending is not None:
                return pending
            return self.open_session(uow, user_row, now, seen, previous_refresh=previous_refresh)

    def open_session(
        self,
        uow: UnitOfWork,
        user: User,
        now: datetime,
        seen: dict[str, SecurityDetail],
        *,
        previous_refresh: str | None = None,
        mfa_method: str | None = None,
    ) -> Started:
        """Inside a transaction: end a carried session, start a password session and record
        `auth.session_created` (payload unchanged) and, after a second factor,
        `auth.mfa_verified` (mfa.md section 5)."""
        if previous_refresh is not None:
            self._revoke_by_refresh(uow, previous_refresh, now, "superseded", seen=seen)
        started = self._start(uow, user, now, "password", mfa_method)
        digest = self._session_hash(uow, started.session)
        self._record(
            uow,
            EventType.AUTH_SESSION_CREATED,
            user.id,
            {"session_hash": digest},
            subject=user.id,
            details={**seen, "email": user.email},
        )
        if mfa_method is not None:
            self._record(
                uow,
                EventType.AUTH_MFA_VERIFIED,
                user.id,
                {"session_hash": digest, "method": mfa_method},
                subject=user.id,
                details={**seen, "email": user.email},
            )
        return started

    def mfa_available(self) -> bool:
        keys = self.settings.mfa_keys
        return keys is not None and keys.available()

    def _mfa_gate(self, uow: UnitOfWork, user: User, now: datetime) -> MfaPending | None:
        """The login decision (mfa.md section 3), read and acted on in the login transaction so a
        session is never made from a stale "no factor" reading: an active factor asks for a code;
        an owner without one, under the policy, must enrol; otherwise None (an ordinary login)."""
        factor = uow.get_mfa_factor(self.org_id, user.id)
        if factor is not None and factor.active:
            purpose: Literal["verify", "enrol"] = "verify"
            factor_id: str | None = factor.id
            life = self.settings.mfa_challenge_seconds
        elif user.role == Role.OWNER and uow.get_org_auth_settings(self.org_id).mfa_required:
            if not self.mfa_available():
                raise AuthProblem(
                    503, "mfa_unavailable", "multi-factor sign-in is not available on this server"
                )
            purpose, factor_id = "enrol", None
            life = min(
                self.settings.mfa_challenge_seconds, int(MFA_ENROL_CHALLENGE_MAX.total_seconds())
            )
        else:
            return None
        token = new_challenge_token()
        uow.put_mfa_challenge(
            MfaChallenge(
                org_id=self.org_id,
                token_hash=challenge_hash(token),
                user_id=user.id,
                purpose=purpose,
                factor_id=factor_id,
                created_at=now,
                expires_at=now + timedelta(seconds=life),
            )
        )
        if purpose == "verify":
            return MfaPending("mfa_required", token, life, ("totp", "recovery_code"))
        return MfaPending("mfa_enroll_required", token, life)

    # -- sessions ------------------------------------------------------------------------

    def _revoke_by_refresh(
        self,
        uow: UnitOfWork,
        refresh: str,
        now: datetime,
        reason: str,
        actor: str = "system",
        seen: dict[str, SecurityDetail] | None = None,
    ) -> bool:
        if len(refresh) > MAX_AUTH_TOKEN_LENGTH:
            return False
        token = uow.get_auth_token(self.org_id, hash_auth_token(refresh))
        if token is None or token.kind is not TokenKind.REFRESH:
            return False
        session = uow.get_auth_session(self.org_id, token.session_id)
        return session is not None and self._revoke(uow, session, now, reason, actor, seen)

    def authenticate(self, access: str | None) -> Principal:
        """The session behind an access token, or `AuthProblem(401, session_invalid)`. Revoked,
        expired and unknown all look the same, and a revocation counts on the very next request
        because the store is read every time."""
        invalid = AuthProblem(401, "session_invalid", "sign in again")
        if not access or len(access) > MAX_AUTH_TOKEN_LENGTH:
            raise invalid
        now = self.now()
        token = self.store.get_auth_token(self.org_id, hash_auth_token(access))
        if token is None or token.kind is not TokenKind.ACCESS or now >= token.expires_at:
            raise invalid
        session = self.store.get_auth_session(self.org_id, token.session_id)
        if session is None or not session.active(now):
            raise invalid
        user = self.store.get_user(self.org_id, session.user_id)
        if user is None or user.erased or user.disabled:
            raise invalid
        return Principal(session, user)

    def require_csrf(
        self, refresh: str | None, presented: str | None, *, session_id: str | None = None
    ) -> None:
        """Every state-changing request made with a cookie session carries `X-CSRF-Token`:
        refuse (403) when it is missing or not the session's, compared in constant time. With
        `session_id` (every route except refresh and logout) the refresh cookie must also be a
        live REFRESH token of that very session: a made-up or foreign pair is refused."""
        if (
            not refresh
            or not presented
            or len(presented) > MAX_AUTH_TOKEN_LENGTH
            or not csrf_matches(presented, refresh)
        ):
            raise AuthProblem(403, "csrf_invalid", "missing or wrong CSRF token")
        if session_id is not None:
            if len(refresh) > MAX_AUTH_TOKEN_LENGTH:
                raise AuthProblem(403, "csrf_invalid", "missing or wrong CSRF token")
            token = self.store.get_auth_token(self.org_id, hash_auth_token(refresh))
            if (
                token is None
                or token.kind is not TokenKind.REFRESH
                or token.used_at is not None
                or token.session_id != session_id
            ):
                raise AuthProblem(403, "csrf_invalid", "missing or wrong CSRF token")

    def csrf_for(self, principal: Principal, refresh: str | None) -> str | None:
        """The CSRF token, only from a live refresh cookie of **this** session: a refresh cookie
        of another session (or a spent or unknown one) yields None."""
        if not refresh or len(refresh) > MAX_AUTH_TOKEN_LENGTH:
            return None
        token = self.store.get_auth_token(self.org_id, hash_auth_token(refresh))
        if (
            token is None
            or token.kind is not TokenKind.REFRESH
            or token.used_at is not None
            or token.session_id != principal.session.id
        ):
            return None
        return csrf_token(refresh)

    def refresh(
        self, refresh: str | None, *, client: str | None = None, user_agent: str | None = None
    ) -> Started:
        """Rotate: a valid refresh token is spent and replaced by a new access and refresh pair.
        A refresh token that was **already spent** is reuse (a copy was stolen, or two tabs
        raced): the whole family is revoked. The caller rate-limits the client first."""
        invalid = AuthProblem(401, "session_invalid", "sign in again")
        if not refresh or len(refresh) > MAX_AUTH_TOKEN_LENGTH:
            raise invalid
        now = self.now()
        digest = hash_auth_token(refresh)
        reuse = False
        started: Started | None = None
        with self.store.transaction() as uow:
            token = uow.get_auth_token(self.org_id, digest)
            if token is None or token.kind is not TokenKind.REFRESH:
                raise invalid
            session = uow.get_auth_session(self.org_id, token.session_id)
            if session is None:
                raise invalid
            spent = token.used_at is not None or not uow.use_auth_token(self.org_id, digest, now)
            if spent:
                token = uow.get_auth_token(self.org_id, digest) or token
                replayed = self._replay(uow, refresh, token, session, now)
                if replayed is not None:
                    return replayed
                reuse = True
                self._revoke_by_refresh(
                    uow,
                    refresh,
                    now,
                    "refresh_reuse",
                    seen=self._seen(client, user_agent),
                )
            elif not session.active(now) or now >= token.expires_at:
                raise invalid
            else:
                user = uow.get_user(self.org_id, session.user_id)
                if user is None or user.erased or user.disabled:
                    raise invalid
                why = self._refresh_refusal(uow, session, user)
                if why is None:
                    issued = issue_tokens(session, now, successor=self._successor(digest))
                    uow.put_auth_token(issued.access)
                    uow.put_auth_token(issued.refresh)
                    uow.touch_auth_session(self.org_id, session.id, now)
                    started = Started(session, user, issued)
                else:  # revoked and recorded; the 401 is raised after the commit
                    self._revoke(uow, session, now, why, seen=self._seen(client, user_agent))
        if reuse or started is None:
            raise invalid
        return started

    def _refresh_refusal(self, uow: UnitOfWork, session: AuthSession, user: User) -> str | None:
        """Why a refresh must end the session (mfa.md section 3): a session with no known
        `login_via` is a defect (never read as password or oidc); a password session of an owner
        without an active factor under the MFA policy. None: the refresh goes on."""
        if session.login_via not in LOGIN_VIA:
            _LOG.error("session %s has no valid login_via; revoking", session.id)
            return "login_via_missing"
        if session.login_via == "oidc":
            return None
        if user.role != Role.OWNER or not uow.get_org_auth_settings(self.org_id).mfa_required:
            return None
        factor = uow.get_mfa_factor(self.org_id, user.id)
        return None if factor is not None and factor.active else "mfa_required"

    def _successor(self, spent_hash: str) -> tuple[bytes, str] | None:
        """The key and spent-token hash to derive the next pair from; None (random tokens) when
        the grace window is off, since nothing will ever replay them."""
        key = self.settings.refresh_successor_key
        if key is None or self.settings.refresh_grace <= timedelta(0):
            return None
        return key, spent_hash

    def _replay(
        self,
        uow: UnitOfWork,
        refresh: str,
        spent: AuthToken,
        session: AuthSession,
        now: datetime,
    ) -> Started | None:
        """The pair a rotation already issued, for a second request with the same spent token
        inside the grace window (T109). None means this is reuse. Writes nothing. A session that is
        over (revoked, past its end) or a user who is erased is refused outright, not reuse."""
        grace = self.settings.refresh_grace
        if spent.used_at is None or not now < spent.used_at + grace:
            return None
        key = self.settings.refresh_successor_key
        if key is None:
            return None
        access_token, refresh_token = successor_tokens(key, hash_auth_token(refresh), session.id)
        successor = uow.get_auth_token(self.org_id, hash_auth_token(refresh_token))
        access = uow.get_auth_token(self.org_id, hash_auth_token(access_token))
        if (
            successor is None
            or access is None
            or successor.session_id != session.id
            or successor.used_at is not None
            or now >= successor.expires_at
        ):
            return None
        if not session.active(now):
            raise AuthProblem(401, "session_invalid", "sign in again")
        user = uow.get_user(self.org_id, session.user_id)
        if user is None or user.erased or user.disabled:
            raise AuthProblem(401, "session_invalid", "sign in again")
        return Started(session, user, IssuedSession(access_token, refresh_token, access, successor))

    def logout(
        self, refresh: str | None, *, client: str | None = None, user_agent: str | None = None
    ) -> bool:
        """End the session of this refresh token. True if one was active."""
        if not refresh:
            return False
        seen = self._seen(client, user_agent)
        with self.store.transaction() as uow:
            return self._revoke_by_refresh(uow, refresh, self.now(), "logout", seen=seen)

    def erase_user(
        self,
        user_id: str,
        actor: str,
        *,
        uow: UnitOfWork | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Erase a user (T64) and end all their sessions at once, ledgering `user.erased` with
        the opaque id only. `actor` is the human or `system` doing it. False if nothing to erase.

        One transaction, in this order (ND60, `spec/auth/README.md` section 3): (1) the user's
        live sessions are revoked (`user_erased`), each with its event and a log row (subject the
        user), written while the identifier key still exists; (2) every security log row whose
        subject is the user, or whose email is theirs, is deleted (including the rows of step 1),
        and a row where the user is only the actor is kept with the actor `erased-user` and no
        client address, user agent or email; (2b) every stored content row that names the user
        is deleted (R1.32), or with the organization on legal hold marked for the next expiry
        pass, and one `content.erased` entry is written (counts and kinds only); (3) the email
        and name are cleared and the credential deleted; (4) the user's identifier key is
        deleted, so their hashes in the ledger can no longer be matched (once all backups older
        than the erasure have expired);
        (5) `user.erased` is appended.

        The last active owner is never erased (409 `last_owner`, T126): the check runs before the
        transaction starts and again inside it, before any write.

        `uow` and `now` are for the operator paths (T128) and org closure, which erase inside
        their own transaction and replace the last-owner rule with their own check: the steps run
        in that transaction at that time and the last-owner checks are skipped. This stays the
        only caller of the store primitive."""
        if uow is None or now is None:
            now = self.now()
            try:
                self.require_not_last_owner(user_id)
                with self.store.transaction() as own:
                    user = own.get_user(self.org_id, user_id)
                    if user is None or user.erased:
                        return False
                    self._require_other_owner(self.store.list_users(self.org_id), user_id)
                    return self.erase_user(user_id, actor, uow=own, now=now)
            except LastOwnerRefused as refused:
                self._record_last_owner_refused(actor, "erase", refused.target)
                raise
        user = uow.get_user(self.org_id, user_id)
        if user is None or user.erased:
            return False
        for session in self.store.list_auth_sessions(self.org_id):
            if session.user_id == user_id and session.active(now):
                self._revoke(uow, session, now, "user_erased", actor)
        revoke_issued_tokens(
            SecurityRecorder(self.org_id, self.now),
            uow,
            self.store.list_api_tokens(self.org_id),
            user_id,
            now,
        )
        agent_credentials_issuer_erased(SecurityRecorder(self.org_id, self.now), uow, user_id, now)
        token_issuers_erased(SecurityRecorder(self.org_id, self.now), uow, user_id)  # 1c (#634)
        self.revoke_creator_invites(uow, user_id)  # 3b: before created_by becomes erased-user
        uow.erase_security_log(self.org_id, user_id, user.email)
        self._erase_content_rows(uow, user_id, actor)
        if self.content is not None:  # operator free text linked to the person (ND218)
            self.content.scrub_person_config(uow, self.org_id, user_id)
        # steps 3 and 4: the store clears the personal data and deletes the identifier key
        if not uow.erase_user(self.org_id, user_id, now):  # pragma: no cover - checked by callers
            return False
        uow.erase_mfa_user(self.org_id, user_id)  # step 3b: factor, codes, challenges, policy actor
        uow.detach_user_from_invites(self.org_id, user_id)  # 3b: no invite row names the person
        uow.delete_email_changes_for_user(self.org_id, user_id)  # 3b: the pending new address goes
        uow.erase_password_reset(self.org_id, user_id)  # 3b: tokens, links and the login block
        self._event(uow, EventType.USER_ERASED, actor, {"user": user_id})
        return True

    def _erase_content_rows(self, uow: UnitOfWork, user_id: str, actor: str) -> None:
        """R1.32 S4: delete every stored content row that names the person (no key is needed to
        delete ciphertext), or, with the organization on legal hold, mark them for the next
        expiry pass. Every call writes one `content.erased` entry (counts and kind names only,
        never the person's id), even when nothing was found."""
        count, kinds, skipped = uow.erase_content_subject(self.org_id, user_id)
        shown = "erased-user" if actor == user_id else actor
        self._event(
            uow,
            EventType.CONTENT_ERASED,
            shown,
            {
                "scope": "subject",
                "count": count,
                "kinds": kinds,
                "skipped": skipped,
                "actor": shown,
            },
        )

    def operator_erase_owner(
        self,
        user_id: str,
        *,
        promote: str | None = None,
        new_owner: NewOwner | None = None,
        actor: str = OPERATOR_ACTOR,
    ) -> OperatorErasure:
        """Operator path (T128), see `_operator_erase_owner`. A refusal because the user is the
        last active owner is ledgered as `auth.last_owner_refused` (ND200 Q8) after the rollback."""
        try:
            return self._operator_erase_owner(
                user_id, promote=promote, new_owner=new_owner, actor=actor
            )
        except LastOwnerRefused as refused:
            self._record_last_owner_refused(actor, "erase", refused.target)
            raise

    def _operator_erase_owner(
        self,
        user_id: str,
        *,
        promote: str | None = None,
        new_owner: NewOwner | None = None,
        actor: str = OPERATOR_ACTOR,
    ) -> OperatorErasure:
        """Operator path (T128): erase a user, usually the sole owner, after handing ownership to
        a named active user (`promote`, their id) or to a new one (`new_owner`), in ONE
        transaction: either the new owner exists and the user is erased, or nothing changes. The
        organization is never left without an owner. There is no HTTP route; the ledger records
        `user.created` (via `operator`) or `user.role_changed`, then `user.erased`, all by
        `actor`, with opaque ids only. False (and nothing written) if the user is unknown or
        erased already."""
        if (promote is None) == (new_owner is None):
            raise AuthProblem(422, "invalid_request", "give exactly one of promote or new owner")
        now = self.now()
        fresh: User | None = None
        credential: Credential | None = None
        if new_owner is not None:
            problem = password_problem(new_owner.password)
            if problem is not None:
                raise AuthProblem(422, "invalid_request", problem)
            try:
                address = normalize_email(new_owner.email)
            except ValueError:
                raise AuthProblem(422, "invalid_request", "not a valid email address") from None
            fresh = User(
                org_id=self.org_id,
                id=new_user_id(),
                email=address,
                display_name=new_owner.display_name or None,
                role=Role.OWNER,
                created_at=now,
            )
            with self.gate.slot(self.settings.scrypt.memory_bytes) as got:
                if not got:  # the operator path never waits and never skips the hash
                    raise AuthProblem(
                        429, "busy", "the server is busy; try again", {"Retry-After": "1"}
                    )
                hashed = hash_password(new_owner.password, self.settings.scrypt)
            credential = Credential(
                org_id=self.org_id,
                user_id=fresh.id,
                password_hash=hashed,
                updated_at=now,
            )
        with self.store.transaction() as uow:
            user = uow.get_user(self.org_id, user_id)
            if user is None or user.erased:
                return OperatorErasure(False, None)
            if fresh is not None and credential is not None:
                if uow.get_user_by_email(self.org_id, fresh.email or "") is not None:
                    raise AuthProblem(409, "email_taken", "a user with this email exists")
                uow.create_user(fresh)
                uow.put_credential(credential)
                self._event(
                    uow,
                    EventType.USER_CREATED,
                    actor,
                    {"user": fresh.id, "role": Role.OWNER.value, "via": "operator"},
                )
                successor_id = fresh.id
            else:
                assert promote is not None
                successor = uow.get_user(self.org_id, promote)
                if (
                    successor is None
                    or successor.erased
                    or successor.disabled
                    or successor.id == user_id
                ):
                    detail = "the new owner must be another active user"
                    if (
                        successor is not None
                        and successor.disabled
                        and not successor.erased
                        and self._sole_active_owner(user)
                    ):  # a disabled owner is no successor: the last active owner stays (D4)
                        raise LastOwnerRefused(422, "invalid_request", detail, user.id)
                    raise AuthProblem(422, "invalid_request", detail)
                if successor.role != Role.OWNER:
                    uow.set_user_role(self.org_id, successor.id, Role.OWNER)
                    self._event(
                        uow,
                        EventType.USER_ROLE_CHANGED,
                        actor,
                        {"user": successor.id, "from": successor.role.value, "to": "owner"},
                    )
                    self.revoke_promoted(uow, successor.id)
                successor_id = successor.id
            self.erase_user(user.id, actor, uow=uow, now=now)
        return OperatorErasure(True, successor_id)

    def record_recovery_code_issued(self, actor: str, expires_at: datetime) -> None:
        """Ledger and security-log that an operator recovery code was issued (T128): the actor
        (`operator:<os user>`), the team (organization id) and the expiry; never the code or
        anything derived from it. Runs before the code is shown; if it fails, no code is left."""
        with self.store.transaction() as uow:
            SecurityRecorder(self.org_id, self.now).record(
                uow,
                EventType.OPERATOR_RECOVERY_CODE_ISSUED,
                actor,
                {"team": self.org_id, "expires_at": expires_at.isoformat()},
                details={"via": "recovery-code issue"},
            )

    def record_recovery_code_used(self, command: str, actor: str) -> None:
        """Ledger and security-log that an operator recovery code was spent for `command`
        (`RECOVERY_COMMANDS`), in its own transaction, before the action's. Opaque:
        the command's name and the actor (`operator:<os user>`); never the code or anything
        derived from it."""
        if command not in RECOVERY_COMMANDS:
            raise ValueError(f"unknown operator command: {command!r}")
        with self.store.transaction() as uow:
            SecurityRecorder(self.org_id, self.now).record(
                uow,
                EventType.OPERATOR_RECOVERY_CODE_USED,
                actor,
                {"command": command},
                details={"via": command},
            )

    def close_organization(self, actor: str = OPERATOR_ACTOR) -> int:
        """Operator path (T128): close the organization. In one transaction every active session
        and API token ends, every live agent credential of the organization ends as `system`
        (reason `org_closed`), every active user is erased (`user.erased` each) and `org.closed`
        is appended with the counts. Returns the users erased; 0 writes nothing. A closed
        organization (`org_closed`) never gets a setup token or a first owner again. The ledger
        stays (ND53: it goes only when the organization is deleted, a separate path)."""
        now = self.now()
        with self.store.transaction() as uow:
            active = [u for u in self.store.list_users(self.org_id) if not u.erased]
            if not active:
                return 0
            rec = SecurityRecorder(self.org_id, self.now)
            for token in self.store.list_api_tokens(self.org_id):
                if token.active(now) and uow.revoke_api_token(self.org_id, token.id, now, actor):
                    token_revoked(rec, uow, token, actor)
            ended = revoke_all_agent_credentials(rec, uow, now, "org_closed")
            for user in active:
                self.erase_user(user.id, actor, uow=uow, now=now)
            self._event(
                uow,
                EventType.ORG_CLOSED,
                actor,
                {"users_erased": len(active), "agent_credentials_revoked": ended},
            )
        return len(active)

    def org_closed(self) -> bool:
        """Whether the organization was closed: an owner existed once and no active user is left.
        Nobody can sign in then, and no setup token or first owner may be made."""
        return self.owner_ever_existed() and not self.owner_exists()

    @staticmethod
    def _require_other_owner(users: list[User], user_id: str) -> None:
        target = next((u for u in users if u.id == user_id), None)
        if target is None or target.erased or target.disabled or target.role != Role.OWNER:
            return  # not an active owner: removing them cannot leave the organization without one
        if not AuthService._other_active_owner(users, user_id):
            raise LastOwnerRefused(
                409,
                "last_owner",
                "this is the last active owner of the organization; transfer ownership to "
                "another owner first",
                user_id,
            )

    @staticmethod
    def _other_active_owner(users: list[User], user_id: str) -> bool:
        return any(
            u.role == Role.OWNER and not u.erased and not u.disabled and u.id != user_id
            for u in users
        )

    def _sole_active_owner(self, target: User) -> bool:
        """Whether `target` is an active owner and no other active owner exists (D4)."""
        if target.role != Role.OWNER or target.erased or target.disabled:
            return False
        return not self._other_active_owner(self.store.list_users(self.org_id), target.id)

    def _record_last_owner_refused(self, actor: str, action: str, target: str) -> None:
        """`auth.last_owner_refused` in its own short transaction after the refusal (ND200 Q8).
        If it cannot be written the refusal still stands and one ERROR line is logged."""
        try:
            with self.store.transaction() as uow:
                self._record(
                    uow,
                    EventType.AUTH_LAST_OWNER_REFUSED,
                    actor,
                    {"action": action, "user": target},
                    subject=target,
                )
        except Exception:
            logging.getLogger("nuramesh.server").error(
                "could not record auth.last_owner_refused (the refusal stands)"
            )

    def require_not_last_owner(self, user_id: str) -> None:
        """Refuse (409 `last_owner`) any operation that would leave the organization without an
        active owner: erasing the user, and any future deactivation or change of role. A user who
        is not an active owner is always fine (T126)."""
        self._require_other_owner(self.store.list_users(self.org_id), user_id)

    # -- people, roles, disable and enable (E1 PR 1; spec/e1 sections 9.1 and 10) -------------

    def list_people(self) -> list[User]:
        """Every person of the organization who is not erased, ordered by id."""
        return [u for u in self.store.list_users(self.org_id) if not u.erased]

    def _active_owner(self, uow: UnitOfWork, actor: str) -> None:
        """The actor, read again inside the write transaction (D4): a person disabled, erased or
        no longer an owner since the request was authenticated is refused (401)."""
        me = uow.get_user(self.org_id, actor)
        if me is None or me.erased or me.disabled or me.role != Role.OWNER:
            raise AuthProblem(401, "session_invalid", "sign in again")

    def _target(self, uow: UnitOfWork, user_id: str) -> User:
        """The person acted on, or 404 `no such user` (unknown, erased and other-organization
        look the same, `spec/auth/org-scope.md` R2)."""
        target = uow.get_user(self.org_id, user_id)
        if target is None or target.erased:
            raise AuthProblem(404, "not_found", "no such user")
        return target

    def require_person(self, user_id: str) -> None:
        """404 `no such user` unless the person exists in this organization and is not erased."""
        found = self.store.get_user(self.org_id, user_id)
        if found is None or found.erased:
            raise AuthProblem(404, "not_found", "no such user")

    def disable_user(self, actor: str, user_id: str) -> bool:
        """An owner disables a person (E1 D1): in one transaction every live session of the
        person ends (`disabled`), every live API token they issued is revoked, `user.disabled`
        is ledgered, all by `actor`, and (D5) one `agent.credential_issuer_disabled` if the person
        issued a live agent credential (which stays valid). False (nothing written) if already
        disabled. The last active owner is never disabled; a refusal for that reason is ledgered
        as `auth.last_owner_refused` (ND200 Q8)."""
        try:
            return self._disable_user(actor, user_id)
        except LastOwnerRefused as refused:
            self._record_last_owner_refused(actor, "disable", refused.target)
            raise

    def _disable_user(self, actor: str, user_id: str) -> bool:
        with self.store.transaction() as uow:
            return self.disable_user_in(uow, actor, user_id)

    def revoke_creator_invites(self, uow: UnitOfWork, creator: str) -> int:
        """Invites section 1b: every pending invite `creator` made is revoked (`creator_changed`)
        in the caller's transaction, one `user.invite_revoked` each (actor `system`)."""
        now = self.now()
        count = 0
        for invite in uow.list_invites(self.org_id):
            if invite.created_by != creator or invite_status(invite, now) != InviteStatus.PENDING:
                continue
            if uow.revoke_invite(self.org_id, invite.id, now, "creator_changed"):
                self._record(
                    uow,
                    EventType.USER_INVITE_REVOKED,
                    "system",
                    {
                        "invite_hash": self._key_hash(uow, invite.id),
                        "reason": "creator_changed",
                    },
                )
                count += 1
        return count

    def _key_hash(self, uow: UnitOfWork, text: str) -> str:
        return identifier_hash(self._key(uow), self.org_id, text)

    def disable_user_in(self, uow: UnitOfWork, actor: str, user_id: str) -> bool:
        """`disable_user` inside the caller's unit of work (R1.35 `_in` variant): opens no
        transaction and records no refusal; a caller that catches `LastOwnerRefused` after the
        rollback calls `record_last_owner_refused`."""
        now = self.now()
        self._active_owner(uow, actor)
        target = self._target(uow, user_id)
        if user_id == actor:
            if self._sole_active_owner(target):
                raise LastOwnerRefused(403, "forbidden", "you cannot disable yourself", user_id)
            raise AuthProblem(403, "forbidden", "you cannot disable yourself")
        if target.disabled:
            return False
        if target.role == Role.OWNER:
            self._require_other_owner(self.store.list_users(self.org_id), user_id)
        uow.set_user_disabled(self.org_id, user_id, now)
        if target.role == Role.OWNER:
            self.revoke_creator_invites(uow, user_id)
        for session in self.store.list_auth_sessions(self.org_id):
            if session.user_id == user_id and session.active(now):
                self._revoke(uow, session, now, "disabled", actor)
        revoke_issued_tokens(
            SecurityRecorder(self.org_id, self.now),
            uow,
            self.store.list_api_tokens(self.org_id),
            user_id,
            now,
            by=actor,
        )
        self._record(uow, EventType.USER_DISABLED, actor, {"user": user_id}, subject=user_id)
        live = [
            c
            for c in uow.list_agent_credentials(self.org_id)
            if c.created_by == user_id and c.active(now)
        ]
        if live:  # D5: they stay valid; the owners are told (and can revoke them one by one)
            SecurityRecorder(self.org_id, self.now).record(
                uow,
                EventType.AGENT_CREDENTIAL_ISSUER_DISABLED,
                "system",
                {"count": len(live), "agents": sorted({c.agent_id for c in live})},
            )
        return True

    def enable_user(self, actor: str, user_id: str) -> bool:
        """An owner enables a disabled person (E1 D3). Nothing that was revoked comes back.
        False (nothing written) if the person is not disabled."""
        with self.store.transaction() as uow:
            return self.enable_user_in(uow, actor, user_id)

    def enable_user_in(self, uow: UnitOfWork, actor: str, user_id: str) -> bool:
        """`enable_user` inside the caller's unit of work (R1.35 `_in` variant)."""
        self._active_owner(uow, actor)
        target = self._target(uow, user_id)
        if not target.disabled:
            return False
        uow.set_user_disabled(self.org_id, user_id, None)
        self._record(uow, EventType.USER_ENABLED, actor, {"user": user_id}, subject=user_id)
        return True

    def set_role(self, actor: str, user_id: str, role: str) -> bool:
        """An owner sets a person's role to `viewer` or `approver` (E1 section 10, checks in the
        spec's order). False (nothing written) if the role is already that."""
        new = self.assignable_role(role)
        try:
            with self.store.transaction() as uow:
                return self.set_role_in(uow, actor, user_id, new)
        except LastOwnerRefused as refused:
            self._record_last_owner_refused(actor, "demote", refused.target)
            raise

    @staticmethod
    def assignable_role(role: str) -> Role:
        """The `Role` for a requested name, or 422 (`invalid_request`, `role_not_assignable`)."""
        if role not in ("viewer", "approver", "owner"):
            raise AuthProblem(422, "invalid_request", "the request is invalid")
        if role == "owner":
            raise AuthProblem(422, "role_not_assignable", "an owner is not made through the API")
        return Role(role)

    def record_last_owner_refused(self, actor: str, action: str, target: str) -> None:
        """Write `auth.last_owner_refused` after a refused transaction rolled back (ND200 Q8)."""
        self._record_last_owner_refused(actor, action, target)

    def set_role_in(self, uow: UnitOfWork, actor: str, user_id: str, new: Role) -> bool:
        """`set_role` after validation, inside the caller's unit of work (R1.35 `_in` variant)."""
        self._active_owner(uow, actor)
        target = self._target(uow, user_id)
        if target.id == actor or target.role == Role.OWNER:
            detail = "an owner's role changes only by the operator"
            if self._sole_active_owner(target):  # any role change of an owner is a demotion
                raise LastOwnerRefused(403, "forbidden", detail, user_id)
            raise AuthProblem(403, "forbidden", detail)
        if target.disabled:
            raise AuthProblem(409, "person_disabled", "enable the person first")
        if target.role == new:
            return False
        self._apply_role(uow, actor, target, new)
        return True

    def _apply_role(self, uow: UnitOfWork, actor: str, target: User, new: Role) -> None:
        """Write the role change and its event; an owner who stops being one loses their pending
        invites (invites section 1b, `creator_changed`) in the same transaction."""
        uow.set_user_role(self.org_id, target.id, new)
        self._record(
            uow,
            EventType.USER_ROLE_CHANGED,
            actor,
            {"user": target.id, "from": target.role.value, "to": new.value},
            subject=target.id,
        )
        if target.role is Role.OWNER and new is not Role.OWNER:
            self.revoke_creator_invites(uow, target.id)
        if new is Role.OWNER and target.role is not Role.OWNER:
            self.revoke_promoted(uow, target.id)

    def revoke_promoted(self, uow: UnitOfWork, user_id: str) -> int:
        """Promotion to owner ends every live session of the person (`promoted_to_owner`, MFA
        spec section 11.2, ND333b): a session made before the MFA policy applied to them does not
        carry over. One `auth.session_revoked` each, in the caller's transaction."""
        now = self.now()
        count = 0
        for session in self.store.list_auth_sessions(self.org_id):
            if session.user_id == user_id and session.active(now):
                count += int(self._revoke(uow, session, now, "promoted_to_owner"))
        return count

    def demote_owner_in(self, uow: UnitOfWork, actor: str, user_id: str, new: Role) -> None:
        """An owner becomes `new` (not owner), inside the caller's unit of work. No API route
        reaches this (an owner's role changes only by the operator); it is the one place an owner
        demotion is written, so the invite revoke cannot be forgotten."""
        target = self._target(uow, user_id)
        if target.role is not Role.OWNER or new is Role.OWNER:
            raise ValueError("demote_owner_in turns an owner into another role")
        self._apply_role(uow, actor, target, new)

    def revoke_session(self, session_id: str, actor: str) -> bool:
        """End one session by an administrator's decision (reason `admin`), ledgered with the
        actor in the same transaction. False if it is unknown or already ended."""
        now = self.now()
        with self.store.transaction() as uow:
            session = uow.get_auth_session(self.org_id, session_id)
            if session is None or not session.active(now):
                return False
            return self._revoke(uow, session, now, "admin", actor)

    # -- first owner ---------------------------------------------------------------------

    def owner_exists(self) -> bool:
        """Whether the organization has an active (not erased) user."""
        return any(not u.erased for u in self.store.list_users(self.org_id))

    def owner_ever_existed(self) -> bool:
        """Whether an owner record has **ever** existed, erased or not (users are never deleted,
        an erased user keeps its role). It only ever turns true, so the sign-in screen's setup
        offer cannot come back (T111)."""
        return any(u.role == Role.OWNER for u in self.store.list_users(self.org_id))

    def open_setup_token_expiry(self) -> datetime | None:
        """When the latest open (unused, unexpired) setup token expires, or None. Shows nothing
        that could redeem it."""
        now = self.now()
        open_tokens = [
            t.expires_at
            for t in self.store.list_setup_tokens(self.org_id)
            if t.consumed_at is None and now < t.expires_at
        ]
        return max(open_tokens, default=None)

    def issue_setup_token(
        self, *, rotate: bool = False, lifetime: timedelta = SETUP_TOKEN_LIFETIME
    ) -> SetupOutcome:
        """Create the first-owner setup token **once**. If an owner exists, none is made. If an
        unexpired, unused token exists it is not shown again (only its expiry is reported);
        `rotate=True` retires it and issues a new one, in one transaction, so the newest token
        wins. `lifetime` is 24 hours for `nuramesh auth new-setup-token` and 900 s for the setup
        URL of `nuramesh serve` (SA-0.8)."""
        now = self.now()
        if self.owner_exists():
            return SetupOutcome("owner_exists")
        if self.org_closed():
            return SetupOutcome("closed")
        with self.store.transaction() as uow:
            open_tokens = [
                t
                for t in self.store.list_setup_tokens(self.org_id)
                if t.consumed_at is None and now < t.expires_at
            ]
            if open_tokens and not rotate:
                return SetupOutcome("token_open", expires_at=max(t.expires_at for t in open_tokens))
            for old in open_tokens:
                uow.consume_setup_token(self.org_id, old.token_hash, now)  # retire
            issued = issue_setup_token(self.org_id, now, lifetime)
            uow.put_setup_token(issued.record)
        return SetupOutcome("issued", issued, issued.record.expires_at, retired=len(open_tokens))

    def redeem_setup(
        self,
        token: str,
        email: str,
        display_name: str | None,
        password: str,
        client: str,
        *,
        user_agent: str | None = None,
    ) -> User:
        """Redeem the one-time setup token: create the first owner and use the token up, in one
        transaction (a failure anywhere leaves the token unused). Every refusal about the token
        is the same answer."""
        self.limit_client(client)
        now = self.now()
        self._locked(client, now)  # no hash before the token check: no reservation is needed
        seen = self._seen(client, user_agent)
        bad = AuthProblem(403, "invalid_setup_token", GENERIC_SETUP_FAILURE)
        digest = hash_setup_token(token) if len(token) <= 256 else ""
        record = self.store.get_setup_token(self.org_id, digest)
        if record is None or check_setup_token(token, record, now) is not SetupTokenCheck.OK:
            self._count_failure(now, client, seen)
            raise bad
        problem = password_problem(password)
        if problem is not None:
            raise AuthProblem(422, "invalid_request", problem)
        try:
            address = normalize_email(email)
        except ValueError:
            raise AuthProblem(422, "invalid_request", "not a valid email address") from None
        with self.gate.slot(self.settings.scrypt.memory_bytes) as got:
            if not got:
                raise AuthProblem(
                    429, "busy", "the server is busy; try again", {"Retry-After": "1"}
                )
            password_hash = hash_password(password, self.settings.scrypt)
        user = User(
            org_id=self.org_id,
            id=new_user_id(),
            email=address,
            display_name=display_name or None,
            role=Role.OWNER,
            created_at=now,
            email_verified_at=now,
            email_verified_via="setup",
        )
        with self.store.transaction() as uow:
            if not uow.consume_setup_token(self.org_id, digest, now):
                raise bad
            if self.owner_exists() or self.org_closed():
                raise bad  # an owner exists already, or the organization was closed (T128)
            uow.create_user(user)
            uow.put_credential(
                Credential(
                    org_id=self.org_id,
                    user_id=user.id,
                    password_hash=password_hash,
                    updated_at=now,
                )
            )
            self._event(
                uow,
                EventType.USER_CREATED,
                user.id,
                {"user": user.id, "role": Role.OWNER.value, "via": "setup_token"},
            )
            self._record(
                uow,
                EventType.AUTH_SETUP_TOKEN_USED,
                user.id,
                {},
                subject=user.id,
                details={**seen, "email": user.email},
            )
        return user
