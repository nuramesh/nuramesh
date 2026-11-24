"""Pure session models, tokens, CSRF and the lockout schedule (R1.22 PR 2, T66)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nuramesh.core.auth import (
    ACCESS_LIFETIME,
    REFRESH_IDLE,
    SESSION_ABSOLUTE,
    SESSION_RETENTION,
    AuthFailure,
    AuthSession,
    LockoutPolicy,
    TokenKind,
    csrf_matches,
    csrf_token,
    hash_auth_token,
    issue_tokens,
    new_session_id,
    new_user_id,
)

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def session(**kw: object) -> AuthSession:
    data: dict[str, object] = {
        "org_id": "acme",
        "id": new_session_id(),
        "user_id": new_user_id(),
        "created_at": T0,
        "last_active_at": T0,
        "absolute_expires_at": T0 + SESSION_ABSOLUTE,
        "login_via": "password",
    }
    data.update(kw)
    return AuthSession(**data)  # type: ignore[arg-type]


def test_the_figures_follow_nd56() -> None:
    assert ACCESS_LIFETIME == timedelta(minutes=15)
    assert REFRESH_IDLE == timedelta(hours=8)
    assert SESSION_ABSOLUTE == timedelta(days=7)
    assert SESSION_RETENTION == timedelta(days=90)


def test_session_ids_are_128_bit_hex_and_unique() -> None:
    ids = {new_session_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) == 32 and int(i, 16) >= 0 for i in ids)
    with pytest.raises(ValidationError):
        session(id="short")


def test_a_session_is_active_until_revoked_or_absolutely_expired() -> None:
    s = session()
    assert s.active(T0) and s.active(T0 + SESSION_ABSOLUTE - timedelta(seconds=1))
    assert not s.active(T0 + SESSION_ABSOLUTE)
    revoked = session(revoked_at=T0 + timedelta(hours=1), revoke_reason="logout")
    assert not revoked.active(T0 + timedelta(minutes=61)) and not revoked.active(T0)
    assert revoked.ended_at == T0 + timedelta(hours=1)
    assert s.ended_at == T0 + SESSION_ABSOLUTE
    with pytest.raises(ValidationError):
        session(revoke_reason="Not Allowed!")
    with pytest.raises(ValidationError):
        session(created_at=datetime(2026, 1, 1))  # no time zone


def test_issued_tokens_are_256_bit_distinct_and_stored_hashed() -> None:
    s = session()
    issued = issue_tokens(s, T0)
    assert issued.access_token != issued.refresh_token
    assert len(issued.access_token) == len(issued.refresh_token) == 43
    assert issued.access.token_hash == hash_auth_token(issued.access_token)
    assert issued.refresh.token_hash == hash_auth_token(issued.refresh_token)
    assert issued.access.kind is TokenKind.ACCESS and issued.refresh.kind is TokenKind.REFRESH
    assert issued.access.expires_at == T0 + timedelta(minutes=15)
    assert issued.refresh.expires_at == T0 + timedelta(hours=8)
    assert issued.access_token not in repr(issued) and issued.refresh_token not in repr(issued)
    assert issue_tokens(s, T0).access_token != issued.access_token


def test_tokens_never_outlive_the_absolute_end() -> None:
    s = session(absolute_expires_at=T0 + timedelta(minutes=5))
    issued = issue_tokens(s, T0)
    assert issued.access.expires_at == issued.refresh.expires_at == T0 + timedelta(minutes=5)


def test_csrf_is_bound_to_the_refresh_token_and_compared_whole() -> None:
    a, b = "a" * 43, "b" * 43
    token = csrf_token(a)
    assert len(token) == 64 and token == csrf_token(a) and token != csrf_token(b)
    assert csrf_matches(token, a) and not csrf_matches(token, b)
    assert not csrf_matches(token[:-1], a) and not csrf_matches("", a)
    assert not csrf_matches(token.upper(), a)
    assert a not in token


def test_lockout_schedule() -> None:
    p = LockoutPolicy()
    assert [p.lock_for(n) for n in range(1, 5)] == [timedelta(0)] * 4
    assert [p.lock_for(n).total_seconds() for n in range(5, 12)] == [
        60,
        120,
        240,
        480,
        900,
        900,
        900,
    ]
    assert p.lock_for(10_000) == timedelta(minutes=15)  # no overflow


def test_failures_count_up_lock_and_decay() -> None:
    p = LockoutPolicy()
    f: AuthFailure | None = None
    for i in range(1, 6):
        f = p.after_failure(f, "k", T0 + timedelta(seconds=i))
    assert f is not None and f.count == 5
    assert f.locked_until == T0 + timedelta(seconds=5) + timedelta(minutes=1)
    assert p.retry_after(f, T0 + timedelta(seconds=5)) == 60
    assert p.retry_after(f, T0 + timedelta(seconds=5, milliseconds=500)) == 60
    assert p.retry_after(f, f.locked_until) == 0
    assert p.retry_after(None, T0) == 0
    later = p.after_failure(f, "k", f.last_failure_at + timedelta(hours=1))
    assert later.count == 1 and later.locked_until is None  # a quiet hour starts over
    assert p.after_failure(f, "k", f.last_failure_at + timedelta(minutes=59)).count == 6
    custom = LockoutPolicy(threshold=2, base=timedelta(seconds=10), cap=timedelta(seconds=25))
    two = custom.after_failure(custom.after_failure(None, "k", T0), "k", T0)
    assert two.locked_until == T0 + timedelta(seconds=10)
