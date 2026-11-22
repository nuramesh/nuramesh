"""Runners for the deterministic and human gates, and the recording of gate results (PLAN D.2).

`run_rule_gate`, `run_schema_gate` and `decide_human_gate` build a `GateResult` and never raise
for a bad *output*: output that can't be checked (too big, not canonical JSON, a pattern that
fails) gives an `error` result, which never counts as passed. A bad *config* is refused when the
task is created (`gate_config`), so seeing one here is also an `error`.

`record_gate_result` writes `gate.completed` to the run's ledger stream in one transaction. It
refuses a result that doesn't belong to the task's current round (right task, attempt and
submission, task `verifying`, gate in the definition of done). The ledger gets verdicts and a hash
of the full result, **not** the evidence text (S2: a human's written reason is content).
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from nuramesh.core.canonical import UnsafeNumberError, canonical_json_jcs, read_jcs, thaw
from nuramesh.core.gate_config import HumanGateConfig, SchemaGateConfig
from nuramesh.core.gates import (
    Evidence,
    GateKind,
    GateOutcome,
    GateResult,
    GateSpec,
    Verdict,
    evidence_problems,
    result_hash,
)
from nuramesh.core.ledger import EventType
from nuramesh.core.people import HUMAN_ID_PATTERN
from nuramesh.core.rules import MAX_TEXT, MISSING, RuleGateConfig, evaluate, resolve_path
from nuramesh.core.schema import validate
from nuramesh.core.tasks import Task, TaskStatus
from nuramesh.engine.content import LedgerHashing
from nuramesh.engine.result_match import recorded_hash_of
from nuramesh.stores.base import (
    EventDraft,
    NotFoundError,
    Store,
    StoreError,
    UnitOfWork,
    run_stream,
)

MAX_DETAIL = 20_000
_HUMAN = re.compile(HUMAN_ID_PATTERN)


class GateError(ValueError):
    """A gate result or human decision that can't be accepted."""


@dataclass(frozen=True)
class _Round:
    """Which verification round a result belongs to."""

    task_id: str
    attempt: int
    submission: int


def _canonical_output(output: Any) -> Any:
    """The output as a task output is stored and read back (RFC 8785 numbers, ND78; `read_jcs`
    keeps a whole float from 2**53 up a float, so every check sees the same value); refuses NaN,
    infinity, huge integers (`UnsafeNumberError`), lone surrogates and anything over `MAX_TEXT`
    bytes (`TypeError`/`ValueError`)."""
    raw = canonical_json_jcs(output)
    if len(raw) > MAX_TEXT:
        raise ValueError(f"output is over {MAX_TEXT} bytes")
    return read_jcs(raw)


def _not_checkable(exc: BaseException) -> str:
    """The error text for an output no check can read: a number problem names its JSON path,
    with the data's keys hidden (ND87); anything else only the kind of problem."""
    if isinstance(exc, UnsafeNumberError):
        return f"the output could not be checked: {exc.public_message()}"
    return f"the output could not be checked ({type(exc).__name__})"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


def _error(spec: GateSpec, ids: _Round, checked_by: str, message: str) -> GateResult:
    return GateResult(
        task_id=ids.task_id,
        attempt=ids.attempt,
        submission=ids.submission,
        gate_id=spec.id,
        outcome=GateOutcome.ERROR,
        checked_by=checked_by,
        error=message,
    )


def _judged(
    spec: GateSpec, ids: _Round, checked_by: str, findings: Mapping[str, tuple[bool, str]]
) -> GateResult:
    try:
        evidence = tuple(
            Evidence(
                criterion=c,
                verdict=Verdict.MET if findings[c][0] else Verdict.NOT_MET,
                detail=_clip(findings[c][1]),
            )
            for c in spec.checks
        )
    except ValidationError:
        return _error(spec, ids, checked_by, "the evidence could not be recorded")
    ok = all(findings[c][0] for c in spec.checks)
    return GateResult(
        task_id=ids.task_id,
        attempt=ids.attempt,
        submission=ids.submission,
        gate_id=spec.id,
        outcome=GateOutcome.PASSED if ok else GateOutcome.FAILED,
        evidence=evidence,
        checked_by=checked_by,
    )


def judged_result(
    spec: GateSpec,
    *,
    task_id: str,
    attempt: int,
    submission: int,
    checked_by: str,
    findings: Mapping[str, tuple[bool, str]],
) -> GateResult:
    """A result from a verdict and a reason per criterion (every criterion the gate checks)."""
    return _judged(spec, _Round(task_id, attempt, submission), checked_by, findings)


def error_result(
    spec: GateSpec, *, task_id: str, attempt: int, submission: int, checked_by: str, message: str
) -> GateResult:
    """An `error` result: the gate couldn't judge (never counts as passed)."""
    return _error(spec, _Round(task_id, attempt, submission), checked_by, message)


def run_rule_gate(
    spec: GateSpec,
    output: Any,
    *,
    task_id: str,
    attempt: int,
    submission: int,
    checked_by: str = "gate:rule",
) -> GateResult:
    """Check `output` against the gate's rules. A criterion is met only if all its rules are."""
    ids = _Round(task_id, attempt, submission)
    if spec.kind is not GateKind.RULE:
        raise GateError(f"gate '{spec.id}' is a {spec.kind} gate, not a rule gate")
    try:
        config = RuleGateConfig.model_validate(thaw(spec.config))
    except ValidationError:
        return _error(spec, ids, checked_by, "the gate's configuration is invalid")
    try:
        output = _canonical_output(output)  # a float or huge integer anywhere is an error
        findings: dict[str, tuple[bool, str]] = {}
        for criterion in spec.checks:
            rules = [r for r in config.rules if r.criterion == criterion]
            if not rules:
                return _error(spec, ids, checked_by, f"no rule for criterion '{criterion}'")
            results = [evaluate(r.check, output) for r in rules]
            findings[criterion] = (all(m for m, _ in results), "; ".join(d for _, d in results))
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        return _error(spec, ids, checked_by, _not_checkable(exc))
    return _judged(spec, ids, checked_by, findings)


def run_schema_gate(
    spec: GateSpec,
    output: Any,
    *,
    task_id: str,
    attempt: int,
    submission: int,
    checked_by: str = "gate:schema",
) -> GateResult:
    """Check `output` (or the value at the gate's `path`) against the gate's schema.

    One verdict covers every criterion the gate checks. Problems name paths and keywords, never
    the checked values.
    """
    ids = _Round(task_id, attempt, submission)
    if spec.kind is not GateKind.SCHEMA:
        raise GateError(f"gate '{spec.id}' is a {spec.kind} gate, not a schema gate")
    try:
        config = SchemaGateConfig.model_validate(thaw(spec.config))
    except ValidationError:
        return _error(spec, ids, checked_by, "the gate's configuration is invalid")
    try:
        value = _canonical_output(output)
        if config.path is not None:
            value = resolve_path(value, config.path)
            if value is MISSING:
                verdict = (False, f"nothing at {config.path}")
                return _judged(spec, ids, checked_by, {c: verdict for c in spec.checks})
        problems = validate(config.schema_, value)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        return _error(spec, ids, checked_by, _not_checkable(exc))
    verdict = (True, "valid against the schema") if not problems else (False, "; ".join(problems))
    return _judged(spec, ids, checked_by, {c: verdict for c in spec.checks})


class DeciderNotAllowed(GateError):
    """The person is not allowed to decide this human gate (default deny)."""


def _human_config(spec: GateSpec) -> HumanGateConfig:
    try:
        return HumanGateConfig.model_validate(thaw(spec.config))
    except ValidationError:
        raise GateError(f"gate '{spec.id}' has an invalid configuration") from None


def _check_decider(spec: GateSpec, config: HumanGateConfig, decided_by: str) -> None:
    """Default deny: only a human, and only one the gate allows, may decide a human gate."""
    if not _HUMAN.fullmatch(decided_by):
        raise DeciderNotAllowed("a human gate can only be decided by a human (actor 'human:<id>')")
    if config.approvers and decided_by not in config.approvers:
        raise DeciderNotAllowed(f"{decided_by} may not decide gate '{spec.id}'")


def decide_human_gate(
    spec: GateSpec,
    *,
    task_id: str,
    attempt: int,
    submission: int,
    decided_by: str,
    findings: Mapping[str, tuple[bool, str]],
) -> GateResult:
    """A human's decision for a human gate: a verdict and a written reason per criterion.

    Refused (`GateError`) unless `decided_by` is a human who may decide (the gate's `approvers`,
    or any human if it lists none), and `findings` covers exactly the criteria the gate checks,
    each with a non-blank reason. Approving every criterion passes the gate; any rejection fails it.
    """
    if spec.kind is not GateKind.HUMAN:
        raise GateError(f"gate '{spec.id}' is a {spec.kind} gate, not a human gate")
    config = _human_config(spec)
    _check_decider(spec, config, decided_by)
    if set(findings) != set(spec.checks):
        raise GateError(f"a decision must cover exactly the criteria {list(spec.checks)}")
    for finding in findings.values():
        if (
            not isinstance(finding, tuple)
            or len(finding) != 2
            or not isinstance(finding[0], bool)
            or not isinstance(finding[1], str)
        ):
            raise GateError("each finding must be (True or False, a written reason)")
        try:
            finding[1].encode("utf-8")
        except UnicodeEncodeError:
            raise GateError("a reason must be valid text") from None
    blank = [c for c, (_, why) in findings.items() if not why.strip()]
    if blank:
        raise GateError(f"a reason is required for: {sorted(blank)}")
    return _judged(
        spec,
        _Round(task_id, attempt, submission),
        decided_by,
        {c: (ok, why.strip()) for c, (ok, why) in findings.items()},
    )


def record_gate_result(
    store: Store,
    result: GateResult,
    *,
    actor: str,
    allow_recorded: bool = False,
    role: str | None = None,
    org_id: str,
    hashing: LedgerHashing,
    in_transaction: Callable[[UnitOfWork, GateResult], Mapping[str, Any]] | None = None,
) -> str:
    """Record `gate.completed` for a result; returns the result's hash.

    Refused (`GateError`, nothing written) unless the result belongs to the task's current round
    (task `verifying`, same attempt and submission, gate in the definition of done), fits its
    gate's evidence rules, and is not already recorded (an `error` may be recorded again: that
    logs a retry). A human gate's result must also come from an allowed human who is the
    recording actor, so an agent can't pass a human gate. With `allow_recorded` an identical
    result that is already recorded is accepted and not written twice (a step that began again
    after a crash, R1.20 PR 2: a deterministic gate gives the same result again).

    `role` (E19) is the role of the agent whose output was gated; it is written into the entry so
    a role-scoped breaker can count the gate.

    `hashing` (required) is bound to `org_id` INSIDE this unit of work (PH-KEY R5): the recorded
    rows are compared as legacy (plain) or keyed (R10), a match returns the recorded string and
    writes nothing, and a new row holds the keyed `gate_result` digest with the marker.

    `in_transaction(uow, result)` (R1.32 G1, the pattern of `move_task`) is called inside this
    transaction, after the duplicate check and as the last write before the ledger append; the
    fields it returns (`evidence_hash`) are merged into the `gate.completed` payload. It is not
    called when the result is already recorded.
    """
    with store.transaction() as uow:
        hasher = hashing.bind(uow, org_id)
        stored = uow.get_task(result.task_id)
        if stored is None:
            raise NotFoundError(f"task '{result.task_id}' doesn't exist")
        task = stored.task
        if task.status is not TaskStatus.VERIFYING:
            raise GateError(f"task '{task.id}' is '{task.status}', not verifying")
        if (result.attempt, result.submission) != (task.attempt, task.submission):
            raise GateError(
                f"result is for attempt {result.attempt}, submission {result.submission}; the task "
                f"is at attempt {task.attempt}, submission {task.submission}"
            )
        try:
            spec = task.definition_of_done.gate(result.gate_id)
        except KeyError:
            raise GateError(f"the task has no gate '{result.gate_id}'") from None
        misfit = evidence_problems(spec, result)
        if misfit:
            raise GateError("; ".join(misfit))
        if spec.kind is GateKind.HUMAN:
            _check_decider(spec, _human_config(spec), result.checked_by)
            if actor != result.checked_by:
                raise GateError("a human gate result must be recorded by the human who decided it")
        if result.outcome is not GateOutcome.ERROR:  # re-running after an error is a retry
            found = recorded_hash_of(uow.task_events(task.run_id, task.id), result, hasher, org_id)
            if found is not None:
                if allow_recorded:
                    return found
                raise GateError("this result is already recorded")
        digest = result_hash(result, hasher)
        extra = dict(in_transaction(uow, result)) if in_transaction is not None else {}
        uow.append(
            EventDraft(
                stream=run_stream(task.run_id),
                type=EventType.GATE_COMPLETED,
                actor=actor,
                run_id=task.run_id,
                task_id=task.id,
                payload={
                    "gate": result.gate_id,
                    "attempt": result.attempt,
                    "submission": result.submission,
                    "outcome": result.outcome.value,
                    "checked_by": result.checked_by,
                    "verdicts": [
                        {"criterion": e.criterion, "verdict": e.verdict.value}
                        for e in result.evidence
                    ],
                    "result_hash": digest,
                    **hasher.marker(),
                    **extra,
                    **({"role": role} if role is not None else {}),
                },
            )
        )
    return digest


def _round_key(payload: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return (payload.get("gate"), payload.get("attempt"), payload.get("submission"))


def record_gate_started(
    store: Store, task: Task, spec: GateSpec, *, actor: str, role: str | None = None
) -> int:
    """Record `gate.started` for the round of `task` (E3, ND117, `spec/e3-otel/README.md` 5.5)
    and return the seq of the start in force.

    Refused (`GateError`, nothing written) unless the stored task exists, is `verifying`, is at
    the attempt and submission of `task`, and `spec` is a gate of its definition of done: the
    same first checks as `record_gate_result`. An **open start** (a `gate.started` with this
    key and no `gate.completed` of this key after it) is reused: nothing is written and its seq
    is returned, so a human gate asked again while it waits, and a step restarted after a crash,
    keep one start. After a completion (an `error` that is run again) a new start is written.
    The payload holds ids and counts only, no content."""
    with store.transaction() as uow:
        stored = uow.get_task(task.id)
        if stored is None:
            raise NotFoundError(f"task '{task.id}' doesn't exist")
        current = stored.task
        if current.status is not TaskStatus.VERIFYING:
            raise GateError(f"task '{current.id}' is '{current.status}', not verifying")
        if current.attempt != task.attempt:
            raise GateError(
                f"the start is for attempt {task.attempt}; the task is at attempt {current.attempt}"
            )
        if current.submission != task.submission:
            raise GateError(
                f"the start is for submission {task.submission}; the task is at submission "
                f"{current.submission}"
            )
        try:
            current.definition_of_done.gate(spec.id)
        except KeyError:
            raise GateError(f"the task has no gate '{spec.id}'") from None
        key = (spec.id, current.attempt, current.submission)
        open_seq: int | None = None
        for event in uow.task_events(current.run_id, current.id):
            if event.type == EventType.GATE_STARTED and _round_key(event.payload) == key:
                open_seq = event.seq
            elif event.type == EventType.GATE_COMPLETED and _round_key(event.payload) == key:
                open_seq = None
        if open_seq is not None:
            return open_seq
        appended = uow.append(
            EventDraft(
                stream=run_stream(current.run_id),
                type=EventType.GATE_STARTED,
                actor=actor,
                run_id=current.run_id,
                task_id=current.id,
                payload={
                    "gate": spec.id,
                    "kind": spec.kind.value,
                    "attempt": current.attempt,
                    "submission": current.submission,
                    **({"role": role} if role is not None else {}),
                },
            )
        )
    return int(appended.seq)


__all__ = [
    "GateError",
    "StoreError",
    "decide_human_gate",
    "record_gate_result",
    "record_gate_started",
    "run_rule_gate",
    "run_schema_gate",
]
