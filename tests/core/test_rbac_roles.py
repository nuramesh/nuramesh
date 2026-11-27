"""R2.13 PR 1 (section 3, case RB-15): roles, permissions and rank, by hand."""

from __future__ import annotations

import pytest

from nuramesh.core.auth import (
    HUMAN_ONLY,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    TokenScope,
    role_rank,
    scope_permissions,
)

COLUMNS = ["read", "run", "decide", "reveal", "build", "admin", "govern"]
# The table of spec section 3, typed by hand: 5 roles x 7 permissions = 35 cells.
TABLE = {
    "viewer": "1000000",
    "approver": "1111000",
    "builder": "1100100",
    "admin": "1111110",
    "owner": "1111111",
}


def test_rb15_role_permissions_match_the_hand_written_table() -> None:
    assert {r.value for r in Role} == set(TABLE)
    assert {p.value for p in Permission} == set(COLUMNS)
    cells = 0
    for role in Role:
        for col, flag in zip(COLUMNS, TABLE[role.value], strict=True):
            cells += 1
            assert (Permission(col) in ROLE_PERMISSIONS[role]) == (flag == "1"), (role, col)
    assert cells == 35
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_rb15_human_only_and_scope_permissions() -> None:
    assert HUMAN_ONLY == {
        Permission.DECIDE,
        Permission.REVEAL,
        Permission.BUILD,
        Permission.ADMIN,
        Permission.GOVERN,
    }
    assert scope_permissions({TokenScope.READ, TokenScope.RUN}) == {Permission.READ, Permission.RUN}
    assert scope_permissions(set(TokenScope)) & HUMAN_ONLY == set()


def test_only_owner_holds_govern_and_admin_is_owner_minus_govern() -> None:
    holders = [r for r in Role if Permission.GOVERN in ROLE_PERMISSIONS[r]]
    assert holders == [Role.OWNER]
    assert ROLE_PERMISSIONS[Role.ADMIN] == ROLE_PERMISSIONS[Role.OWNER] - {Permission.GOVERN}


def test_rank_values_from_the_spec() -> None:
    roles = (Role.VIEWER, Role.APPROVER, Role.BUILDER, Role.ADMIN, Role.OWNER)
    assert [role_rank(r) for r in roles] == [1, 2, 2, 3, 4]
    assert role_rank(Role.APPROVER) == role_rank(Role.BUILDER)


def test_rank_accepts_only_roles() -> None:
    with pytest.raises(ValueError):
        role_rank("unknown")  # type: ignore[arg-type]


def test_role_rank_refuses_a_plain_string() -> None:
    import pytest

    for text in ("owner", "builder", "viewer", "root"):
        with pytest.raises(ValueError):
            role_rank(text)  # type: ignore[arg-type]
