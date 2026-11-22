"""Per-kind gate configuration, checked when a task is created (so bad config fails early).

- `rule`: `RuleGateConfig` (core.rules); every rule's criterion must be one the gate checks, and
  every criterion it checks needs at least one rule.
- `schema`: a JSON Schema (`schema`, the subset in core.schema; ND20) the output (or the value at
  `path`) must satisfy; it judges every criterion the gate checks.
- `verifier`: `verifier` (the id of an agent whose role kind is `verifier`), optional `notes` for
  the verifier, `max_output_tokens` and `require_different_model` (the verifier must not use the
  worker's model; off by default, decision A4). `verifier_problems` checks the agent against
  the org.
- `human`: `instructions` for the reviewer and, optionally, which humans may decide.

- `test_command`: `TestCommandGateConfig` (core.sandbox): a digest-pinned image, the command as
  an argument list, an environment and limits.

`custom` gates are checked by their own runners.
"""

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    field_validator,
)

from nuramesh.core.canonical import canonical_json, thaw
from nuramesh.core.gates import DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.models import parse_model_ref
from nuramesh.core.org import OrgGraph, RoleKind, Slug
from nuramesh.core.people import HUMAN_ID_PATTERN
from nuramesh.core.rules import JsonPath, RuleGateConfig
from nuramesh.core.sandbox import TestCommandGateConfig
from nuramesh.core.schema import MAX_DEPTH, MAX_NODES, SchemaNode, count_nodes, depth

MAX_SCHEMA_BYTES = 200_000


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SchemaGateConfig(_Config):
    """The output (or the value at `path`) must satisfy `schema` (JSON Schema, draft 2020-12)."""

    schema_: SchemaNode = Field(alias="schema")
    path: JsonPath | None = None

    @field_validator("schema_")
    @classmethod
    def _bounded(cls, node: SchemaNode) -> SchemaNode:
        if not node.model_fields_set:
            raise ValueError("a schema that says nothing checks nothing")
        if depth(node) > MAX_DEPTH:
            raise ValueError(f"schema nests deeper than {MAX_DEPTH} levels")
        if count_nodes(node) > MAX_NODES:
            raise ValueError(f"schema has more than {MAX_NODES} nodes")
        if len(canonical_json(node.model_dump(mode="json", by_alias=True))) > MAX_SCHEMA_BYTES:
            raise ValueError(f"schema is over {MAX_SCHEMA_BYTES} bytes")
        return node


class HumanGateConfig(_Config):
    """What the reviewer is asked to check, and who may decide (any human if empty)."""

    instructions: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=5_000)
    ]
    approvers: tuple[Annotated[str, StringConstraints(pattern=HUMAN_ID_PATTERN)], ...] = ()


class VerifierGateConfig(_Config):
    """Which agent verifies, what extra guidance it gets, and how much it may write."""

    verifier: Slug
    notes: Annotated[str, StringConstraints(max_length=5_000)] = ""
    max_output_tokens: int = Field(default=1_500, ge=100, le=100_000, strict=True)
    require_different_model: StrictBool = False
    calibration_suites: tuple[Slug, ...] = ()


def gate_config_problems(gate: GateSpec) -> list[str]:
    """Problems with one gate's config (empty if fine or if the kind isn't checked here)."""
    config = thaw(gate.config)
    try:
        if gate.kind is GateKind.RULE:
            parsed = RuleGateConfig.model_validate(config)
            ruled = {r.criterion for r in parsed.rules}
            problems = [
                f"rule for '{c}', which this gate doesn't check"
                for c in sorted(ruled - set(gate.checks))
            ]
            problems += [f"no rule for '{c}'" for c in gate.checks if c not in ruled]
            return problems
        if gate.kind is GateKind.SCHEMA:
            SchemaGateConfig.model_validate(config)
        elif gate.kind is GateKind.HUMAN:
            HumanGateConfig.model_validate(config)
        elif gate.kind is GateKind.VERIFIER:
            VerifierGateConfig.model_validate(config)
        elif gate.kind is GateKind.TEST_COMMAND:
            TestCommandGateConfig.model_validate(config)
    except ValidationError as exc:
        return [f"{'.'.join(map(str, e['loc'])) or '(config)'}: {e['msg']}" for e in exc.errors()]
    except RecursionError:
        return ["(config): nested too deeply"]
    return []


def definition_problems(dod: DefinitionOfDone) -> list[str]:
    """Every gate config problem in a definition of done, prefixed by the gate id."""
    return [f"gate '{g.id}': {p}" for g in dod.gates for p in gate_config_problems(g)]


def verifier_gate_problems(
    gate: GateSpec, org: OrgGraph, *, assignee: str | None = None
) -> list[str]:
    """Problems with one verifier gate against the org (empty if fine, or not a verifier gate).

    The verifier must be an agent in the org whose role kind is `verifier` and who has a model
    written `provider/model`; it can't be the worker (`assignee`) when that is known; with
    `require_different_model` its model must differ from the worker's.
    """
    if gate.kind is not GateKind.VERIFIER:
        return []
    try:
        config = VerifierGateConfig.model_validate(thaw(gate.config))
    except ValidationError:
        return []  # reported by gate_config_problems
    try:
        agent = org.agent(config.verifier)
    except KeyError:
        return [f"verifier '{config.verifier}' isn't an agent in the org"]
    problems: list[str] = []
    if org.role_of(agent.id).kind is not RoleKind.VERIFIER:
        problems.append(f"'{agent.id}' doesn't have a verifier role")
    model = org.model_for(agent.id)
    if model is None:
        problems.append(f"verifier '{agent.id}' has no model")
    else:
        try:
            parse_model_ref(model)
        except ValueError as exc:
            problems.append(f"verifier '{agent.id}': {exc}")
    if assignee is not None:
        if agent.id == assignee:
            problems.append("a verifier can't check its own work")
        elif config.require_different_model and model is not None:
            worker_model = org.model_for(assignee)
            if worker_model is None:
                problems.append(
                    "the worker's model is unknown, so a different model can't be checked"
                )
            elif _same_model(model, worker_model):
                problems.append("the verifier uses the worker's model")
    return problems


def _same_model(a: str, b: str) -> bool:
    """Whether two `provider/model` settings name the same model (case and spacing ignored).

    The same model offered under different provider ids (for example a cloud reseller) is not
    detected: keep model settings consistent across providers if the difference matters.
    """
    try:
        (pa, ea, ma), (pb, eb, mb) = parse_model_ref(a), parse_model_ref(b)
    except ValueError:
        return a.strip().casefold() == b.strip().casefold()
    # a model's identity is (provider, endpoint id, model): two endpoints are never one model
    return (pa, ea, ma.casefold()) == (pb, eb, mb.casefold())


def verifier_problems(
    dod: DefinitionOfDone, org: OrgGraph, *, assignee: str | None = None
) -> list[str]:
    """Every verifier-gate problem in a definition of done, prefixed by the gate id."""
    return [
        f"gate '{g.id}': {p}"
        for g in dod.gates
        for p in verifier_gate_problems(g, org, assignee=assignee)
    ]
