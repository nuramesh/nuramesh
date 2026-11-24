"""Email change (R1.40): start a change and finish it.

The address changes only when the link mailed to the **new** address is opened. The start answer
is the same (202) whether the new address is free or already held, and it is given **before any
SMTP work**: both mails go through R1.39's background queues, so no status, header or delay can
show whether an address is taken. The notice to the current address goes on the main queue in
every case (taken or free, identical text, queued first); the verification mail to the new address
goes on its own queue with its own worker, only when the address is free, so nothing about the
address changes the main queue's depth, occupancy or order. One slot in each queue is reserved
before anything is written and the same 503 `busy` answers every address.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from nuramesh.core.auth import (
    User,
    normalize_email,
    parse_hash,
    verify_password,
    verify_unknown,
)
from nuramesh.core.email_change import (
    EMAIL_CHANGE_PER_HOUR,
    EMAIL_VERIFY_PREFIX,
    EmailChange,
    email_change_link_record,
)
from nuramesh.core.identity import (
    DEFAULT_ORG_NAME,
    LinkCheck,
    address_problem,
    check_link_token,
    hash_link_token,
    new_link_token,
)
from nuramesh.core.ledger import EventType, format_time
from nuramesh.server.limits import RateLimiter
from nuramesh.server.mail import MailBusy, MailService
from nuramesh.server.mail_templates import notice_mail, verify_mail
from nuramesh.server.sessions import GENERIC_LOGIN_FAILURE, AuthProblem, AuthService
from nuramesh.stores import AlreadyExistsError, UnitOfWork

GENERIC_VERIFY_FAILURE = "this link is not valid"
RATE_WINDOW = 3600.0
NOTICE_TEXT = (
    "A change of your email address to an address at {domain} was requested. "
    "If this was not you, change your password and tell your owner."
)


@dataclass(frozen=True)
class EmailVerifySettings:
    """`NURAMESH_EMAIL_VERIFY_TTL_HOURS` (1 to 168, default 24) and the fallback link base."""

    ttl: timedelta = timedelta(hours=24)
    per_hour: int = EMAIL_CHANGE_PER_HOUR
    public_url: str | None = None

    def __post_init__(self) -> None:
        if not timedelta(hours=1) <= self.ttl <= timedelta(hours=168):
            raise ValueError("the verification lifetime is 1 to 168 hours")


def email_verify_settings_from_env(env: Mapping[str, str]) -> EmailVerifySettings:
    """Settings from the environment; a bad value raises `ValueError` naming the variable."""
    raw = env.get("NURAMESH_EMAIL_VERIFY_TTL_HOURS", "").strip()
    hours = 24
    if raw:
        if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 168:
            raise ValueError("NURAMESH_EMAIL_VERIFY_TTL_HOURS must be a whole number from 1 to 168")
        hours = int(raw)
    return EmailVerifySettings(ttl=timedelta(hours=hours))


class EmailChangeService:
    """Start and finish an address change for the users of one organization."""

    def __init__(
        self,
        auth: AuthService,
        mail: MailService | None = None,
        settings: EmailVerifySettings | None = None,
    ) -> None:
        self.auth, self.mail = auth, mail
        self.settings = settings or EmailVerifySettings()
        self.limiter = RateLimiter(self.settings.per_hour, RATE_WINDOW, auth.settings.monotonic)

    @property
    def org_id(self) -> str:
        return self.auth.org_id

    def _org_name(self) -> str:
        return self.mail.org_name if self.mail is not None else DEFAULT_ORG_NAME

    def _link(self, token: str) -> str:
        base = (self.mail.public_url if self.mail else None) or self.settings.public_url or ""
        return f"{base.rstrip('/')}/verify-email#t={token}"

    def _holder(self, uow: UnitOfWork, address: str, except_user: str) -> bool:
        other = uow.get_user_by_email(self.org_id, address)
        return other is not None and not other.erased and other.id != except_user

    # -- start ---------------------------------------------------------------------------

    def start(
        self,
        user_id: str,
        new_email: str,
        password: str,
        client: str,
        *,
        user_agent: str | None = None,
    ) -> None:
        """Order: address (422), same address (409), mail on (503), room in both queues (503
        `busy`), per-user limit (429), client limit, lockout, hashing budget, password (401), then
        one transaction. The caller answers 202 for every outcome after the password."""
        try:
            address = normalize_email(new_email)
        except ValueError:
            raise AuthProblem(422, "invalid_request", "not a valid email address") from None
        if address_problem(address) is not None:
            raise AuthProblem(422, "invalid_request", "not a valid email address")
        user = self.auth.store.get_user(self.org_id, user_id)
        if user is None or user.erased or user.email is None:
            raise AuthProblem(401, "session_invalid", "you are not signed in")
        if address == user.email:
            raise AuthProblem(409, "same_email", "that is already your address")
        if self.mail is None or not self.mail.enabled:
            raise AuthProblem(503, "email_not_configured", "email is not configured")
        try:
            reservation = self.mail.reserve(1, verify_slots=1)
        except MailBusy:
            raise AuthProblem(
                503, "busy", "the server is busy; try again", {"Retry-After": "1"}
            ) from None
        with reservation:
            wait = self.limiter.allow(user_id)
            if wait > 0:
                raise AuthProblem(
                    429,
                    "rate_limited",
                    "too many requests; try again later",
                    {"Retry-After": str(max(1, int(wait) + (wait % 1 > 0)))},
                )
            self._check_password(user, password, client, user_agent)
            deliverable, token = self._write(user, address, client, user_agent)
            self._send(reservation, user, address, token, deliverable)

    def _check_password(
        self, user: User, password: str, client: str, user_agent: str | None
    ) -> None:
        """Exactly a login's password step: lockout and counters, hashing budget, a wrong
        password counts a failure on the account and the client (`via` password); a right one
        clears nothing."""
        auth = self.auth
        auth.limit_client(client)
        assert user.email is not None
        reserved = auth._reserve(auth.now(), client, user.email, user.id)
        try:
            # R1.41 Q15: a login block makes the old password fail here as it does at login
            blocked = auth.store.get_password_login_block(self.org_id, user.id) is not None
            credential = auth.store.get_credential(self.org_id, user.id)
            parsed = None if credential is None else parse_hash(credential.password_hash)
            cost = max(
                auth.settings.scrypt.memory_bytes,
                0 if parsed is None else parsed.params.memory_bytes,
            )
            with auth.gate.slot(cost) as got:
                if not got:
                    raise AuthProblem(
                        429, "busy", "the server is busy; try again", {"Retry-After": "1"}
                    )
                ok = (
                    verify_password(password, credential.password_hash)
                    if credential is not None
                    else verify_unknown(password, auth.settings.scrypt)
                )
        except BaseException:
            auth._release(reserved)
            raise
        if not ok or blocked:
            auth._settle_failure(
                reserved, user.id, user.email, client, auth._seen(client, user_agent)
            )
            raise AuthProblem(401, "invalid_credentials", GENERIC_LOGIN_FAILURE)
        auth._release(reserved)

    def _write(
        self, user: User, address: str, client: str, user_agent: str | None
    ) -> tuple[bool, str]:
        auth = self.auth
        token = new_link_token(EMAIL_VERIFY_PREFIX)
        now = auth.now()
        with auth.store.transaction() as uow:
            deliverable = not self._holder(uow, address, user.id)
            uow.cancel_email_changes(self.org_id, user.id, now)
            uow.put_email_change(
                EmailChange(
                    org_id=self.org_id,
                    user_id=user.id,
                    new_email=address,
                    token_hash=hash_link_token(token),
                    deliverable=deliverable,
                    created_at=now,
                    expires_at=now + self.settings.ttl,
                )
            )
            auth._record(
                uow,
                EventType.USER_EMAIL_CHANGE_REQUESTED,
                user.id,
                {"new_email_hash": auth._email_hash(uow, address, user.id)},
                subject=user.id,
                details={**auth._seen(client, user_agent), "email": address},
            )
        return deliverable, token

    def _send(
        self, reservation: Any, user: User, address: str, token: str, deliverable: bool
    ) -> None:
        """After the commit, in the background (MJ2): first the notice (no link) to the current
        address if it is verified and ASCII, in both cases with the same text; then, only for a
        deliverable address, the link to the new one on the verification queue. A decoy gives its
        unused verification slot back at once. Nothing here can change the answer."""
        assert self.mail is not None and user.email is not None
        org = self._org_name()
        expires = format_time(self.auth.now() + self.settings.ttl)
        if user.email_verified_at is not None and user.email.isascii():
            domain = address.partition("@")[2]
            self.mail.enqueue(
                reservation,
                notice_mail(
                    user.email,
                    what=NOTICE_TEXT.format(domain=domain),
                    at=self.auth.now(),
                    org_name=org,
                ),
                user.id,
                ledger=False,
            )
        if deliverable:
            self.mail.enqueue(
                reservation,
                verify_mail(address, link=self._link(token), expires=expires, org_name=org),
                user.id,
                ledger=False,
            )
        else:
            reservation.release_verify()

    # -- verify --------------------------------------------------------------------------

    def verify(self, token: str, client: str, *, user_agent: str | None = None) -> None:
        """Open the link: client limit, lockout, token check (one generic 403), then one
        transaction that consumes it and changes the address (409 `already_exists` rolls the
        consume back). Sessions, password and MFA are untouched."""
        auth = self.auth
        auth.limit_client(client)
        now = auth.now()
        auth._locked(client, now)
        bad = AuthProblem(403, "invalid_verification_token", GENERIC_VERIFY_FAILURE)
        digest = hash_link_token(token) if len(token) <= 256 else ""
        change = auth.store.get_email_change(self.org_id, digest)
        user = None if change is None else auth.store.get_user(self.org_id, change.user_id)
        if (
            change is None
            or not change.deliverable
            or check_link_token(token, email_change_link_record(change), now) is not LinkCheck.OK
            or user is None
            or user.erased
            or user.disabled
        ):
            auth._count_failure(now, client, auth._seen(client, user_agent))
            raise bad
        with auth.store.transaction() as uow:
            if not uow.consume_email_change(self.org_id, change.token_hash, now):
                raise bad
            current = uow.get_user(self.org_id, change.user_id)
            if current is None or current.erased or current.disabled or current.email is None:
                raise bad
            if self._holder(uow, change.new_email, current.id):
                raise AuthProblem(409, "already_exists", "that address is no longer available")
            old = current.email
            try:
                uow.set_user_email(self.org_id, current.id, change.new_email, now)
            except AlreadyExistsError:
                raise AuthProblem(
                    409, "already_exists", "that address is no longer available"
                ) from None
            uow.cancel_email_changes(self.org_id, current.id, now)
            uow.revoke_reset_tokens(self.org_id, current.id, now)
            uow.adopt_security_log_rows(self.org_id, old, current.id)
            uow.adopt_security_log_rows(self.org_id, change.new_email, current.id)
            auth._record(
                uow,
                EventType.USER_EMAIL_CHANGED,
                current.id,
                {
                    "old_email_hash": auth._email_hash(uow, old, current.id),
                    "new_email_hash": auth._email_hash(uow, change.new_email, current.id),
                },
                subject=current.id,
                details={**auth._seen(client, user_agent), "email": change.new_email},
            )


__all__ = [
    "EmailChangeService",
    "EmailVerifySettings",
    "email_verify_settings_from_env",
]
