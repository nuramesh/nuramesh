"""R1.37 core: shared link-token rules (spec/identity/README.md section 2) and the invite model
(spec/identity/invites.md section 1)."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from nuramesh.core.identity import (
    INVITE_PREFIX,
    Invite,
    InviteStatus,
    LinkCheck,
    LinkRecord,
    check_link_token,
    hash_link_token,
    invite_ended_at,
    invite_status,
    new_invite_id,
    new_link_token,
)

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def record(**kw: object) -> LinkRecord:
    base: dict[str, object] = {
        "token_hash": "",
        "prefix": INVITE_PREFIX,
        "expires_at": T0 + timedelta(hours=72),
    }
    return LinkRecord(**{**base, **kw})  # type: ignore[arg-type]


def test_token_shape_and_hash() -> None:
    token = new_link_token(INVITE_PREFIX)
    assert re.fullmatch(r"nmi_[A-Za-z0-9_-]{43}", token)
    digest = hash_link_token(token)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert new_link_token(INVITE_PREFIX) != token


def test_check_order_wrong_revoked_used_expired_ok() -> None:
    token = new_link_token(INVITE_PREFIX)
    rec = record(token_hash=hash_link_token(token))
    assert check_link_token(token, rec, T0) is LinkCheck.OK
    assert check_link_token(token, None, T0) is LinkCheck.WRONG
    assert check_link_token("nmi_" + "A" * 43, rec, T0) is LinkCheck.WRONG
    assert check_link_token("x" * 300, rec, T0) is LinkCheck.WRONG
    # a token of another prefix is wrong even if its hash somehow matched
    other = new_link_token("nmr_")
    assert check_link_token(other, record(token_hash=hash_link_token(other)), T0) is LinkCheck.WRONG
    both = record(token_hash=rec.token_hash, revoked_at=T0, used_at=T0, expires_at=T0)
    assert check_link_token(token, both, T0) is LinkCheck.REVOKED
    assert (
        check_link_token(token, record(token_hash=rec.token_hash, used_at=T0, expires_at=T0), T0)
        is LinkCheck.USED
    )
    exp = record(token_hash=rec.token_hash)
    assert check_link_token(token, exp, exp.expires_at - timedelta(seconds=1)) is LinkCheck.OK
    assert check_link_token(token, exp, exp.expires_at) is LinkCheck.EXPIRED


def _invite(**kw: object) -> Invite:
    base: dict[str, object] = {
        "org_id": "acme",
        "id": new_invite_id(),
        "email": "a@example.org",
        "role": "viewer",
        "token_hash": "0" * 64,
        "created_by": "service:cli",
        "created_at": T0,
        "expires_at": T0 + timedelta(hours=72),
    }
    return Invite(**{**base, **kw})  # type: ignore[arg-type]


def test_invite_id_and_status() -> None:
    assert re.fullmatch(r"inv_[0-9a-f]{20}", new_invite_id())
    inv = _invite()
    assert invite_status(inv, T0) is InviteStatus.PENDING
    assert invite_status(inv, inv.expires_at) is InviteStatus.EXPIRED
    assert (
        invite_status(
            inv.model_copy(update={"accepted_at": T0, "email": None}), T0 + timedelta(days=9)
        )
        is InviteStatus.ACCEPTED
    )
    assert (
        invite_status(inv.model_copy(update={"revoked_at": T0, "revoke_reason": "owner"}), T0)
        is InviteStatus.REVOKED
    )


def test_invite_rules_refuse_owner_and_both_ends() -> None:
    with pytest.raises(ValueError):
        _invite(role="owner")
    with pytest.raises(ValueError):
        _invite(accepted_at=T0, revoked_at=T0, revoke_reason="owner")
    with pytest.raises(ValueError):
        _invite(resent_count=6)
    assert "a@example.org" not in repr(_invite())  # personal data stays out of repr


def test_ended_at_is_the_latest_of_the_three() -> None:
    inv = _invite(revoked_at=T0 + timedelta(hours=100), revoke_reason="owner")
    assert invite_ended_at(inv) == T0 + timedelta(hours=100)
    assert invite_ended_at(_invite()) == T0 + timedelta(hours=72)
