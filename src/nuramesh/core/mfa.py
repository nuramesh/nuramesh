"""Stored records of MFA (R1.38): the factor, the login
challenge and the organization's policy. Pure data; the secret is only ever held sealed."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MFA_PENDING_LIFETIME = timedelta(minutes=10)  # a factor not confirmed by then is dropped
MFA_CHALLENGE_KEEP = timedelta(hours=1)  # a challenge row is deleted this long after expiry (Q9)
MFA_CHALLENGE_ATTEMPTS = 5  # wrong codes a challenge allows
MFA_METHODS = ("totp", "recovery")
SEALED_SECRET_BYTES = 48  # nonce(12) + ciphertext(20) + tag(16)
_FACTOR_ID = re.compile(r"^mf_[0-9a-f]{20}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


CHALLENGE_PREFIX = "nmc_"
MFA_ENROL_CHALLENGE_MAX = timedelta(seconds=300)  # an enrol challenge never lives longer (Q13)


def new_challenge_token() -> str:
    """A fresh login challenge token (shown once; only `challenge_hash` is stored)."""
    return CHALLENGE_PREFIX + secrets.token_urlsafe(32)


def challenge_hash(token: str) -> str:
    """The stored form of a challenge token: SHA-256 hex."""
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("times must carry a time zone")
    return value


class MfaFactor(BaseModel):
    """One TOTP factor of a person. `secret_enc` is sealed under the auth key `key_id`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    user_id: str
    id: str
    secret_enc: bytes = Field(repr=False)
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    created_at: datetime
    confirmed_at: datetime | None = None
    last_used_step: int | None = None
    password_checked_at: datetime

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _FACTOR_ID.fullmatch(value) is None:
            raise ValueError("a factor id is mf_ and 20 hex digits")
        return value

    @field_validator("secret_enc")
    @classmethod
    def _sealed(cls, value: bytes) -> bytes:
        if len(value) != SEALED_SECRET_BYTES:
            raise ValueError(f"a sealed secret is {SEALED_SECRET_BYTES} bytes")
        return value

    @field_validator("created_at", "confirmed_at", "password_checked_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @property
    def active(self) -> bool:
        """Confirmed (a pending factor is not a second factor)."""
        return self.confirmed_at is not None

    def pending_expired(self, now: datetime) -> bool:
        """A pending factor older than ten minutes is ignored."""
        return self.confirmed_at is None and now >= self.created_at + MFA_PENDING_LIFETIME


class MfaChallenge(BaseModel):
    """A single-use token issued after a correct password; only its hash is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    token_hash: str
    user_id: str
    purpose: Literal["verify", "enrol"]
    factor_id: str | None = None
    created_at: datetime
    expires_at: datetime
    attempts: int = Field(default=0, ge=0)
    consumed_at: datetime | None = None

    @field_validator("token_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if _HEX64.fullmatch(value) is None:
            raise ValueError("a challenge is stored as a SHA-256 hex digest")
        return value

    @field_validator("created_at", "expires_at", "consumed_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    def purge_at(self) -> datetime:
        """When the row is deleted: one hour after it expired."""
        return self.expires_at + MFA_CHALLENGE_KEEP


class OrgAuthSettings(BaseModel):
    """The organization's sign-in policy. No stored row means MFA is not required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    mfa_required: bool = False
    updated_at: datetime | None = None
    updated_by: str | None = None

    @field_validator("updated_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)
