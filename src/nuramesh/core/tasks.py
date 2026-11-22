"""Tasks and their status machine. A task becomes `done` only through passed required gates.

Statuses and allowed moves (anything else is refused):

    proposed    → assigned, needs_human, cancelled
    assigned    → in_progress, blocked, needs_human, cancelled
    in_progress → submitted, blocked, cancelled
    submitted   → verifying, cancelled
    verifying   → done, in_progress, needs_human, blocked, cancelled
    needs_human → assigned, cancelled
    blocked     → needs_human, cancelled
    done, cancelled: final

Rules on top of the table (checked in this order; the first failure refuses the whole move and the
task is unchanged):

1. `reason` must not be blank (it goes to the ledger).
2. Nothing leaves `done` or `cancelled`.
3. The move must be in the table.
4. `assignee` only when moving to `assigned`; `extra_attempts` only for a human decision
   (`needs_human → assigned`), never negative, and refused when the task is already at the cap.
   `gate_results` only for moves out of `verifying` to `done`, `in_progress` or `needs_human`, and
   every result must be for this task.
5. Moving to `assigned` needs an assignee (given, or the current one).
6. **Verification** (moves out of `verifying` that take gate results) uses only results for the
   current attempt **and submission** (`gates.verify_submission`); a result for a gate not in the
   definition of done is refused (`unknown_gate`).
   - to `done`: every required gate passed. A failed gate → `verification_failed`; otherwise
     anything missing or errored → `verification_incomplete`.
   - to `in_progress` (retry): refused if all passed (`verification_passed`); needs at least one
     failed required gate (otherwise `verification_incomplete`). Closes the attempt.
   - to `needs_human`: refused if all passed (`verification_passed`); allowed with a failed gate
     (closes the attempt) or an errored one (attempt stays open: infrastructure, not the agent);
     only missing results → `verification_incomplete`.
7. **Attempts** are verification rounds. Entering `in_progress` starts a new attempt
   (attempt + 1) if there is none yet or the current one is closed, and refuses with
   `attempts_exhausted` if that would exceed `max_attempts`; otherwise the same attempt continues
   (resuming after a block).
8. **Submissions:** `in_progress → submitted` adds one to `submission`, so results for earlier
   output never count for new output.

`transition` returns a `TaskMove`: the new task plus what the engine records in the ledger.
Stores must only rebuild tasks from data written through `transition` (constructing a `Task` in
any status is possible, for loading).
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Any, NoReturn, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from nuramesh.core.content import LedgerHasher
from nuramesh.core.gates import (
    DefinitionOfDone,
    GateResult,
    GateVerdict,
    UnknownGateError,
    Verification,
    result_hash,
    verify_submission,
)
from nuramesh.core.org import Name, Slug
from nuramesh.core.prices import MAX_TOKENS


class TaskStatus(StrEnum):
    """Where a task is in its life."""

    PROPOSED = "proposed"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    VERIFYING = "verifying"
    DONE = "done"
    NEEDS_HUMAN = "needs_human"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


S = TaskStatus

ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.PROPOSED: frozenset({S.ASSIGNED, S.NEEDS_HUMAN, S.CANCELLED}),
    S.ASSIGNED: frozenset({S.IN_PROGRESS, S.BLOCKED, S.NEEDS_HUMAN, S.CANCELLED}),
    S.IN_PROGRESS: frozenset({S.SUBMITTED, S.BLOCKED, S.CANCELLED}),
    S.SUBMITTED: frozenset({S.VERIFYING, S.CANCELLED}),
    S.VERIFYING: frozenset({S.DONE, S.IN_PROGRESS, S.NEEDS_HUMAN, S.BLOCKED, S.CANCELLED}),
    S.NEEDS_HUMAN: frozenset({S.ASSIGNED, S.CANCELLED}),
    S.BLOCKED: frozenset({S.NEEDS_HUMAN, S.CANCELLED}),
    S.DONE: frozenset(),
    S.CANCELLED: frozenset(),
}
"""Allowed moves. Guards in `transition` add the rules this table can't express."""

FINAL = frozenset({S.DONE, S.CANCELLED})
NEEDS_ASSIGNEE = frozenset({S.ASSIGNED, S.IN_PROGRESS, S.SUBMITTED, S.VERIFYING, S.DONE})
WORK_STARTED = frozenset({S.IN_PROGRESS, S.SUBMITTED, S.VERIFYING, S.DONE})
HAS_SUBMISSION = frozenset({S.SUBMITTED, S.VERIFYING, S.DONE})
MAY_BE_CLOSED = frozenset({S.NEEDS_HUMAN, S.ASSIGNED, S.BLOCKED, S.CANCELLED})
TAKES_RESULTS = frozenset({S.DONE, S.IN_PROGRESS, S.NEEDS_HUMAN})
MAX_ATTEMPTS_LIMIT = 50


class TransitionCode(StrEnum):
    """Why a move was refused (stable, for the API and the ledger)."""

    NOT_ALLOWED = "not_allowed"
    FINAL = "final"
    MISSING_REASON = "missing_reason"
    MISSING_ASSIGNEE = "missing_assignee"
    UNEXPECTED_ARGUMENT = "unexpected_argument"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    VERIFICATION_INCOMPLETE = "verification_incomplete"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_PASSED = "verification_passed"
    FOREIGN_RESULT = "foreign_result"
    UNKNOWN_GATE = "unknown_gate"


class TaskTransitionError(ValueError):
    """A refused move. `code` says why; `problems` lists gates that didn't pass, if relevant."""

    def __init__(
        self, code: TransitionCode, message: str, problems: tuple[GateVerdict, ...] = ()
    ) -> None:
        self.code = code
        self.problems = problems
        super().__init__(f"[{code}] {message}")


class TextRef(BaseModel):
    """Where the free text of an API-started task lives (R1.20 PR B, ND347 4.3): content ids of
    the content store, never the text. Either a `template` id, or the ids of the title and of the
    definition of done. The task then holds a fixed placeholder for the text and the engine reads
    the real text through these ids; if it is gone the task goes to a human (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title_id: str | None = Field(default=None, max_length=128)
    dod_id: str | None = Field(default=None, max_length=128)
    template_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _one_mode(self) -> Self:
        if self.template_id is not None:
            if self.title_id is not None or self.dod_id is not None:
                raise ValueError("a text reference names a template or its own text, not both")
        elif self.title_id is None or self.dod_id is None:
            raise ValueError("a text reference needs a template or both a title and a definition")
        return self

    @property
    def ids(self) -> tuple[str, ...]:
        """Every content id this reference names."""
        found = (self.template_id, self.title_id, self.dod_id)
        return tuple(i for i in found if i is not None)


PLACEHOLDER_TITLE = "(text in the content store)"
"""What the tasks table holds in place of a title that lives in the content store (display only)."""


class Task(BaseModel):
    """A unit of work with a definition of done. Immutable: moves return a new task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug
    run_id: Slug
    parent_id: Slug | None = None
    title: Name
    instructions: str = Field(default="", max_length=100_000)
    definition_of_done: DefinitionOfDone
    assignee: Slug | None = None
    status: TaskStatus = TaskStatus.PROPOSED
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=MAX_ATTEMPTS_LIMIT)
    attempt_closed: bool = False
    submission: int = Field(default=0, ge=0)
    text_ref: TextRef | None = None
    """Set only for a task made from an API plan (the free text is in the content store)."""
    max_tokens: int | None = Field(default=None, strict=True, ge=1, le=MAX_TOKENS)
    """R1.42: an output cap for this task's worker calls (lowers the role cap, never raises it)."""

    # No return annotation on purpose: one would replace the serialisation JSON schema (OpenAPI).
    @model_serializer(mode="wrap")
    def _omit_unset_optionals(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        data: dict[str, object] = handler(self)
        if data.get("max_tokens") is None:
            data.pop("max_tokens", None)  # existing tasks serialise byte-identically
        if data.get("text_ref") is None:
            data.pop("text_ref", None)  # same for tasks not made from an API plan
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status in NEEDS_ASSIGNEE and self.assignee is None:
            raise ValueError(f"a task in '{self.status}' needs an assignee")
        if self.attempt > self.max_attempts:
            raise ValueError("attempt can't exceed max_attempts")
        if self.attempt == 0 and self.status in WORK_STARTED:
            raise ValueError(f"a task in '{self.status}' must have started an attempt")
        if self.attempt_closed and (self.attempt == 0 or self.status not in MAY_BE_CLOSED):
            raise ValueError(f"a task in '{self.status}' can't have a closed attempt")
        if self.submission == 0 and self.status in HAS_SUBMISSION:
            raise ValueError(f"a task in '{self.status}' must have a submission")
        if self.parent_id == self.id:
            raise ValueError("a task can't be its own parent")
        return self


class TaskMove(BaseModel):
    """A completed move: the new task and what happened, for the ledger."""

    model_config = ConfigDict(frozen=True)

    task: Task
    from_status: TaskStatus
    to_status: TaskStatus
    reason: str
    attempt_before: int
    attempt_after: int
    closed_attempt: bool
    submission: int
    verification: Verification | None = None

    def ledger_payload(self, hasher: LedgerHasher) -> dict[str, Any]:
        """The `task.transitioned` event payload (canonical JSON). `hasher` is called only when
        the move has a `verification` (PH-KEY R5)."""
        payload: dict[str, Any] = {
            "from": self.from_status.value,
            "to": self.to_status.value,
            "reason": self.reason,
            "attempt_before": self.attempt_before,
            "attempt_after": self.attempt_after,
            "closed_attempt": self.closed_attempt,
            "submission": self.submission,
            "assignee": self.task.assignee,
            "max_attempts": self.task.max_attempts,
        }
        if self.verification is not None:
            payload["verification"] = [
                {
                    "gate": g.gate_id,
                    "required": g.required,
                    "state": g.state.value,
                    "deciding_results": [result_hash(r, hasher) for r in g.deciding],
                }
                for g in self.verification.gates
            ]
        return payload


def _refuse(code: TransitionCode, message: str, problems: tuple[GateVerdict, ...] = ()) -> NoReturn:
    raise TaskTransitionError(code, message, problems)


def _describe(verdicts: tuple[GateVerdict, ...]) -> str:
    return "; ".join(f"{v.gate_id}: {v.state}" for v in verdicts)


def transition(
    task: Task,
    to: TaskStatus,
    *,
    reason: str,
    assignee: str | None = None,
    extra_attempts: int = 0,
    gate_results: Iterable[GateResult] = (),
) -> TaskMove:
    """Move `task` to `to`, or raise `TaskTransitionError`. Pure: the input task is unchanged."""
    results = tuple(gate_results)
    if not reason.strip():
        _refuse(TransitionCode.MISSING_REASON, "every move needs a reason")
    if task.status in FINAL:
        _refuse(TransitionCode.FINAL, f"task is already '{task.status}'")
    if to not in ALLOWED[task.status]:
        _refuse(TransitionCode.NOT_ALLOWED, f"can't move from '{task.status}' to '{to}'")
    if assignee is not None and to is not S.ASSIGNED:
        _refuse(TransitionCode.UNEXPECTED_ARGUMENT, "an assignee can only be given when assigning")
    if extra_attempts:
        if not (task.status is S.NEEDS_HUMAN and to is S.ASSIGNED):
            _refuse(
                TransitionCode.UNEXPECTED_ARGUMENT,
                "extra attempts can only be granted by a human (needs_human → assigned)",
            )
        if extra_attempts < 0:
            _refuse(TransitionCode.UNEXPECTED_ARGUMENT, "extra_attempts is negative")
        if task.max_attempts >= MAX_ATTEMPTS_LIMIT:
            _refuse(
                TransitionCode.UNEXPECTED_ARGUMENT,
                f"the task is already at the {MAX_ATTEMPTS_LIMIT}-attempt limit",
            )
    takes_results = task.status is S.VERIFYING and to in TAKES_RESULTS
    if results and not takes_results:
        _refuse(
            TransitionCode.UNEXPECTED_ARGUMENT,
            "gate results are only used when leaving 'verifying' for done, in_progress or "
            "needs_human",
        )
    for result in results:
        if not isinstance(result, GateResult):
            _refuse(TransitionCode.UNEXPECTED_ARGUMENT, "gate_results must be GateResult objects")
        if result.task_id != task.id:
            _refuse(
                TransitionCode.FOREIGN_RESULT,
                f"gate result for task '{result.task_id}' given to task '{task.id}'",
            )

    changes: dict[str, Any] = {"status": to}
    closed_now = False

    if to is S.ASSIGNED:
        new_assignee = assignee if assignee is not None else task.assignee
        if new_assignee is None:
            _refuse(TransitionCode.MISSING_ASSIGNEE, "assigning needs an assignee")
        changes["assignee"] = new_assignee
        if extra_attempts:
            changes["max_attempts"] = min(task.max_attempts + extra_attempts, MAX_ATTEMPTS_LIMIT)

    verification: Verification | None = None
    if takes_results:
        try:
            verification = verify_submission(
                task.definition_of_done,
                results,
                task_id=task.id,
                attempt=task.attempt,
                submission=task.submission,
            )
        except UnknownGateError as exc:
            raise TaskTransitionError(TransitionCode.UNKNOWN_GATE, str(exc)) from exc
        problems = verification.problems()
        if to is S.DONE:
            if verification.failed:
                _refuse(
                    TransitionCode.VERIFICATION_FAILED, _describe(verification.failed), problems
                )
            if problems:
                _refuse(TransitionCode.VERIFICATION_INCOMPLETE, _describe(problems), problems)
        elif not problems:
            _refuse(
                TransitionCode.VERIFICATION_PASSED,
                "every required gate passed; the task should be done",
            )
        elif to is S.IN_PROGRESS:
            if not verification.failed:
                _refuse(
                    TransitionCode.VERIFICATION_INCOMPLETE,
                    "a retry needs a failed gate: " + _describe(problems),
                    problems,
                )
            closed_now = True
        elif verification.failed:  # needs_human after a failure
            closed_now = True
        elif not verification.errored:  # needs_human with only missing results
            _refuse(
                TransitionCode.VERIFICATION_INCOMPLETE,
                "nothing to escalate yet: " + _describe(problems),
                problems,
            )
        if closed_now:
            changes["attempt_closed"] = True

    if to is S.IN_PROGRESS:
        closed = closed_now or task.attempt_closed
        if task.attempt == 0 or closed:
            if task.attempt >= task.max_attempts:
                _refuse(
                    TransitionCode.ATTEMPTS_EXHAUSTED, f"all {task.max_attempts} attempts are used"
                )
            changes["attempt"] = task.attempt + 1
            changes["attempt_closed"] = False

    if to is S.SUBMITTED:
        changes["submission"] = task.submission + 1

    new_task = Task.model_validate({**dict(task), **changes})
    return TaskMove(
        task=new_task,
        from_status=task.status,
        to_status=to,
        reason=reason,
        attempt_before=task.attempt,
        attempt_after=new_task.attempt,
        closed_attempt=closed_now,
        submission=new_task.submission,
        verification=verification,
    )
