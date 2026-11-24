"""Operator MFA commands (R1.38): the auth key's
creation record, rotation (re-seal), destruction, and a reset of one person's MFA.

Run from the operator's shell against the server's store; there is no HTTP route. Each action
is one transaction that writes its ledger event and exactly one security-log row. Nothing here
prints, logs or returns a key, a secret or a code.
"""

from dataclasses import dataclass

from nuramesh.core.ledger import EventType
from nuramesh.core.mfa import OrgAuthSettings
from nuramesh.server.mfa_keys import (
    KeyRing,
    MfaUnavailable,
    destroy_key_files,
    key_id,
    open_secret,
    seal_secret,
)
from nuramesh.server.sessions import AuthProblem, AuthService


class KeyCommandError(Exception):
    """The command cannot run (missing key, nothing to do); the text is safe to print."""


@dataclass(frozen=True)
class Rotation:
    """What `auth factor-key rotate` did."""

    rotated: int
    skipped: int
    unreadable: int
    from_key_id: str
    to_key_id: str


@dataclass(frozen=True)
class Destruction:
    """What `auth factor-key destroy` did."""

    key_id: str
    factors_removed: int
    policy_switched_off: bool
    files_removed: int
    users: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonStatus:
    """One line of `auth mfa status`."""

    user_id: str
    role: str
    enrolled: bool


class MfaOperator:
    """The operator's MFA actions over an `AuthService`."""

    def __init__(self, auth: AuthService) -> None:
        self.auth = auth
        self.store = auth.store
        self.org = auth.org_id

    def key_created(self, new_key_id: str, actor: str) -> None:
        """Ledger `auth.key_created` (payload exactly `key_id`) and the security-log row."""
        with self.store.transaction() as uow:
            self.auth._record(
                uow,
                EventType.AUTH_KEY_CREATED,
                actor,
                {"key_id": new_key_id},
                details={"via": "auth-key-init"},
            )

    def status(self) -> tuple[bool, list[PersonStatus]]:
        """The policy and one entry per person who is not erased, by id."""
        policy = self.store.get_org_auth_settings(self.org).mfa_required
        active = {f.user_id for f in self.store.list_mfa_factors(self.org) if f.active}
        people = [
            PersonStatus(u.id, u.role.value, u.id in active)
            for u in self.store.list_users(self.org)
            if not u.erased
        ]
        return policy, sorted(people, key=lambda p: p.user_id)

    def rotate(self, ring: KeyRing, actor: str) -> Rotation:
        """Re-seal, in one transaction, every factor sealed under the previous key. Factors on
        the current key are skipped; a factor under neither key (or one that does not open) is
        counted as unreadable and left alone. The re-sealed ones are committed regardless."""
        try:
            new_key = ring.seal_key()
        except MfaUnavailable:
            raise KeyCommandError("NURAMESH_AUTH_KEY_FILE cannot be read") from None
        old_key = ring.previous_key()
        if old_key is None:
            raise KeyCommandError("NURAMESH_AUTH_KEY_FILE_PREVIOUS is not set or cannot be read")
        new_id, old_id = key_id(new_key), key_id(old_key)
        if old_id == new_id:
            raise KeyCommandError("the previous and the current key are the same key")
        rotated = skipped = unreadable = 0
        with self.store.transaction() as uow:
            for factor in self.store.list_mfa_factors(self.org):
                if factor.key_id == new_id:
                    skipped += 1
                    continue
                if factor.key_id != old_id:
                    unreadable += 1
                    continue
                try:
                    secret = open_secret(
                        old_key, factor.secret_enc, self.org, factor.user_id, factor.id
                    )
                except MfaUnavailable:
                    unreadable += 1
                    continue
                sealed = seal_secret(new_key, secret, self.org, factor.user_id, factor.id)
                if uow.reseal_mfa_factor(
                    self.org, factor.user_id, factor.id, old_id, sealed, new_id
                ):
                    rotated += 1
                else:  # changed under us: left for the next run
                    unreadable += 1
            self.auth._record(
                uow,
                EventType.AUTH_KEY_ROTATED,
                actor,
                {"from_key_id": old_id, "to_key_id": new_id, "factors": rotated},
                details={"via": "auth-key-rotate"},
            )
        return Rotation(rotated, skipped, unreadable, old_id, new_id)

    def reset_user(self, user_id: str, actor: str) -> tuple[bool, int]:
        """Remove one person's factor and recovery codes and end their sessions (`mfa_changed`).
        `(changed, sessions revoked)`; `AuthProblem` 404 for an unknown person. A person without
        MFA changes nothing and writes no event."""
        with self.store.transaction() as uow:
            if self.auth._target(uow, user_id).disabled:  # ND333b, mi10
                raise AuthProblem(409, "user_disabled", "enable the person first")
            if not uow.delete_mfa_factor(self.org, user_id):
                return False, 0
            uow.delete_mfa_challenges(self.org, user_id)  # MJ1: they die with the factor
            now = self.auth.now()
            revoked = 0
            for session in self.store.list_auth_sessions(self.org):
                if session.user_id == user_id and session.active(now):
                    revoked += int(self.auth._revoke(uow, session, now, "mfa_changed"))
            self.auth._record(
                uow,
                EventType.AUTH_MFA_RESET,
                actor,
                {"sessions_revoked": revoked},
                subject=user_id,
                details={"via": "mfa-reset"},
            )
        return True, revoked

    def check_destroyable(self, ring: KeyRing | None) -> str:
        """The id of the key `auth factor-key destroy` would erase, or `KeyCommandError`.

        The current key file must be set and readable, and (when factors exist) must be the key
        that sealed at least one of them (current or previous id). Never falls back to a
        factor's recorded key id or any guessed one: without the key file nothing is destroyed.
        """
        if ring is None or ring.current is None:
            raise KeyCommandError(
                "NURAMESH_AUTH_KEY_FILE is not set; "
                "auth factor-key destroy needs the key file it erases"
            )
        try:
            current = key_id(ring.seal_key())
        except MfaUnavailable:
            raise KeyCommandError(
                "NURAMESH_AUTH_KEY_FILE cannot be read (missing, unreadable or invalid); "
                "auth factor-key destroy needs the key file it erases"
            ) from None
        factors = self.store.list_mfa_factors(self.org)
        if factors:
            known = {current}
            previous = ring.previous_key()
            if previous is not None:
                known.add(key_id(previous))
            if not any(f.key_id in known for f in factors):
                raise KeyCommandError(
                    "NURAMESH_AUTH_KEY_FILE names a key that sealed none of the factors "
                    "(wrong file); point it at the key file in use"
                )
        return current

    def destroy(self, ring: KeyRing | None, actor: str) -> Destruction:
        """Delete every factor, recovery code and challenge, switch the policy off, ledger it,
        and after the commit zero and unlink the key files. Sessions are untouched. Refuses
        (`KeyCommandError`, nothing changed) unless the key file is readable and right."""
        current = self.check_destroyable(ring)
        assert ring is not None
        switched_off = False
        with self.store.transaction() as uow:
            owners = tuple(f.user_id for f in self.store.list_mfa_factors(self.org))
            removed = uow.delete_all_mfa(self.org)
            if uow.get_org_auth_settings(self.org).mfa_required:
                uow.put_org_auth_settings(
                    OrgAuthSettings(
                        org_id=self.org,
                        mfa_required=False,
                        updated_at=self.auth.now(),
                        updated_by=actor,
                    )
                )
                self.auth._record(
                    uow,
                    EventType.AUTH_MFA_POLICY_SET,
                    actor,
                    {"required": False},
                    details={"via": "auth-key-destroy"},
                )
                switched_off = True
            self.auth._record(
                uow,
                EventType.AUTH_KEY_DESTROYED,
                actor,
                {"key_id": current, "factors_removed": removed},
                details={"via": "auth-key-destroy"},
            )
        files = destroy_key_files(ring.current, ring.previous)
        return Destruction(current, removed, switched_off, files, owners)
