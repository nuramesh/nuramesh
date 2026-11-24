"""MFA for a signed-in person and the owner's policy (R1.38).

Enrol (password, then a TOTP code), regenerate the MFA recovery codes, disable, an owner's reset
of another person and the organization's `mfa_required` setting. Every wrong code counts on the
`mfa:` counter (keyed by the user id) and the client counter; while `mfa:` is locked a code is
refused 429 before it is checked. Fails closed: with no usable auth key TOTP answers 503
`mfa_unavailable` (not counted as a wrong code).
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from nuramesh.core.auth import Role, SecurityDetail, User, address_key, identifier_hash
from nuramesh.core.identity import (
    new_recovery_codes,
    new_totp_secret,
    normalise_recovery_code,
    provisioning_uri,
    recovery_code_hash,
    secret_to_base32,
    verify_totp,
)
from nuramesh.core.ledger import EventType
from nuramesh.core.mfa import MfaFactor, OrgAuthSettings
from nuramesh.server.mfa_keys import KeyRing, MfaUnavailable, key_id, open_secret, seal_secret
from nuramesh.server.mfa_notice import NoticeDesk, slot_of
from nuramesh.server.sessions import AuthProblem, AuthService, _Reserved
from nuramesh.stores import UnitOfWork

FRESH_PASSWORD = timedelta(seconds=300)
"""A factor is activated only if the password was entered within this long (Q13)."""
T = TypeVar("T")
S = TypeVar("S")


class _Replay(Exception):
    """The code's step was spent meanwhile (a concurrent use): counted as a wrong code."""


@dataclass(frozen=True)
class Asker:
    """Who asks and from where (the security log only)."""

    user_id: str
    session_id: str | None
    client: str
    user_agent: str | None


def _unavailable() -> AuthProblem:
    return AuthProblem(
        503, "mfa_unavailable", "multi-factor sign-in is not available on this server"
    )


def _invalid() -> AuthProblem:
    return AuthProblem(401, "mfa_invalid", "the code is not right")


class MfaService:
    """The MFA rules of one organization over an `AuthService`."""

    def __init__(self, auth: AuthService, desk: NoticeDesk | None = None) -> None:
        self.auth = auth
        self.desk = desk
        self.store = auth.store
        self.org = auth.org_id

    # -- the key ---------------------------------------------------------------------------

    @property
    def keys(self) -> KeyRing | None:
        return self.auth.settings.mfa_keys

    def available(self) -> bool:
        return self.keys is not None and self.keys.available()

    def _seal_key(self) -> bytes:
        if self.keys is None:
            raise _unavailable()
        try:
            return self.keys.seal_key()
        except MfaUnavailable:
            raise _unavailable() from None

    def _secret_of(self, factor: MfaFactor) -> bytes:
        """The factor's TOTP secret, or 503 `mfa_unavailable` (never read as "no MFA")."""
        key = None if self.keys is None else self.keys.key_for(factor.key_id)
        if key is None:
            raise _unavailable()
        try:
            return open_secret(key, factor.secret_enc, self.org, factor.user_id, factor.id)
        except MfaUnavailable:
            raise _unavailable() from None

    # -- the `mfa:` and client counters ---------------------------------------------------

    def _reserve(self, user_id: str, client: str) -> list[_Reserved]:
        """Check the locks and reserve one failure on the `mfa:` and client counters in one
        transaction; locked: 429 `too_many_attempts`, nothing written."""
        a = self.auth
        with self.store.transaction() as uow:
            now = a.now()
            keys = [
                (
                    "client",
                    a._counter_key(uow, now, "ip", address_key(client)),
                    a.settings.ip_lockout,
                ),
                (
                    "mfa",
                    a._counter_key(uow, now, "mfa", user_id, user_id),
                    a.settings.account_lockout,
                ),
            ]
            current = [(s, k, p, uow.get_auth_failure(k)) for s, k, p in keys]
            wait = max((p.retry_after(f, now) for _, _, p, f in current), default=0)
            if wait > 0:
                raise a._too_many(wait)
            reserved = []
            for scope, key, policy, previous in current:
                failure = policy.after_failure(previous, key, now)
                uow.put_auth_failure(failure)
                reserved.append(_Reserved(scope, key, policy, previous, failure))
        return reserved

    def _count_failure(self, reserved: list[_Reserved], who: Asker) -> None:
        """Record the wrong code: `auth.login_failed` (`via` mfa), then `auth.lockout` (scope
        `mfa` or `client`) for each lock it started, in one transaction."""
        a = self.auth
        seen: dict[str, SecurityDetail] = a._seen(who.client, who.user_agent)
        try:
            with self.store.transaction() as uow:
                started = {}
                details: dict[str, SecurityDetail] = {**seen, "via": "mfa"}
                for r in reserved:
                    current = uow.get_auth_failure(r.key)
                    if current is None:
                        continue
                    details[f"{r.scope}_failures"] = current.count
                    if r.reserved.locked_until is not None and current.locked_until is not None:
                        started[r.scope] = current
                hashes = {
                    "mfa": identifier_hash(a._key(uow, who.user_id), self.org, who.user_id),
                    "client": a._address_hash(uow, who.client),
                }
                a._record(
                    uow,
                    EventType.AUTH_LOGIN_FAILED,
                    "system",
                    {"identifier_hash": hashes["mfa"], "via": "mfa"},
                    subject=who.user_id,
                    details=details,
                )
                a._lockouts(uow, started, hashes, details, who.user_id)
        except BaseException:
            a._release(reserved)
            raise

    def _clear(self, uow: UnitOfWork, reserved: list[_Reserved]) -> None:
        """A right code: the `mfa:` counter is cleared, the client's earlier failures stay."""
        for r in reserved:
            if r.scope == "mfa":
                uow.delete_auth_failure(r.key)
            else:
                self.auth._give_back(uow, r)

    def spend_code(
        self,
        who: Asker,
        factor: MfaFactor,
        code: str,
        work: Callable[[UnitOfWork, int], T],
    ) -> T:
        """Check a TOTP code against `factor` and, if right, run `work(uow, step)` in one
        transaction that also clears the counter. Order: locks (429), key (503), verify; a wrong
        or replayed code is 401 `mfa_invalid` and counted. `work` must spend the step itself
        (`advance_mfa_step` or `activate_mfa_factor`) and raise `_Replay` if it changed nothing."""
        a = self.auth

        def check() -> int | None:
            secret = self._secret_of(factor)
            return verify_totp(secret, code, int(a.now().timestamp()), factor.last_used_step)

        return self.spend(who, check, work)

    def spend(
        self,
        who: Asker,
        check: Callable[[], S | None],
        work: Callable[[UnitOfWork, S], T],
    ) -> T:
        """The shared order of every second-factor check: lock reservation (429), `check()` (None
        is a wrong code; it may raise 503), then `work(uow, value)` in one transaction that also
        clears the `mfa:` counter. A wrong code or a `_Replay` is counted and 401 `mfa_invalid`;
        any other refusal gives the reservation back."""
        a = self.auth
        reserved = self._reserve(who.user_id, who.client)
        try:
            value = check()
        except BaseException:
            a._release(reserved)
            raise
        if value is None:
            self._count_failure(reserved, who)
            raise _invalid()
        try:
            with self.store.transaction() as uow:
                self._clear(uow, reserved)
                return work(uow, value)
        except _Replay:
            self._count_failure(reserved, who)  # rolled back: the reservation stands, now counted
            raise _invalid() from None
        except BaseException:
            a._release(reserved)
            raise

    # -- helpers ---------------------------------------------------------------------------

    def _active_user(self, uow: UnitOfWork, user_id: str) -> User:
        user = uow.get_user(self.org, user_id)
        if user is None or user.erased or user.disabled:
            raise AuthProblem(401, "session_invalid", "sign in again")
        return user

    def _revoke_sessions(
        self, uow: UnitOfWork, user_id: str, now: datetime, keep: str | None
    ) -> int:
        count = 0
        for session in self.store.list_auth_sessions(self.org):
            if session.user_id == user_id and session.id != keep and session.active(now):
                count += int(self.auth._revoke(uow, session, now, "mfa_changed"))
        return count

    def _mark_current(self, uow: UnitOfWork, who: Asker, now: datetime) -> None:
        if who.session_id is not None:
            uow.set_auth_session_mfa(self.org, who.session_id, now, "totp")

    def _event(
        self,
        uow: UnitOfWork,
        kind: EventType,
        actor: str,
        payload: dict[str, Any],
        subject: str | None,
    ) -> None:
        self.auth._record(uow, kind, actor, payload, subject=subject)

    def _store_codes(self, uow: UnitOfWork, user_id: str, now: datetime) -> list[str]:
        codes = new_recovery_codes()
        hashes = []
        for code in codes:
            normal = normalise_recovery_code(code)
            assert normal is not None
            hashes.append(recovery_code_hash(normal))
        uow.replace_mfa_recovery_codes(self.org, user_id, hashes, now)
        return codes

    def policy(self) -> OrgAuthSettings:
        return self.store.get_org_auth_settings(self.org)

    # -- the person's own routes ------------------------------------------------------------

    def status(self, user_id: str) -> dict[str, Any]:
        user = self.auth.store.get_user(self.org, user_id)
        if user is None or user.erased:
            raise AuthProblem(401, "session_invalid", "sign in again")
        factor = self.store.get_mfa_factor(self.org, user_id)
        active = factor is not None and factor.active
        return {
            "available": self.available(),
            "enrolled": active,
            "confirmed_at": factor.confirmed_at.isoformat()
            if factor is not None and factor.confirmed_at is not None
            else None,
            "recovery_codes_remaining": self.store.count_mfa_recovery_codes(self.org, user_id)
            if active
            else 0,
            "required": user.role == Role.OWNER and self.policy().mfa_required,
        }

    def enroll(self, who: Asker, password: str) -> dict[str, str]:
        """Start enrolment: a pending factor with a new secret (shown once, never stored in the
        clear). 503 without a key; 401 for a wrong password; 409 when already enrolled."""
        key = self._seal_key()
        self.auth.confirm_password(who.user_id, password, who.client, user_agent=who.user_agent)
        now = self.auth.now()
        with self.store.transaction() as uow:
            user = self._active_user(uow, who.user_id)
            if user.email is None:
                raise AuthProblem(401, "session_invalid", "sign in again")
            current = uow.get_mfa_factor(self.org, who.user_id)
            if current is not None and current.active:
                raise AuthProblem(409, "mfa_already_enrolled", "multi-factor sign-in is on")
            secret = new_totp_secret()
            factor_id = "mf_" + secrets.token_hex(10)
            uow.put_mfa_factor(
                MfaFactor(
                    org_id=self.org,
                    user_id=who.user_id,
                    id=factor_id,
                    secret_enc=seal_secret(key, secret, self.org, who.user_id, factor_id),
                    key_id=key_id(key),
                    created_at=now,
                    password_checked_at=now,
                )
            )
        return {
            "factor_id": factor_id,
            "secret": secret_to_base32(secret),
            "otpauth_uri": provisioning_uri(self.auth.settings.mfa_issuer, user.email, secret),
        }

    def confirm_enroll(self, who: Asker, factor_id: str, code: str) -> list[str]:
        """Activate the pending factor with a right code; returns the ten MFA recovery codes."""
        self._seal_key()
        now = self.auth.now()
        factor = self.store.get_mfa_factor(self.org, who.user_id)
        if factor is not None and factor.active and factor.id == factor_id:
            raise AuthProblem(409, "mfa_already_enrolled", "multi-factor sign-in is on")
        if factor is None or factor.active or factor.id != factor_id or factor.pending_expired(now):
            raise AuthProblem(409, "no_pending_factor", "start enrolment again")
        if now - factor.password_checked_at >= FRESH_PASSWORD:
            with self.store.transaction() as uow:
                current = uow.get_mfa_factor(self.org, who.user_id)
                if current is not None and current.id == factor.id and not current.active:
                    uow.delete_mfa_factor(self.org, who.user_id)
            raise AuthProblem(409, "reauth_required", "enter your password again")

        def work(uow: UnitOfWork, step: int) -> list[str]:
            current = uow.get_mfa_factor(self.org, who.user_id)
            if current is None or current.id != factor.id:
                raise AuthProblem(409, "no_pending_factor", "start enrolment again")
            if current.active or not uow.activate_mfa_factor(
                self.org, who.user_id, factor.id, now, step
            ):
                raise AuthProblem(409, "mfa_already_enrolled", "multi-factor sign-in is on")
            self._active_user(uow, who.user_id)
            codes = self._store_codes(uow, who.user_id, now)
            self._revoke_sessions(uow, who.user_id, now, who.session_id)
            self._mark_current(uow, who, now)
            self._event(
                uow, EventType.AUTH_MFA_ENROLLED, who.user_id, {"method": "totp"}, who.user_id
            )
            return codes

        with slot_of(self.desk, who.user_id, "turned on") as notice:
            codes = self.spend_code(who, factor, code, work)
            notice.send(who.user_id)
        return codes

    def _active_factor(self, user_id: str) -> MfaFactor:
        factor = self.store.get_mfa_factor(self.org, user_id)
        if factor is None or not factor.active:
            raise AuthProblem(409, "mfa_not_enrolled", "multi-factor sign-in is not on")
        return factor

    def regenerate(self, who: Asker, password: str, code: str) -> list[str]:
        """Ten new MFA recovery codes; every old one stops working."""
        self._seal_key()
        self.auth.confirm_password(who.user_id, password, who.client, user_agent=who.user_agent)
        factor = self._active_factor(who.user_id)
        now = self.auth.now()

        def work(uow: UnitOfWork, step: int) -> list[str]:
            if not uow.advance_mfa_step(self.org, who.user_id, factor.id, step):
                raise _Replay
            codes = self._store_codes(uow, who.user_id, now)
            self._event(
                uow,
                EventType.AUTH_MFA_RECOVERY_REGENERATED,
                who.user_id,
                {"count": len(codes)},
                who.user_id,
            )
            return codes

        return self.spend_code(who, factor, code, work)

    def disable(self, who: Asker, password: str, code: str) -> None:
        """Turn the person's MFA off. An owner may not while the policy is on."""
        self._seal_key()
        self.auth.confirm_password(who.user_id, password, who.client, user_agent=who.user_agent)
        factor = self._active_factor(who.user_id)
        self._policy_allows_disable(self.store.get_user(self.org, who.user_id))
        now = self.auth.now()

        def work(uow: UnitOfWork, step: int) -> None:
            self._policy_allows_disable(uow.get_user(self.org, who.user_id), uow)
            if not uow.advance_mfa_step(self.org, who.user_id, factor.id, step):
                raise _Replay
            uow.delete_mfa_factor(self.org, who.user_id)
            uow.delete_mfa_challenges(self.org, who.user_id)  # MJ1: they die with the factor
            self._revoke_sessions(uow, who.user_id, now, who.session_id)
            self._mark_current(uow, who, now)
            self._event(
                uow, EventType.AUTH_MFA_DISABLED, who.user_id, {"reason": "self"}, who.user_id
            )

        with slot_of(self.desk, who.user_id, "turned off") as notice:
            self.spend_code(who, factor, code, work)
            notice.send(who.user_id)

    def _policy_allows_disable(self, user: User | None, uow: UnitOfWork | None = None) -> None:
        if user is None or user.erased:
            raise AuthProblem(401, "session_invalid", "sign in again")
        settings = (uow or self.store).get_org_auth_settings(self.org)
        if user.role == Role.OWNER and settings.mfa_required:
            raise AuthProblem(
                409, "mfa_required_by_policy", "your organization requires MFA for owners"
            )

    # -- the owner's routes -----------------------------------------------------------------

    def reset_in(self, uow: UnitOfWork, actor: str, user_id: str) -> tuple[bool, int]:
        """An owner removes another person's MFA: `(changed, sessions revoked)`; all of the
        person's sessions end (`mfa_changed`)."""
        a = self.auth
        a._active_owner(uow, actor)
        if user_id == actor:
            raise AuthProblem(409, "use_self_service", "turn off your own MFA yourself")
        if a._target(uow, user_id).disabled:  # ND333b, mi10: re-enable the person first
            raise AuthProblem(409, "user_disabled", "enable the person first")
        now = a.now()
        if not uow.delete_mfa_factor(self.org, user_id):
            return False, 0
        uow.delete_mfa_challenges(self.org, user_id)  # MJ1: they die with the factor
        revoked = self._revoke_sessions(uow, user_id, now, None)
        self._event(uow, EventType.AUTH_MFA_RESET, actor, {"sessions_revoked": revoked}, user_id)
        return True, revoked

    def settings_view(self) -> dict[str, bool]:
        return {"mfa_required_for_owners": self.policy().mfa_required}

    def set_policy_in(self, uow: UnitOfWork, actor: str, required: bool) -> None:
        """Turn the owners' MFA policy on or off. On needs a usable key and the caller enrolled."""
        self.auth._active_owner(uow, actor)
        if required:
            if not self.available():
                raise _unavailable()
            factor = uow.get_mfa_factor(self.org, actor)
            if factor is None or not factor.active:
                raise AuthProblem(409, "mfa_not_enrolled", "enrol your own MFA first")
        now = self.auth.now()
        uow.put_org_auth_settings(
            OrgAuthSettings(
                org_id=self.org, mfa_required=required, updated_at=now, updated_by=actor
            )
        )
        self._event(uow, EventType.AUTH_MFA_POLICY_SET, actor, {"required": required}, None)

    def set_policy(self, actor: str, required: bool) -> None:
        with self.store.transaction() as uow:
            self.set_policy_in(uow, actor, required)
