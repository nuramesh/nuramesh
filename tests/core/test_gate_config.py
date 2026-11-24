"""Gate configuration is checked when a task is created, so a bad gate fails early."""

from typing import Any

from nuramesh.core.gate_config import definition_problems, gate_config_problems
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec

RULE = {"rules": [{"criterion": "a", "check": {"type": "contains", "value": "x"}}]}


def gate(kind: GateKind, config: dict[str, Any], checks: tuple[str, ...] = ("a",)) -> GateSpec:
    return GateSpec(id="g", kind=kind, checks=checks, config=config)


def test_good_configs_have_no_problems() -> None:
    assert gate_config_problems(gate(GateKind.RULE, RULE)) == []
    assert gate_config_problems(gate(GateKind.SCHEMA, {"schema": {"type": "object"}})) == []
    assert (
        gate_config_problems(gate(GateKind.SCHEMA, {"schema": {"type": "string"}, "path": "a.b"}))
        == []
    )
    assert gate_config_problems(gate(GateKind.HUMAN, {"instructions": "Check the diff"})) == []
    human = {"instructions": "Check", "approvers": ["human:ann", "human:bo"]}
    assert gate_config_problems(gate(GateKind.HUMAN, human)) == []


def test_custom_gates_are_not_judged_here_but_test_commands_are() -> None:
    assert gate_config_problems(gate(GateKind.CUSTOM, {"anything": 1})) == []
    assert gate_config_problems(gate(GateKind.TEST_COMMAND, {"anything": 1}))
    good = {"image": "busybox@sha256:" + "a" * 64, "command": ["true"]}
    assert gate_config_problems(gate(GateKind.TEST_COMMAND, good)) == []


def test_rule_gate_problems() -> None:
    assert gate_config_problems(gate(GateKind.RULE, {})) == ["rules: Field required"]
    other = {"rules": [{"criterion": "zzz", "check": {"type": "contains", "value": "x"}}]}
    assert gate_config_problems(gate(GateKind.RULE, other)) == [
        "rule for 'zzz', which this gate doesn't check",
        "no rule for 'a'",
    ]
    extra = gate(GateKind.RULE, RULE, checks=("a", "b"))
    assert gate_config_problems(extra) == ["no rule for 'b'"]


def test_schema_gate_problems() -> None:
    assert gate_config_problems(gate(GateKind.SCHEMA, {})) == ["schema: Field required"]
    assert gate_config_problems(gate(GateKind.SCHEMA, {"schema": {}})) != []  # says nothing
    unsupported = gate_config_problems(gate(GateKind.SCHEMA, {"schema": {"$ref": "#/x"}}))
    assert unsupported and "$ref" in unsupported[0]
    assert (
        gate_config_problems(gate(GateKind.SCHEMA, {"schema": {"type": "string"}, "path": "a..b"}))
        != []
    )
    assert gate_config_problems(gate(GateKind.SCHEMA, {"schema": {"type": "string"}, "x": 1})) != []


def test_deep_schema_is_refused() -> None:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(40):
        node = {"items": node}
    problems = gate_config_problems(gate(GateKind.SCHEMA, {"schema": node}))
    assert problems and "deeper" in problems[0]


def test_human_gate_problems() -> None:
    assert gate_config_problems(gate(GateKind.HUMAN, {})) == ["instructions: Field required"]
    assert gate_config_problems(gate(GateKind.HUMAN, {"instructions": "  "})) != []
    bad = {"instructions": "Check", "approvers": ["ann"]}
    assert gate_config_problems(gate(GateKind.HUMAN, bad)) != []  # must be 'human:<id>'


def test_definition_problems_name_the_gate() -> None:
    dod = DefinitionOfDone(
        criteria=(Criterion(id="a", text="A"),),
        gates=(gate(GateKind.RULE, {}),),
    )
    assert definition_problems(dod) == ["gate 'g': rules: Field required"]


def test_verifier_gate_config() -> None:
    assert gate_config_problems(gate(GateKind.VERIFIER, {"verifier": "vera"})) == []
    full = {
        "verifier": "vera",
        "notes": "Focus on security",
        "max_output_tokens": 800,
        "require_different_model": True,
    }
    assert gate_config_problems(gate(GateKind.VERIFIER, full)) == []
    assert gate_config_problems(gate(GateKind.VERIFIER, {})) == ["verifier: Field required"]
    for bad in (
        {"verifier": "vera", "max_output_tokens": 50},
        {"verifier": "vera", "max_output_tokens": True},
        {"verifier": "vera", "require_different_model": 1},
        {"verifier": "Bad Id"},
        {"verifier": "vera", "x": 1},
    ):
        assert gate_config_problems(gate(GateKind.VERIFIER, bad)) != [], bad


def test_verifier_gate_problems_against_the_org() -> None:
    from nuramesh.core.gate_config import verifier_gate_problems
    from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind

    def org(model: str | None) -> OrgGraph:
        return OrgGraph(
            id="acme",
            name="Acme",
            roles=(
                Role(id="cos", name="CoS", kind=RoleKind.CHIEF_OF_STAFF),
                Role(id="w", name="Worker", default_model="fake/Worker-1"),
                Role(id="v", name="Verifier", kind=RoleKind.VERIFIER, default_model=model),
            ),
            agents=(
                Agent(id="boss", name="Boss", role="cos"),
                Agent(id="dev", name="Dev", role="w", reports_to="boss"),
                Agent(id="vera", name="Vera", role="v", reports_to="boss"),
            ),
        )

    spec = gate(GateKind.VERIFIER, {"verifier": "vera", "require_different_model": True})
    # an invalid config is reported by gate_config_problems, not here
    assert verifier_gate_problems(gate(GateKind.VERIFIER, {"x": 1}), org("fake/a")) == []
    # a model setting that is not provider/model
    assert any("verifier 'vera'" in p for p in verifier_gate_problems(spec, org("nonsense")))
    # an unparseable model on both sides falls back to a spacing and case comparison
    org_same = org("Not A Ref")
    org_same = org_same.model_copy(
        update={
            "roles": tuple(
                r.model_copy(update={"default_model": " not a ref "}) if r.id == "w" else r
                for r in org_same.roles
            )
        }
    )
    assert "the verifier uses the worker's model" in verifier_gate_problems(
        spec, org_same, assignee="dev"
    )
