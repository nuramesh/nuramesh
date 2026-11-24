"""Mutation pins for core.gate_config (#1309): limits, boundaries, defaults and error text."""

from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.canonical import canonical_json
from nuramesh.core.gate_config import (
    MAX_SCHEMA_BYTES,
    HumanGateConfig,
    SchemaGateConfig,
    VerifierGateConfig,
    gate_config_problems,
)
from nuramesh.core.gates import GateKind, GateSpec
from nuramesh.core.schema import MAX_DEPTH, MAX_NODES, SchemaNode


def _gate(kind: GateKind, config: dict[str, Any]) -> GateSpec:
    return GateSpec(id="g", kind=kind, checks=("a",), config=config)


def _schema(config: dict[str, Any]) -> list[str]:
    return gate_config_problems(_gate(GateKind.SCHEMA, config))


def _chain(levels: int) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(levels - 1):
        node = {"items": node}
    return node


def test_limits_are_the_documented_numbers() -> None:
    assert MAX_SCHEMA_BYTES == 200_000
    assert (MAX_DEPTH, MAX_NODES) == (32, 1_000)


def test_empty_schema_message() -> None:
    assert _schema({"schema": {}}) == [
        "schema: Value error, a schema that says nothing checks nothing"
    ]


def test_depth_limit_boundary_and_message() -> None:
    assert _schema({"schema": _chain(MAX_DEPTH)}) == []
    assert _schema({"schema": _chain(MAX_DEPTH + 1)}) == [
        "schema: Value error, schema nests deeper than 32 levels"
    ]


def test_node_limit_boundary_and_message() -> None:
    def many(total: int) -> dict[str, Any]:
        return {"allOf": [{"type": "string"} for _ in range(total - 1)]}

    # a schema of exactly 1000 nodes is already over the byte limit, so the node check passes
    # it (the message is the byte one); one more node trips the node limit first
    assert _schema({"schema": many(MAX_NODES)}) == [
        "schema: Value error, schema is over 200000 bytes"
    ]
    assert _schema({"schema": many(MAX_NODES + 1)}) == [
        "schema: Value error, schema has more than 1000 nodes"
    ]


def test_byte_limit_boundary_and_message() -> None:
    def size(filler: int) -> int:
        node = SchemaNode.model_validate({"const": "x" * filler})
        return len(canonical_json(node.model_dump(mode="json", by_alias=True)))

    exact = MAX_SCHEMA_BYTES - size(0)
    assert size(exact) == MAX_SCHEMA_BYTES
    assert _schema({"schema": {"const": "x" * exact}}) == []
    assert _schema({"schema": {"const": "x" * (exact + 1)}}) == [
        "schema: Value error, schema is over 200000 bytes"
    ]


def test_schema_config_path_defaults_to_none() -> None:
    assert SchemaGateConfig.model_validate({"schema": {"type": "string"}}).path is None


def test_too_nested_config_gets_its_own_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RecursionError

    monkeypatch.setattr(SchemaGateConfig, "model_validate", boom)
    assert _schema({"schema": {"type": "string"}}) == ["(config): nested too deeply"]


def test_human_instructions_bounds_and_approvers_default() -> None:
    def problems(text: str) -> list[str]:
        return gate_config_problems(_gate(GateKind.HUMAN, {"instructions": text}))

    assert problems("abc") == []
    assert problems("ab") != []
    assert problems("x" * 5_000) == []
    assert problems("x" * 5_001) != []
    assert problems("  ab  ") != []  # stripped before the length check
    assert HumanGateConfig.model_validate({"instructions": "abc"}).approvers == ()


def test_verifier_defaults() -> None:
    cfg = VerifierGateConfig.model_validate({"verifier": "vera"})
    assert cfg.notes == ""
    assert cfg.max_output_tokens == 1_500
    assert cfg.require_different_model is False
    assert cfg.calibration_suites == ()


def test_verifier_numeric_and_notes_bounds() -> None:
    def problems(**extra: Any) -> list[str]:
        return gate_config_problems(_gate(GateKind.VERIFIER, {"verifier": "vera", **extra}))

    assert problems(max_output_tokens=100) == []
    assert problems(max_output_tokens=99) != []
    assert problems(max_output_tokens=100_000) == []
    assert problems(max_output_tokens=100_001) != []
    assert problems(notes="x" * 5_000) == []
    assert problems(notes="x" * 5_001) != []


def test_max_output_tokens_is_strict_not_coerced() -> None:
    for value in ("800", "1e3"):
        problems = gate_config_problems(
            _gate(GateKind.VERIFIER, {"verifier": "vera", "max_output_tokens": value})
        )
        assert problems and problems[0].startswith("max_output_tokens:"), value


def test_gate_configs_are_frozen() -> None:
    for model in (
        HumanGateConfig.model_validate({"instructions": "abc"}),
        VerifierGateConfig.model_validate({"verifier": "vera"}),
        SchemaGateConfig.model_validate({"schema": {"type": "string"}}),
    ):
        with pytest.raises(ValidationError):
            setattr(model, "notes" if hasattr(model, "notes") else "path", "x")  # noqa: B010
