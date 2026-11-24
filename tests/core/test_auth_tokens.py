"""API tokens, scopes and the permission sets of roles and scopes (R1.22 PR 3; ND56)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nuramesh.core.auth import (
    API_TOKEN_LIFETIME,
    API_TOKEN_PREFIX,
    HUMAN_ONLY,
    RESERVED_TOKEN_NAMES,
    ROLE_PERMISSIONS,
    ApiToken,
    Permission,
    Role,
    TokenScope,
    hash_api_token,
    issue_api_token,
    looks_like_api_token,
    scope_permissions,
    token_actor,
    token_name_problem,
)
from nuramesh.core.people import is_human_id

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def test_a_token_is_256_random_bits_shown_once_and_stored_as_its_sha256() -> None:
    issued = issue_api_token("demo", "ci", {TokenScope.RUN}, created_by="human:u_1", now=T0)
    assert issued.token.startswith(API_TOKEN_PREFIX)
    assert len(issued.token) == len(API_TOKEN_PREFIX) + 43  # token_urlsafe(32): 256 bits
    assert issued.record.token_hash == hash_api_token(issued.token)
    assert issued.token not in repr(issued) and issued.token not in issued.record.model_dump_json()
    assert issued.record.token_hash not in repr(issued.record)  # the hash stays out of repr too
    other = issue_api_token("demo", "ci2", {TokenScope.RUN}, created_by="human:u_1", now=T0)
    assert other.token != issued.token and other.record.id != issued.record.id


def test_the_default_lifetime_is_90_days_and_lifetimes_are_bounded() -> None:
    issued = issue_api_token("demo", "ci", {TokenScope.READ}, created_by="human:u_1", now=T0)
    assert issued.record.expires_at - issued.record.created_at == API_TOKEN_LIFETIME
    assert API_TOKEN_LIFETIME == timedelta(days=90)
    for bad in (timedelta(0), timedelta(seconds=-1), timedelta(days=366)):
        with pytest.raises(ValueError):
            issue_api_token(
                "demo", "ci", {TokenScope.READ}, created_by="human:u_1", now=T0, lifetime=bad
            )
    longest = issue_api_token(
        "demo", "ci", {TokenScope.READ}, created_by="human:u_1", now=T0, lifetime=timedelta(365)
    )
    assert longest.record.expires_at == T0 + timedelta(days=365)


def test_active_until_expiry_or_revocation() -> None:
    record = issue_api_token("demo", "ci", {TokenScope.READ}, created_by="human:u_1", now=T0).record
    end = record.expires_at
    assert record.active(T0) and record.active(end - timedelta(microseconds=1))
    assert not record.active(end)  # the expiry instant is already expired
    revoked = record.model_copy(update={"revoked_at": T0, "revoked_by": "human:u_1"})
    assert not ApiToken.model_validate(revoked.model_dump()).active(T0)


def test_the_actor_is_service_and_never_human() -> None:
    record = issue_api_token("demo", "ci", {TokenScope.RUN}, created_by="human:u_1", now=T0).record
    assert record.actor == f"service:{record.id}"
    assert not is_human_id(record.actor)
    for bad in ("human:u_x", "Human", "a b", "", "x" * 64, "-lead", "a:b", "a.b", "a@b"):
        assert token_name_problem(bad) is not None, bad
        with pytest.raises(ValueError):
            token_actor(bad)
    for reserved in RESERVED_TOKEN_NAMES:
        assert token_name_problem(reserved) is not None
    assert (
        token_name_problem("cli-deploy") is None
        and token_actor("tok_" + "a" * 24) == "service:tok_" + "a" * 24
    )


def test_scopes_never_grant_human_only_permissions_and_there_is_no_approve_scope() -> None:
    assert {s.value for s in TokenScope} == {"read", "run"}
    assert scope_permissions({TokenScope.READ}) == {Permission.READ}
    assert scope_permissions({TokenScope.RUN}) == {Permission.READ, Permission.RUN}
    assert scope_permissions(set(TokenScope)) == {Permission.READ, Permission.RUN}
    assert not scope_permissions(set(TokenScope)) & HUMAN_ONLY
    assert Permission.ADMIN in HUMAN_ONLY


def test_role_permissions() -> None:
    assert ROLE_PERMISSIONS[Role.VIEWER] == {Permission.READ}
    assert ROLE_PERMISSIONS[Role.APPROVER] == {
        Permission.READ,
        Permission.RUN,
        Permission.DECIDE,
        Permission.REVEAL,
    }
    assert ROLE_PERMISSIONS[Role.OWNER] == set(Permission)


def test_a_token_record_refuses_bad_shapes() -> None:
    good = issue_api_token("demo", "ci", {TokenScope.READ}, created_by="human:u_1", now=T0).record
    data = good.model_dump()
    changes: tuple[dict[str, object], ...] = (
        {"scopes": frozenset()},
        {"scopes": frozenset({"approve"})},
        {"name": "cli"},
        {"id": "tok_short"},
        {"token_hash": "A" * 64},
        {"created_by": "agent:x"},
        {"created_by": "service:ci"},  # only a person or the CLI creates tokens
        {"expires_at": T0},
        {"created_at": T0.replace(tzinfo=None)},
        {"revoked_at": T0},  # without revoked_by
        {"revoked_by": "human:u_1"},  # without revoked_at
        {"extra": 1},
    )
    for change in changes:
        with pytest.raises(ValidationError):
            ApiToken.model_validate({**data, **change})


def test_the_shape_check_is_cheap_and_bounded() -> None:
    assert looks_like_api_token(API_TOKEN_PREFIX + "x" * 43)
    assert not looks_like_api_token("x" * 47)
    assert not looks_like_api_token(API_TOKEN_PREFIX)
    assert not looks_like_api_token(API_TOKEN_PREFIX + "x" * 300)
