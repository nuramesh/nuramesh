"""The configuration document: one pass, every problem, and the generated JSON Schema (R1.13)."""

import json
from pathlib import Path
from typing import Any

import pytest

from nuramesh.core.config import (
    ConfigValidationError,
    NuraMeshConfig,
    config_json_schema,
    parse_config,
)

SCHEMA_FILE = (
    Path(__file__).resolve().parents[2] / "spec" / "schema" / "nuramesh-config.schema.json"
)

ORG: dict[str, Any] = {
    "id": "acme",
    "name": "Acme",
    "roles": [
        {"id": "cos", "name": "Chief of Staff", "kind": "chief_of_staff"},
        {"id": "dev", "name": "Developer", "default_model": "anthropic/claude-sonnet"},
        {"id": "vera", "name": "Verifier", "kind": "verifier", "default_model": "openai/gpt-x"},
    ],
    "agents": [
        {"id": "boss", "name": "Boss", "role": "cos"},
        {"id": "ada", "name": "Ada", "role": "dev", "reports_to": "boss"},
        {"id": "vera", "name": "Vera", "role": "vera", "reports_to": "boss"},
    ],
}
FULL: dict[str, Any] = {
    "version": 1,
    "org": ORG,
    "policies": {
        "policies": [
            {
                "role": "dev",
                "rules": [
                    {"action": "call_model", "decision": "allow", "target": "anthropic/*"},
                    {
                        "action": "spend",
                        "decision": "ask",
                        "over": {"amount": "5", "currency": "USD"},
                    },
                ],
                "approvers": ["human:ann"],
            }
        ]
    },
    "budgets": [
        {
            "id": "run-1",
            "scope": "run",
            "scope_id": "r1",
            "limit": {"amount": "10", "currency": "USD"},
        },
        {
            "id": "ada",
            "scope": "agent",
            "scope_id": "ada",
            "limit": {"amount": "2.5", "currency": "USD"},
        },
        {
            "id": "team",
            "scope": "team",
            "scope_id": "boss",
            "limit": {"amount": "20", "currency": "USD"},
        },
    ],
    "prices": {
        "prices": [
            {
                "provider": "anthropic",
                "model": "claude-sonnet",
                "input_per_million": "3",
                "output_per_million": "15",
                "effective_from": "2026-01-01T00:00:00Z",
                "source": "provider price list",
            }
        ]
    },
}


def problems(data: Any) -> list[tuple[str, str]]:
    with pytest.raises(ConfigValidationError) as info:
        parse_config(data)
    return [(i.path, i.message) for i in info.value.issues]


def test_a_full_configuration_is_parsed() -> None:
    config = parse_config(FULL)
    assert isinstance(config, NuraMeshConfig)
    assert [a.id for a in config.org.agents] == ["boss", "ada", "vera"]
    assert config.policies.policy_for("dev") is not None and len(config.budgets) == 3
    assert config.prices.prices[0].model == "claude-sonnet"
    assert config.version == 1


def test_only_the_org_is_required() -> None:
    config = parse_config({"org": ORG})
    assert config.budgets == () and config.policies.policies == () and config.prices.prices == ()


def test_the_configuration_must_be_a_mapping_with_an_org_and_known_keys() -> None:
    with pytest.raises(ConfigValidationError, match="must be a mapping"):
        parse_config([])  # type: ignore[arg-type]
    found = problems({})
    assert ("$.org", "the configuration needs an org") in found
    unknown = problems({"org": ORG, "extras": 1, "budget": []})
    assert {p for p, _ in unknown} == {"$.extras", "$.budget"}
    assert all("allowed" in m for _, m in unknown)


def test_org_graph_rules_are_reported_with_the_subject() -> None:
    org = json.loads(json.dumps(ORG))
    org["agents"].append({"id": "cos-2", "name": "Two", "role": "cos", "reports_to": "boss"})
    found = problems({"org": org})
    assert any("chief_of_staff_not_root" in m and p == "$.org.agents[3]" for p, m in found)


def test_cross_references_are_checked() -> None:
    data = json.loads(json.dumps(FULL))
    data["policies"]["policies"].append({"role": "ghost"})
    data["budgets"].append(
        {
            "id": "x",
            "scope": "agent",
            "scope_id": "nobody",
            "limit": {"amount": "1", "currency": "USD"},
        }
    )
    data["budgets"].append(
        {
            "id": "x",
            "scope": "team",
            "scope_id": "nobody-lead",
            "limit": {"amount": "1", "currency": "USD"},
        }
    )
    found = dict(problems(data))
    assert "unknown role 'ghost'" in found["$.policies.policies[1].role"]
    assert "agent 'nobody' isn't in the org" in found["$.budgets[3].scope_id"]
    assert "duplicate budget id 'x'" in found["$.budgets[4].id"]
    assert "team lead 'nobody-lead' isn't in the org" in found["$.budgets[4].scope_id"]


def test_two_overrides_for_one_model_are_refused() -> None:
    data = json.loads(json.dumps(FULL))
    override = {
        "provider": "openai",
        "model": "mystery",
        "input_per_million": "1",
        "output_per_million": "2",
        "set_by": "human:ann",
        "set_at": "2026-10-01T00:00:00Z",
        "reason": "provisional until listed",
    }
    data["price_overrides"] = [override, override]
    assert any("two overrides for openai/mystery" in m for _, m in problems(data))


def test_every_problem_is_reported_in_one_pass_across_parts() -> None:
    data = json.loads(json.dumps(FULL))
    data["org"]["agents"][1]["role"] = "no-such-role"
    data["budgets"][0]["limit"] = {"amount": "-1", "currency": "USD"}
    data["policies"]["policies"][0]["rules"][0]["decision"] = "maybe"
    data["extra"] = 1
    found = problems(data)
    paths = " ".join(p for p, _ in found)
    assert (
        "$.org" in paths and "$.budgets" in paths and "$.policies" in paths and "$.extra" in paths
    )


def test_a_wrong_version_and_float_money_are_refused() -> None:
    assert any(p == "$.version" for p, _ in problems({"org": ORG, "version": 2}))
    bad = json.loads(json.dumps(FULL))
    bad["budgets"][0]["limit"] = {"amount": 1.5, "currency": "USD"}
    assert problems(bad)


def test_the_committed_schema_is_the_generated_one() -> None:
    generated = (
        json.dumps(config_json_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert SCHEMA_FILE.read_text("utf-8") == generated, (
        "The JSON Schema drifted from the models. Regenerate it with:\n"
        'uv run python -c "import json; from nuramesh.core.config import config_json_schema as s; '
        "open('spec/schema/nuramesh-config.schema.json','w').write(json.dumps(s(), indent=2, "
        "sort_keys=True, ensure_ascii=False)+'\\n')\""
    )


def test_the_canon_key_describes_a_pinned_commit() -> None:
    commit = config_json_schema()["$defs"]["CanonSource"]["properties"]["commit"]
    assert commit["pattern"] == "^[0-9a-f]{40}$"


def test_the_schema_describes_the_shape() -> None:
    schema = config_json_schema()
    assert schema["$schema"].endswith("2020-12/schema") and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "version",
        "org",
        "policies",
        "budgets",
        "breakers",
        "prices",
        "price_overrides",
        "content",
        "canon",
        "calibration",
        "evals",
        "models",
        "presets",
    }
    assert schema["required"] == ["org"]
    defs = schema["$defs"]
    assert set(defs["BudgetScope"]["enum"]) == {"run", "task", "agent", "team", "org"}
    assert set(defs["Decision"]["enum"]) == {"allow", "ask", "deny"}
    assert "chief_of_staff" in defs["RoleKind"]["enum"]
    assert json.loads(json.dumps(schema)) == schema  # plain JSON


def budget(id_: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id_,
        "scope": "run",
        "scope_id": "r1",
        "limit": {"amount": "1", "currency": "USD"},
    }
    base.update(kw)
    return base


def test_an_unknown_key_never_hides_other_problems() -> None:
    data = {
        "org": ORG,
        "extra": 1,
        "policies": {"policies": [{"role": "ghost"}]},
        "budgets": [budget("x"), budget("x"), budget("z", scope="agent", scope_id="nobody")],
    }
    found = dict(problems(data))
    assert "$.extra" in found
    assert "unknown role 'ghost'" in found["$.policies.policies[0].role"]
    assert "duplicate budget id 'x'" in found["$.budgets[1].id"]
    assert "agent 'nobody' isn't in the org" in found["$.budgets[2].scope_id"]


def test_malformed_items_are_all_reported_with_their_list_positions() -> None:
    data = {"org": ORG, "extra": 1, "budgets": [budget("ok"), budget("BAD"), budget("y", zz=1)]}
    found = dict(problems(data))
    assert "$.extra" in found and "$.budgets[1].id" in found and "$.budgets[2].zz" in found


def test_problems_in_every_part_keep_their_positions_and_messages() -> None:
    data = {
        "org": ORG,
        "version": 2,
        "budgets": [budget("ok"), budget("bad", scope="agnet")],
        "price_overrides": [{"provider": "x"}],
    }
    found = dict(problems(data))
    assert "$.version" in found
    assert "$.budgets[1].scope" in found
    assert any(p.startswith("$.price_overrides[0]") for p in found)


def test_org_problems_point_at_the_agent_or_role() -> None:
    org = json.loads(json.dumps(ORG))
    org["agents"].append({"id": "cos-2", "name": "Two", "role": "cos", "reports_to": "boss"})
    found = dict(problems({"org": org}))
    assert "$.org.agents[3]" in found and "chief_of_staff_not_root" in found["$.org.agents[3]"]


def test_references_into_an_invalid_org_are_skipped_but_the_rest_is_still_checked() -> None:
    org = json.loads(json.dumps(ORG))
    org["agents"][1]["role"] = "nope"
    data = {
        "org": org,
        "budgets": [budget("x"), budget("x"), budget("a", scope="agent", scope_id="ada")],
    }
    found = problems(data)
    paths = [p for p, _ in found]
    assert any(p.startswith("$.org") for p in paths)  # the org's own problem
    assert "$.budgets[1].id" in paths  # duplicates need no org
    assert "$.budgets[2].scope_id" not in paths  # can't be checked against a broken org


def test_hostile_shapes_give_clean_issues() -> None:
    for data in (
        {"org": None},
        {"org": "text"},
        {"org": ORG, "budgets": "text"},
        {"org": ORG, "budgets": None},
        {"org": ORG, "policies": []},
        {"org": ORG, "prices": 5},
        {"org": ORG, 5: 1, (1, 2): 2},
        {"org": ORG, "budgets": [budget("x")] * 10_001},
    ):
        assert problems(data), data
    long_key = {"org": ORG, "k" * 5000: 1}
    assert all(len(p) < 200 for p, _ in problems(long_key))


# -- time-window budgets and the org scope (T56) ---------------------------------------------------


def test_budgets_accept_a_window_and_the_org_scope() -> None:
    data = {
        "org": ORG,
        "budgets": [
            budget("org-daily", scope="org", scope_id="acme", window="day"),
            budget("org-monthly", scope="org", scope_id="acme", window="month"),
            budget("run-life", window="none"),
            budget("run-plain"),
            budget("ada-daily", scope="agent", scope_id="ada", window="day"),
        ],
    }
    config = parse_config(data)
    assert [b.window.value for b in config.budgets] == ["day", "month", "none", "none", "day"]
    assert config.budgets[0].scope.value == "org"
    # serialised, an unwindowed budget says `none`; one without the key still loads as before
    assert config.budgets[3].model_dump(mode="json")["window"] == "none"
    assert parse_config(config.model_dump(mode="json")) == config


def test_a_bad_window_is_reported_with_its_path() -> None:
    data = {
        "org": ORG,
        "budgets": [
            budget("a", scope="org", scope_id="acme", window="week"),
            budget("c", scope="org", scope_id="acme", window=1),
            budget("d", scope="org", scope_id="acme", window="day", limit={"amount": 30.0}),
        ],
    }
    found = dict(problems(data))
    assert "$.budgets[0].window" in found and "$.budgets[1].window" in found
    assert any(path.startswith("$.budgets[2].limit") for path in found)  # never a float


def test_an_org_budget_must_name_the_configured_org() -> None:
    data = {
        "org": ORG,
        "budgets": [budget("b", scope="org", scope_id="someone-else", window="day")],
    }
    found = dict(problems(data))
    assert "an org budget must name this org ('acme')" in found["$.budgets[0].scope_id"]


def test_the_schema_lists_the_window_values_and_defaults_to_none() -> None:
    defs = config_json_schema()["$defs"]
    assert set(defs["BudgetWindow"]["enum"]) == {"none", "day", "month"}
    assert defs["Budget"]["properties"]["window"]["default"] == "none"
    assert "window" not in defs["Budget"].get("required", [])


def test_a_yaml_file_with_an_org_daily_budget_loads(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    from nuramesh.config import load_config_file

    path = tmp_path / "c.yaml"
    path.write_text(
        """
version: 1
org:
  id: acme
  name: Acme
  roles:
    - {id: cos, name: C, kind: chief_of_staff}
  agents:
    - {id: boss, name: B, role: cos}
budgets:
  - {id: org-daily, scope: org, scope_id: acme, window: day,
     limit: {amount: "30.00", currency: USD}}
""",
        "utf-8",
    )
    [b] = parse_config(load_config_file(path)).budgets
    assert (b.scope.value, b.window.value, str(b.limit.amount)) == ("org", "day", "30.00")


def test_on_breach_is_accepted_with_a_window_and_refused_as_reset_without_one() -> None:
    config = parse_config(
        {
            "org": ORG,
            "budgets": [
                budget("a", scope="org", scope_id="acme", window="day", on_breach="hold"),
                budget("b", scope="org", scope_id="acme", window="month", on_breach="reset"),
                budget("c", scope="org", scope_id="acme", window="day"),
                budget("d", on_breach="hold"),  # a lifetime budget always holds: allowed
                budget("e"),
            ],
        }
    )
    assert [b.breach_mode.value for b in config.budgets] == [
        "hold",
        "reset",
        "reset",
        "hold",
        "hold",
    ]
    found = dict(problems({"org": ORG, "budgets": [budget("x", on_breach="reset")]}))
    assert "on_breach: reset needs a window" in found["$.budgets[0]"]
    found = dict(problems({"org": ORG, "budgets": [budget("x", window="day", on_breach="stop")]}))
    assert "$.budgets[0].on_breach" in found


def test_the_schema_lists_on_breach() -> None:
    defs = config_json_schema()["$defs"]
    assert set(defs["BreachMode"]["enum"]) == {"reset", "hold"}
    assert "on_breach" not in defs["Budget"].get("required", [])


def test_the_content_setting_defaults_to_thirty_days_and_is_validated() -> None:
    """`content:` (ND52): a durable run keeps task outputs this long after the task is final."""
    assert parse_config({"org": ORG}).content.retention_days == 30
    assert parse_config({"org": ORG, "content": {"retention_days": 0}}).content.retention_days == 0
    assert (
        parse_config({"org": ORG, "content": {"retention_days": 365}}).content.retention_days == 365
    )  # self-hosted sets its own value; a hosted engine refuses more than 90 (ContentSettings)
    for bad in ({"retention_days": -1}, {"retention_days": 1.5}, {"days": 7}):
        with pytest.raises(ConfigValidationError) as caught:
            parse_config({"org": ORG, "content": bad})
        assert any(issue.path.startswith("$.content") for issue in caught.value.issues)


def test_the_hosted_content_maximum_is_reported_by_the_setting() -> None:
    from nuramesh.core.content import ContentSettings

    assert ContentSettings(retention_days=90).problems(hosted=True) == []
    assert "at most 90" in ContentSettings(retention_days=91).problems(hosted=True)[0]
    assert ContentSettings(retention_days=3650).problems(hosted=False) == []


def test_a_malformed_org_is_reported_not_raised() -> None:
    from nuramesh.core.config import parse_config

    with pytest.raises(ConfigValidationError) as excinfo:
        parse_config({"org": 5})
    assert any(i.path == "$.org" for i in excinfo.value.issues)
