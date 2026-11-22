"""User invites (R1.37; `spec/identity/invites.md`): create, list, revoke, resend, inspect, accept.

An `InviteService` sits over the `AuthService` of one organization and reuses its rules (client
limiter and lockout, hashing budget, security events). The raw token exists only in the mail (or
the operator's printed link); only its SHA-256 is stored. Every refusal that is not about a link
token leaves the stored token alone, and **no password hashing happens before the token check
passes**. Mail goes through R1.39's `MailService` (`server.mail`: the real sender, the
templates and the `email.sent` / `email.failed` facts); `None`, or a service that is not
enabled, means mail is off.
"""

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from nuramesh.core.auth import (
    Credential,
    Role,
    User,
    email_text,
    hash_password,
    identifier_hash,
    new_user_id,
    normalize_email,
    password_problem,
)
from nuramesh.core.identity import (
    DEFAULT_ORG_NAME,
    ERASED_CREATOR,
    INVITE_PREFIX,
    MAX_RESENDS,
    Invite,
    InviteStatus,
    LinkCheck,
    MailSendError,
    check_link_token,
    hash_link_token,
    invite_link_record,
    invite_status,
    new_invite_id,
    new_link_token,
)
from nuramesh.core.ledger import EventType, format_time
from nuramesh.server.limits import RateLimiter
from nuramesh.server.mail import MailService
from nuramesh.server.mail_templates import invite_mail
from nuramesh.server.secevents import SecurityRecorder
from nuramesh.server.sessions import OPERATOR_ACTOR, AuthProblem, AuthService
from nuramesh.stores import UnitOfWork

GENERIC_INVITE_FAILURE = "this invitation is not valid"
NO_SUCH_INVITE = "no such invite"
RATE_PER_HOUR = 30
RATE_WINDOW = 3600.0
MAX_DISPLAY_NAME = 200


@dataclass(frozen=True)
class InviteSettings:
    """`NURAMESH_INVITE_TTL_HOURS` (1 to 168), `NURAMESH_INVITE_MAX_PENDING` (1 to 1000) and the
    organization name shown in the mail and the inspect answer."""

    ttl: timedelta = timedelta(hours=72)
    max_pending: int = 100
    org_name: str = "your organization"
    public_url: str | None = None
    rate_per_hour: int = RATE_PER_HOUR

    def __post_init__(self) -> None:
        if not timedelta(hours=1) <= self.ttl <= timedelta(hours=168):
            raise ValueError("the invite lifetime is 1 to 168 hours")
        if not 1 <= self.max_pending <= 1000:
            raise ValueError("the pending invite cap is 1 to 1000")


def public_url_problem(raw: str) -> str | None:
    """Why `raw` is not a usable `NURAMESH_PUBLIC_URL`, or None (email.md section 3)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(raw)
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError for a bad port)
    except ValueError:
        return "not a URL"
    if parts.scheme not in ("http", "https") or not host:
        return "must start with https://"
    if parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        return "http is only allowed for localhost"
    if parts.username or parts.password or parts.query or parts.fragment:
        return "must have no user, query or fragment"
    if parts.path not in ("", "/"):
        return "must have no path"
    return None


def invite_settings_from_env(env: Mapping[str, str]) -> InviteSettings:
    """`InviteSettings` from `NURAMESH_INVITE_TTL_HOURS`, `NURAMESH_INVITE_MAX_PENDING`,
    `NURAMESH_PUBLIC_URL` and `NURAMESH_ORG_NAME`. A bad value raises `ValueError` naming the
    variable (never the value of a URL's secrets: none are allowed in it)."""

    def number(name: str, default: int, low: int, high: int) -> int:
        raw = env.get(name, "").strip()
        if not raw:
            return default
        if not raw.isascii() or not raw.isdigit() or not low <= int(raw) <= high:
            raise ValueError(f"{name} must be a whole number from {low} to {high}")
        return int(raw)

    url = env.get("NURAMESH_PUBLIC_URL", "").strip()
    if url:
        problem = public_url_problem(url)
        if problem is not None:
            raise ValueError(f"NURAMESH_PUBLIC_URL {problem}")
    name = env.get("NURAMESH_ORG_NAME", "").strip()
    if name and (len(name) > 80 or any(unicodedata.category(c) in ("Cc", "Cf") for c in name)):
        raise ValueError("NURAMESH_ORG_NAME must be 1 to 80 characters without control characters")
    return InviteSettings(
        ttl=timedelta(hours=number("NURAMESH_INVITE_TTL_HOURS", 72, 1, 168)),
        max_pending=number("NURAMESH_INVITE_MAX_PENDING", 100, 1, 1000),
        org_name=name or "your organization",
        public_url=url.rstrip("/") or None,
    )


@dataclass(frozen=True)
class Issued:
    """A created or resent invite. `link` is set only for the operator's `--print-link`."""

    invite: Invite
    link: str | None = field(default=None, repr=False)


def invite_view(invite: Invite, now: datetime) -> dict[str, Any]:
    """What an owner sees: never a token or a hash."""
    return {
        "id": invite.id,
        "email": invite.email,
        "role": invite.role,
        "status": invite_status(invite, now).value,
        "created_by": invite.created_by,
        "created_at": format_time(invite.created_at),
        "expires_at": format_time(invite.expires_at),
        "accepted_user_id": invite.accepted_user_id,
        "resent_count": invite.resent_count,
    }


class InviteService:
    """The invite rules for one organization."""

    def __init__(
        self,
        auth: AuthService,
        mail: MailService | None = None,
        settings: InviteSettings | None = None,
    ) -> None:
        self.auth, self.mail = auth, mail
        self.settings = settings or InviteSettings()
        self.limiter = RateLimiter(
            self.settings.rate_per_hour, RATE_WINDOW, auth.settings.monotonic
        )
        self._rec = SecurityRecorder(auth.org_id, auth.now)

    @property
    def org_name(self) -> str:
        """The name shown in mails and the inspect answer: R1.39's `NURAMESH_ORG_NAME` when the
        mail service is wired, else the invite settings' own."""
        named = self.mail.org_name if self.mail is not None else DEFAULT_ORG_NAME
        return named if named != DEFAULT_ORG_NAME else self.settings.org_name

    @property
    def org_id(self) -> str:
        return self.auth.org_id

    # -- helpers -------------------------------------------------------------------------

    def _hash(self, uow: UnitOfWork, text: str) -> str:
        return identifier_hash(self._rec.key(uow, None), self.org_id, text)

    def _link(self, token: str) -> str:
        base = (self.mail.public_url if self.mail else None) or self.settings.public_url or ""
        base = base.rstrip("/")
        return f"{base}/accept-invite#t={token}"

    def _limit(self) -> None:
        wait = self.limiter.allow(self.org_id)
        if wait > 0:
            raise AuthProblem(
                429,
                "rate_limited",
                "too many invitations; try again later",
                {"Retry-After": str(max(1, int(wait) + (wait % 1 > 0)))},
            )

    def _mail_on(self) -> None:
        if self.mail is None or not self.mail.enabled:
            raise AuthProblem(503, "email_not_configured", "email is not configured")

    def _creator_qualifies(self, uow: UnitOfWork, created_by: str) -> bool:
        if created_by == OPERATOR_ACTOR or created_by.startswith("operator:"):
            return True
        creator = uow.get_user(self.org_id, created_by)
        return (
            creator is not None
            and not creator.erased
            and not creator.disabled
            and creator.role is Role.OWNER
        )

    def _address_taken(self, uow: UnitOfWork, address: str) -> bool:
        return any(
            u.email == address and not u.erased for u in self.auth.store.list_users(self.org_id)
        )

    def _invite(self, uow: UnitOfWork, invite_id: str) -> Invite:
        invite = uow.get_invite(self.org_id, invite_id)
        if invite is None:
            raise AuthProblem(404, "not_found", NO_SUCH_INVITE)
        return invite

    # -- owner operations ---------------------------------------------------------------

    def list_invites(self) -> list[dict[str, Any]]:
        now = self.auth.now()
        return [invite_view(i, now) for i in self.auth.store.list_invites(self.org_id)]

    def create(self, actor: str, email: str, role: str, *, print_link: bool = False) -> Issued:
        """Create and deliver an invite. Order of checks: email, role, mail on, rate limit,
        then one transaction (address taken, invite pending, cap), then the send. With
        `print_link` (the operator CLI) no mail is sent, the rate limit does not apply and the
        link comes back once in the result."""
        try:
            address = normalize_email(email)
        except ValueError:
            raise AuthProblem(422, "invalid_request", "not a valid email address") from None
        self.auth.assignable_role(role)
        if not print_link:
            self._mail_on()
            self._limit()
        token = new_link_token(INVITE_PREFIX)
        now = self.auth.now()
        with self.auth.store.transaction() as uow:
            if self._address_taken(uow, address):
                raise AuthProblem(409, "already_exists", "that address already has an account")
            live = 0
            for old in uow.list_invites(self.org_id):
                if old.email != address or invite_status(old, now) is not InviteStatus.PENDING:
                    live += invite_status(old, now) is InviteStatus.PENDING
                    continue
                raise AuthProblem(409, "invite_pending", "that address has a pending invitation")
            if live >= self.settings.max_pending:
                raise AuthProblem(409, "invite_limit", "too many pending invitations")
            for old in uow.list_invites(self.org_id):  # expired and unaccepted: superseded
                if old.email == address and old.accepted_at is None and old.revoked_at is None:
                    uow.revoke_invite(self.org_id, old.id, now, "superseded")
                    self._revoked_event(uow, "system", old, "superseded")
            invite = Invite(
                org_id=self.org_id,
                id=new_invite_id(),
                email=address,
                role=role,  # type: ignore[arg-type]
                token_hash=hash_link_token(token),
                created_by=actor,
                created_at=now,
                expires_at=now + self.settings.ttl,
            )
            uow.put_invite(invite)
            self._rec.record(
                uow,
                EventType.USER_INVITED,
                actor,
                {
                    "invite_hash": self._hash(uow, invite.id),
                    "email_hash": identifier_hash(
                        self._rec.key(uow, None), self.org_id, email_text(address)
                    ),
                    "role": role,
                    "expires_at": format_time(invite.expires_at),
                },
                details={"email": address},
            )
            if print_link:
                self._printed(uow, actor, invite, via="print-link")
        return self._deliver(actor, invite, token, print_link, EventType.USER_INVITED)

    def resend(self, actor: str, invite_id: str) -> Issued:
        """A new token and expiry for a pending invite. Old token dies. Always mails; no link is
        ever printed (design decision)."""
        self._mail_on()
        self._limit()
        token = new_link_token(INVITE_PREFIX)
        now = self.auth.now()
        with self.auth.store.transaction() as uow:
            invite = self._invite(uow, invite_id)
            if invite_status(invite, now) is not InviteStatus.PENDING:
                raise AuthProblem(409, "invite_not_pending", "that invitation is not pending")
            if not self._creator_qualifies(uow, invite.created_by):
                raise AuthProblem(409, "creator_inactive", "the owner who invited is not active")
            if invite.resent_count >= MAX_RESENDS:
                raise AuthProblem(409, "resend_limit", "that invitation was resent too often")
            expires = now + self.settings.ttl
            if not uow.resend_invite(self.org_id, invite.id, hash_link_token(token), expires):
                raise AuthProblem(409, "invite_not_pending", "that invitation is not pending")
            self._rec.record(
                uow,
                EventType.USER_INVITE_RESENT,
                actor,
                {"invite_hash": self._hash(uow, invite.id), "expires_at": format_time(expires)},
            )
            fresh = uow.get_invite(self.org_id, invite.id)
            assert fresh is not None
        return self._deliver(actor, fresh, token, False, EventType.USER_INVITE_RESENT)

    def revoke(self, actor: str, invite_id: str) -> tuple[Invite, bool]:
        """Revoke a pending invite: (invite, changed). Already revoked: changed is False."""
        with self.auth.store.transaction() as uow:
            return self.revoke_in(uow, actor, invite_id)

    def revoke_in(self, uow: UnitOfWork, actor: str, invite_id: str) -> tuple[Invite, bool]:
        invite = self._invite(uow, invite_id)
        status = invite_status(invite, self.auth.now())
        if status is InviteStatus.REVOKED:
            return invite, False
        if status is not InviteStatus.PENDING:
            raise AuthProblem(409, "invite_not_pending", "that invitation is not pending")
        uow.revoke_invite(self.org_id, invite.id, self.auth.now(), "owner")
        self._revoked_event(uow, actor, invite, "owner")
        fresh = uow.get_invite(self.org_id, invite_id)
        assert fresh is not None
        return fresh, True

    def _revoked_event(self, uow: UnitOfWork, actor: str, invite: Invite, reason: str) -> None:
        self._rec.record(
            uow,
            EventType.USER_INVITE_REVOKED,
            actor,
            {"invite_hash": self._hash(uow, invite.id), "reason": reason},
        )

    def _printed(self, uow: UnitOfWork, actor: str, invite: Invite, *, via: str) -> None:
        self._rec.record(
            uow,
            EventType.USER_INVITE_LINK_PRINTED,
            actor,
            {"invite_hash": self._hash(uow, invite.id)},
            details={"via": via, "email": invite.email},
        )

    def _deliver(
        self, actor: str, invite: Invite, token: str, print_link: bool, kind: EventType
    ) -> Issued:
        """After the commit: print mode hands the link back; mail mode sends. A failed send
        revokes the invite as `send_failed` (events `email.failed`, `user.invite_revoked`)."""
        if print_link:
            return Issued(invite, self._link(token))
        assert self.mail is not None and invite.email is not None
        message = invite_mail(
            invite.email,
            link=self._link(token),
            role=invite.role,
            expires=format_time(invite.expires_at),
            org_name=self.org_name,
        )
        try:  # R1.39's service writes `email.sent` / `email.failed` (keyed hash, no address)
            self.mail.deliver(message, actor)
        except MailSendError:
            with self.auth.store.transaction() as uow:
                uow.revoke_invite(self.org_id, invite.id, self.auth.now(), "send_failed")
                self._revoked_event(uow, actor, invite, "send_failed")
            raise AuthProblem(
                502, "email_send_failed", "the invitation could not be sent"
            ) from None
        fresh = self.auth.store.get_invite(self.org_id, invite.id)
        return Issued(fresh or invite)

    def revoke_after_failed_print(self, actor: str, invite_id: str) -> None:
        """The CLI could not print the link: revoke as `send_failed`."""
        with self.auth.store.transaction() as uow:
            invite = self._invite(uow, invite_id)
            if uow.revoke_invite(self.org_id, invite_id, self.auth.now(), "send_failed"):
                self._revoked_event(uow, actor, invite, "send_failed")

    # -- the invitee ----------------------------------------------------------------------

    def _check_token(self, token: str, client: str, user_agent: str | None) -> Invite:
        """Client limit, lockout, token check; a bad token is one answer and counts as a client
        failure. No hashing of a password happens before this passes."""
        self.auth.limit_client(client)
        now = self.auth.now()
        self.auth._locked(client, now)
        bad = AuthProblem(403, "invalid_invite", GENERIC_INVITE_FAILURE)
        digest = hash_link_token(token) if len(token) <= 256 else ""
        invite = self.auth.store.get_invite_by_token_hash(self.org_id, digest)
        record = None if invite is None else invite_link_record(invite)
        if check_link_token(token, record, now) is not LinkCheck.OK or invite is None:
            self.auth._count_failure(now, client, self.auth._seen(client, user_agent))
            raise bad
        return invite

    def inspect(self, token: str, client: str, *, user_agent: str | None = None) -> dict[str, Any]:
        invite = self._check_token(token, client, user_agent)
        return {
            "email": invite.email,
            "role": invite.role,
            "organization": self.org_name,
            "expires_at": format_time(invite.expires_at),
        }

    def accept(
        self,
        token: str,
        password: str,
        display_name: str | None,
        client: str,
        *,
        user_agent: str | None = None,
    ) -> User:
        """Consume the invite and create the user in one transaction."""
        invite = self._check_token(token, client, user_agent)
        problem = password_problem(password)
        if problem is not None:
            raise AuthProblem(422, "invalid_request", problem)
        if display_name is not None and (
            len(display_name) > MAX_DISPLAY_NAME
            or any(unicodedata.category(c) == "Cf" for c in display_name)
        ):
            raise AuthProblem(422, "invalid_request", "not a valid display name")
        settings = self.auth.settings
        with self.auth.gate.slot(settings.scrypt.memory_bytes) as got:
            if not got:
                raise AuthProblem(
                    429, "busy", "the server is busy; try again", {"Retry-After": "1"}
                )
            password_hash = hash_password(password, settings.scrypt)
        bad = AuthProblem(403, "invalid_invite", GENERIC_INVITE_FAILURE)
        now = self.auth.now()
        with self.auth.store.transaction() as uow:
            current = uow.get_invite(self.org_id, invite.id)
            address = None if current is None else current.email
            if current is None or address is None:
                raise bad
            user_id = new_user_id()
            if not uow.consume_invite(self.org_id, invite.id, current.token_hash, now, user_id):
                raise bad
            if now >= current.expires_at or not self._creator_qualifies(uow, current.created_by):
                raise bad
            if self._address_taken(uow, address):
                raise AuthProblem(409, "already_exists", "that address already has an account")
            user = User(
                org_id=self.org_id,
                id=user_id,
                email=address,
                display_name=display_name or None,
                role=Role(current.role),
                created_at=now,
                email_verified_at=now,
                email_verified_via="invite",
            )
            uow.create_user(user)
            uow.put_credential(
                Credential(
                    org_id=self.org_id, user_id=user.id, password_hash=password_hash, updated_at=now
                )
            )
            uow.adopt_security_log_rows(self.org_id, address, user.id)
            seen = self.auth._seen(client, user_agent)
            self.auth._record(
                uow,
                EventType.USER_CREATED,
                user.id,
                {"user": user.id, "role": current.role, "via": "invite"},
                subject=user.id,
                details={**seen, "via": "invite", "email": address},
            )
            self.auth._record(
                uow,
                EventType.USER_INVITE_ACCEPTED,
                user.id,
                {"invite_hash": self._hash(uow, current.id)},
                subject=user.id,
            )
        return user


__all__ = [
    "ERASED_CREATOR",
    "InviteService",
    "InviteSettings",
    "Issued",
    "invite_settings_from_env",
    "invite_view",
    "public_url_problem",
]
