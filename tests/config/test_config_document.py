"""YAML and JSON files become a checked NuraMesh configuration (R1.13)."""

from pathlib import Path

import pytest

pytest.importorskip("yaml")

from nuramesh.config.loader import ConfigError, load_config_file, parse_json, parse_yaml  # noqa: E402
from nuramesh.core.config import ConfigValidationError, parse_config  # noqa: E402

YAML = """
version: 1
org:
  id: acme
  name: Acme
  roles:
    - {id: cos, name: Chief of Staff, kind: chief_of_staff}
    - {id: dev, name: Developer, default_model: anthropic/claude-sonnet}
  agents:
    - {id: boss, name: Boss, role: cos}
    - {id: ada, name: Ada, role: dev, reports_to: boss}
policies:
  policies:
    - role: dev
      rules:
        - {action: use_tool, decision: allow, target: "git*"}
        - {action: spend, decision: ask, over: {amount: "5.00", currency: USD}}
budgets:
  - {id: ada-cap, scope: agent, scope_id: ada, limit: {amount: "2.50", currency: USD}}
"""


def test_a_yaml_file_is_parsed_and_checked(tmp_path: Path) -> None:
    config = parse_config(parse_yaml(YAML))
    assert config.budgets[0].limit.amount.as_tuple().exponent == -2  # decimals stay exact
    path = tmp_path / "org.yaml"
    path.write_text(YAML, "utf-8")
    assert parse_config(load_config_file(path)).org.id == "acme"


def test_the_same_document_as_json_gives_the_same_configuration() -> None:
    as_json = (
        '{"org": {"id": "acme", "name": "Acme", "roles": [{"id": "cos", "name": "C", '
        '"kind": "chief_of_staff"}], "agents": [{"id": "boss", "name": "B", "role": "cos"}]}}'
    )
    assert parse_config(parse_json(as_json)).org.agents[0].id == "boss"


def test_a_yaml_typo_is_reported_with_its_path() -> None:
    bad = YAML.replace("scope: agent", "scope: agnet")
    with pytest.raises(ConfigValidationError) as info:
        parse_config(parse_yaml(bad))
    assert any(i.path.startswith("$.budgets") for i in info.value.issues)


def test_loader_hardening_still_applies_before_the_checks() -> None:
    with pytest.raises(ConfigError):
        parse_yaml("org: &a {id: x}\nagain: *a\n")  # aliases are refused
    with pytest.raises(ConfigError):
        parse_json('{"org": 1, "org": 2}')  # duplicate keys are refused
