"""Round 3 behaviour pins for `nuramesh.core.auth` found by mutation testing (tests only):
exact bounds of the setup-token length, the scrypt cost floor and the dummy-hash cache."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nuramesh.core import auth
from nuramesh.core.auth import (
    MAX_SETUP_TOKEN_LENGTH,
    ScryptParams,
    SetupToken,
    SetupTokenCheck,
    check_setup_token,
    hash_setup_token,
)

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def record_for(token: str) -> SetupToken:
    return SetupToken(
        org_id="acme",
        token_hash=hash_setup_token(token),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def test_setup_token_length_limit_is_exactly_256() -> None:
    assert MAX_SETUP_TOKEN_LENGTH == 256
    longest = "a" * 256
    too_long = "a" * 257
    assert check_setup_token(longest, record_for(longest), NOW) is SetupTokenCheck.OK
    # even a token whose hash matches its record is refused once it is over the limit
    assert check_setup_token(too_long, record_for(too_long), NOW) is SetupTokenCheck.WRONG


def test_scrypt_cost_floor_is_two() -> None:
    assert ScryptParams(n=2).n == 2
    with pytest.raises(ValidationError):
        ScryptParams(n=1)


def test_dummy_hash_cache_keeps_eight_parameter_sets() -> None:
    assert auth._dummy_hash.cache_info().maxsize == 8
