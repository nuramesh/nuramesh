"""Behaviour pins for `core/org.py` found by the 2026-11-15 mutation farm (#1202).

Every test goes through a public entry point (`validate_org`, `OrgGraph.build`, the models, the
`nuramesh validate` command) and is written so that the mutant it names changes the result.
"""

import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from nuramesh.cli import main
from nuramesh.core.canonical import content_hash
from nuramesh.core.org import (
    Agent,
    EndpointId,
    IssueCode,
    OrgGraph,
    OrgValidationError,
    Role,
    RoleKind,
    Severity,
    validate_org,
)

_ROLES: list[dict[str, Any]] = [
    {"id": "cos", "name": "CoS", "kind": "chief_of_staff"},
    {"id": "dev", "name": "Dev"},
]
_AGENTS: list[dict[str, Any]] = [
    {"id": "boss", "name": "Boss", "role": "cos"},
    {"id": "ada", "name": "Ada", "role": "dev", "reports_to": "boss"},
]


def _data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "mini",
        "name": "Mini",
        "roles": [dict(r) for r in _ROLES],
        "agents": [dict(a) for a in _AGENTS],
    }
    data.update(overrides)
    return data


def _issues(data: dict[str, Any]) -> list[Any]:
    with pytest.raises(OrgValidationError) as caught:
        OrgGraph.build(data)
    return list(caught.value.issues)


# --- warnings never come before errors (cmp 256: `is not` -> `is`) -----------------------------


def test_errors_are_listed_before_warnings() -> None:
    roles = (
        Role(id="cos", name="CoS", kind=RoleKind.CHIEF_OF_STAFF),
        Role(id="idle", name="Idle"),
    )
    agents = (
        Agent(id="boss", name="Boss", role="cos"),
        Agent(id="ghost", name="Ghost", role="missing", reports_to="boss"),
    )
    got = validate_org(roles, agents)
    severities = [i.severity for i in got]
    assert Severity.ERROR in severities and Severity.WARNING in severities
    assert severities[0] is Severity.ERROR
    assert severities[-1] is Severity.WARNING
    assert severities == sorted(severities, key=lambda s: s is not Severity.ERROR)


# --- the subject of a malformed field (bool 384: `and` -> `or`) --------------------------------


def test_malformed_field_in_a_list_item_names_the_item_id() -> None:
    data = _data()
    data["roles"][1]["name"] = ""  # empty name: an invalid field of role 'dev'
    issues = _issues(data)
    assert [(i.code, i.subject) for i in issues] == [(IssueCode.INVALID_FIELD, "dev")]


def test_malformed_field_in_a_non_list_collection_has_no_subject() -> None:
    # a deque is accepted as a sequence by pydantic, but only list/tuple items are looked up
    data = _data(roles=deque([{"id": "cos", "name": "", "kind": "chief_of_staff"}]))
    issues = _issues(data)
    assert issues
    assert all(i.code is IssueCode.INVALID_FIELD and i.subject is None for i in issues)


# --- strict string types (bool-const 40 and 57: `strict=True` -> False) ------------------------


def test_a_slug_must_be_a_real_string() -> None:
    data = _data()
    data["roles"][1]["id"] = b"dev"
    issues = _issues(data)
    assert any(i.code is IssueCode.INVALID_FIELD for i in issues)
    with pytest.raises(ValidationError):
        Role.model_validate({"id": b"dev", "name": "Dev"})


def test_an_endpoint_id_must_be_a_real_string() -> None:
    adapter = TypeAdapter(EndpointId)
    assert adapter.validate_python("local") == "local"
    with pytest.raises(ValidationError):
        adapter.validate_python(b"local")


# --- the version hash covers exactly the non-default content (bool-const 543) ------------------


def test_version_hash_is_the_hash_of_the_non_default_content() -> None:
    org = OrgGraph.build(_data())
    expected = content_hash(
        {
            "id": "mini",
            "name": "Mini",
            "roles": [
                {"id": "cos", "name": "CoS", "kind": "chief_of_staff"},
                {"id": "dev", "name": "Dev"},
            ],
            "agents": [
                {"id": "ada", "name": "Ada", "role": "dev", "reports_to": "boss"},
                {"id": "boss", "name": "Boss", "role": "cos"},
            ],
        }
    )
    assert org.version_hash() == expected


# --- field limits are exact (int 37, 63, 84, 85, 87, 88, 101, 102) -----------------------------

_MAX_TOKENS = 10**12


@pytest.mark.parametrize(
    ("model", "base", "field", "ok", "bad"),
    [
        (Role, {"id": "r", "name": "R"}, "max_tokens", [1, _MAX_TOKENS], [0, _MAX_TOKENS + 1]),
        (Role, {"id": "r", "name": "R"}, "name", ["x", "x" * 120], ["", "x" * 121]),
        (Role, {"id": "r", "name": "R"}, "description", ["x" * 2_000], ["x" * 2_001]),
        (Role, {"id": "r", "name": "R"}, "instructions", ["x" * 50_000], ["x" * 50_001]),
        (Role, {"id": "r", "name": "R"}, "default_model", ["m", "m" * 200], ["", "m" * 201]),
        (Agent, {"id": "a", "name": "A", "role": "r"}, "model", ["m", "m" * 200], ["", "m" * 201]),
    ],
)
def test_field_limits_are_exact(
    model: Any, base: dict[str, Any], field: str, ok: list[Any], bad: list[Any]
) -> None:
    for value in ok:
        assert getattr(model.model_validate({**base, field: value}), field) == value
    for value in bad:
        with pytest.raises(ValidationError):
            model.model_validate({**base, field: value})


def test_an_agent_may_check_at_most_200_agents() -> None:
    base = {"id": "a", "name": "A", "role": "r"}
    ids = [f"x{n}" for n in range(201)]
    assert len(Agent.model_validate({**base, "checks": ids[:200]}).checks) == 200
    with pytest.raises(ValidationError):
        Agent.model_validate({**base, "checks": ids})


# --- the loop message lists the loop and closes it on its first member (int 341) ---------------


def test_loop_message_closes_the_loop_on_its_first_member() -> None:
    roles = (Role(id="dev", name="Dev"),)
    agents = (
        Agent(id="a", name="A", role="dev", reports_to="b"),
        Agent(id="b", name="B", role="dev", reports_to="c"),
        Agent(id="c", name="C", role="dev", reports_to="a"),
    )
    cycles = [i for i in validate_org(roles, agents) if i.code is IssueCode.CYCLE]
    assert sorted(i.subject or "" for i in cycles) == ["a", "b", "c"]
    assert {i.message for i in cycles} == {"Reporting lines form a loop: a → b → c → a"}


# --- the same rules through `nuramesh validate`: errors exit 2, warnings only exit 0 -----------


def _config(path: Path, org: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"version": 1, "org": org}), "utf-8")
    return path


def test_validate_command_exits_zero_on_warnings_and_two_on_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warning_only = _data(roles=[*_ROLES, {"id": "idle", "name": "Idle"}])  # a role nobody holds
    assert main(["validate", str(_config(tmp_path / "warn.json", warning_only))]) == 0
    assert "valid" in capsys.readouterr().out

    broken = _data()
    broken["agents"][1]["role"] = "missing"
    assert main(["validate", str(_config(tmp_path / "bad.json", broken))]) == 2
    assert "unknown_role" in capsys.readouterr().err


def test_reserved_org_id_is_refused_and_a_normal_id_is_kept() -> None:
    assert OrgGraph.build(_data()).id == "mini"
    issues = _issues(_data(id="store-startup"))
    assert any("reserved" in i.message for i in issues)


def test_an_agent_cannot_delegate_to_itself_and_only_downwards() -> None:
    org = OrgGraph.build(_data())
    assert org.can_delegate("boss", "boss") is False
    assert org.can_delegate("boss", "ada") is True
    assert org.can_delegate("ada", "boss") is False
