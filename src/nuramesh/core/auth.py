"""Local accounts: users, roles, password hashes and the first-owner setup token (R1.22 PR 1).

What lives here is the **format and the checks**, as pure functions and models (stdlib and pydantic
only). Sessions, login endpoints, rate limits and the **concurrency semaphore that bounds password
hashing** belong to the server layer (PR 2): a scrypt hash costs about 32 MiB, so an
unauthenticated flood must queue before it hashes, and that queue is not a `core` concern.

**People:** a user is `human:u_<20 hex>` everywhere the system names them (T42). The `users` row
holds the email and display name apart from that opaque id, and erasing a user clears them and
leaves the id, so ledger events that name the id keep their bytes and the chain still verifies.

**Passwords** are hashed with scrypt (standard library, ND53 B). Every stored hash is
self-describing, `scrypt$n$r$p$salt$hash`, so a later move to Argon2id verifies old hashes and
re-hashes at the next login (`needs_rehash`) with no resets. The parameters are data
(`ScryptParams`), measured in T64.

**Setup token:** no default password exists. The first start issues one random 256-bit token for
the first owner; only its SHA-256 is stored, it is single use and it expires (24 h by default).

**Data rule (W.03, ND52):** until the lawful-basis answer arrives, the users table holds
**synthetic data only**: accounts and people are customer data too.
"""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import unicodedata
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nuramesh.core.ledger import Actor
from nuramesh.core.people import is_human_id

# ---- users --------------------------------------------------------------------------------------


class Role(StrEnum):
    """What a person may do (`ROLE_PERMISSIONS`; the endpoint table is in the
    platform architecture doc)."""

    OWNER = "owner"
    ADMIN = "admin"
    BUILDER = "builder"
    APPROVER = "approver"
    VIEWER = "viewer"


_ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 1,
    Role.APPROVER: 2,
    Role.BUILDER: 2,
    Role.ADMIN: 3,
    Role.OWNER: 4,
}


def role_rank(role: Role) -> int:
    """The rank used by the rank rule: viewer 1, approver 2, builder 2,
    admin 3, owner 4. Approver and builder are equal, so neither outranks the other.

    Raises ValueError if `role` is not a Role member (a plain string is refused, even "owner")."""
    if not isinstance(role, Role):
        raise ValueError(f"not a role: {role!r}")
    return _ROLE_RANK[role]


def new_user_id() -> str:
    """A fresh opaque person id, `human:u_` and 20 random hex digits (80 bits)."""
    return f"human:u_{secrets.token_hex(10)}"


MAX_EMAIL_LENGTH = 254
_EMAIL_FORBIDDEN = re.compile(r"[\s\x00-\x1f\x7f]")


def normalize_email(email: str) -> str:
    """The stored form of an email: NFKC, trimmed, lower-cased. Uniqueness is on this form.

    Deliberately small: exactly one `@`, a non-empty local part and a domain with a dot, no
    whitespace or control characters, at most 254 characters. Not a full RFC 5322 check (the
    only real proof of an address is a mail that arrives).
    """
    value = unicodedata.normalize("NFKC", email).strip().casefold()
    local, at, domain = value.partition("@")
    if (
        not value
        or len(value) > MAX_EMAIL_LENGTH
        or _EMAIL_FORBIDDEN.search(value)
        or not at
        or not local
        or "@" in domain
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or ".." in domain
    ):
        raise ValueError("not a valid email address")
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("times must carry a time zone")
    return value


class User(BaseModel):
    """One person. `email` and `display_name` are personal data: kept out of `repr`, and both are
    None once the user is erased (`erased_at` set)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    org_id: str = Field(min_length=1, max_length=128)
    id: str
    email: str | None = Field(default=None, repr=False)
    display_name: str | None = Field(default=None, max_length=200, repr=False)
    role: Role
    created_at: datetime
    erased_at: datetime | None = None
    disabled_at: datetime | None = None
    email_verified_at: datetime | None = None
    email_verified_via: str | None = None  # setup | invite | link | legacy (R1.37)

    @field_validator("id")
    @classmethod
    def _person_id(cls, value: str) -> str:
        if not is_human_id(value):
            raise ValueError("a user id is human:<opaque id> (T42)")
        return value

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else normalize_email(value)

    @field_validator("created_at", "erased_at", "disabled_at", "email_verified_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _erased_means_empty(self) -> "User":
        if self.erased_at is None and self.email is None:
            raise ValueError("a user who is not erased has an email")
        if self.erased_at is not None and (self.email is not None or self.display_name is not None):
            raise ValueError("an erased user keeps no email or name")
        return self

    @property
    def erased(self) -> bool:
        """Whether the personal data was erased."""
        return self.erased_at is not None

    @property
    def disabled(self) -> bool:
        """Whether an owner disabled the person (E1: no sign-in, no session, no token)."""
        return self.disabled_at is not None


def erased_copy(user: User, at: datetime) -> User:
    """`user` after erasure: same id, org, role and creation time; no email, no name."""
    return user.model_copy(
        update={
            "email": None,
            "display_name": None,
            "erased_at": at,
            "email_verified_at": None,
            "email_verified_via": None,
        }
    )


# ---- password hashes ----------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # characters; bounds the work one request can ask for
ALGORITHM = "scrypt"
_MAXMEM_MARGIN = 1 << 20

# What a stored hash may claim. Parsing refuses anything outside, so a tampered row can't make
# the server allocate gigabytes.
_MAX_N = 1 << 20
_MAX_R = 32
_MAX_P = 16
_MAX_MEMORY = 1 << 30  # 1 GiB: 128 * r * (n + p + 2) of any accepted hash stays under this


class ScryptParams(BaseModel):
    """The scrypt cost settings and the sizes of salt and hash (data, not literals: T64)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    n: int = 1 << 15
    r: int = 8
    p: int = 3
    salt_bytes: int = 16
    key_bytes: int = 32

    @model_validator(mode="after")
    def _bounds(self) -> "ScryptParams":
        if self.n < 2 or self.n > _MAX_N or self.n & (self.n - 1):
            raise ValueError(f"n must be a power of two up to {_MAX_N}")
        if not 1 <= self.r <= _MAX_R or not 1 <= self.p <= _MAX_P:
            raise ValueError("r and p are out of range")
        if not 8 <= self.salt_bytes <= 64 or not 16 <= self.key_bytes <= 64:
            raise ValueError("salt must be 8 to 64 bytes and the hash 16 to 64")
        if self.memory_bytes > _MAX_MEMORY:
            raise ValueError("these parameters need more than 1 GiB")
        return self

    @property
    def memory_bytes(self) -> int:
        """What OpenSSL's scrypt needs: 128 * r * (n + p + 2)."""
        return 128 * self.r * (self.n + self.p + 2)

    @property
    def maxmem(self) -> int:
        """The `maxmem` to pass to `hashlib.scrypt`: always above 128 * n * r, with a margin."""
        return self.memory_bytes + _MAXMEM_MARGIN


DEFAULT_PARAMS = ScryptParams()
"""N=2^15, r=8, p=3: OWASP's lower-memory minimum, 32 MiB, about 140 ms on an Apple M4 (T64)."""


class PasswordRuleError(ValueError):
    """The password breaks a length rule."""


def password_problem(password: str) -> str | None:
    """Why `password` is refused as a new password, or None. Counted in characters after NFKC."""
    length = len(unicodedata.normalize("NFKC", password))
    if length == 0:
        return "the password is empty"
    if length < MIN_PASSWORD_LENGTH:
        return f"the password needs at least {MIN_PASSWORD_LENGTH} characters"
    if length > MAX_PASSWORD_LENGTH:
        return f"the password is longer than {MAX_PASSWORD_LENGTH} characters"
    return None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes | None:
    """Decode unpadded base64url, only if `text` is the canonical encoding of the bytes."""
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return data if _b64(data) == text else None


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    secret = unicodedata.normalize("NFKC", password).encode("utf-8")
    return hashlib.scrypt(
        secret,
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=params.maxmem,
        dklen=params.key_bytes,
    )


def hash_password(password: str, params: ScryptParams = DEFAULT_PARAMS) -> str:
    """The self-describing hash `scrypt$n$r$p$salt$hash` of a new password with a fresh salt.
    Raises `PasswordRuleError` if the password breaks the length rules."""
    problem = password_problem(password)
    if problem is not None:
        raise PasswordRuleError(problem)
    salt = secrets.token_bytes(params.salt_bytes)
    key = _derive(password, salt, params)
    return f"{ALGORITHM}${params.n}${params.r}${params.p}${_b64(salt)}${_b64(key)}"


@dataclass(frozen=True)
class ParsedHash:
    """A stored hash taken apart."""

    params: ScryptParams
    salt: bytes
    key: bytes = field(repr=False)


_DECIMAL = re.compile(r"[1-9][0-9]{0,9}")


def parse_hash(stored: str) -> ParsedHash | None:
    """Take a stored hash apart, or None if it isn't exactly the documented format. Never raises."""
    try:
        parts = stored.split("$")
        if len(parts) != 6 or parts[0] != ALGORITHM:
            return None
        if not all(_DECIMAL.fullmatch(part) for part in parts[1:4]):
            return None
        salt, key = _unb64(parts[4]), _unb64(parts[5])
        if salt is None or key is None:
            return None
        params = ScryptParams(
            n=int(parts[1]),
            r=int(parts[2]),
            p=int(parts[3]),
            salt_bytes=len(salt),
            key_bytes=len(key),
        )
    except (ValueError, TypeError, AttributeError):
        return None
    return ParsedHash(params=params, salt=salt, key=key)


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` matches `stored`. The derived key is compared with
    `hmac.compare_digest`. A malformed hash or an over-long password gives False and never
    raises."""
    if len(password) > MAX_PASSWORD_LENGTH * 4:  # cheap bound before NFKC
        return False
    parsed = parse_hash(stored)
    if parsed is None or len(unicodedata.normalize("NFKC", password)) > MAX_PASSWORD_LENGTH:
        return False
    try:
        key = _derive(password, parsed.salt, parsed.params)
    except UnicodeEncodeError:  # a lone surrogate can't be UTF-8: not a password
        return False
    return hmac.compare_digest(key, parsed.key)


@lru_cache(maxsize=8)
def _dummy_hash(params: ScryptParams) -> str:
    return hash_password("dummy password for unknown users", params)


def verify_unknown(password: str, params: ScryptParams = DEFAULT_PARAMS) -> bool:
    """The stand-in for a login whose user doesn't exist: the same work and the same comparison
    as `verify_password` against a hash made with `params`, and always False. Calling it keeps an
    unknown user from answering faster than a wrong password."""
    verify_password(password, _dummy_hash(params))
    return False


def needs_rehash(stored: str, current: ScryptParams = DEFAULT_PARAMS) -> bool:
    """Whether a hash that just verified should be replaced: another algorithm or weaker or
    different parameters than `current`, or a hash that can't be parsed."""
    parsed = parse_hash(stored)
    return parsed is None or parsed.params != current


class Credential(BaseModel):
    """A user's password hash. One per user; replaced when the password changes or is re-hashed."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    org_id: str = Field(min_length=1, max_length=128)
    user_id: str
    password_hash: str = Field(repr=False)
    updated_at: datetime

    @field_validator("user_id")
    @classmethod
    def _person_id(cls, value: str) -> str:
        if not is_human_id(value):
            raise ValueError("a user id is human:<opaque id> (T42)")
        return value

    @field_validator("password_hash")
    @classmethod
    def _well_formed(cls, value: str) -> str:
        if parse_hash(value) is None:
            raise ValueError("not a scrypt$n$r$p$salt$hash string")
        return value

    @field_validator("updated_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return _aware(value) or value


# ---- the first-owner setup token --------------------------------------------------

SETUP_TOKEN_LIFETIME = timedelta(hours=24)
SERVE_SETUP_TOKEN_LIFETIME = timedelta(seconds=900)
"""How long the token in the one-time setup URL of `nuramesh serve` lives (SA-0.8, #950). The token
of `nuramesh auth new-setup-token` keeps `SETUP_TOKEN_LIFETIME`."""
MAX_SETUP_TOKEN_LENGTH = 256


class SetupToken(BaseModel):
    """The stored side of a setup token: only the SHA-256 of the secret, never the secret."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    org_id: str = Field(min_length=1, max_length=128)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @field_validator("created_at", "expires_at", "consumed_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


@dataclass(frozen=True)
class IssuedSetupToken:
    """What issuing returns: the secret to show once, and the record to store."""

    token: str = field(repr=False)
    record: SetupToken


class SetupTokenCheck(StrEnum):
    """The outcome of checking a presented token against a stored record."""

    OK = "ok"
    WRONG = "wrong"
    USED = "used"
    EXPIRED = "expired"


def hash_setup_token(token: str) -> str:
    """SHA-256 hex of the token (enough for 256 random bits; a slow hash would add nothing)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_setup_token(
    org_id: str, now: datetime, lifetime: timedelta = SETUP_TOKEN_LIFETIME
) -> IssuedSetupToken:
    """A fresh 256-bit token (43 URL-safe characters) and its record, valid for `lifetime`."""
    if lifetime <= timedelta(0):
        raise ValueError("the lifetime must be positive")
    token = secrets.token_urlsafe(32)
    record = SetupToken(
        org_id=org_id, token_hash=hash_setup_token(token), created_at=now, expires_at=now + lifetime
    )
    return IssuedSetupToken(token=token, record=record)


def check_setup_token(token: str, record: SetupToken, now: datetime) -> SetupTokenCheck:
    """Whether `token` redeems `record` at `now`. Callers answer all three failures the same way.
    The store's atomic `consume_setup_token` is what makes the use single."""
    if len(token) > MAX_SETUP_TOKEN_LENGTH:
        return SetupTokenCheck.WRONG
    try:
        token_hash = hash_setup_token(token)
    except UnicodeEncodeError:
        return SetupTokenCheck.WRONG
    if not hmac.compare_digest(token_hash, record.token_hash):
        return SetupTokenCheck.WRONG
    if record.consumed_at is not None:
        return SetupTokenCheck.USED
    if now >= record.expires_at:
        return SetupTokenCheck.EXPIRED
    return SetupTokenCheck.OK


# ---- sessions, refresh families and lockout (R1.22 PR 2, T66) -----------------------------

ACCESS_LIFETIME = timedelta(minutes=15)
REFRESH_IDLE = timedelta(hours=8)
SESSION_ABSOLUTE = timedelta(days=7)
SESSION_RETENTION = timedelta(days=90)  # ND53 D: how long an ended session record is kept
MAX_AUTH_TOKEN_LENGTH = 256
_SESSION_ID = r"^[0-9a-f]{32}$"
_CSRF_LABEL = b"nuramesh-csrf-v1"
LOGIN_VIA = ("password", "oidc")  # the closed set of sign-in methods (R1.38, Q11)


class TokenKind(StrEnum):
    """Which cookie a stored token hash belongs to."""

    ACCESS = "access"
    REFRESH = "refresh"


class AuthSession(BaseModel):
    """One login and everything refreshed from it (the refresh *family*). The id is a random
    128-bit label, not a secret: the secrets are the access and refresh tokens, stored hashed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    id: str = Field(pattern=_SESSION_ID)
    user_id: str
    created_at: datetime
    last_active_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None = None
    revoke_reason: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    step_up_at: datetime | None = None
    """When the person last authenticated strongly on this session, by any method; NULL until
    then, so a session from before R2.13 is asked once. Per session, never per person."""
    # How the person signed in (R1.38, MA-R2): required by the constructor, closed set. A stored
    # row read back with `model_validate` may hold None or an unknown value (a defect); the
    # refresh rules refuse such a session, they never read it as a password session.
    login_via: str | None = None
    mfa_verified_at: datetime | None = None
    mfa_method: str | None = Field(default=None, pattern=r"^(totp|recovery)$")
    # SA-8: when this session last re-confirmed the password. Set
    # only by the confirm route; a sign-in never sets it, a refresh never changes it.
    password_confirmed_at: datetime | None = None

    def __init__(self, /, *, login_via: str, **data: Any) -> None:
        """`login_via` has no default (T69): `password` or `oidc`, else `ValueError`."""
        if login_via not in LOGIN_VIA:
            raise ValueError(f"login_via is one of {', '.join(LOGIN_VIA)}")
        super().__init__(login_via=login_via, **data)

    @classmethod
    def from_stored(cls, data: dict[str, Any]) -> Self:
        """Rebuild a session from a stored row. Everything is validated except `login_via`,
        which is kept as stored (None or unknown included) so refresh can refuse it (T68)."""
        stored = data.get("login_via")
        built = cls(**{**data, "login_via": LOGIN_VIA[0]})
        return built.model_copy(update={"login_via": stored})

    def check_login_via(self) -> None:
        """Refuse to store a session whose `login_via` is missing or unknown (both stores call
        this on insert; a row read back is never checked, so refresh can see and refuse it)."""
        if self.login_via not in LOGIN_VIA:
            raise ValueError(f"login_via is one of {', '.join(LOGIN_VIA)}")

    @field_validator("user_id")
    @classmethod
    def _person_id(cls, value: str) -> str:
        if not is_human_id(value):
            raise ValueError("a user id is human:<opaque id> (T42)")
        return value

    @field_validator(
        "created_at", "last_active_at", "absolute_expires_at", "revoked_at", "mfa_verified_at"
    )
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    def active(self, now: datetime) -> bool:
        """Not revoked and before its absolute end."""
        return self.revoked_at is None and now < self.absolute_expires_at

    @property
    def ended_at(self) -> datetime:
        """When the record stopped being usable (revoked, or its absolute end): the retention
        clock starts here."""
        if self.revoked_at is not None:
            return min(self.revoked_at, self.absolute_expires_at)
        return self.absolute_expires_at


class AuthToken(BaseModel):
    """The stored side of an access or refresh token: only the SHA-256, never the secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=_SESSION_ID)
    kind: TokenKind
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None  # a refresh token rotated out

    @field_validator("created_at", "expires_at", "used_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


@dataclass(frozen=True)
class IssuedSession:
    """What starting or refreshing a session returns: the secrets (shown once, set as cookies)
    and the records to store."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access: AuthToken
    refresh: AuthToken


def new_session_id() -> str:
    """A random 128-bit session label (32 hex digits)."""
    return secrets.token_hex(16)


def hash_auth_token(token: str) -> str:
    """SHA-256 hex of an access or refresh token (256 random bits, so no slow hash is needed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


REFRESH_GRACE_MAX = timedelta(seconds=30)
_SUCCESSOR_ACCESS = b"nuramesh-successor-access-v1"
_SUCCESSOR_REFRESH = b"nuramesh-successor-refresh-v1"


REFRESH_SUCCESSOR_KEY_MIN = 32


def successor_tokens(key: bytes, spent_token_hash: str, session_id: str) -> tuple[str, str]:
    """The access and refresh token that rotating a refresh token issues (T109): HMAC-SHA256 under
    the SERVER's `key` (never stored in the database, never sent to a client) over the spent
    token's hash, the session id and a label; base64url without padding (43 characters, 256 bits).
    Deriving them lets a second request inside the grace window get the same pair without any
    token stored. Whoever holds only the spent token (or only the database, which has hashes)
    cannot compute them, so a stolen old token does not predict the live pair."""
    if len(key) < REFRESH_SUCCESSOR_KEY_MIN:
        raise ValueError("the refresh successor key must be at least 32 bytes")

    def derive(label: bytes) -> str:
        message = b"\x00".join([label, spent_token_hash.encode("ascii"), session_id.encode()])
        digest = hmac.new(key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    return derive(_SUCCESSOR_ACCESS), derive(_SUCCESSOR_REFRESH)


def issue_tokens(
    session: AuthSession,
    now: datetime,
    *,
    idle: timedelta = REFRESH_IDLE,
    successor: tuple[bytes, str] | None = None,
) -> IssuedSession:
    """A fresh access token (15 minutes) and refresh token (idle limit, never past the session's
    absolute end), each 256 bits, and the records that store their hashes. Random, except when
    `successor` is (server key, hash of the token being rotated): then `successor_tokens`."""
    if successor is None:
        access_token, refresh_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    else:
        access_token, refresh_token = successor_tokens(successor[0], successor[1], session.id)
    access = AuthToken(
        org_id=session.org_id,
        session_id=session.id,
        created_at=now,
        token_hash=hash_auth_token(access_token),
        kind=TokenKind.ACCESS,
        expires_at=min(now + ACCESS_LIFETIME, session.absolute_expires_at),
    )
    refresh = AuthToken(
        org_id=session.org_id,
        session_id=session.id,
        created_at=now,
        token_hash=hash_auth_token(refresh_token),
        kind=TokenKind.REFRESH,
        expires_at=min(now + idle, session.absolute_expires_at),
    )
    return IssuedSession(access_token, refresh_token, access, refresh)


def csrf_token(refresh_token: str) -> str:
    """The CSRF token of a cookie session: HMAC-SHA256 keyed by the refresh token, so it is bound
    to this session, unforgeable without the HttpOnly cookie, and nothing extra is stored."""
    return hmac.new(refresh_token.encode("utf-8"), _CSRF_LABEL, hashlib.sha256).hexdigest()


def csrf_matches(presented: str, refresh_token: str) -> bool:
    """Constant-time check of a presented CSRF token against the session's."""
    return hmac.compare_digest(presented.encode("utf-8"), csrf_token(refresh_token).encode())


class AuthFailure(BaseModel):
    """A failure counter for lockout, keyed by a hash (of the account or the client address), so
    no email or address is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=1)
    last_failure_at: datetime
    locked_until: datetime | None = None

    @field_validator("last_failure_at", "locked_until")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


@dataclass(frozen=True)
class LockoutPolicy:
    """After `threshold` failures the key is locked for `base`, and every further failure doubles
    the lock up to `cap`. With no failure for `decay` the count starts again."""

    threshold: int = 5
    base: timedelta = timedelta(minutes=1)
    cap: timedelta = timedelta(minutes=15)
    decay: timedelta = timedelta(hours=1)

    def lock_for(self, count: int) -> timedelta:
        """How long the `count`-th failure locks the key (zero below the threshold)."""
        if count < self.threshold:
            return timedelta(0)
        steps = min(count - self.threshold, 32)
        delay: timedelta = self.base * (2**steps)
        return min(self.cap, delay)

    def after_failure(self, previous: AuthFailure | None, key: str, now: datetime) -> AuthFailure:
        """The counter after one more failure at `now`."""
        count = 1
        if previous is not None and now - previous.last_failure_at < self.decay:
            count = previous.count + 1
        lock = self.lock_for(count)
        return AuthFailure(
            key=key,
            count=count,
            last_failure_at=now,
            locked_until=now + lock if lock > timedelta(0) else None,
        )

    @staticmethod
    def retry_after(failure: AuthFailure | None, now: datetime) -> int:
        """Whole seconds until the key may try again (0 if it isn't locked)."""
        if failure is None or failure.locked_until is None or failure.locked_until <= now:
            return 0
        return max(1, math.ceil((failure.locked_until - now).total_seconds()))


# ---- security events: keyed identifier hashes and the expiring security log (ND60) --------------

IDENTIFIER_KIND = "identifier"
IDENTIFIER_KEY_BYTES = 32
IDENTIFIER_EPOCH_SECONDS = 90 * 24 * 3600  # 7,776,000: the organization key rotates every 90 days
ERASED_ACTOR = "erased-user"  # ND60: the actor of a kept security-log row once its user is erased
ERASED_DETAIL_KEYS = ("client_address", "user_agent", "email")  # removed from such a row's details
SECURITY_LOG_RETENTION = timedelta(days=90)  # ND60: raw security details are kept 90 days
# ND60: after its epoch ends, the previous organization key lives on for the lockout window (the
# quiet time after which a failure counter starts again), only to find and carry counters over
IDENTIFIER_KEY_OVERLAP = timedelta(seconds=3600)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def new_identifier_key() -> bytes:
    """32 random bytes: a user's identifier key, or an organization's for one epoch. Never
    derived from another key, so a destroyed key cannot be made again (ND60)."""
    return secrets.token_bytes(IDENTIFIER_KEY_BYTES)


def identifier_epoch(at: datetime) -> int:
    """The organization key's epoch at `at`: `floor(unix seconds / 7,776,000)`, fixed 90-day
    epochs counted from 1970-01-01 UTC. A new epoch begins at exactly its first second."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("times must carry a time zone")
    seconds = (at - _UNIX_EPOCH) // timedelta(seconds=1)
    return seconds // IDENTIFIER_EPOCH_SECONDS


def epoch_start(epoch: int) -> datetime:
    """The first instant of an organization-key epoch (UTC)."""
    return _UNIX_EPOCH + timedelta(seconds=epoch * IDENTIFIER_EPOCH_SECONDS)


def email_text(email: str) -> str:
    """The text an email is hashed as (ND60): `normalize_email` (T64: NFKC, trimmed, case-folded)
    when it is a valid address; otherwise the text trimmed, as given."""
    try:
        return normalize_email(email)
    except ValueError:
        return email.strip()


def address_text(address: str) -> str:
    """The text a client address is hashed as (ND60): the canonical form of an IP address
    (`ipaddress` compressed text, an IPv4-mapped IPv6 address shown as IPv4); otherwise the text
    trimmed, as given."""
    text = address.strip()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def address_key(address: str) -> str:
    """The key an address is rate limited and locked out under: `address_text`, with these IPv6
    rules (IPv4 and text that is not an address are unchanged):

    - an ordinary address is keyed by its /64 prefix, so rotating addresses inside one network
      gets no new allowance;
    - loopback (`::1`) keeps the full address (it is one host);
    - a link-local address (`fe80::/10`) is keyed per interface: `fe80::/64%<zone>`, or
      `fe80::/64` when it names no zone;
    - a NAT64 address (`64:ff9b::/96`) is keyed by the IPv4 address embedded in it.
    """
    text = address_text(address)
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return text
    if not isinstance(ip, ipaddress.IPv6Address):
        return text
    if ip.is_loopback:
        return str(ip)
    if ip in _NAT64:
        return str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    network = ipaddress.IPv6Network((int(ip) >> 64 << 64, 64))
    prefix = str(network)
    if ip.is_link_local and ip.scope_id:
        return f"{prefix}%{ip.scope_id}"
    return prefix


_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def identifier_hash(key: bytes, org_id: str, text: str) -> str:
    """The keyed hash the ledger holds instead of an identifier: HMAC-SHA256, lower-case hex,
    keyed with an **identifier key** (the user's own, or the organization's key of the epoch), over
    the T46 message `len(org) || org || len(kind) || kind || len(text) || text` (8-byte big-endian
    byte lengths of the UTF-8 parts, kind `identifier`). **No normalisation here:** callers pass
    `email_text`, `address_text` or an opaque row id. Once the key is destroyed the hash can never
    be matched again (crypto-shredding, ND60)."""
    if len(key) != IDENTIFIER_KEY_BYTES:
        raise ValueError(f"an identifier key is exactly {IDENTIFIER_KEY_BYTES} bytes")
    parts = (org_id.encode("utf-8"), IDENTIFIER_KIND.encode("ascii"), text.encode("utf-8"))
    message = b"".join(len(p).to_bytes(8, "big") + p for p in parts)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


TOKEN_NAME_KIND = "token_name"


def token_name_hash(key: bytes, org_id: str, name: str) -> str:
    """The keyed hash a retired token name is stored as (#634 R16): HMAC-SHA256 under the
    organization's identifier key of the epoch over the T46 message with the kind `token_name`,
    never the name and never an unkeyed hash, so it never equals an `identifier_hash` of the same
    text under the same key."""
    if len(key) != IDENTIFIER_KEY_BYTES:
        raise ValueError(f"an identifier key is exactly {IDENTIFIER_KEY_BYTES} bytes")
    parts = (org_id.encode("utf-8"), TOKEN_NAME_KIND.encode("ascii"), name.encode("utf-8"))
    message = b"".join(len(p).to_bytes(8, "big") + p for p in parts)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


SecurityDetail = str | int | None
# ND60: the closed set of details: the raw facts (where they apply) and non-identifying context;
# never a password or token
SECURITY_DETAIL_KEYS = frozenset(
    {
        "email",
        "client_address",
        "user_agent",
        "via",
        "scope",
        "reason",
        "episode",
        "account_failures",
        "mfa_failures",
        "client_failures",
        "locked_until",
        "count",
        "origin",
        "token_hash",
    }
)
_EXTRA_DETAIL_KEYS: set[str] = set()


def register_security_detail_keys(*keys: str) -> None:
    """Allow more detail keys (a plugin's, ND72). Each is a short lower-case word; like the built-in
    set it must hold closed, non-identifying values, never caller text."""
    for key in keys:
        if not re.fullmatch(r"[a-z][a-z_]{0,31}", key):
            raise ValueError(f"security detail key {key!r} is not a short lower-case word")
        _EXTRA_DETAIL_KEYS.add(key)


# ND60 amendment (R1.37, T122): `count` is a bounded integer (1 to 1,000,000) used only by the
# coalesced rows below; it never identifies a person.
COALESCED_TYPES = frozenset(
    {
        "content.capture_refused",
        "content.key_resurrected",
        "content.capture_failed",
        "run.start_refused",
        "auth.token_names_erased",  # #634: how many token rows lost a name or an issuer
    }
)
MAX_COUNT = 1_000_000


HALT_REASON = "org_halted"
HALT_EPISODE_REASON = "org_halted_episode"
HALT_PENDING_REASON = "org_halted_pending"
"""T154: the `reason` of the marker row (one per halt episode) and of the pending row (refusals not
yet in the ledger) that bound the ledger growth of halt refusals. Stores skip a pending row in
`purge_security_log`."""


def halt_row_id(kind: str, org_id: str, episode: int | str) -> str:
    """The deterministic id of a T154 marker (`kind` `halt.episode`) or pending (`halt.pending`)
    row: the first 32 hex of SHA-256 over `kind`, the organization id and the episode (the seq of
    the governing `org.halted` as decimal text, or `unknown`), joined by NUL bytes."""
    raw = "\0".join([kind, org_id, str(episode)]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def coalesced_row_id(kind: str, org_id: str, at: datetime, reason: str) -> str:
    """The deterministic id of a coalesced row: the first 32 hex of SHA-256 over the type string,
    the organization id, the UTC date `YYYY-MM-DD` and the reason, joined by NUL bytes."""
    day = at.astimezone(UTC).strftime("%Y-%m-%d")
    raw = "\0".join([kind, org_id, day, reason]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class SecurityLogEntry(BaseModel):
    """One row of the security log: the raw facts behind a security event (an email as typed, a
    client address, a user agent), kept apart from the ledger because the ledger cannot erase.
    **Mutable on purpose:** rows expire 90 days after `at` and are deleted when their user is
    erased: rows about them (by `subject`, and by `details["email"]`, the normalised address) are
    deleted; rows where they are only the actor are kept with the actor `ERASED_ACTOR` and the
    keys `ERASED_DETAIL_KEYS` removed from `details`. `actor`
    is the event's actor (`system` for system events); `subject` is the opaque id of the user the
    row concerns (set whenever that user is known, even when the actor is `system`), so erasure
    finds every row about the user. It holds an id only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    at: datetime
    type: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=64)
    actor: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=200)
    details: dict[str, SecurityDetail] = Field(default_factory=dict)
    expires_at: datetime

    @field_validator("at", "expires_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        _aware(value)
        return value

    @field_validator("details", mode="before")
    @classmethod
    def _count_is_exact(cls, value: Any) -> Any:
        # before coercion: pydantic would turn True into 1 and 5.0 into 5
        if isinstance(value, dict) and "count" in value:
            count = value["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
                raise ValueError(f"count is an integer from 1 to {MAX_COUNT}")
        return value

    @field_validator("details")
    @classmethod
    def _bounded(cls, value: dict[str, SecurityDetail]) -> dict[str, SecurityDetail]:
        allowed = SECURITY_DETAIL_KEYS | _EXTRA_DETAIL_KEYS
        if set(value) - allowed:
            raise ValueError(f"security log details are only {sorted(allowed)}")
        return {k: v[:512] if isinstance(v, str) else v for k, v in value.items()}

    @model_validator(mode="after")
    def _count_only_when_coalesced(self) -> "SecurityLogEntry":
        if "count" in self.details and self.type not in COALESCED_TYPES:
            raise ValueError("count is only for coalesced rows")
        return self

    @classmethod
    def load(cls, **fields: Any) -> "SecurityLogEntry":
        """A stored row, read back. Strict on write, tolerant on load: a row written with a
        plugin's detail keys loads in a process without that plugin, the keys it does not know
        kept as they are (opaque, never shown as meaning anything)."""
        details = dict(fields.get("details") or {})
        known = SECURITY_DETAIL_KEYS | _EXTRA_DETAIL_KEYS
        entry = cls(**{**fields, "details": {k: v for k, v in details.items() if k in known}})
        if len(details) == len(entry.details):
            return entry
        return entry.model_copy(update={"details": details})

    @classmethod
    def new(
        cls,
        org_id: str,
        at: datetime,
        type: str,
        *,
        actor: str | None = None,
        subject: str | None = None,
        details: dict[str, SecurityDetail] | None = None,
    ) -> "SecurityLogEntry":
        """A fresh row expiring `SECURITY_LOG_RETENTION` after `at`; `None` details are dropped."""
        kept: dict[str, SecurityDetail] = {
            k: v for k, v in (details or {}).items() if v is not None
        }
        return cls(
            org_id=org_id,
            id=secrets.token_hex(16),
            at=at,
            type=type,
            actor=actor,
            subject=subject,
            details=kept,
            expires_at=at + SECURITY_LOG_RETENTION,
        )


# ---- API tokens and permissions (R1.22 PR 3; ND56) ----------------------------------------

API_TOKEN_LIFETIME = timedelta(days=90)  # ND53 C, ND56
MAX_API_TOKEN_LIFETIME = timedelta(days=365)
MAX_CLI_TOKEN_LIFETIME = timedelta(days=30)
"""Longest lifetime of a token the CLI mints (design decision 2026-10-25)."""
API_TOKEN_PREFIX = "nmt_"  # makes a leaked token recognizable to secret scanners
MAX_API_TOKEN_LENGTH = 256
TOKEN_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
RESERVED_TOKEN_NAMES = frozenset({"cli", "system", "api", "engine"})
"""Names a token may not take. The ledger names a token by its id (`service:tok_...`), never its
name (T85), so this is no longer needed for the ledger; it keeps `cli` (the CLI's actor class,
ND67) from being confused with a token anywhere a name is shown."""
TOKEN_ID_PREFIX = "tok_"
_TOKEN_ID = r"^tok_[0-9a-f]{24}$"
_TOKEN_NAME = re.compile(TOKEN_NAME_PATTERN)
_TOKEN_ID_RE = re.compile(_TOKEN_ID)


class TokenScope(StrEnum):
    """What an API token may do. **There is no approve scope** (ND56): approvals, human gates,
    budget unblocks and reading a person's reason need a human session."""

    READ = "read"
    RUN = "run"


class Permission(StrEnum):
    """What an endpoint needs (the authorization matrix)."""

    READ = "read"  # read state, the ledger and reports; validate a configuration
    RUN = "run"  # act on runs as a program or a person (stop a run)
    DECIDE = "decide"  # human only: answer approvals, lift budget blocks
    REVEAL = "reveal"  # human only: read the text behind a content hash (a person's reason)
    BUILD = "build"  # human only: design and change the team (the graph, roles, policies)
    ADMIN = "admin"  # human only: manage API tokens and people
    GOVERN = "govern"  # human only: owner-only organization controls


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.APPROVER: frozenset(
        {Permission.READ, Permission.RUN, Permission.DECIDE, Permission.REVEAL}
    ),
    Role.BUILDER: frozenset({Permission.READ, Permission.RUN, Permission.BUILD}),
    Role.ADMIN: frozenset(Permission) - {Permission.GOVERN},
    Role.OWNER: frozenset(Permission),
}
"""What a person's role allows when they act through a human session."""

SCOPE_PERMISSIONS: dict[TokenScope, frozenset[Permission]] = {
    TokenScope.READ: frozenset({Permission.READ}),
    TokenScope.RUN: frozenset({Permission.READ, Permission.RUN}),
}
"""What a token scope allows. No scope ever grants DECIDE, REVEAL, BUILD, ADMIN or GOVERN."""

HUMAN_ONLY = frozenset(
    {
        Permission.DECIDE,
        Permission.REVEAL,
        Permission.BUILD,
        Permission.ADMIN,
        Permission.GOVERN,
    }
)


def scope_permissions(scopes: frozenset[TokenScope] | set[TokenScope]) -> frozenset[Permission]:
    """The union of what `scopes` allow; never anything in `HUMAN_ONLY`."""
    found: set[Permission] = set()
    for scope in scopes:
        found |= SCOPE_PERMISSIONS[scope]
    return frozenset(found - HUMAN_ONLY)


def token_name_problem(name: str) -> str | None:
    """Why `name` can't name a token, or None. This is the stored-row rule (the `ApiToken`
    validator): it must keep accepting every name already stored, replacement names included."""
    if not _TOKEN_NAME.fullmatch(name):
        return "a token name is 1 to 63 of a-z, 0-9, _ and -, starting with a letter or digit"
    if name in RESERVED_TOKEN_NAMES:
        return f"'{name}' is reserved"
    if name.startswith(TOKEN_ID_PREFIX):
        return f"names starting '{TOKEN_ID_PREFIX}' are reserved for token ids"
    return None


ERASED_TOKEN_NAME_PREFIX = "erased-"
RESERVED_NAME_TEXT = "that name is reserved"
_ERASED_NAME_SUFFIX = re.compile(r"-[1-9][0-9]*")


def new_token_name_problem(name: str) -> str | None:
    """Why `name` can't be given to a token by a person or the CLI now (create and rename), or
    None. The pattern, then the reserved names and anything starting `erased-` (the fixed text
    `that name is reserved`), then `tok_`. No text echoes the submitted value (#634 R13)."""
    if not _TOKEN_NAME.fullmatch(name):
        return "a token name is 1 to 63 of a-z, 0-9, _ and -, starting with a letter or digit"
    if name in RESERVED_TOKEN_NAMES or name.startswith(ERASED_TOKEN_NAME_PREFIX):
        return RESERVED_NAME_TEXT
    if name.startswith(TOKEN_ID_PREFIX):
        return f"names starting '{TOKEN_ID_PREFIX}' are reserved for token ids"
    return None


def next_erased_token_name(
    token_id: str, taken: "set[str] | frozenset[str]", *, current: str | None = None
) -> str:
    """The replacement name of a token whose issuer was erased (#634 R3): `erased-` and the 24 hex
    digits of its id, then `-1`, `-2` ... to the first name not in `taken` (the names the other
    rows of the organization hold). `current` already in its own exact form (the base, or the
    base and a canonical `-<n>`, n at least 1) is kept as it is."""
    base = ERASED_TOKEN_NAME_PREFIX + token_id.removeprefix(TOKEN_ID_PREFIX)
    if current is not None and current.startswith(base):
        rest = current[len(base) :]
        if rest == "" or _ERASED_NAME_SUFFIX.fullmatch(rest):
            return current
    if base not in taken:
        return base
    n = 1
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def token_actor(token_id: str) -> str:
    """The ledger actor of a token: `service:<token id>`, never `human:` and never the name
    (T85: a name is free text and may identify a person; it lives only in the token table)."""
    if not _TOKEN_ID_RE.fullmatch(token_id):
        raise ValueError("a token id is 'tok_' and 24 lower-case hex digits")
    return f"service:{token_id}"


class ApiToken(BaseModel):
    """The stored side of an API token: the SHA-256 of the secret, never the secret. A name is
    unique among the live rows of its organization; a renamed or deleted name stays reserved for a
    bounded time (`api_token_retired_names`, keyed hashes only), and an issuer's erasure replaces
    the name (`erased-<id>`). The ledger names a token by `service:<id>` only (T85)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    id: str = Field(pattern=_TOKEN_ID)
    name: str
    scopes: frozenset[TokenScope] = Field(min_length=1)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    created_by: str = Field(
        pattern=(
            r"^(human:[A-Za-z0-9_-]{1,64}|service:cli|erased-user|system"
            r"|operator:[A-Za-z0-9._-]{1,64})$"
        )
    )
    # a person, the CLI, `erased-user` (the person was erased) or, for a row from before, `system`
    # or `operator:<user>`; only a person with a live account or `service:cli` authenticates
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: Actor | Literal["erased-user"] | None = None
    # any ledger actor (`system` = ended by the erasure of its issuer, ND60; `operator:<user>` = the
    # CLI's organization close), so every actor that may revoke can be recorded

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        problem = token_name_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value

    @field_validator("created_at", "expires_at", "revoked_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _consistent(self) -> "ApiToken":
        if self.expires_at <= self.created_at:
            raise ValueError("a token expires after it is created")
        if (self.revoked_at is None) != (self.revoked_by is None):
            raise ValueError("revoked_at and revoked_by go together")
        return self

    @property
    def actor(self) -> str:
        """`service:<id>`: what the ledger records for what this token does (never the name)."""
        return token_actor(self.id)

    def active(self, now: datetime) -> bool:
        """Not revoked and not expired."""
        return self.revoked_at is None and now < self.expires_at

    def permissions(self) -> frozenset[Permission]:
        """What the token's scopes allow."""
        return scope_permissions(self.scopes)


@dataclass(frozen=True)
class IssuedApiToken:
    """What issuing returns: the secret to show once, and the record to store."""

    token: str = field(repr=False)
    record: ApiToken


def new_token_id() -> str:
    """A random token label `tok_<24 hex>` (96 bits, not a secret)."""
    return f"tok_{secrets.token_hex(12)}"


def hash_api_token(token: str) -> str:
    """SHA-256 hex of an API token (256 random bits, so no slow hash is needed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_api_token(
    org_id: str,
    name: str,
    scopes: frozenset[TokenScope] | set[TokenScope],
    *,
    created_by: str,
    now: datetime,
    lifetime: timedelta = API_TOKEN_LIFETIME,
) -> IssuedApiToken:
    """A fresh token `nmt_` + 43 URL-safe characters (256 random bits) and its record."""
    if not timedelta(0) < lifetime <= MAX_API_TOKEN_LIFETIME:
        raise ValueError("a token lives more than 0 and at most 365 days")
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    record = ApiToken(
        org_id=org_id,
        id=new_token_id(),
        name=name,
        scopes=frozenset(scopes),
        token_hash=hash_api_token(token),
        created_by=created_by,
        created_at=now,
        expires_at=now + lifetime,
    )
    return IssuedApiToken(token=token, record=record)


def looks_like_api_token(value: str) -> bool:
    """Cheap shape check before any lookup: the prefix and a bounded length."""
    return value.startswith(API_TOKEN_PREFIX) and 4 < len(value) <= MAX_API_TOKEN_LENGTH


# ---- agent credentials (E2) -------------------------------------------

AGENT_CREDENTIAL_PREFIX = "nma_"
AGENT_CREDENTIAL_ID_PREFIX = "agc_"
MAX_AGENT_CREDENTIAL_LENGTH = 256
AGENT_CREDENTIAL_LIFETIME = timedelta(days=30)  # default; NURAMESH_AGENT_CREDENTIAL_DAYS (1..90)
MAX_AGENT_CREDENTIAL_LIFETIME = timedelta(days=90)
MIN_AGENT_CREDENTIAL_LIFETIME = timedelta(days=1)
AGENT_CREDENTIAL_MAX_LIVE = 5  # per (organization, agent); NURAMESH_AGENT_CREDENTIAL_MAX_LIVE
AGENT_CREDENTIAL_CALLS_PER_MINUTE = 600  # NURAMESH_AGENT_CREDENTIAL_CALLS_PER_MINUTE (10..6000)
AGENT_FAILURES_PER_MINUTE = 30  # the separate `nma_` failure counter (not configurable)
AGENT_ROTATION_GRACE_MAX = 86_400  # seconds
AGENT_CREDENTIAL_ENDED_RETENTION = timedelta(days=90)  # Q11
_AGENT_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_AGENT_CREDENTIAL_ID = r"^agc_[0-9a-f]{24}$"
_AGENT_CREDENTIAL_ID_RE = re.compile(_AGENT_CREDENTIAL_ID)
_SLUG = r"^[a-z0-9][a-z0-9_-]{0,62}$"


def agent_actor(agent_id: str) -> str:
    """The ledger actor of an agent: `agent:<slug>`; the slug is the configuration's agent id."""
    if not _AGENT_SLUG.fullmatch(agent_id):
        raise ValueError("an agent id is 1 to 63 of a-z, 0-9, _ and -, starting with a letter")
    return f"agent:{agent_id}"


def is_agent_slug(value: str) -> bool:
    """Whether `value` has the shape of an agent id."""
    return _AGENT_SLUG.fullmatch(value) is not None


def is_agent_credential_id(value: str) -> bool:
    """Whether `value` has the shape `agc_<24 hex>`."""
    return _AGENT_CREDENTIAL_ID_RE.fullmatch(value) is not None


def team_key(folder_name: str) -> str:
    """The team key of a team folder name (spec 2.6): lower-cased, every character outside
    `[a-z0-9_-]` replaced by `-`, `t` put in front when the first character is not a lower-case
    letter or digit, cut to 63 characters (prefix included). The caller resolves the path first."""
    key = re.sub(r"[^a-z0-9_-]", "-", folder_name.lower())
    if not key or not re.match(r"[a-z0-9]", key[0]):
        key = "t" + key
    return key[:63]


class _KindLike(Protocol):
    @property
    def value(self) -> str: ...


class _AgentLike(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def reports_to(self) -> str | None: ...
    @property
    def model(self) -> str | None: ...
    @property
    def checks(self) -> Sequence[str]: ...


class _RoleLike(Protocol):
    @property
    def kind(self) -> _KindLike: ...


def agent_fingerprint(agent: _AgentLike, role: _RoleLike) -> str:
    """SHA-256 (64 lower-case hex) of the compact sorted-key JSON of
    `{id, role, role_kind, reports_to, model, checks}` (spec 2.6, Q17). Names, descriptions,
    instructions, tools and a role's default model are not part of it."""
    entry = {
        "id": agent.id,
        "role": agent.role,
        "role_kind": str(role.kind.value),
        "reports_to": agent.reports_to,
        "model": agent.model,
        "checks": list(agent.checks),
    }
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AgentCredential(BaseModel):
    """The stored side of an agent credential: the SHA-256 of the secret, never the secret. There
    is **no name or free-text field** (R1.22c lesson); the ledger names the agent by
    `agent:<agent id>` only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    id: str = Field(pattern=_AGENT_CREDENTIAL_ID)
    agent_id: str = Field(pattern=_SLUG)
    team: str = Field(pattern=_SLUG)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: frozenset[TokenScope] = Field(min_length=1)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    created_by: str = Field(pattern=r"^(human:[A-Za-z0-9_-]{1,64}|erased-user)$")
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = Field(
        default=None, pattern=r"^(human:[A-Za-z0-9_-]{1,64}|service:cli|erased-user|system)$"
    )

    @field_validator("created_at", "expires_at", "revoked_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _consistent(self) -> "AgentCredential":
        if self.expires_at <= self.created_at:
            raise ValueError("a credential expires after it is created")
        if (self.revoked_at is None) != (self.revoked_by is None):
            raise ValueError("revoked_at and revoked_by go together")
        return self

    @property
    def actor(self) -> str:
        """`agent:<agent id>`: what the ledger records for what this credential does."""
        return agent_actor(self.agent_id)

    def active(self, now: datetime) -> bool:
        """Not revoked and not expired (at exactly `expires_at` it is expired)."""
        return self.revoked_at is None and now < self.expires_at

    def permissions(self) -> frozenset[Permission]:
        """What the scopes allow; never anything in `HUMAN_ONLY`."""
        return scope_permissions(self.scopes)

    def ended_at(self) -> datetime:
        """When it stopped working: `revoked_at`, or `expires_at`, the earlier if both."""
        if self.revoked_at is None:
            return self.expires_at
        return min(self.revoked_at, self.expires_at)


@dataclass(frozen=True)
class IssuedAgentCredential:
    """What issuing returns: the secret to show once, and the record to store."""

    credential: str = field(repr=False)
    record: AgentCredential


def new_agent_credential_id() -> str:
    """A random credential label `agc_<24 hex>` (96 bits, not a secret)."""
    return f"{AGENT_CREDENTIAL_ID_PREFIX}{secrets.token_hex(12)}"


def hash_agent_credential(credential: str) -> str:
    """SHA-256 hex of an agent credential (256 random bits, so no slow hash is needed)."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def looks_like_agent_credential(value: str) -> bool:
    """Cheap shape check before any lookup: the prefix and a bounded length."""
    return (
        value.startswith(AGENT_CREDENTIAL_PREFIX) and 4 < len(value) <= MAX_AGENT_CREDENTIAL_LENGTH
    )


def issue_agent_credential(
    org_id: str,
    agent_id: str,
    *,
    team: str,
    fingerprint: str,
    scopes: frozenset[TokenScope] | set[TokenScope],
    created_by: str,
    now: datetime,
    lifetime: timedelta = AGENT_CREDENTIAL_LIFETIME,
) -> IssuedAgentCredential:
    """A fresh credential `nma_` + 43 URL-safe characters (256 random bits) and its record."""
    if not MIN_AGENT_CREDENTIAL_LIFETIME <= lifetime <= MAX_AGENT_CREDENTIAL_LIFETIME:
        raise ValueError("a credential lives 1 to 90 days")
    agent_actor(agent_id)
    secret = AGENT_CREDENTIAL_PREFIX + secrets.token_urlsafe(32)
    record = AgentCredential(
        org_id=org_id,
        id=new_agent_credential_id(),
        agent_id=agent_id,
        team=team,
        fingerprint=fingerprint,
        scopes=frozenset(scopes),
        token_hash=hash_agent_credential(secret),
        created_by=created_by,
        created_at=now,
        expires_at=now + lifetime,
    )
    return IssuedAgentCredential(credential=secret, record=record)


class CallCap:
    """The per-credential cap of valid calls (spec 3, Q16): a presentation at `t` is refused
    when `limit` presentations of the same credential were accepted in the half-open window
    (`t` minus 60 s, `t`]. Refused presentations are not counted. In process memory only."""

    WINDOW = timedelta(seconds=60)

    def __init__(self, limit: int = AGENT_CREDENTIAL_CALLS_PER_MINUTE) -> None:
        if not 10 <= limit <= 6000:
            raise ValueError("calls per minute is 10 to 6000")
        self.limit = limit
        self._seen: dict[str, deque[datetime]] = {}

    FORGET_ABOVE = 1024
    """Above this many remembered credentials, those idle for a whole window are forgotten."""

    def _forget_idle(self, now: datetime) -> None:
        for key in [
            k for k, calls in self._seen.items() if not calls or calls[-1] <= now - self.WINDOW
        ]:
            del self._seen[key]

    def admit(self, credential_id: str, now: datetime) -> int | None:
        """None when the call is accepted (and counted); else the `Retry-After` in whole seconds
        (rounded up, 1 to 60) until the oldest counted call leaves the window."""
        if len(self._seen) > self.FORGET_ABOVE:
            self._forget_idle(now)
        calls = self._seen.setdefault(credential_id, deque())
        if calls and calls[-1] > now:  # the clock moved back: stamps in the future are outside
            calls = deque(c for c in calls if c <= now)  # the window (never a longer lockout)
            self._seen[credential_id] = calls
        while calls and calls[0] <= now - self.WINDOW:
            calls.popleft()
        if len(calls) >= self.limit:
            wait = (calls[0] + self.WINDOW - now).total_seconds()
            return max(1, min(60, math.ceil(wait)))
        calls.append(now)
        return None
