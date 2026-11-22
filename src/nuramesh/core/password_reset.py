"""Password reset (R1.41, `spec/identity/password-reset.md`): the stored shapes.

Pure (stdlib and pydantic). A reset token and an API-token revoke link are one-time link tokens
(`nuramesh.core.identity`): only the SHA-256 of the full token is stored. A decoy has no user
(`user_id` None): it is never mailed and never valid. `PasswordLoginBlock` is the flag set by an
owner or CLI reset. None of these rows holds an email address.
"""

import re
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nuramesh.core.identity import LinkRecord

RESET_PREFIX: Final = "nmr_"
REVOKE_PREFIX: Final = "nmk_"
RESET_RETENTION: Final = timedelta(days=7)  # Q9
MAX_REVOKE_LINKS: Final = 20
DEFAULT_RESET_TTL: Final = timedelta(minutes=60)
DEFAULT_REVOKE_TTL: Final = timedelta(hours=24)
RESET_REASON: Final = "password_reset"  # the session `revoke_reason`

_HASH = re.compile(r"[0-9a-f]{64}")


def _check_hash(value: str) -> str:
    if not _HASH.fullmatch(value):
        raise ValueError("a token hash is 64 lowercase hex digits")
    return value


class ResetToken(BaseModel):
    """One reset link (or a decoy when `user_id` is None)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    org_id: str = Field(min_length=1, max_length=128)
    token_hash: str = Field(repr=False)
    user_id: str | None = None
    via: Literal["self", "owner", "cli"]
    created_by: str | None = None
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("token_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _check_hash(value)


class RevokeLink(BaseModel):
    """A single-use link that revokes one API token of a user (section 4a)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    org_id: str = Field(min_length=1, max_length=128)
    token_hash: str = Field(repr=False)
    user_id: str
    api_token_id: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("token_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _check_hash(value)


class PasswordLoginBlock(BaseModel):
    """Q15: the old password is refused for this user until a reset is redeemed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=128)
    user_id: str
    blocked_at: datetime
    via: Literal["owner", "cli"]


def reset_link_record(row: ResetToken) -> LinkRecord:
    """The link-token view of a reset row."""
    return LinkRecord(row.token_hash, RESET_PREFIX, row.expires_at, row.consumed_at, row.revoked_at)


def revoke_link_record(row: RevokeLink) -> LinkRecord:
    """The link-token view of a revoke-link row."""
    return LinkRecord(
        row.token_hash, REVOKE_PREFIX, row.expires_at, row.consumed_at, row.revoked_at
    )


def reset_retention_end(row: ResetToken) -> datetime:
    """When the row may be deleted: `max(expires_at, consumed_at, revoked_at)` + 7 days."""
    return max(t for t in (row.expires_at, row.consumed_at, row.revoked_at) if t) + RESET_RETENTION


def revoke_link_retention_end(row: RevokeLink) -> datetime:
    """`max(expires_at, consumed_at)` + 7 days."""
    return max(t for t in (row.expires_at, row.consumed_at) if t) + RESET_RETENTION
