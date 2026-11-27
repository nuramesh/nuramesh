"""R1.41 core types (sections 1, 4a): reset tokens, revoke links
and the password-login block are pure data; the link-token check is shared with R1.37."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nuramesh.core.identity import LinkCheck, check_link_token, hash_link_token, new_link_token
from nuramesh.core.password_reset import (
    RESET_PREFIX,
    REVOKE_PREFIX,
    PasswordLoginBlock,
    ResetToken,
    RevokeLink,
    reset_link_record,
    reset_retention_end,
    revoke_link_record,
    revoke_link_retention_end,
)

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def reset(**kw: object) -> ResetToken:
    base: dict[str, object] = {
        "org_id": "acme",
        "token_hash": "a" * 64,
        "user_id": "u_1",
        "via": "self",
        "created_at": T0,
        "expires_at": T0 + timedelta(minutes=60),
    }
    return ResetToken(**{**base, **kw})  # type: ignore[arg-type]


def test_prefixes() -> None:
    assert (RESET_PREFIX, REVOKE_PREFIX) == ("nmr_", "nmk_")


def test_via_is_closed_and_user_may_be_null() -> None:
    assert reset(user_id=None).user_id is None  # a decoy
    for via in ("self", "owner", "cli"):
        assert reset(via=via).via == via
    with pytest.raises(ValidationError):
        reset(via="invite")


def test_hash_shape_and_no_token_in_repr() -> None:
    with pytest.raises(ValidationError):
        reset(token_hash="nmr_x")
    assert "a" * 64 not in repr(reset())


def test_check_link_token_order_for_reset_records() -> None:
    token = new_link_token(RESET_PREFIX)
    row = reset(token_hash=hash_link_token(token))
    assert check_link_token(token, reset_link_record(row), T0) is LinkCheck.OK
    at_expiry = T0 + timedelta(minutes=60)
    assert check_link_token(token, reset_link_record(row), at_expiry) is LinkCheck.EXPIRED
    just_before = at_expiry - timedelta(seconds=1)
    assert check_link_token(token, reset_link_record(row), just_before) is LinkCheck.OK
    used = row.model_copy(update={"consumed_at": T0})
    assert check_link_token(token, reset_link_record(used), T0) is LinkCheck.USED
    gone = row.model_copy(update={"revoked_at": T0})
    assert check_link_token(token, reset_link_record(gone), T0) is LinkCheck.REVOKED
    other = new_link_token("nmi_")  # an invite token is WRONG for a reset record
    assert check_link_token(other, reset_link_record(row), T0) is LinkCheck.WRONG


def test_retention_is_seven_days_after_the_latest_end() -> None:
    row = reset()
    assert reset_retention_end(row) == T0 + timedelta(minutes=60, days=7)
    late = row.model_copy(update={"revoked_at": T0 + timedelta(days=1)})
    assert reset_retention_end(late) == T0 + timedelta(days=8)
    link = RevokeLink(
        org_id="acme",
        token_hash="b" * 64,
        user_id="u_1",
        api_token_id="tok_1",
        created_at=T0,
        expires_at=T0 + timedelta(hours=24),
    )
    assert revoke_link_retention_end(link) == T0 + timedelta(hours=24, days=7)
    token = new_link_token(REVOKE_PREFIX)
    kept = link.model_copy(update={"token_hash": hash_link_token(token)})
    assert check_link_token(token, revoke_link_record(kept), T0) is LinkCheck.OK


def test_block_has_no_personal_data() -> None:
    block = PasswordLoginBlock(org_id="acme", user_id="u_1", blocked_at=T0, via="owner")
    assert set(block.model_dump()) == {"org_id", "user_id", "blocked_at", "via"}
    with pytest.raises(ValidationError):
        PasswordLoginBlock(org_id="acme", user_id="u_1", blocked_at=T0, via="self")  # type: ignore[arg-type]
