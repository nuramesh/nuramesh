"""Round 4 behaviour pins for `nuramesh.core.auth` found by mutation testing (tests only):
the exact role ranks, the scrypt r and p floors, session rebuild and the tolerant security-log
load. Test data is made up for the test and never key-shaped."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.auth import (
    AuthSession,
    Role,
    ScryptParams,
    SecurityLogEntry,
    role_rank,
)

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def test_role_ranks_are_exactly_the_spec_values() -> None:
    # spec/rbac section 2: viewer 1, approver 2, builder 2, admin 3, owner 4
    ranks = {role: role_rank(role) for role in Role}
    assert ranks == {
        Role.VIEWER: 1,
        Role.APPROVER: 2,
        Role.BUILDER: 2,
        Role.ADMIN: 3,
        Role.OWNER: 4,
    }
    assert all(type(rank) is int for rank in ranks.values())


def test_role_rank_refuses_a_non_role() -> None:
    with pytest.raises(ValueError, match="not a role"):
        role_rank("nobody")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a role"):
        role_rank(["owner"])  # type: ignore[arg-type]


def test_scrypt_r_and_p_floor_is_one_and_zero_is_refused() -> None:
    assert ScryptParams(n=2, r=1, p=2).r == 1
    assert ScryptParams(n=2, r=2, p=1).p == 1
    for bad in ({"r": 0}, {"p": 0}):
        with pytest.raises(ValidationError):
            ScryptParams(n=2, **bad)


def _session_data(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "org_id": "o",
        "id": "0123456789abcdef" * 2,
        "user_id": "human:u_" + "ab12" * 5,
        "created_at": NOW,
        "last_active_at": NOW,
        "absolute_expires_at": NOW + timedelta(days=7),
    }
    data.update(kw)
    return data


@pytest.mark.parametrize("stored", ["oidc", "password", "ftp", None])
def test_from_stored_returns_a_session_keeping_login_via_as_stored(stored: str | None) -> None:
    row = AuthSession.from_stored(_session_data(login_via=stored))
    assert isinstance(row, AuthSession)
    assert row.login_via == stored
    assert row.org_id == "o"


def test_from_stored_still_validates_everything_else() -> None:
    with pytest.raises(ValidationError):
        AuthSession.from_stored(_session_data(login_via="oidc", id="not-an-id"))


def test_check_login_via_accepts_only_the_closed_set() -> None:
    for good in ("password", "oidc"):
        AuthSession(login_via=good, **_session_data()).check_login_via()
    for bad in (None, "ftp", ""):
        row = AuthSession.from_stored(_session_data(login_via=bad))
        with pytest.raises(ValueError, match="login_via is one of"):
            row.check_login_via()


def _log_fields(**kw: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "org_id": "org",
        "id": "0" * 32,
        "at": NOW,
        "type": "auth.login_failed",
        "expires_at": NOW + timedelta(days=90),
    }
    fields.update(kw)
    return fields


def test_load_keeps_known_details_and_returns_the_entry() -> None:
    details = {"email": "a@example.test", "reason": "wrong", "count": 3}
    entry = SecurityLogEntry.load(**_log_fields(type="run.start_refused", details=details))
    assert isinstance(entry, SecurityLogEntry)
    assert entry.details == details


def test_load_without_details_gives_an_empty_dict() -> None:
    entry = SecurityLogEntry.load(**_log_fields())
    assert entry.details == {}
    assert SecurityLogEntry.load(**_log_fields(details=None)).details == {}


def test_load_keeps_keys_it_does_not_know_as_they_are() -> None:
    details = {"email": "a@example.test", "zz_plugin_word": "kept", "zz_other": 7}
    entry = SecurityLogEntry.load(**_log_fields(details=details))
    assert isinstance(entry, SecurityLogEntry)
    assert entry.details == details
    # a strict constructor still refuses the same keys
    with pytest.raises(ValidationError):
        SecurityLogEntry(**_log_fields(details=details))


@pytest.mark.parametrize("n", [2, 4, 8, 1024, 16384])
def test_scrypt_n_accepts_every_power_of_two(n: int) -> None:
    assert ScryptParams(n=n).n == n


@pytest.mark.parametrize("n", [0, 1, 3, 5, 6, 12, 1000])
def test_scrypt_n_refuses_what_is_not_a_power_of_two(n: int) -> None:
    with pytest.raises(ValidationError):
        ScryptParams(n=n)
