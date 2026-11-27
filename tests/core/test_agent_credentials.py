"""E2 per-agent identity, core side (section 2, 3, 19): the credential
format and record, the fingerprint formula (recomputed here independently), the team key and the
sliding-window cap's arithmetic (cases A2, A3, A8, A9, A21, A29, A30, I5, I36, P1)."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.auth import (
    AGENT_CREDENTIAL_PREFIX,
    AgentCredential,
    CallCap,
    Permission,
    TokenScope,
    agent_actor,
    agent_fingerprint,
    hash_agent_credential,
    issue_agent_credential,
    looks_like_agent_credential,
    team_key,
)
from nuramesh.core.ledger import EventType
from nuramesh.core.org import Agent, Role, RoleKind
from nuramesh.core.people import is_human_id

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
READ = frozenset({TokenScope.READ})


def _issue(scopes: frozenset[TokenScope] = READ, days: int = 30, by: str = "human:u_own1") -> Any:
    return issue_agent_credential(
        "acme",
        "developer",
        team="dev-team",
        fingerprint="a" * 64,
        scopes=scopes,
        created_by=by,
        now=NOW,
        lifetime=timedelta(days=days),
    )


def test_format_is_nma_plus_43_characters_and_stores_only_the_hash() -> None:
    issued = _issue()
    assert issued.credential.startswith(AGENT_CREDENTIAL_PREFIX)
    assert len(issued.credential) == 47
    record = issued.record
    assert record.token_hash == hashlib.sha256(issued.credential.encode()).hexdigest()
    assert record.token_hash == hash_agent_credential(issued.credential)
    assert issued.credential not in repr(issued) and issued.credential not in repr(record)
    assert record.id.startswith("agc_") and len(record.id) == 28
    assert record.actor == "agent:developer"
    assert not is_human_id(record.actor)


def test_lifetime_bounds_and_expiry_boundary() -> None:  # I5, A2, A3
    assert _issue(days=1).record.expires_at == NOW + timedelta(days=1)
    assert _issue(days=90).record.expires_at == NOW + timedelta(days=90)
    for bad in (0, 91):
        with pytest.raises(ValueError):
            _issue(days=bad)
    record = _issue().record
    assert record.active(record.expires_at - timedelta(seconds=1))
    assert not record.active(record.expires_at)


def test_shape_check_bounds() -> None:  # A7, A8, A9
    assert not looks_like_agent_credential("nma_")
    assert looks_like_agent_credential("nma_" + "x" * 252)  # 256 in all
    assert not looks_like_agent_credential("nma_" + "x" * 253)
    assert not looks_like_agent_credential("nmt_" + "x" * 43)


def test_scopes_are_read_and_run_only_and_never_human_only() -> None:
    rec = _issue(scopes=frozenset({TokenScope.READ, TokenScope.RUN})).record
    assert rec.permissions() == {Permission.READ, Permission.RUN}
    with pytest.raises(ValidationError):
        AgentCredential.model_validate({**rec.model_dump(), "scopes": frozenset({"admin"})})
    with pytest.raises(ValidationError):
        AgentCredential.model_validate({**rec.model_dump(), "scopes": frozenset()})


def test_created_by_and_revoked_by_forms() -> None:
    rec = _issue().record
    ok = {**rec.model_dump(), "revoked_at": NOW}
    for by in ("human:u_x1", "service:cli", "erased-user", "system"):
        AgentCredential.model_validate({**ok, "revoked_by": by})
    with pytest.raises(ValidationError):
        AgentCredential.model_validate({**ok, "revoked_by": "agent:developer"})
    with pytest.raises(ValidationError):
        AgentCredential.model_validate({**ok, "revoked_by": None})  # both or neither
    with pytest.raises(ValidationError):
        AgentCredential.model_validate({**rec.model_dump(), "created_by": "service:cli"})
    AgentCredential.model_validate({**rec.model_dump(), "created_by": "erased-user"})


def test_no_free_text_field() -> None:  # P1
    assert set(AgentCredential.model_fields) == {
        "org_id",
        "id",
        "agent_id",
        "team",
        "fingerprint",
        "scopes",
        "token_hash",
        "created_by",
        "created_at",
        "expires_at",
        "revoked_at",
        "revoked_by",
    }


def test_agent_actor_is_a_slug_only() -> None:
    assert agent_actor("developer") == "agent:developer"
    for bad in ("Developer", "", "a b", "x" * 64, "_x"):
        with pytest.raises(ValueError):
            agent_actor(bad)


def test_ledger_types_exist() -> None:
    assert EventType.AGENT_CREDENTIAL_ISSUED.value == "agent.credential_issued"
    assert EventType.AGENT_CREDENTIAL_REVOKED.value == "agent.credential_revoked"
    assert EventType.AGENT_CREDENTIAL_ISSUER_ERASED.value == "agent.credential_issuer_erased"


def _expected(entry: dict[str, Any]) -> str:  # the formula of section 2.6, written independently
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_fingerprint_formula() -> None:  # I36
    role = Role(id="developer", name="Developer", kind=RoleKind.WORKER)
    agent = Agent(
        id="developer",
        name="Developer",
        role="developer",
        reports_to="cos",
        model="m1",
        checks=("c2", "c1"),
    )
    assert agent_fingerprint(agent, role) == _expected(
        {
            "id": "developer",
            "role": "developer",
            "role_kind": "worker",
            "reports_to": "cos",
            "model": "m1",
            "checks": ["c2", "c1"],
        }
    )
    bare = Agent(id="sam", name="Sam", role="developer")
    assert agent_fingerprint(bare, role) == _expected(
        {
            "id": "sam",
            "role": "developer",
            "role_kind": "worker",
            "reports_to": None,
            "model": None,
            "checks": [],
        }
    )


def test_fingerprint_ignores_names_and_role_text_but_not_definition() -> None:  # A25-A27
    role = Role(id="developer", name="Developer")
    base = Agent(id="developer", name="Developer", role="developer", model="m1")
    fp = agent_fingerprint(base, role)
    renamed = base.model_copy(update={"name": "Developer Q"})
    texty = role.model_copy(update={"description": "d", "instructions": "i", "name": "Dev"})
    assert agent_fingerprint(renamed, texty) == fp
    for changed in (
        base.model_copy(update={"model": "m2"}),
        base.model_copy(update={"checks": ("c1",)}),
        base.model_copy(update={"reports_to": "cos"}),
        base.model_copy(update={"role": "reviewer"}),
    ):
        assert agent_fingerprint(changed, role) != fp
    verifier = role.model_copy(update={"kind": RoleKind.VERIFIER})
    assert agent_fingerprint(base, verifier) != fp


def test_team_key() -> None:  # section 2.6
    assert team_key("dev-team") == "dev-team"
    assert team_key("My Team") == "my-team"
    assert team_key("-x") == "t-x"
    assert team_key("") == "t"
    assert team_key("A" * 100) == "a" * 63
    assert team_key("-" + "a" * 100) == "t-" + "a" * 61


def test_call_cap_window_is_half_open() -> None:  # A21, A29, A30
    cap = CallCap(limit=600)
    for _ in range(600):
        assert cap.admit("agc_1", NOW) is None
    assert cap.admit("agc_1", NOW) == 60  # Retry-After, whole seconds rounded up
    assert cap.admit("agc_1", NOW + timedelta(seconds=59)) == 1
    for _ in range(10):  # refused calls are not counted
        assert cap.admit("agc_1", NOW + timedelta(seconds=30)) == 30
    assert cap.admit("agc_1", NOW + timedelta(seconds=60)) is None
    assert cap.admit("agc_2", NOW) is None  # per credential


def test_call_cap_clock_moved_back_does_not_lock_out_longer_than_the_window() -> None:  # spec 3, 12
    cap = CallCap(limit=10)
    assert all(cap.admit("c", NOW) is None for _ in range(10))
    back = NOW - timedelta(hours=1)
    retry = cap.admit("c", back)
    # calls stamped in the future of "now" are outside the window: not locked out for an hour
    assert retry is None or retry <= 60
    assert cap.admit("c", back + timedelta(seconds=61)) is None


def test_call_cap_small_limit() -> None:  # S15
    cap = CallCap(limit=10)
    assert all(cap.admit("c", NOW) is None for _ in range(10))
    assert cap.admit("c", NOW) is not None
