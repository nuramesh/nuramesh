"""Definition of done, gate specifications, gate results and verdicts (data; runners are engine).

A task's **definition of done** lists criteria and the gates that check them. Every criterion must
be checked by at least one **required** gate, so nothing can count as done unchecked.

A gate result carries **evidence**: one verdict (`met` / `not_met`) per criterion, with detail.
A passing result needs `met` evidence for every criterion its gate checks and none it doesn't;
"looks good" without evidence is not representable (D.4). A gate that couldn't judge (timeout,
crash, verifier unavailable) reports `error`.

Results belong to one **submission** of one attempt. A gate's verdict for a submission ignores the
order of its results:

1. any `failed` result, or any `passed` result that doesn't fit the gate's evidence rules → failed
   (**a failure is final for that submission**: re-running a gate can't overturn it);
2. otherwise any fitting `passed` result → passed;
3. otherwise any `error` result → error (re-running after an error is how infrastructure trouble
   is retried);
4. otherwise → missing.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from nuramesh.core.canonical import canonical_json, canonical_value, content_hash, thaw
from nuramesh.core.content import LedgerHasher, LedgerHashKind
from nuramesh.core.org import Slug


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]


class GateKind(StrEnum):
    """How a gate checks work. Deterministic kinds run before verifier agents (D.5)."""

    TEST_COMMAND = "test_command"
    RULE = "rule"
    SCHEMA = "schema"
    VERIFIER = "verifier"
    HUMAN = "human"
    CUSTOM = "custom"


DETERMINISTIC_KINDS = frozenset({GateKind.TEST_COMMAND, GateKind.RULE, GateKind.SCHEMA})


class Criterion(_Frozen):
    """One item of a definition of done, written so a checker can say met or not met."""

    id: Slug
    text: Text


class GateSpec(_Frozen):
    """A check to run on a task's output. `checks` lists the criterion ids it judges.

    `config` is kind-specific, must be canonical JSON (it is hashed into the ledger) and is
    read-only after validation.
    """

    id: Slug
    kind: GateKind
    checks: tuple[Slug, ...] = Field(min_length=1)
    required: bool = True
    config: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("checks")
    @classmethod
    def _unique_checks(cls, checks: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(checks)) != len(checks):
            raise ValueError("a gate lists the same criterion twice")
        return checks

    @field_validator("config")
    @classmethod
    def _canonical_config(cls, config: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            canonical_json(config)  # no floats, string keys: it must hash the same everywhere
        except TypeError as exc:  # pydantic only turns ValueError into a validation error
            raise ValueError(str(exc)) from exc
        stored: Mapping[str, Any] = canonical_value(config)  # read-only, storage form
        return stored

    @field_serializer("config")
    def _plain_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        plain: dict[str, Any] = thaw(config)
        return plain

    def __hash__(self) -> int:
        return hash(content_hash(self.model_dump(mode="json")))


class DefinitionOfDone(_Frozen):
    """Criteria plus the gates that check them."""

    criteria: tuple[Criterion, ...] = Field(min_length=1)
    gates: tuple[GateSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        criterion_ids = [c.id for c in self.criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("criterion ids must be unique")
        gate_ids = [g.id for g in self.gates]
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError("gate ids must be unique")
        known = set(criterion_ids)
        for gate in self.gates:
            unknown = [c for c in gate.checks if c not in known]
            if unknown:
                raise ValueError(f"gate '{gate.id}' checks unknown criteria: {unknown}")
        covered = {c for g in self.gates if g.required for c in g.checks}
        uncovered = [c for c in criterion_ids if c not in covered]
        if uncovered:
            raise ValueError(f"criteria not checked by any required gate: {uncovered}")
        return self

    @property
    def required_gates(self) -> tuple[GateSpec, ...]:
        """The gates that must all pass for the task to be done."""
        return tuple(g for g in self.gates if g.required)

    def gate(self, gate_id: str) -> GateSpec:
        """The gate with this id; `KeyError` if there is none."""
        for gate in self.gates:
            if gate.id == gate_id:
                return gate
        raise KeyError(gate_id)

    def run_order(self) -> tuple[GateSpec, ...]:
        """Deterministic gates first, then the rest, keeping their listed order (D.5)."""
        return tuple(sorted(self.gates, key=lambda g: 0 if g.kind in DETERMINISTIC_KINDS else 1))


class Verdict(StrEnum):
    """A checker's finding for one criterion."""

    MET = "met"
    NOT_MET = "not_met"


class Evidence(_Frozen):
    """Why a criterion is met or not: what was checked and what was found."""

    criterion: Slug
    verdict: Verdict
    detail: Text


class GateOutcome(StrEnum):
    """`error` means the gate couldn't judge (timeout, crash); it never counts as passed."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class GateResult(_Frozen):
    """The outcome of one gate run on one submission of one attempt of one task."""

    task_id: Slug
    attempt: int = Field(ge=1)
    submission: int = Field(ge=1)
    gate_id: Slug
    outcome: GateOutcome
    evidence: tuple[Evidence, ...] = ()
    checked_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    error: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        criteria = [e.criterion for e in self.evidence]
        if len(set(criteria)) != len(criteria):
            raise ValueError("evidence lists the same criterion twice")
        verdicts = {e.verdict for e in self.evidence}
        if self.outcome is GateOutcome.PASSED:
            if not self.evidence or verdicts != {Verdict.MET} or self.error is not None:
                raise ValueError("a passed result needs evidence, all of it 'met', and no error")
        elif self.outcome is GateOutcome.FAILED:
            if Verdict.NOT_MET not in verdicts or self.error is not None:
                raise ValueError("a failed result needs at least one 'not_met' and no error")
        elif not self.error:
            raise ValueError("an error result needs an error message")
        return self

    @property
    def passed(self) -> bool:
        """True only for `passed` (whether it fits its gate is checked separately)."""
        return self.outcome is GateOutcome.PASSED


def result_bytes(result: GateResult) -> bytes:
    """The canonical bytes of a result: what its keyed hash covers (PH-KEY R3)."""
    return canonical_json(result.model_dump(mode="json"))


def result_hash(result: GateResult, hasher: LedgerHasher) -> str:
    """The keyed hash that ties a result to its `gate.completed` ledger event (kind
    `gate_result`)."""
    return hasher(LedgerHashKind.GATE_RESULT, result_bytes(result))


def evidence_problems(spec: GateSpec, result: GateResult) -> list[str]:
    """Ways a result doesn't fit its gate. Empty means it fits (D.4 evidence rules)."""
    problems: list[str] = []
    if result.gate_id != spec.id:
        problems.append(f"result is for gate '{result.gate_id}', not '{spec.id}'")
    checked = set(spec.checks)
    given = {e.criterion for e in result.evidence}
    extra = sorted(given - checked)
    if extra:
        problems.append(f"evidence for criteria this gate doesn't check: {extra}")
    if result.outcome is GateOutcome.PASSED:
        missing = sorted(checked - given)
        if missing:
            problems.append(f"passed without evidence for: {missing}")
    return problems


class GateState(StrEnum):
    """A gate's verdict for one submission."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    MISSING = "missing"


class GateVerdict(_Frozen):
    """One gate's verdict for a submission and the results that decided it."""

    gate_id: Slug
    required: bool
    state: GateState
    details: tuple[str, ...] = ()
    deciding: tuple[GateResult, ...] = ()


class Verification(_Frozen):
    """Every gate's verdict for one submission. Built by `verify_submission`."""

    task_id: Slug
    attempt: int
    submission: int
    gates: tuple[GateVerdict, ...]

    def _required(self, state: GateState) -> tuple[GateVerdict, ...]:
        return tuple(g for g in self.gates if g.required and g.state is state)

    @property
    def failed(self) -> tuple[GateVerdict, ...]:
        """Required gates that failed."""
        return self._required(GateState.FAILED)

    @property
    def errored(self) -> tuple[GateVerdict, ...]:
        """Required gates that couldn't judge."""
        return self._required(GateState.ERROR)

    @property
    def missing(self) -> tuple[GateVerdict, ...]:
        """Required gates with no result yet."""
        return self._required(GateState.MISSING)

    @property
    def passed(self) -> bool:
        """Every required gate passed."""
        return all(g.state is GateState.PASSED for g in self.gates if g.required)

    def problems(self) -> tuple[GateVerdict, ...]:
        """Required gates that didn't pass."""
        return tuple(g for g in self.gates if g.required and g.state is not GateState.PASSED)


class UnknownGateError(ValueError):
    """A result names a gate the definition of done doesn't have."""


def verify_submission(
    dod: DefinitionOfDone,
    results: Iterable[GateResult],
    *,
    task_id: str,
    attempt: int,
    submission: int,
) -> Verification:
    """Each gate's verdict for one submission (rules in the module docstring).

    Results for other tasks, attempts or submissions are ignored; a result for a gate the
    definition of done doesn't have raises `UnknownGateError`.
    """
    by_gate: dict[str, list[GateResult]] = {g.id: [] for g in dod.gates}
    for result in results:
        if (result.task_id, result.attempt, result.submission) != (task_id, attempt, submission):
            continue
        if result.gate_id not in by_gate:
            raise UnknownGateError(f"result for unknown gate '{result.gate_id}'")
        by_gate[result.gate_id].append(result)

    verdicts: list[GateVerdict] = []
    for gate in dod.gates:
        mine = by_gate[gate.id]
        failures: list[GateResult] = []
        details: list[str] = []
        fitting_passes: list[GateResult] = []
        for result in mine:
            misfit = evidence_problems(gate, result)
            if result.outcome is GateOutcome.FAILED:
                failures.append(result)
                details.extend(
                    f"{e.criterion}: {e.detail}"
                    for e in result.evidence
                    if e.verdict is Verdict.NOT_MET
                )
                details.extend(misfit)
            elif result.outcome is GateOutcome.PASSED and misfit:
                failures.append(result)
                details.extend(misfit)
            elif result.outcome is GateOutcome.PASSED:
                fitting_passes.append(result)
        errors = [r for r in mine if r.outcome is GateOutcome.ERROR]
        if failures:
            state, deciding = GateState.FAILED, failures
        elif fitting_passes:
            state, deciding = GateState.PASSED, fitting_passes
        elif errors:
            state, deciding = GateState.ERROR, errors
            details = [r.error or "error" for r in errors]
        else:
            state, deciding = GateState.MISSING, []
            details = [f"no result for attempt {attempt}, submission {submission}"]
        verdicts.append(
            GateVerdict(
                gate_id=gate.id,
                required=gate.required,
                state=state,
                details=tuple(details),
                deciding=tuple(deciding),
            )
        )
    return Verification(
        task_id=task_id, attempt=attempt, submission=submission, gates=tuple(verdicts)
    )
