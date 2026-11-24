"""Email change requests (R1.40).

Pure (stdlib and pydantic). A request holds the **new** address until its link is opened; the
token (`nme_` + 43 characters) is never stored, only its lowercase hex SHA-256. A `deliverable`
of False is a decoy: the address was already taken, nothing was mailed, and the verify step
refuses it.
"""

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nuramesh.core.identity import LinkRecord

EMAIL_VERIFY_PREFIX = "nme_"
EMAIL_CHANGE_RETENTION = timedelta(days=7)
EMAIL_CHANGE_PER_HOUR = 5


class EmailChange(BaseModel):
    """One pending (or finished) change of a person's address. `new_email` is personal data."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    org_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    new_email: str = Field(repr=False, min_length=3, max_length=320)
    token_hash: str = Field(repr=False)
    deliverable: bool
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    cancelled_at: datetime | None = None

    @field_validator("token_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("a token hash is 64 lowercase hex digits")
        return value


def email_change_ended_at(change: EmailChange) -> datetime:
    """`max(expires_at, consumed_at, cancelled_at)`: when the 7-day retention clock starts."""
    return max(t for t in (change.expires_at, change.consumed_at, change.cancelled_at) if t)


def email_change_link_record(change: EmailChange) -> LinkRecord:
    """The link-token view of a row (a decoy is refused by the caller: it is never mailed)."""
    return LinkRecord(
        token_hash=change.token_hash,
        prefix=EMAIL_VERIFY_PREFIX,
        expires_at=change.expires_at,
        used_at=change.consumed_at,
        revoked_at=change.cancelled_at,
    )
