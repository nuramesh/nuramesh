"""SA-9: `policies.solo_approvals_per_hour` (the design notes; cases B39 and B60). The
key is absent or an integer from 1 to 30; it can only lower the limit; nothing is clamped or
converted. A configuration without the key keeps the hash it had before the key existed."""

import json
from pathlib import Path
from typing import Any

import pytest

from nuramesh.core.config import ConfigValidationError, parse_config
from nuramesh.core.policy import PolicySet


def raw(**policies: Any) -> dict[str, Any]:
    """A small plain-dict configuration (no YAML file: core tests run without the yaml extra)."""
    return {
        "version": 1,
        "org": {
            "id": "solo-limit-example",
            "name": "Solo limit example",
            "roles": [
                {"id": "cos", "name": "Chief of Staff", "kind": "chief_of_staff"},
                {"id": "developer", "name": "Developer", "default_model": "fake/dev-1"},
            ],
            "agents": [
                {"id": "chief", "name": "Chief of Staff", "role": "cos"},
                {
                    "id": "developer",
                    "name": "Developer",
                    "role": "developer",
                    "reports_to": "chief",
                },
            ],
        },
        "policies": {
            "policies": [
                {
                    "role": "developer",
                    "rules": [{"action": "call_model", "decision": "allow", "target": "fake/*"}],
                }
            ],
            **policies,
        },
    }


@pytest.mark.parametrize("value", [31, 0, -1, 2.5, "ten", 5.0, "10", True, False, None, [5], {}])
def test_b39_every_other_value_is_a_configuration_error(value: object) -> None:
    with pytest.raises(ConfigValidationError) as caught:
        parse_config(raw(solo_approvals_per_hour=value))
    assert "solo_approvals_per_hour" in str(caught.value)


@pytest.mark.parametrize(("value", "limit"), [(1, 1), (30, 30), (5, 5)])
def test_b60_an_integer_from_1_to_30_loads(value: int, limit: int) -> None:
    config = parse_config(raw(solo_approvals_per_hour=value))
    assert config.policies.solo_limit == limit
    assert config.effective_policies.solo_limit == limit  # presets keep the organization's limit


def test_b60_the_key_absent_means_30() -> None:
    config = parse_config(raw())
    assert config.policies.solo_approvals_per_hour is None
    assert config.policies.solo_limit == 30 and config.effective_policies.solo_limit == 30


def test_an_unset_limit_is_not_in_the_dump_so_old_configuration_hashes_stand() -> None:
    plain = parse_config(raw()).policies
    assert "solo_approvals_per_hour" not in plain.model_dump(mode="json")
    assert "solo_approvals_per_hour" not in plain.model_dump()
    lowered = parse_config(raw(solo_approvals_per_hour=5)).policies
    assert lowered.model_dump(mode="json")["solo_approvals_per_hour"] == 5
    assert PolicySet.model_validate(lowered.model_dump(mode="json")) == lowered
    assert PolicySet.model_validate(plain.model_dump(mode="json")) == plain


def test_b39_the_command_line_validate_refuses_a_bad_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from nuramesh.cli import main

    for value in (31, "ten"):
        path = tmp_path / "nuramesh.json"
        path.write_text(json.dumps(raw(solo_approvals_per_hour=value)), encoding="utf-8")
        assert main(["validate", str(path)]) != 0
        assert "solo_approvals_per_hour" in capsys.readouterr().err
    path.write_text(json.dumps(raw(solo_approvals_per_hour=5)), encoding="utf-8")
    assert main(["validate", str(path)]) == 0
