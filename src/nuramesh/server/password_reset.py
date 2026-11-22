"""Password reset (R1.41; `spec/identity/password-reset.md`): the self-service request, the
redeem, the owner and CLI reset and the API-token revoke links.

A `ResetService` sits over the `AuthService` of one organization (client limiter and lockout,
hashing budget, security events) and R1.39's `MailService`. Only the SHA-256 of a token is stored;
the raw token exists only in the mail. The public request never reveals whether an address has an
account: every address takes the same statements and the same answer, and only an eligible user
gets a mail (queued in the background after the commit).
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from nuramesh.core.auth import (
    ApiToken,
    Credential,
    User,
    address_key,
    email_text,
    hash_password,
    identifier_epoch,
    identifier_hash,
    normalize_email,
    password_problem,
)
from nuramesh.core.identity import (
    LinkCheck,
    check_link_token,
    hash_link_token,
    new_link_token,
)
from nuramesh.core.ledger import EventType, format_time
from nuramesh.core.password_reset import (
    DEFAULT_RESET_TTL,
    DEFAULT_REVOKE_TTL,
    MAX_REVOKE_LINKS,
    RESET_PREFIX,
    RESET_REASON,
    REVOKE_PREFIX,
    ResetToken,
    RevokeLink,
    reset_link_record,
    revoke_link_record,
)
from nuramesh.server.limits import RateLimiter
from nuramesh.server.mail import MailBusy, MailService, Reservation
from nuramesh.server.mail_templates import notice_mail, reset_mail
from nuramesh.server.secevents import SecurityRecorder, token_revoked
from nuramesh.server.sessions import AuthProblem, AuthService
from nuramesh.stores import UnitOfWork

GENERIC_RESET_FAILURE = "this link is not valid"
CLI_ACTOR = "service:cli"
HOUR = 3600.0
CLIENT_PER_HOUR = 5
ADDRESS_PER_HOUR = 3
TARGET_PER_HOUR = 5
MAX_ADDRESS_KEYS = 10_000


@dataclass(frozen=True)
class ResetSettings:
    """`NURAMESH_RESET_TTL_MINUTES` (5 to 1440, default 60). The revoke-link lifetime is fixed
    at 24 hours (design decision): no setting."""

    ttl: timedelta = DEFAULT_RESET_TTL

    def __post_init__(self) -> None:
        if not timedelta(minutes=5) <= self.ttl <= timedelta(minutes=1440):
            raise ValueError("the reset lifetime is 5 to 1440 minutes")


def reset_settings_from_env(env: Mapping[str, str]) -> ResetSettings:
    """Settings from the environment; a bad value raises `ValueError` naming the variable."""

    def number(name: str, default: int, low: int, high: int) -> int:
        raw = env.get(name, "").strip()
        if not raw:
            return default
        if not raw.isascii() or not raw.isdigit() or not low <= int(raw) <= high:
            raise ValueError(f"{name} must be a whole number from {low} to {high}")
        return int(raw)

    return ResetSettings(
        ttl=timedelta(minutes=number("NURAMESH_RESET_TTL_MINUTES", 60, 5, 1440)),
    )


class ResetService:
    """The reset rules for one organization."""

    def __init__(
        self,
        auth: AuthService,
        mail: MailService | None = None,
        settings: ResetSettings | None = None,
    ) -> None:
        self.auth, self.mail = auth, mail
        self.settings = settings or ResetSettings()
        mono = auth.settings.monotonic
        self.client_hourly = RateLimiter(CLIENT_PER_HOUR, HOUR, mono)
        self.address_window = RateLimiter(ADDRESS_PER_HOUR, HOUR, mono, max_keys=MAX_ADDRESS_KEYS)
        self.target_hourly = RateLimiter(TARGET_PER_HOUR, HOUR, mono)
        self._rec = SecurityRecorder(auth.org_id, auth.now)

    @property
    def org_id(self) -> str:
        return self.auth.org_id

    @property
    def org_name(self) -> str:
        return self.mail.org_name if self.mail is not None else "your organization"

    # -- helpers -------------------------------------------------------------------------

    def _base(self) -> str:
        return ((self.mail.public_url if self.mail else None) or "").rstrip("/")

    def _mail_enabled(self) -> bool:
        return self.mail is not None and self.mail.enabled

    def _require_mail(self) -> MailService:
        if self.mail is None or not self.mail.enabled:
            raise AuthProblem(503, "email_not_configured", "email is not configured")
        return self.mail

    @staticmethod
    def _busy() -> AuthProblem:
        return AuthProblem(503, "busy", "the server is busy; try again later")

    @staticmethod
    def _wait(seconds: float) -> dict[str, str]:
        return {"Retry-After": str(max(1, int(seconds) + (seconds % 1 > 0)))}

    def _eligible(self, uow: UnitOfWork, user: User | None) -> bool:
        """Section 1: exists, not erased or disabled, ASCII verified address, has a password."""
        credential = uow.get_credential(self.org_id, user.id if user is not None else "")
        return (
            user is not None
            and not user.erased
            and not user.disabled
            and user.email is not None
            and user.email.isascii()
            and user.email_verified_at is not None
            and credential is not None
        )

    def _new_token(
        self, uow: UnitOfWork, user_id: str | None, via: str, created_by: str | None, now: datetime
    ) -> tuple[str, ResetToken]:
        token = new_link_token(RESET_PREFIX)
        row = ResetToken(
            org_id=self.org_id,
            token_hash=hash_link_token(token),
            user_id=user_id,
            via=via,  # type: ignore[arg-type]
            created_by=created_by,
            created_at=now,
            expires_at=now + self.settings.ttl,
        )
        uow.put_reset_token(row)
        return token, row

    def _revoke_sessions(self, uow: UnitOfWork, user_id: str, now: datetime) -> int:
        count = 0
        for session in self.auth.store.list_auth_sessions(self.org_id):
            if session.user_id == user_id and session.active(now):
                count += self.auth._revoke(uow, session, now, RESET_REASON)
        return count

    def _active_tokens(self, uow: UnitOfWork, user_id: str, now: datetime) -> list[ApiToken]:
        return [
            t
            for t in self.auth.store.list_api_tokens(self.org_id)
            if t.created_by == user_id and t.active(now)
        ]

    def _revoke_tokens(self, uow: UnitOfWork, user_id: str, now: datetime, by: str) -> int:
        count = 0
        for token in self._active_tokens(uow, user_id, now):
            if uow.revoke_api_token(self.org_id, token.id, now, by):
                token_revoked(self._rec, uow, token, by)
                count += 1
        return count

    # -- the public request ----------------------------------------------------------------

    def _request_hash(self, uow: UnitOfWork, address: str, known: User | None) -> str:
        """The keyed hash of the address. Both keys are always read, so a known and an unknown
        address run the same statements (spec P2/P3: no side channel)."""
        org_key = uow.org_identifier_key(self.org_id, identifier_epoch(self.auth.now()))
        user_key = uow.user_identifier_key(self.org_id, "" if known is None else known.id)
        key = user_key if user_key is not None else org_key
        if key is None:
            raise RuntimeError("the identifier key of this epoch was destroyed")
        return identifier_hash(key, self.org_id, email_text(address))

    def request(self, email: str, client: str, *, user_agent: str | None = None) -> None:
        """Section 3. Returns nothing: the caller answers 202 `accepted` for every return."""
        self.auth.limit_client(client)
        wait = self.client_hourly.allow(address_key(client))
        if wait > 0:
            raise AuthProblem(429, "rate_limited", "too many requests; slow down", self._wait(wait))
        try:
            address = normalize_email(email)
        except ValueError:
            raise AuthProblem(422, "invalid_request", "not a valid email address") from None
        mail = self._require_mail()
        try:
            reservation = mail.reserve(1)
        except MailBusy:
            raise self._busy() from None
        keep = False
        try:
            digest = hashlib.sha256(address.encode("utf-8")).hexdigest()
            if self.address_window.allow(digest) > 0:  # still 202; nothing written or sent
                return
            now = self.auth.now()
            seen = self.auth._seen(client, user_agent)
            with self.auth.store.transaction() as uow:
                user = uow.get_user_by_email(self.org_id, address)
                eligible = self._eligible(uow, user)
                known = user if user is not None and not user.erased else None
                uow.revoke_reset_tokens(self.org_id, user.id if user is not None else "", now)
                token, row = self._new_token(
                    uow, user.id if eligible and user is not None else None, "self", None, now
                )
                self._rec.record(
                    uow,
                    EventType.AUTH_PASSWORD_RESET_REQUESTED,
                    "system",
                    {"identifier_hash": self._request_hash(uow, address, known)},
                    subject=None if known is None else known.id,
                    details={**seen, "email": address},
                )
            if eligible:
                keep = True
                message = reset_mail(
                    address,
                    link=f"{self._base()}/reset-password#t={token}",
                    expires=format_time(row.expires_at),
                    org_name=self.org_name,
                )
                mail.enqueue(reservation, message, "system", ledger=False)
        finally:
            if not keep:
                reservation.release()

    # -- the owner / CLI reset -------------------------------------------------------------

    @staticmethod
    def _owner_refusals(target: User, has_credential: bool) -> None:
        """Section 2 refusals after the erased and self checks, in the spec's order."""
        if target.disabled:
            raise AuthProblem(409, "user_disabled", "that user is disabled")
        if not has_credential:
            raise AuthProblem(409, "no_password_credential", "that user has no password")
        if target.email is None or target.email_verified_at is None:
            raise AuthProblem(409, "email_unverified", "that address was never confirmed")
        if not target.email.isascii():
            raise AuthProblem(409, "email_unsupported", "that address cannot be mailed")

    def owner_issue(self, actor: str, user_id: str, *, via: str = "owner") -> datetime:
        """Section 2: refusals in the spec's order, then one transaction that revokes every live
        session and active API token of the target, blocks the old password and writes the token;
        the mail is queued after the commit. Returns `expires_at`."""
        self.auth.require_person(user_id)
        target = self.auth.store.get_user(self.org_id, user_id)
        if target is None or target.erased:
            raise AuthProblem(404, "not_found", "no such user")
        if user_id == actor:
            raise AuthProblem(409, "use_self_service", "use the forgot-password page")
        self._owner_refusals(
            target, self.auth.store.get_credential(self.org_id, user_id) is not None
        )
        mail = self._require_mail()
        try:
            reservation = mail.reserve(1)
        except MailBusy:
            raise self._busy() from None
        keep = False
        try:
            wait = self.target_hourly.allow(user_id)
            if wait > 0:
                raise AuthProblem(
                    429, "rate_limited", "too many resets for that user", self._wait(wait)
                )
            now = self.auth.now()
            with self.auth.store.transaction() as uow:
                fresh = uow.get_user(self.org_id, user_id)
                if fresh is None or fresh.erased:
                    raise AuthProblem(404, "not_found", "no such user")
                self._owner_refusals(fresh, uow.get_credential(self.org_id, user_id) is not None)
                address = fresh.email
                assert address is not None  # _owner_refusals checked it
                uow.revoke_reset_tokens(self.org_id, user_id, now)
                sessions = self._revoke_sessions(uow, user_id, now)
                tokens = self._revoke_tokens(uow, user_id, now, actor)
                token, row = self._new_token(uow, user_id, via, actor, now)
                uow.set_password_login_block(self.org_id, user_id, now, via)
                uow.delete_mfa_challenges(self.org_id, user_id)  # MJ1: no ledger event
                self._rec.record(
                    uow,
                    EventType.AUTH_PASSWORD_RESET_ISSUED,
                    actor,
                    {
                        "via": via,
                        "expires_at": format_time(row.expires_at),
                        "sessions_revoked": sessions,
                        "tokens_revoked": tokens,
                    },
                    subject=user_id,
                )
            keep = True
            message = reset_mail(
                address,
                link=f"{self._base()}/reset-password#t={token}",
                expires=format_time(row.expires_at),
                org_name=self.org_name,
            )
            mail.enqueue(reservation, message, actor, ledger=True)
            return row.expires_at
        finally:
            if not keep:
                reservation.release()

    # -- redeem ----------------------------------------------------------------------------

    def _invalid(self) -> AuthProblem:
        return AuthProblem(403, "invalid_reset_token", GENERIC_RESET_FAILURE)

    def _check_reset(self, token: str, client: str, user_agent: str | None) -> ResetToken:
        """Client limit, lockout, token check; every bad token is one answer and counts as a
        client failure. No password is hashed before this passes."""
        self.auth.limit_client(client)
        now = self.auth.now()
        self.auth._locked(client, now)
        digest = hash_link_token(token) if len(token) <= 256 else ""
        row = self.auth.store.get_reset_token(self.org_id, digest)
        ok = row is not None and row.user_id is not None
        if ok and row is not None:
            ok = check_link_token(token, reset_link_record(row), now) is LinkCheck.OK
        if ok and row is not None and row.user_id is not None:
            user = self.auth.store.get_user(self.org_id, row.user_id)
            ok = (
                user is not None
                and not user.erased
                and not user.disabled
                and self.auth.store.get_credential(self.org_id, user.id) is not None
            )
        if not ok or row is None:
            self.auth._count_failure(now, client, self.auth._seen(client, user_agent))
            raise self._invalid()
        return row

    def redeem(
        self, token: str, password: str, client: str, *, user_agent: str | None = None
    ) -> int:
        """Section 4. Returns how many sessions were revoked."""
        row = self._check_reset(token, client, user_agent)
        problem = password_problem(password)
        if problem is not None:
            raise AuthProblem(422, "invalid_request", problem)
        settings = self.auth.settings
        with self.auth.gate.slot(settings.scrypt.memory_bytes) as got:
            if not got:
                raise AuthProblem(
                    429, "busy", "the server is busy; try again", {"Retry-After": "1"}
                )
            password_hash = hash_password(password, settings.scrypt)
        reservation: Reservation | None = None
        if self._mail_enabled():
            assert self.mail is not None
            try:
                reservation = self.mail.reserve(1)
            except MailBusy:
                raise self._busy() from None
        keep = False
        try:
            try:
                outcome = self._redeem_in_transaction(row, password_hash, client, user_agent)
            except AuthProblem as problem_after_check:
                if problem_after_check.code == "invalid_reset_token":
                    now = self.auth.now()
                    self.auth._count_failure(now, client, self.auth._seen(client, user_agent))
                raise
            keep = reservation is not None
        finally:
            if not keep and reservation is not None:
                reservation.release()
        sessions, user, lines = outcome
        if reservation is not None and self.mail is not None and user.email is not None:
            self.mail.enqueue(
                reservation,
                notice_mail(
                    user.email,
                    what="Your password was changed." + lines,
                    at=self.auth.now(),
                    org_name=self.org_name,
                ),
                "system",
                ledger=True,
            )
        return sessions

    def _redeem_in_transaction(
        self, row: ResetToken, password_hash: str, client: str, user_agent: str | None
    ) -> tuple[int, User, str]:
        now = self.auth.now()
        assert row.user_id is not None
        user_id = row.user_id
        lines = ""
        with self.auth.store.transaction() as uow:
            if not uow.consume_reset_token(self.org_id, row.token_hash, now):
                raise self._invalid()
            user = uow.get_user(self.org_id, user_id)
            if not self._eligible(uow, user):  # step 2; a reset never proves an address (mi6)
                raise self._invalid()
            assert user is not None
            uow.put_credential(
                Credential(
                    org_id=self.org_id, user_id=user_id, password_hash=password_hash, updated_at=now
                )
            )
            # SA-8 and SU-7 (W3): a new password un-confirms and un-steps-up every session of the
            # person, also one this reset would not end
            uow.clear_session_proofs(self.org_id, user_id)
            uow.clear_password_login_block(self.org_id, user_id)
            uow.delete_mfa_challenges(self.org_id, user_id)  # MJ1: no ledger event
            sessions = self._revoke_sessions(uow, user_id, now)
            uow.revoke_reset_tokens(self.org_id, user_id, now)
            if user.email is not None:
                uow.delete_auth_failure(
                    self.auth._counter_key(uow, now, "acct", email_text(user.email), user_id)
                )
            uow.revoke_revoke_links(self.org_id, user_id, now)
            active = self._active_tokens(uow, user_id, now)
            if row.via == "self":
                if self._mail_enabled() and active:
                    lines = self._issue_links(uow, user_id, active, now)
            else:
                self._revoke_tokens(uow, user_id, now, "system")
            self._rec.record(
                uow,
                EventType.AUTH_PASSWORD_RESET_COMPLETED,
                user_id,
                {"via": row.via, "sessions_revoked": sessions},
                subject=user_id,
                details={**self.auth._seen(client, user_agent)},
            )
        return sessions, user, lines

    def _issue_links(
        self, uow: UnitOfWork, user_id: str, active: list[ApiToken], now: datetime
    ) -> str:
        """Section 4: one revoke link per active API token (at most 20) and the notice lines."""
        text = "\n\nThese API keys are still active:"
        for token in active[:MAX_REVOKE_LINKS]:
            secret = new_link_token(REVOKE_PREFIX)
            expires = now + DEFAULT_REVOKE_TTL
            uow.put_revoke_link(
                RevokeLink(
                    org_id=self.org_id,
                    token_hash=hash_link_token(secret),
                    user_id=user_id,
                    api_token_id=token.id,
                    created_at=now,
                    expires_at=expires,
                )
            )
            text += (
                f"\n- {token.id}, created {format_time(token.created_at)}, "
                f"expires {format_time(token.expires_at)}\n  revoke: "
                f"{self._base()}/revoke-token#t={secret}"
            )
        if len(active) > MAX_REVOKE_LINKS:
            text += f"\nand {len(active) - MAX_REVOKE_LINKS} more: revoke them in the app"
        self._rec.record(
            uow,
            EventType.AUTH_TOKEN_REVOKE_LINKS_ISSUED,
            "system",
            {"count": min(len(active), MAX_REVOKE_LINKS)},
            subject=user_id,
        )
        return text

    # -- revoke by link --------------------------------------------------------------------

    def revoke_by_link(self, token: str, client: str, *, user_agent: str | None = None) -> None:
        """Section 4a: consume the link, ledger the use, revoke its API token if still active."""
        bad = AuthProblem(403, "invalid_revoke_link", GENERIC_RESET_FAILURE)
        self.auth.limit_client(client)
        now = self.auth.now()
        self.auth._locked(client, now)
        digest = hash_link_token(token) if len(token) <= 256 else ""
        link = self.auth.store.get_revoke_link(self.org_id, digest)
        ok = link is not None
        if ok and link is not None:
            ok = check_link_token(token, revoke_link_record(link), now) is LinkCheck.OK
        if ok and link is not None:
            owner = self.auth.store.get_user(self.org_id, link.user_id)
            ok = owner is not None and not owner.erased and not owner.disabled
        if not ok or link is None:
            self.auth._count_failure(now, client, self.auth._seen(client, user_agent))
            raise bad
        with self.auth.store.transaction() as uow:
            if not uow.consume_revoke_link(self.org_id, digest, now):
                raise bad
            api = uow.get_api_token(self.org_id, link.api_token_id)
            live = api is not None and api.active(now)
            self._rec.record(
                uow,
                EventType.AUTH_TOKEN_REVOKE_LINK_USED,
                "system",
                {
                    "token_hash": self._rec.hash_of(uow, link.api_token_id, link.user_id),
                    "revoked": live,
                },
                subject=link.user_id,
            )
            if (
                api is not None
                and live
                and uow.revoke_api_token(self.org_id, api.id, now, "system")
            ):
                token_revoked(self._rec, uow, api, "system")
