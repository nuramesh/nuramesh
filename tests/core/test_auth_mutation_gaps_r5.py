"""Round 5 behaviour pin for `nuramesh.core.auth` found by mutation testing (tests only, #1184):
the API token lifetime guard reports its own bounds message. Test data is made up for the test and
never key-shaped."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nuramesh.core import auth
from nuramesh.core.auth import TokenScope

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def test_a_zero_lifetime_is_refused_by_the_bounds_guard_not_the_record_validator() -> None:
    # `0 < lifetime` is strict: with `0 <= lifetime` a zero lifetime would slip past the guard and
    # be refused later by the record's "expires after it is created" validator (a
    # ValidationError with another message), so the caller would not be told the real bounds.
    with pytest.raises(ValueError, match="a token lives more than 0 and at most 365 days") as info:
        auth.issue_api_token(
            "o", "bot", {TokenScope.READ}, created_by="service:cli", now=NOW, lifetime=timedelta(0)
        )
    assert not isinstance(info.value, ValidationError)
