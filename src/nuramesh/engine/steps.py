"""The steps of a task, read back from the store (R1.20 PR 2, T63).

A task moves through steps, and the **store already records where it is**: its status, its attempt
and submission, and the ledger events written in the same transaction as each move. So a step needs
no table of its own. This module reads that state and says which step comes next, which is what a
resumed run (and, in R1.20 PR 3, a Temporal activity) needs.

- `start`: from `proposed`; acknowledged by the `assigned` move.
- `work`: from `assigned` or `in_progress`; acknowledged by the `submitted` move, which also stores
  the output (durable runs), in the same transaction.
- `gates`: from `submitted` or `verifying`; acknowledged by the move out of `verifying` (`done`,
  another attempt, or `needs_human`).

**The guarantee:** after a step is acknowledged, repeating it never calls the worker, a model, a
tool or an action again. **What is not promised (ND55):** a crash in the middle of a step runs that
step again from its start, in a new **epoch** (recorded in the ledger), so calls the first try
already made may be made again. The epoch goes into the reservation ids, because the in-memory
call counter restarts and `reserve` refuses an id that exists.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NamedTuple

from nuramesh.core.ledger import EventType
from nuramesh.core.tasks import TaskStatus
from nuramesh.stores import Store


class Step(StrEnum):
    START = "start"
    WORK = "work"
    GATES = "gates"


RESTART_KIND = "step_restarted"
"""`decision.recorded` payload `kind` for a step that began again after a crash."""

HUMAN_DECISION = "human_decision"
"""`decision.recorded` payload `kind` for a person's decision on a human gate of a durable run (the
text is kept in the content store; the entry carries `decision_hash`, never the text)."""

HUMAN_DECISION_REFUSED = "human_decision_refused"
"""`decision.recorded` payload `kind` for a late decision on an expired input (T78). Payload:
`task_id`, `attempt`, `why`. No free text."""

HUMAN_WAIT_WARNING = "human_wait_warning"
"""`decision.recorded` payload `kind` for the warning that a task waiting for a human is about to
lose its input (ND55). Payload: `task_id`, `attempt`, `days_left`. No free text."""

HUMAN_WAIT_EXPIRED = "human_wait_expired"
"""`decision.recorded` payload `kind` for the stop of a task whose human wait reached the 90-day
limit (ND55). Payload: `task_id`, `attempt`, `days_left` (0). No free text."""

WAITING_NOTE = "waiting for a human"
"""The words a task's note carries while a person is what it waits for (also in the reason of a
dependent cancelled or held because of it)."""

_NEXT: dict[TaskStatus, Step] = {
    TaskStatus.PROPOSED: Step.START,
    TaskStatus.ASSIGNED: Step.WORK,
    TaskStatus.IN_PROGRESS: Step.WORK,
    TaskStatus.SUBMITTED: Step.GATES,
    TaskStatus.VERIFYING: Step.GATES,
}


def step_key(run_id: str, task_id: str, attempt: int, step: Step, epoch: int = 0) -> str:
    """The idempotency key of one step try: the same try always has the same key."""
    return f"{run_id}/{task_id}/a{attempt}/{step.value}/e{epoch}"


@dataclass(frozen=True)
class TaskProgress:
    """Where a task is, from the store."""

    status: TaskStatus
    attempt: int
    submission: int
    next_step: Step | None
    """None when the task is waiting for a human or final (nothing for the engine to run)."""
    epochs: dict[Step, int]
    """For this attempt: how many times each step was restarted after a crash."""
    output_hash: str | None
    """The keyed hash of this attempt's latest stored output (durable runs), if it was submitted."""
    feedback_hash: str | None
    """The keyed hash of the gate feedback this attempt started with (durable runs, ND57)."""


def read_progress(store: Store, task_id: str) -> TaskProgress:
    """The task's progress. Raises `KeyError` for an unknown task."""
    stored = store.get_task(task_id)
    if stored is None:
        raise KeyError(task_id)
    task = stored.task
    epochs: dict[Step, int] = {}
    output_hash: str | None = None
    feedback_hash: str | None = None
    for event in store.task_events(task.run_id, task.id):
        payload = event.payload
        if event.type == EventType.DECISION_RECORDED and payload.get("kind") == RESTART_KIND:
            if payload.get("attempt") == task.attempt:
                step = Step(str(payload["step"]))
                epochs[step] = epochs.get(step, 0) + 1
        elif event.type == EventType.TASK_TRANSITIONED and payload.get("attempt_after") == (
            task.attempt
        ):
            if "output_hash" in payload:
                output_hash = str(payload["output_hash"])
            if "feedback_hash" in payload:
                feedback_hash = str(payload["feedback_hash"])
    return TaskProgress(
        status=task.status,
        attempt=task.attempt,
        submission=task.submission,
        next_step=_NEXT.get(task.status),
        epochs=epochs,
        output_hash=output_hash,
        feedback_hash=feedback_hash,
    )


class Settled(NamedTuple):
    """How a task ended for the tasks that depend on it: its status, and whether a person is what
    it (or something it depends on) waits for."""

    status: TaskStatus
    waiting: bool


_WAITING_STATUSES = (TaskStatus.NEEDS_HUMAN, TaskStatus.BLOCKED, TaskStatus.VERIFYING)


def blocking_dependency(dependencies: Mapping[str, Settled]) -> str | None:
    """Of the dependencies that ended (`dependencies`: the ones that did), the one that keeps a
    task from running, if any: those waiting for a human first, then by id (stable reasons)."""
    bad = sorted(
        (d for d, s in dependencies.items() if s.status is not TaskStatus.DONE),
        key=lambda d: (
            not dependencies[d].waiting and dependencies[d].status not in _WAITING_STATUSES,
            d,
        ),
    )
    return bad[0] if bad else None


def dependency_cancel_reason(dependency: str, settled: Settled) -> str:
    """Why a dependent task is cancelled, in words a person can act on (ledger text).

    The reason follows the dependency's **own final status**, never whether something above it
    once waited. A dependency that stopped at `needs_human`, `blocked` or `verifying` is waiting
    for a person who cannot approve it inside the run that ended with it: in-process runs end
    and cannot resume, and a durable run holds dependents only during the `verifying` human
    wait (R1.20a, `dependency_wait_reason`); it says to plan the task again. Any other
    dependency (cancelled, or not finished) is named with the status it ended in, without `done`."""
    if settled.status in _WAITING_STATUSES:
        return (
            f"cancelled: dependency {dependency} stopped at {settled.status.value} and nothing in "
            "this run can approve it (R1.20a holds dependents only during a durable run's human "
            "wait); this run can't wait for that task, so plan this task again, as a new task"
        )
    return f"cancelled: dependency {dependency} ended {settled.status.value} without `done`"


DEPENDENCY_WAITING = "dependency_waiting"
"""`decision.recorded` payload `kind` for a task held `proposed` while a task it depends on
(directly or through others) is in a durable run's human wait (R1.20a). Payload, exactly: `kind`,
`task_id` (the held task), `waiting_on` (the task in the wait), `attempt` (of that task). No text.
One per (held task, waiting task), written when the wait begins; none for a task cancelled
instead."""


def dependency_wait_reason(waiting_on: str) -> str:
    """The fixed words for a held task (report and outcome), built from the note's fields; they
    are not stored in the ledger. A held task is `proposed`, shown as held: it is not itself
    waiting for a human, so the words do not start with `WAITING_NOTE`."""
    return (
        f"held: task {waiting_on} is waiting for a person's decision and this task depends on "
        "it (directly or through other tasks); this task stays proposed and can run if that "
        "task ends done, and is cancelled if it does not, the run is stopped, or the wait "
        "expires"
    )


def submitted_at(store: Store, run_id: str, task_id: str, attempt: int) -> datetime | None:
    """The ledger time of the latest submit move of this attempt: the clock of a durable run's
    human wait starts there (the output was kept in the same transaction, ND55)."""
    when: datetime | None = None
    for event in store.task_events(run_id, task_id):
        if (
            event.type == EventType.TASK_TRANSITIONED
            and event.payload.get("to") == TaskStatus.SUBMITTED.value
            and event.payload.get("attempt_after") == attempt
        ):
            when = event.at
    return when


__all__ = [
    "DEPENDENCY_WAITING",
    "HUMAN_DECISION",
    "HUMAN_DECISION_REFUSED",
    "HUMAN_WAIT_EXPIRED",
    "HUMAN_WAIT_WARNING",
    "RESTART_KIND",
    "WAITING_NOTE",
    "Settled",
    "Step",
    "TaskProgress",
    "blocking_dependency",
    "dependency_cancel_reason",
    "dependency_wait_reason",
    "read_progress",
    "step_key",
    "submitted_at",
]
