"""The second step of a login (R1.38).

A correct password that needs a second factor answered a challenge (`AuthService.login`); these
routes turn a challenge and a right code into a session. Everything fails closed: an unusable
challenge is one answer (401 `challenge_invalid`) that counts nothing; a wrong code counts on the
challenge (five), the `mfa:` counter and the client counter; the session is made in the same
transaction that re-reads the factor and spends the code, so a changed factor or a replayed
code can never open one.
"""

import secrets
from typing import Literal

from nuramesh.core.auth import User
from nuramesh.core.identity import (
    new_totp_secret,
    normalise_recovery_code,
    provisioning_uri,
    recovery_code_hash,
    secret_to_base32,
    verify_totp,
)
from nuramesh.core.ledger import EventType
from nuramesh.core.mfa import (
    CHALLENGE_PREFIX,
    MFA_CHALLENGE_ATTEMPTS,
    MfaChallenge,
    MfaFactor,
    challenge_hash,
)
from nuramesh.server.mfa_keys import key_id, seal_secret
from nuramesh.server.mfa_notice import slot_of
from nuramesh.server.mfa_service import Asker, MfaService, _Replay
from nuramesh.server.sessions import AuthProblem, Started
from nuramesh.stores import UnitOfWork

Purpose = Literal["verify", "enrol"]
MAX_TOKEN = 128


def _gone() -> AuthProblem:
    return AuthProblem(
        401, "challenge_invalid", "this sign-in step is no longer valid; sign in again"
    )


class MfaLogin:
    """The login-time MFA steps of one organization."""

    def __init__(self, mfa: MfaService) -> None:
        self.mfa = mfa
        self.auth = mfa.auth
        self.store = mfa.store
        self.org = mfa.org

    # -- the challenge ---------------------------------------------------------------------

    def _challenge(self, token: str, purpose: Purpose) -> MfaChallenge:
        """The usable challenge for `token`, or 401 `challenge_invalid` (one body for an unknown,
        used, expired, wrong-purpose or exhausted challenge, and a user who is gone). An enrol
        challenge that ran out also drops the pending factor recorded on it (Q13)."""
        if not token.startswith(CHALLENGE_PREFIX) or len(token) > MAX_TOKEN:
            raise _gone()
        found = self.store.get_mfa_challenge(self.org, challenge_hash(token))
        if found is None or found.purpose != purpose:
            raise _gone()
        now = self.auth.now()
        if found.consumed_at is None and now >= found.expires_at:
            if purpose == "enrol" and found.factor_id is not None:
                self._drop_pending(found)
            raise _gone()
        if found.consumed_at is not None or found.attempts >= MFA_CHALLENGE_ATTEMPTS:
            raise _gone()
        user = self.store.get_user(self.org, found.user_id)
        if user is None or user.erased or user.disabled:
            raise _gone()
        return found

    def _drop_pending(self, challenge: MfaChallenge) -> None:
        with self.store.transaction() as uow:
            current = uow.get_mfa_factor(self.org, challenge.user_id)
            if current is not None and current.id == challenge.factor_id and not current.active:
                uow.delete_mfa_factor(self.org, challenge.user_id)

    def _consume(self, uow: UnitOfWork, challenge: MfaChallenge) -> None:
        if not uow.consume_mfa_challenge(self.org, challenge.token_hash, self.auth.now()):
            raise _gone()

    def _user(self, uow: UnitOfWork, user_id: str) -> User:
        user = uow.get_user(self.org, user_id)
        if user is None or user.erased or user.disabled:
            raise _gone()
        # MJ1/Q15: while a reset has blocked the old password, no challenge may mint anything
        if uow.get_password_login_block(self.org, user_id) is not None:
            raise _gone()
        return user

    def _attempt(self, challenge: MfaChallenge) -> None:
        with self.store.transaction() as uow:
            uow.add_mfa_challenge_attempt(self.org, challenge.token_hash)

    def _asker(self, challenge: MfaChallenge, client: str, user_agent: str | None) -> Asker:
        return Asker(challenge.user_id, None, client, user_agent)

    # -- login/mfa -------------------------------------------------------------------------

    def verify(
        self,
        token: str,
        code: str | None,
        recovery_code: str | None,
        client: str,
        user_agent: str | None,
        previous_refresh: str | None,
    ) -> Started:
        """A TOTP code or an MFA recovery code for a `verify` challenge: the session."""
        a = self.auth
        a.limit_client(client)
        challenge = self._challenge(token, "verify")
        who = self._asker(challenge, client, user_agent)
        seen = a._seen(client, user_agent)
        factor = self.store.get_mfa_factor(self.org, challenge.user_id)
        if factor is None or not factor.active or factor.id != challenge.factor_id:
            raise _gone()

        def settle(uow: UnitOfWork) -> User:
            """Inside the winning transaction: the challenge, the factor and the user as they
            are now (a reset, a replaced factor or a disabled user ends it)."""
            self._consume(uow, challenge)
            current = uow.get_mfa_factor(self.org, challenge.user_id)
            if current is None or not current.active or current.id != challenge.factor_id:
                raise _gone()
            return self._user(uow, challenge.user_id)

        try:
            if code is not None:

                def check_totp() -> int | None:
                    secret = self.mfa._secret_of(factor)
                    now = int(a.now().timestamp())
                    return verify_totp(secret, code, now, factor.last_used_step)

                def work_totp(uow: UnitOfWork, step: int) -> Started:
                    user = settle(uow)
                    if not uow.advance_mfa_step(self.org, user.id, factor.id, step):
                        raise _Replay
                    return a.open_session(
                        uow,
                        user,
                        a.now(),
                        seen,
                        previous_refresh=previous_refresh,
                        mfa_method="totp",
                    )

                return self.mfa.spend(who, check_totp, work_totp)

            assert recovery_code is not None
            normal = normalise_recovery_code(recovery_code)

            def check_recovery() -> str | None:
                return None if normal is None else recovery_code_hash(normal)

            def work_recovery(uow: UnitOfWork, digest: str) -> Started:
                user = settle(uow)
                now = a.now()
                if not uow.use_mfa_recovery_code(self.org, user.id, digest, now):
                    raise _Replay
                remaining = uow.count_mfa_recovery_codes(self.org, user.id)
                a._record(
                    uow,
                    EventType.AUTH_MFA_RECOVERY_USED,
                    user.id,
                    {"remaining": remaining},
                    subject=user.id,
                )
                return a.open_session(
                    uow,
                    user,
                    now,
                    seen,
                    previous_refresh=previous_refresh,
                    mfa_method="recovery",
                )

            return self.mfa.spend(who, check_recovery, work_recovery)
        except AuthProblem as exc:
            if exc.code == "mfa_invalid":
                self._attempt(challenge)
            raise

    # -- login/mfa/enroll and enroll/confirm -------------------------------------------------

    def enroll(self, token: str, client: str) -> dict[str, str]:
        """Start the enrolment an `enrol` challenge asks for: a pending factor whose id is
        recorded on the challenge. One start per challenge; an active factor appearing meanwhile
        ends the challenge and touches nothing."""
        a = self.auth
        a.limit_client(client)
        challenge = self._challenge(token, "enrol")
        if challenge.factor_id is not None:
            raise _gone()
        key = self.mfa._seal_key()
        now = a.now()
        secret = new_totp_secret()
        factor_id = "mf_" + secrets.token_hex(10)
        refused = False
        with self.store.transaction() as uow:
            user = self._user(uow, challenge.user_id)
            current = uow.get_mfa_factor(self.org, user.id)
            if current is not None and current.active:
                self._consume(uow, challenge)
                refused = True  # commit the consumption, then answer
            else:
                uow.put_mfa_factor(
                    MfaFactor(
                        org_id=self.org,
                        user_id=user.id,
                        id=factor_id,
                        secret_enc=seal_secret(key, secret, self.org, user.id, factor_id),
                        key_id=key_id(key),
                        created_at=now,
                        password_checked_at=challenge.created_at,
                    )
                )
                if not uow.set_mfa_challenge_factor(self.org, challenge.token_hash, factor_id):
                    raise _gone()
        if refused:
            raise _gone()
        return {
            "factor_id": factor_id,
            "secret": secret_to_base32(secret),
            "otpauth_uri": provisioning_uri(a.settings.mfa_issuer, user.email or "", secret),
        }

    def confirm(
        self,
        token: str,
        factor_id: str,
        code: str,
        client: str,
        user_agent: str | None,
        previous_refresh: str | None,
    ) -> tuple[Started, list[str]]:
        """Confirm the enrolment with a right code: the factor, ten MFA recovery codes and the
        session, in one transaction. The factor must be the one recorded on this challenge."""
        a = self.auth
        a.limit_client(client)
        challenge = self._challenge(token, "enrol")
        who = self._asker(challenge, client, user_agent)
        seen = a._seen(client, user_agent)
        now = a.now()
        factor = self.store.get_mfa_factor(self.org, challenge.user_id)
        if (
            challenge.factor_id is None
            or challenge.factor_id != factor_id
            or factor is None
            or factor.active
            or factor.id != factor_id
            or factor.pending_expired(now)
        ):
            raise _gone()

        def check() -> int | None:
            return verify_totp(
                self.mfa._secret_of(factor), code, int(a.now().timestamp()), factor.last_used_step
            )

        def work(uow: UnitOfWork, step: int) -> tuple[Started, list[str]]:
            self._consume(uow, challenge)
            current = uow.get_mfa_factor(self.org, challenge.user_id)
            if current is None or current.id != factor_id or current.active:
                raise _gone()
            if not uow.activate_mfa_factor(self.org, challenge.user_id, factor_id, a.now(), step):
                raise _gone()
            user = self._user(uow, challenge.user_id)
            codes = self.mfa._store_codes(uow, user.id, a.now())
            a._record(
                uow,
                EventType.AUTH_MFA_ENROLLED,
                user.id,
                {"method": "totp"},
                subject=user.id,
            )
            started = a.open_session(
                uow, user, a.now(), seen, previous_refresh=previous_refresh, mfa_method="totp"
            )
            return started, codes

        try:
            with slot_of(self.mfa.desk, challenge.user_id, "turned on") as notice:
                done = self.mfa.spend(who, check, work)
                notice.send(challenge.user_id)
            return done
        except AuthProblem as exc:
            if exc.code == "mfa_invalid":
                self._attempt(challenge)
            raise
