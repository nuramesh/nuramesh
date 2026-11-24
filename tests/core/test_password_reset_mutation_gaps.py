"""Mutation pins for `nuramesh.core.password_reset` (#1268): frozen rows, input never echoed in a
validation error, organization id bounds on all three rows, and the revoke link's hidden hash.
Fake data only: the hashes are one repeated character."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nuramesh.core.password_reset import PasswordLoginBlock, ResetToken, RevokeLink

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HASH = "b" * 64
MARKER = "obviously-fake-marker-not-a-hash"


def make_reset(**kw: object) -> ResetToken:
    base: dict[str, object] = {
        "org_id": "acme",
        "token_hash": HASH,
        "via": "self",
        "created_at": T0,
        "expires_at": T0 + timedelta(minutes=60),
    }
    return ResetToken(**{**base, **kw})  # type: ignore[arg-type]


def make_revoke(**kw: object) -> RevokeLink:
    base: dict[str, object] = {
        "org_id": "acme",
        "token_hash": HASH,
        "user_id": "u_1",
        "api_token_id": "tok_1",
        "created_at": T0,
        "expires_at": T0 + timedelta(hours=24),
    }
    return RevokeLink(**{**base, **kw})  # type: ignore[arg-type]


def make_block(**kw: object) -> PasswordLoginBlock:
    base: dict[str, object] = {"org_id": "acme", "user_id": "u_1", "blocked_at": T0, "via": "cli"}
    return PasswordLoginBlock(**{**base, **kw})  # type: ignore[arg-type]


def test_rows_are_frozen() -> None:
    with pytest.raises(ValidationError):
        make_reset().consumed_at = T0
    with pytest.raises(ValidationError):
        make_revoke().consumed_at = T0
    with pytest.raises(ValidationError):
        make_block().via = "owner"


def test_bad_hash_input_is_not_echoed_in_the_error() -> None:
    for build in (make_reset, make_revoke):
        with pytest.raises(ValidationError) as caught:
            build(token_hash=MARKER)
        assert MARKER not in str(caught.value)


def test_org_id_bounds_on_every_row() -> None:
    for build in (make_reset, make_revoke, make_block):
        assert build(org_id="x").org_id == "x"  # one character is allowed
        assert build(org_id="x" * 128).org_id == "x" * 128  # the longest allowed
        with pytest.raises(ValidationError):
            build(org_id="")
        with pytest.raises(ValidationError):
            build(org_id="x" * 129)


def test_revoke_link_repr_hides_the_hash() -> None:
    assert HASH not in repr(make_revoke())
