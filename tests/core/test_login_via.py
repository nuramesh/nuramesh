"""R1.38 MA-R2 (spec/identity/mfa.md section 2, case T69): every session insert names how the
person signed in (`login_via`), with no default."""

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nuramesh.core.auth import SESSION_ABSOLUTE, AuthSession, new_session_id, new_user_id

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parents[2] / "src" / "nuramesh"


def data() -> dict[str, Any]:
    return {
        "org_id": "acme",
        "id": new_session_id(),
        "user_id": new_user_id(),
        "created_at": T0,
        "last_active_at": T0,
        "absolute_expires_at": T0 + SESSION_ABSOLUTE,
    }


def test_t69_a_session_without_login_via_raises_type_error() -> None:
    with pytest.raises(TypeError):
        AuthSession(**data())


def test_t69_an_unknown_login_via_raises_value_error() -> None:
    with pytest.raises(ValueError):
        AuthSession(**data(), login_via="ftp")
    with pytest.raises(ValueError):
        AuthSession(**data(), login_via=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("via", ["password", "oidc"])
def test_t69_the_closed_set_is_accepted(via: str) -> None:
    session = AuthSession(**data(), login_via=via)
    assert session.login_via == via
    assert session.mfa_verified_at is None and session.mfa_method is None


def test_t69_a_stored_row_with_a_bad_value_can_still_be_read_back() -> None:
    """Refresh (T68) must see a NULL or unknown value to refuse it, so `from_stored` keeps it."""
    row = AuthSession.from_stored({**data(), "login_via": "ftp"})
    assert row.login_via == "ftp"
    assert AuthSession.from_stored(data()).login_via is None


def test_t69_every_call_site_of_the_session_constructor_passes_login_via() -> None:
    sites = 0
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AuthSession"
            ):
                sites += 1
                names = {k.arg for k in node.keywords}
                assert "login_via" in names, f"{path}:{node.lineno} builds a session without it"
    assert sites >= 1  # the server's constructor (the stores read rows with model_validate)
