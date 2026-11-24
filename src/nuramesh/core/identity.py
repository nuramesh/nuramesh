"""Identity, phase 1 (ND329): one-time link tokens, the invite model, TOTP and MFA recovery
codes (R1.38) and the types of outgoing mail.

Pure (stdlib and pydantic only). Shapes cover link tokens and invites. The raw token exists
only in the mail (or the operator's printed link); only its lowercase hex SHA-256 is ever
stored, and no `repr` shows a token.

The mail types (R1.39: `MailSender`, `OutgoingMail`, `MailSendError`,
`MailSettings` and the address, URL and name checks) are below; nothing here opens a socket or a
file, and nothing here holds a password. The SMTP sender and the test `RecordingMailSender` live in
`nuramesh.server.mail`.
"""

import base64
import hashlib
import hmac
import re
import secrets
import struct
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Literal, Protocol
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

INVITE_PREFIX = "nmi_"
MAX_TOKEN_LENGTH = 256
ERASED_CREATOR = "erased-user"
INVITE_ROLES = ("approver", "viewer")
INVITE_RETENTION = timedelta(days=90)
MAX_RESENDS = 5
REVOKE_REASONS = ("owner", "send_failed", "superseded", "creator_changed")
_TOKEN_BODY = re.compile(r"[A-Za-z0-9_-]{43}")
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_BATCH = 10
_CODE_RE = re.compile(r"[0-9]{6}", re.ASCII)
_RECOVERY_RE = re.compile(r"[A-HJ-NP-Z2-9]{16}")

# ---- TOTP and MFA recovery codes (R1.38) ---


def totp_step(unix_seconds: int) -> int:
    """The 30-second step number of a Unix time."""
    return unix_seconds // TOTP_STEP_SECONDS


def hotp(secret: bytes, counter: int) -> str:
    """RFC 4226 HOTP, 6 digits, HMAC-SHA1, dynamic truncation."""
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def verify_totp(secret: bytes, code: str, now: int, last_used_step: int | None) -> int | None:
    """Return the accepted step, or None. Steps s-1, s, s+1 are all computed and compared."""
    if not isinstance(code, str) or _CODE_RE.fullmatch(code) is None:
        return None
    s = totp_step(now)
    accepted: int | None = None
    for step in (s - 1, s, s + 1):
        if step < 0:
            expected = hotp(secret, 0)  # keep the work constant; never accepted
            hmac.compare_digest(expected.encode(), code.encode())
            continue
        match = hmac.compare_digest(hotp(secret, step).encode(), code.encode())
        if match and (last_used_step is None or step > last_used_step):
            accepted = step if accepted is None else max(accepted, step)
    return accepted


def new_totp_secret() -> bytes:
    """A new 20-byte TOTP secret."""
    return secrets.token_bytes(20)


def secret_to_base32(secret: bytes) -> str:
    """Base32, upper case, no padding."""
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def secret_from_base32(text: str) -> bytes:
    """Inverse of `secret_to_base32`."""
    return base64.b32decode(text + "=" * (-len(text) % 8))


def provisioning_uri(issuer: str, email: str, secret: bytes) -> str:
    """The otpauth URI shown only to the person enrolling."""
    iss = quote(issuer, safe="")
    return (
        f"otpauth://totp/{iss}:{quote(email, safe='')}?secret={secret_to_base32(secret)}"
        f"&issuer={iss}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


def new_recovery_codes(count: int = RECOVERY_BATCH) -> list[str]:
    """`count` distinct recovery codes, displayed XXXX-XXXX-XXXX-XXXX."""
    out: set[str] = set()
    while len(out) < count:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(16))
        out.add("-".join(raw[i : i + 4] for i in range(0, 16, 4)))
    return sorted(out, key=lambda _: secrets.randbits(32))


_ASCII_UPPER = {c: c - 32 for c in range(ord("a"), ord("z") + 1)}


def normalise_recovery_code(text: str) -> str | None:
    """Upper-case, remove hyphens and spaces; None unless 16 symbols of the alphabet remain."""
    # ASCII letters only: str.upper() would also turn "ß" into "SS" (R1.38 m5)
    cleaned = text.translate(_ASCII_UPPER).replace("-", "").replace(" ", "")
    return cleaned if _RECOVERY_RE.fullmatch(cleaned) else None


def recovery_code_hash(normalised: str) -> str:
    """Lowercase hex SHA-256 of the normalised 16 characters."""
    return hashlib.sha256(normalised.encode("ascii")).hexdigest()


# ---- link tokens ----------------------------------------------------------------------------


def new_link_token(prefix: str) -> str:
    """A fresh one-time token: the prefix and 43 url-safe characters (256 bits)."""
    return prefix + secrets.token_urlsafe(32)


def hash_link_token(token: str) -> str:
    """Lowercase hex SHA-256 of the UTF-8 full token (prefix included): the only stored form."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LinkCheck(StrEnum):
    """The answer of `check_link_token`. Callers show WRONG, REVOKED, USED and EXPIRED alike."""

    OK = "ok"
    WRONG = "wrong"
    REVOKED = "revoked"
    USED = "used"
    EXPIRED = "expired"


@dataclass(frozen=True)
class LinkRecord:
    """What `check_link_token` needs of a stored token row."""

    token_hash: str
    prefix: str
    expires_at: datetime
    used_at: datetime | None = None
    revoked_at: datetime | None = None


def check_link_token(token: str, record: LinkRecord | None, now: datetime) -> LinkCheck:
    """Order: WRONG (unknown, hash differs, wrong prefix or shape), REVOKED, USED, EXPIRED (at
    exactly `expires_at`), OK. Constant-time hash comparison."""
    if record is None or len(token) > MAX_TOKEN_LENGTH:
        return LinkCheck.WRONG
    if not token.startswith(record.prefix) or not _TOKEN_BODY.fullmatch(
        token[len(record.prefix) :]
    ):
        return LinkCheck.WRONG
    if not hmac.compare_digest(hash_link_token(token), record.token_hash):
        return LinkCheck.WRONG
    if record.revoked_at is not None:
        return LinkCheck.REVOKED
    if record.used_at is not None:
        return LinkCheck.USED
    if now >= record.expires_at:
        return LinkCheck.EXPIRED
    return LinkCheck.OK


# ---- invites --------------------------------------------------------------------------------


class InviteStatus(StrEnum):
    """Computed, never stored."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


def new_invite_id() -> str:
    """`inv_` and 20 lowercase hex digits (80 bits)."""
    return "inv_" + secrets.token_hex(10)


class Invite(BaseModel):
    """One invitation. `email` is personal data: out of `repr`, None once accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    org_id: str = Field(min_length=1, max_length=128)
    id: str
    email: str | None = Field(default=None, repr=False)
    role: Literal["approver", "viewer"]
    token_hash: str = Field(repr=False)
    created_by: str = Field(min_length=1, max_length=200)
    created_at: datetime
    expires_at: datetime
    resent_count: int = Field(default=0, ge=0, le=MAX_RESENDS)
    accepted_at: datetime | None = None
    accepted_user_id: str | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not re.fullmatch(r"inv_[0-9a-f]{20}", value):
            raise ValueError("an invite id is inv_ and 20 lowercase hex digits")
        return value

    @field_validator("token_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("a token hash is 64 lowercase hex digits")
        return value

    @field_validator("revoke_reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        if value is not None and value not in REVOKE_REASONS:
            raise ValueError("unknown revoke reason")
        return value

    @model_validator(mode="after")
    def _ends(self) -> "Invite":
        if self.accepted_at is not None and self.revoked_at is not None:
            raise ValueError("an invite is accepted or revoked, not both")
        if (self.revoked_at is None) != (self.revoke_reason is None):
            raise ValueError("a revoked invite has a reason, and only a revoked one")
        return self


def invite_status(invite: Invite, now: datetime) -> InviteStatus:
    """accepted, revoked, expired (`now >= expires_at`), else pending."""
    if invite.accepted_at is not None:
        return InviteStatus.ACCEPTED
    if invite.revoked_at is not None:
        return InviteStatus.REVOKED
    if now >= invite.expires_at:
        return InviteStatus.EXPIRED
    return InviteStatus.PENDING


def invite_ended_at(invite: Invite) -> datetime:
    """`max(accepted_at, revoked_at, expires_at)`: when the 90-day retention clock starts."""
    return max(t for t in (invite.accepted_at, invite.revoked_at, invite.expires_at) if t)


def invite_link_record(invite: Invite) -> LinkRecord:
    """The link-token view of an invite row."""
    return LinkRecord(
        token_hash=invite.token_hash,
        prefix=INVITE_PREFIX,
        expires_at=invite.expires_at,
        used_at=invite.accepted_at,
        revoked_at=invite.revoked_at,
    )


# ---- the mail seam (names) -------------------------------


# -- outgoing mail (R1.39) ----------------------------------------

DEFAULT_ORG_NAME: Final = "your organization"
MAX_ORG_NAME: Final = 80
MAX_PUBLIC_URL: Final = 200
_LOOPBACK_NAMES: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_BAD_CHARS: Final = re.compile(r"[\r\n\x00]")
_ADDRESS: Final = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,253}")


class MailKind(StrEnum):
    """What a message is for (a fixed set; the template follows from it)."""

    INVITE = "invite"
    RESET = "reset"
    VERIFY = "verify"
    NOTICE = "notice"
    TEST = "test"


class MailErrorCode(StrEnum):
    """Why a send failed. The closed set callers may show or ledger."""

    NOT_CONFIGURED = "not_configured"
    CONNECT = "connect"
    TLS = "tls"
    AUTH = "auth"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    INVALID_MESSAGE = "invalid_message"


class MailSendError(Exception):
    """A send failed. The text is the code and nothing else: never the server's reply, the
    recipient, the password or a link."""

    def __init__(self, code: MailErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class MailSecurity(StrEnum):
    """How the connection is protected."""

    STARTTLS = "starttls"
    TLS = "tls"
    NONE = "none"


DEFAULT_PORTS: Final = {MailSecurity.STARTTLS: 587, MailSecurity.TLS: 465, MailSecurity.NONE: 25}


@dataclass(frozen=True)
class MailSettings:
    """The SMTP settings. The password is read from `password_file` at send time (never cached,
    never held here); `repr` shows no path, user or sender."""

    host: str = field(repr=False)
    port: int
    security: MailSecurity
    sender: str = field(repr=False)
    public_url: str
    user: str | None = field(default=None, repr=False)
    password_file: str | None = field(default=None, repr=False)
    timeout: float = 10.0


@dataclass(frozen=True)
class OutgoingMail:
    """One plain-text message. `text` carries a secret link, so `repr` hides it and the address."""

    to: str = field(repr=False)
    kind: MailKind
    subject: str = field(repr=False)
    text: str = field(repr=False)


class MailSender(Protocol):
    """Anything that can deliver a message; raises `MailSendError` when it cannot."""

    def send(self, mail: OutgoingMail) -> None: ...


def address_problem(address: str) -> str | None:
    """Why `address` cannot be used in a header (a fixed reason), or None. Refused: CR, LF or NUL,
    any non-ASCII character (SMTPUTF8 is not supported: write the domain in punycode), and
    anything that is not a plain `local@domain`."""
    if _BAD_CHARS.search(address):
        return "control character"
    if not address.isascii():
        return "non-ASCII character"
    if len(address) > 254 or not _ADDRESS.fullmatch(address):
        return "not a plain address"
    local, _, domain = address.partition("@")
    if (
        local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or "." not in domain
        or domain.startswith((".", "-"))
        or domain.endswith((".", "-"))
        or ".." in domain
    ):
        return "not a plain address"
    return None


def is_loopback_host(host: str) -> bool:
    """Exactly `localhost`, `127.0.0.1` or `::1` (the spec names; not the rest of 127/8)."""
    return host.strip("[]").lower() in _LOOPBACK_NAMES


def parse_public_url(text: str) -> str:
    """The base URL people open, normalised (a trailing `/` removed). `https`, or `http` only for
    `localhost` and loopback addresses; no userinfo, path, query or fragment. Raises `ValueError`
    (a fixed message, never the value)."""
    if not text or len(text) > MAX_PUBLIC_URL or re.search(r"[\s\x00-\x1f\x7f?#]", text):
        raise ValueError("not a plain base URL")
    try:
        parts = urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError for a bad port)
    except ValueError:
        raise ValueError("not a plain base URL") from None
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        raise ValueError("not a plain base URL")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("no path, query or fragment")
    if parts.scheme == "http" and not is_loopback_host(host):
        raise ValueError("http is allowed for localhost only")
    return f"{parts.scheme}://{parts.netloc}"


def org_name_problem(name: str) -> str | None:
    """Why `name` cannot be the organization name in mails, or None: 1 to 80 characters, no
    control (`Cc`), format (`Cf`), line (`Zl`) or paragraph (`Zp`) separator characters."""
    if not 1 <= len(name) <= MAX_ORG_NAME:
        return "1 to 80 characters"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in name):
        return "no control or format characters"
    return None
