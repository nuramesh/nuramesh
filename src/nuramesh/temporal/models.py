"""What crosses Temporal's boundary: ids, statuses, counts and timestamps. Nothing else.

Temporal records every input, result and failure in a workflow history that its cluster keeps
(R1.20 PR 3, T72). Task titles, outputs, gate feedback, reasons and exception messages may hold
personal data, so none of them is ever a field here. A task is its id; a status is a word from a
fixed list; a failure is a code from `errors`.
"""

from dataclasses import dataclass, field


@dataclass
class RunRef:
    """Which run: the workflow input and the first argument of every activity."""

    run_id: str
    org_id: str


@dataclass
class TaskRef:
    run_id: str
    org_id: str
    task_id: str


@dataclass
class PlanView:
    """The plan as the database holds it: ids and dependency edges only."""

    tasks: list[str]
    depends_on: dict[str, list[str]]
    ended: bool
    max_parallel: int
    warn_days: list[int]
    """Lead times (days before expiry) at which the approver of a waiting task is warned."""
    working_days: int
    """How long a task's input lives while the task is alive (the hosted maximum, ND55)."""


@dataclass
class StepView:
    """The outcome of one task step."""

    task_id: str
    status: str
    waiting: bool
    stopped: bool
    waited_seconds: float | None = None
    """For a task waiting for a person: how long it has waited, by the worker's clock and the
    ledger's. The workflow counts the rest on Temporal's clock, so the two clocks never meet."""
    round: str = ""
    """`<attempt>.<submission>`: a new round starts a new wait (a rejection, a new attempt)."""
    not_due: bool = False
    """The expiry was refused: the stored deadline has not come under the store clock (spec 8)."""
    due_in_seconds: float | None = None
    """With `not_due`: seconds from the store's now to the stored deadline."""


@dataclass
class DependencyRef:
    run_id: str
    org_id: str
    task_id: str
    dependency: str
    dependency_status: str
    dependency_waiting: bool


@dataclass
class WarnRef:
    run_id: str
    org_id: str
    task_id: str
    days_left: int


@dataclass
class WarnView:
    """The outcome of a warning step. `opens_in_seconds` is set when it was refused only because
    the store clock is before the warning window: seconds from the store's now to the window."""

    written: bool
    opens_in_seconds: float | None = None


@dataclass
class FinishRef:
    run_id: str
    org_id: str
    stop_acted: bool
    aborted: str = ""
    """The type name of what ended the workflow early (a cancel, an activity that failed for
    good); empty for a run that went to its end."""


@dataclass
class RunResult:
    """The workflow result: how many tasks ended in each status, and whether a Stop applied."""

    counts: dict[str, int] = field(default_factory=dict)
    stopped: bool = False


@dataclass
class StartView:
    """What the first activity did with the run's start request: a word from a fixed list
    (`started`, `refused`, `held`, `none`) and how long to wait before looking again when held."""

    outcome: str
    recheck_seconds: float = 30.0
