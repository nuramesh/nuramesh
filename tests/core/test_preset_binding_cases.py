"""Spec/app-v1-followup: preset gates and evals bind the run (core side: C17-C27, C36, C45-C48)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from nuramesh.core.config import ConfigValidationError, parse_config
from nuramesh.core.evals import (
    EvalConfig,
    EvalRecord,
    SubjectKind,
    check_eligibility,
)
from nuramesh.core.presets import (
    PresetConfig,
    effective_suites,
    gates_for_role,
    preset_content_hash,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
IMG = "registry.test/py@sha256:" + "a" * 64
RULES2 = {
    "rules": [
        {"criterion": "c1", "check": {"type": "contains", "value": "ok"}},
        {"criterion": "c2", "check": {"type": "contains", "value": "done"}},
    ]
}


def suite(sid: str, role: str) -> dict[str, Any]:
    return {
        "id": sid,
        "role": role,
        "valid_days": 30,
        "cases": [
            {
                "id": "a",
                "prompt": "Say x",
                "rules": [{"criterion": "k", "check": {"type": "contains", "value": "x"}}],
            }
        ],
    }


def presets(std_evals: list[str] | None = None) -> PresetConfig:
    return PresetConfig.model_validate(
        {
            "gate": [
                {
                    "id": "std",
                    "name": "Std",
                    "gates": [
                        {"id": "lint", "kind": "rule", "config": RULES2},
                        {
                            "id": "tests",
                            "kind": "test_command",
                            "config": {"image": IMG, "command": ["pytest"]},
                        },
                        {"id": "review", "kind": "verifier", "config": {"verifier": "qa1"}},
                    ],
                    "evals": ["s-dev"] if std_evals is None else std_evals,
                },
                {
                    "id": "light",
                    "name": "Light",
                    "gates": [
                        {"id": "lint", "kind": "rule", "required": False, "config": RULES2},
                        {
                            "id": "sign",
                            "kind": "human",
                            "config": {
                                "instructions": "check the output",
                                "approvers": ["human:alice"],
                            },
                        },
                    ],
                },
            ],
            "assignments": [
                {"role": "dev", "gate_preset": "std"},
                {"role": "ops", "gate_preset": "light"},
            ],
        }
    )


def evals() -> EvalConfig:
    return EvalConfig.model_validate(
        {
            "suites": [suite("s-dev", "dev"), suite("s-base", "dev"), suite("s-ops", "ops")],
            "requirements": [{"role": "dev", "suites": ["s-base"]}],
        }
    )


def test_c17_gates_for_role_builds_the_preset_gates() -> None:
    gates = gates_for_role(
        presets(), "dev", {"lint": ["c1", "c2"], "tests": ["c1"], "review": ["c2"]}
    )
    assert gates is not None
    assert [g.id for g in gates] == ["lint", "tests", "review"]
    assert [g.checks for g in gates] == [("c1", "c2"), ("c1",), ("c2",)]
    assert all(g.required for g in gates)
    assert dict(gates[2].config) == {"verifier": "qa1"}


def test_c18_c19_gates_for_role_errors() -> None:
    with pytest.raises(ValueError, match=r"say which criteria these gates check: \['review'\]"):
        gates_for_role(presets(), "dev", {"lint": ["c1", "c2"], "tests": ["c1"]})
    with pytest.raises(ValueError, match=r"checks for gates the preset doesn't have: \['typo'\]"):
        gates_for_role(
            presets(),
            "dev",
            {"lint": ["c1"], "tests": ["c1"], "review": ["c1"], "typo": ["c1"]},
        )


def test_c20_c21_no_preset_gives_none() -> None:
    assert gates_for_role(presets(), "boss", {}) is None
    assert gates_for_role(None, "dev", {}) is None


def test_c23_to_c26_effective_suites() -> None:
    assert effective_suites(evals(), presets(), "dev") == ("s-base", "s-dev")
    assert effective_suites(evals(), presets(), "ops") == ()
    assert effective_suites(evals(), presets(), "boss") == ()
    assert effective_suites(evals(), presets(["s-base", "s-dev"]), "dev") == ("s-base", "s-dev")
    assert effective_suites(evals(), None, "dev") == ("s-base",)
    assert effective_suites(None, presets(), "dev") == ("s-dev",)
    assert effective_suites(None, presets(), "ops") == ()


def test_preset_content_hash_ignores_the_name() -> None:
    p = presets().gate[0]
    renamed = p.model_copy(update={"name": "Other"})
    assert preset_content_hash(p) == preset_content_hash(renamed)
    changed = presets(["s-base"]).gate[0]
    assert preset_content_hash(p) != preset_content_hash(changed)


def record(sid: str, config: EvalConfig, **kw: Any) -> EvalRecord:
    s = config.suite(sid)
    assert s is not None
    return EvalRecord(
        subject_kind=SubjectKind.AGENT,
        subject_id="dev1",
        model="fake/m1",
        suite_id=s.id,
        suite_version=s.version,
        suite_hash=s.suite_hash,
        score=Decimal("1"),
        passed=kw.get("passed", True),
        outcomes=(),
        at=kw.get("at", NOW - timedelta(days=1)),
        seq=kw.get("seq", 1),
    )


def elig(
    config: EvalConfig | None, suites: tuple[str, ...], records: list[EvalRecord]
) -> tuple[bool, tuple[str, ...]]:
    e = check_eligibility(
        config,
        role="dev",
        agent="dev1",
        model="fake/m1",
        records=records,
        now=NOW,
        suites=suites,
    )
    return e.eligible, e.reasons


def test_c28_c30_c31_preset_suites_count() -> None:
    cfg = evals()
    suites = effective_suites(cfg, presets(), "dev")
    ok = record("s-base", cfg)
    assert elig(cfg, suites, [ok]) == (
        False,
        ("suite 's-dev': not evaluated for this suite version and model",),
    )
    assert elig(cfg, suites, [ok, record("s-dev", cfg, seq=2)]) == (True, ())
    none = "not evaluated for this suite version and model"
    assert elig(cfg, suites, []) == (
        False,
        (f"suite 's-base': {none}", f"suite 's-dev': {none}"),
    )


def test_c36_c45_undefined_suites_fail_closed() -> None:
    cfg = evals()
    assert elig(cfg, ("s-base", "ghost"), [record("s-base", cfg)]) == (
        False,
        ("suite 'ghost': not defined",),
    )
    assert elig(None, ("s-dev",), []) == (False, ("suite 's-dev': not defined",))


def test_c48_a_suite_for_another_role_is_never_satisfiable() -> None:
    cfg = evals()
    assert elig(cfg, ("s-base", "s-ops"), [record("s-base", cfg)]) == (
        False,
        ("suite 's-ops': is for role 'ops', not 'dev'",),
    )


def test_suites_none_keeps_the_old_behaviour() -> None:
    cfg = evals()
    e = check_eligibility(cfg, role="dev", agent="dev1", model="fake/m1", records=[], now=NOW)
    assert e.reasons == ("suite 's-base': not evaluated for this suite version and model",)


def config_with(std_evals: list[str], assignments: list[dict[str, str]]) -> dict[str, Any]:
    p = presets(std_evals).model_dump(mode="json")
    p["assignments"] = assignments
    return {
        "org": {
            "id": "acme",
            "name": "Acme",
            "roles": [
                {"id": "boss", "name": "Boss", "kind": "chief_of_staff"},
                {"id": "dev", "name": "Dev", "kind": "worker"},
                {"id": "ops", "name": "Ops", "kind": "worker"},
            ],
            "agents": [
                {"id": "ceo", "name": "Ceo", "role": "boss"},
                {"id": "dev1", "name": "Dev1", "role": "dev", "reports_to": "ceo"},
                {"id": "ops1", "name": "Ops1", "role": "ops", "reports_to": "ceo"},
            ],
        },
        "evals": evals().model_dump(mode="json"),
        "presets": p,
    }


def test_c46_c47_a_suite_for_another_role_is_refused_at_load() -> None:
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(config_with(["s-ops"], [{"role": "dev", "gate_preset": "std"}]))
    assert exc.value.issues[0].path == "$.presets.gate[0].evals"
    assert exc.value.issues[0].message == (
        "gate preset 'std' names eval suite 's-ops' for role 'ops', but role 'dev' uses the preset"
    )
    with pytest.raises(ConfigValidationError) as exc2:
        parse_config(
            config_with(
                ["s-dev"],
                [{"role": "dev", "gate_preset": "std"}, {"role": "ops", "gate_preset": "std"}],
            )
        )
    assert len(exc2.value.issues) == 1
    assert exc2.value.issues[0].message == (
        "gate preset 'std' names eval suite 's-dev' for role 'dev', but role 'ops' uses the preset"
    )
