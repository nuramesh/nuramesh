"""In-process run engine: delegation, parallel tasks, gates, budgets, policies, report (R1.15; T29).

A **run** executes a plan of tasks for one organization. The plan comes from the Chief of Staff
(the org's root agent): either a fixed list of `PlannedTask`s or a `Planner` callable. Agents do
their work through a `Worker` callable per agent: NuraMesh is a trust layer, not an agent
framework, so the worker is where an adapter (LangGraph, R1.16) or plain code plugs in. Everything
else is the engine's job, and every step goes through the services that write the ledger:

1. **Start:** `run.started`; the canon (if any) is loaded once at its pinned commit and recorded
   (`canon.loaded`); every task is created (`task.created`, gate config checked first).
2. **Delegation** (per task, when its dependencies are done): the Chief of Staff may only delegate
   downwards (`org.can_delegate`) and only as its policy allows (`delegate` action, default deny;
   `policy.decided`); the assignee must be **eligible** under its role's evals (T27). Anything
   else sends the task to `needs_human` with the reason.
3. **Work:** `assigned → in_progress`, the worker runs with a `WorkContext` whose `complete` makes
   model calls through `call_model` (policy check, price, budget reservation, settlement, ledger).
   A budget hard stop, a policy refusal, a pending approval or a worker crash moves the task to
   `blocked → needs_human` with the reason: never silently dropped.
4. **Verification:** `submitted → verifying`; gates run cheap first (D.5: test-command, rule and
   schema before verifier and human gates); if a required deterministic gate fails, the costly
   gates are skipped for this round. **Every result is recorded** (`gate.completed`, verifier
   results included) before it is used. Verifier gates get the run's canon. Human gates ask the
   `Approver`; with no decision the task waits in `verifying`. Test-command and custom gates need
   a runner (`gate_runners`, for example the sandbox, R1.08); without one the gate is an `error`,
   never a pass.
5. **Decision:** all required gates passed → `done`; a failed required gate → another attempt
   with the gate feedback (`in_progress`), or `needs_human` when attempts are used up; an errored
   gate → `needs_human`. An output holding NaN, an infinity or an integer beyond +-(2^53 - 1) is an
   error, not a person's job (ND87): the task is `cancelled` with `output error: <JSON path>`
   before any gate runs. Tasks that depend on a task that didn't finish are `cancelled`: a run
   here ends and can't resume, so nothing waits for a person. A durable run
   (`nuramesh.temporal`) holds the dependents of a task in its human wait `proposed`
   instead (R1.20a).
6. **End:** a final reconciler pass, `run.ended` with the counts, and a `RunReport`.

**Stop** (`engine.run_control`): a human appends `run.stop_requested` to the run's stream (API,
`nuramesh stop`, or Ctrl-C in the terminal that runs it). The engine looks for it between steps:
before each task and attempt, before each worker model call, and around each gate (a
verifier's one call is covered by the check right before its gate; a gate that finished is
recorded before the stop applies). When it finds one it
makes no further model call, cancels the tasks that were running or waiting (with the recorded
reason), releases any reservation still open, tells registered hooks (for example a running
sandbox container is killed) and ends with `run.ended` `status: stopped`. A worker is stopped
cooperatively: a step already in flight (one provider call) finishes and is settled; a worker that
never returns can't be stopped from here.

**Reservations a crash left open** are reconciled (T24) by a background thread every
`reconcile_every` while the run is going and once at the end (`max_age` from the engine settings).

Tasks run in parallel up to `max_parallel`, as soon as their dependencies are done. The store is
the only shared state; each service call is one transaction (A1).
"""

import base64
import collections
import dataclasses
import functools
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn, Protocol

from pydantic import ValidationError

from nuramesh.core.breakers import BreakerRule, check_rules
from nuramesh.core.budgets import (
    BudgetExceededError,
    CallContext,
    ReservationStatus,
    applicable_budgets,
    as_of,
)
from nuramesh.core.calibration import CalibrationConfig, calibration_problems
from nuramesh.core.canon import Canon, CanonSource
from nuramesh.core.canonical import (
    UnsafeNumberError,
    canonical_json,
    canonical_json_jcs,
    read_jcs,
    thaw,
)
from nuramesh.core.content import (
    MAX_TEXT_BYTES,
    ContentKind,
    ContentSettings,
    LedgerHashKind,
    key_id,
)
from nuramesh.core.evals import EvalConfig
from nuramesh.core.gate_config import definition_problems, verifier_problems
from nuramesh.core.gates import (
    DETERMINISTIC_KINDS,
    GateKind,
    GateOutcome,
    GateResult,
    GateSpec,
    Verdict,
    result_bytes,
)
from nuramesh.core.ledger import CostBasis, EventType, LedgerEvent, format_time
from nuramesh.core.model_governance import HALTED_TEXT, PlanTask, Report
from nuramesh.core.models import (
    Message,
    ModelProvider,
    ModelRequest,
    parse_model_ref,
    payload_model_ref,
)
from nuramesh.core.org import OrgGraph
from nuramesh.core.policy import (
    POLICY_CONTRACT,
    Action,
    ActionKind,
    Decision,
    PolicySet,
    command_text,
    is_canonical_write_target,
)
from nuramesh.core.presets import (
    GatePreset,
    PresetConfig,
    effective_suites,
    gate_preset_for_role,
    preset_content_hash,
)
from nuramesh.core.price_words import COST_UNKNOWN_SHORT, at_least_words, cost_unknown_call_words
from nuramesh.core.prices import PriceBook, PriceOverride
from nuramesh.core.redaction import reason_text_visible
from nuramesh.core.sandbox import Isolation
from nuramesh.core.tasks import FINAL, Task, TaskStatus, TaskTransitionError
from nuramesh.core.toolcalls import DEADLINE_WAIT_SECONDS, DENIAL_LIMIT
from nuramesh.core.verifier import CanonDocument
from nuramesh.engine.approvals_sweep import sweep_approvals
from nuramesh.engine.autonomy import recapture_configs, record_config, required_suites_of
from nuramesh.engine.budgets import release_reservation, settle_reservation
from nuramesh.engine.call_signal import CallCancelled, CallControl
from nuramesh.engine.canon import load_canon, record_canon_loaded
from nuramesh.engine.content import (
    WORKING_DAYS,
    ContentError,
    ContentService,
    LedgerHashing,
    OrphanedContentError,
    default_ledger_hashing,
    gate_error,
    is_gate_refusal,
)
from nuramesh.engine.deny_checks import deny_checks
from nuramesh.engine.evals import agent_eligibility
from nuramesh.engine.gate_runners import (
    GateError,
    decide_human_gate,
    error_result,
    record_gate_result,
    record_gate_started,
    run_rule_gate,
    run_schema_gate,
)
from nuramesh.engine.governance import Governance, ModelBlocked, ProviderSource
from nuramesh.engine.halt_refusal import (
    REFUSAL_ACTOR,
    halt_window_seconds,
    record_halt_refusal,
)
from nuramesh.engine.human_decisions import DecisionRefused, RecordedDecision, record_decision
from nuramesh.engine.killswitch import (
    BREAKER_ACTOR,
    BreakerEvaluationFailed,
    evaluate_breakers,
)
from nuramesh.engine.models import (
    ModelCallError,
    ModelResult,
    PolicyDenied,
    call_model,
    record_unevaluated,
)
from nuramesh.engine.org_halt import HaltWatch, OrgHalted, RolePaused
from nuramesh.engine.org_ledger import org_stream
from nuramesh.engine.plan_text import (
    PLAN_TEXT_UNAVAILABLE,
    PlanTextUnavailable,
    resolve_task_text,
)
from nuramesh.engine.policy import ApprovalRequired, ModelPolicy, check_action, pending_approvals
from nuramesh.engine.reconcile import ReconcileReport, reconcile_stale_reservations
from nuramesh.engine.run_control import HALT_VIAS, StopWatch
from nuramesh.engine.sandbox import CommandCancelled
from nuramesh.engine.steps import (
    DEPENDENCY_WAITING,
    HUMAN_DECISION,
    HUMAN_WAIT_EXPIRED,
    HUMAN_WAIT_WARNING,
    RESTART_KIND,
    WAITING_NOTE,
    Settled,
    Step,
    blocking_dependency,
    dependency_cancel_reason,
    dependency_wait_reason,
    read_progress,
    step_key,
    submitted_at,
)
from nuramesh.engine.tasks import create_task, move_task
from nuramesh.engine.toolcalls import GuardedRun, ToolLedger, budget_gate
from nuramesh.engine.verifier import run_verifier_gate
from nuramesh.engine.workspace import WorkspacePathError, workspace_target
from nuramesh.stores.base import (
    ConcurrencyError,
    EventDraft,
    Store,
    StreamTaken,
    UnitOfWork,
    run_stream,
)

S = TaskStatus
_LOG = logging.getLogger("nuramesh.engine.run")
# the content ids a task keeps, by the ledger entry that names them (R1.32 G5)
_KEPT_FIELDS: dict[str, tuple[str, ...]] = {
    EventType.TASK_TRANSITIONED.value: ("output_hash", "feedback_hash"),
    EventType.GATE_COMPLETED.value: ("evidence_hash",),
}
EVIDENCE_TOO_LARGE = "gate evidence too large to keep"
EVIDENCE_GONE = "the stored gate evidence is gone; it can't be resumed"
DECISION_GONE = "the stored human decision is gone; it can't be resumed"
RECAPTURE_EVERY = timedelta(hours=1)  # ND194: how often the engine runs the re-capture sweep

STOP_REASON = "cancelled: the run was stopped by a human"


class RunStopped(Exception):  # noqa: N818 (a signal, not an error)
    """A human asked the run to stop; raised at the next check so no further work starts."""


ROLE_PAUSED_REASON = "role_paused"
"""The reason (and `reason_code`) of a task cancelled because its role is paused (E19, spec 5)."""


class RolePausedStop(RunStopped):
    """The task's role is paused (E19): this task stops, the run goes on."""


class DurableRequestError(ValueError):
    """A request to a durable entry point that can never succeed (an unknown task, another run's
    task, a run that never started). An orchestrator must not retry it; every other failure is
    treated as possibly transient."""


class PlanRefused(ValueError):
    """`_check_plan` refused a plan for a reason with a fixed code (ND347 4.6): the runner records
    the code, never the text. A `ValueError`, so callers that caught the old one still work."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StartInfo:
    """What a runner adds to `run.started` for a run an API request started (ND347 4.5 step 11):
    the actor is the acting principal; the payload names them, the engine, the idempotency key's
    hash and the keyed hash of the plan. `plan_text` is the canonical plan the hash covers."""

    started_by: str
    engine: str
    plan_text: str
    idempotency_key_hash: str | None = None


class RunIdTaken(ValueError, ConcurrencyError):
    """The run id already has a ledger stream (R3.02 F1). The text is fixed: no id, no org, no
    hint about who owns it, so the refusal is the same for another org's run and for one's own.
    Also a `ConcurrencyError`: a runner that loses the race for the id backs off (E30)."""

    def __init__(self) -> None:
        super().__init__(RUN_ID_TAKEN_TEXT)


RUN_ID_TAKEN_TEXT = "run id is already in use"
RUN_ID_ISSUED_TEXT = "run id must be issued by the system"
_RUN_ID_ISSUED = re.compile(r"^run_[a-z2-7]{26}$")


def new_run_id() -> str:
    """A system-issued run id: `run_` + 26 base32 characters from 128 random bits (R3.02 F1)."""
    return "run_" + base64.b32encode(secrets.token_bytes(16)).decode().lower().rstrip("=")


@dataclass(frozen=True)
class WarnResult:
    """The outcome of `warn_human_wait_result`: `written`, or refused. `opens_at` is set when the
    refusal is only "not yet" under the store clock (the window opens at that store time)."""

    written: bool
    opens_at: datetime | None = None


class _WaitOver(Exception):  # noqa: N818 (a signal inside the engine)
    """The human wait ended while the engine was about to stop it."""


class _NotDue(Exception):  # noqa: N818 (a signal inside the engine)
    """The stored deadline of the human wait has not come under the store clock."""

    def __init__(self, deadline: datetime) -> None:
        super().__init__("not_due")
        self.deadline = deadline


class _DecisionGone(Exception):  # noqa: N818 (a signal inside the engine)
    """A person's recorded decision can't be read any more."""


class NeedsHuman(Exception):  # noqa: N818 (a signal, not an error)
    """Raised by a worker that has to stop and wait for a human; `reason` goes to the ledger.

    The reason must be fixed text chosen by the code, never content (A7).
    """

    reason = "the worker is waiting for a human"


NO_USABLE_ANSWER = "model_no_usable_answer"
"""The fixed code of a task whose model gave no usable answer (real-models design R28)."""


class NoUsableAnswer(NeedsHuman):
    """On the runner a worker's model answered with a tool call, a filtered answer or no text
    (R28): the call is billed as reported, the task goes to a person, no gate runs on it."""

    reason = "the model gave no usable answer; a person must look at this task"


@functools.lru_cache(maxsize=16)
def _read_canon(source: CanonSource) -> Canon:
    return load_canon(source)


_PATH_REFUSAL = "write_file_path"  # the `refusals` tag of a refused path (never cleared)
_KIND_REFUSAL = "unknown_kind"  # the `refusals` tag of an action kind that is not an ActionKind
_TARGET_REFUSAL = "action_target"  # the `refusals` tag of any other target `Action` rejects
_NETWORK_UNDECLARED_REFUSAL = "network_tool_undeclared"  # tool not declared network-capable
_NON_TEXT_TARGET = "<non-text target>"  # the refusals key of a non-str target (not the empty path)
_NETWORK_REFUSAL = "network_target"  # the `refusals` tag of a malformed network target


class WorkContext:
    """What a worker gets: its task, the round, gate feedback, the canon and a model client."""

    def __init__(
        self,
        engine: "RunEngine",
        run_id: str,
        task: Task,
        feedback: tuple[str, ...],
        epoch: int = 0,
    ):
        self._engine = engine
        self.epoch = epoch
        self.run_id = run_id
        self.task = task
        self.agent_id: str = task.assignee or ""
        self.attempt = task.attempt
        self.feedback = feedback
        self.canon: tuple[CanonDocument, ...] = engine.canon_documents
        self._calls = 0
        self._lock = threading.Lock()
        self.refusals: dict[tuple[str, str], str] = {}  # (kind, target) -> why, until allowed

    def tool_ledger(
        self,
        adapter: str,
        *,
        denial_limit: object = DENIAL_LIMIT,
        deadline_seconds: object = DEADLINE_WAIT_SECONDS,
    ) -> ToolLedger:
        """The embedded transport of the adapter tool-call seam (the adapter design, S4): the party
        that writes the ledger for this guarded run (run, task, attempt, assignee), with the
        real budget gate (AD-32). The role comes from the organization, never from the caller
        (AD-21); the options can only be stricter than the defaults (AD-69)."""
        engine = self._engine
        return ToolLedger(
            engine.store,
            engine.policies,
            engine.org,
            GuardedRun(self.run_id, self.task.id, self.attempt, self.agent_id),
            adapter=adapter,
            hashing=engine.hashing,
            gate=budget_gate(engine.org),
            content=engine.content,
            required_suites=required_suites_of(engine.evals),
            denial_limit=denial_limit,
            deadline_seconds=deadline_seconds,
        )

    def authorize(self, kind: ActionKind, target: str) -> None:
        """Check an action (a tool, a command, a file write, a message...) against this agent's
        policy before doing it: recorded (`policy.decided`); raises `PolicyDenied` (deny, default)
        or `ApprovalRequired` (ask), both of which send the task to a human. A `kind` that is the
        text of an `ActionKind` is normalised; any other value is refused (recorded deny with
        reason code `unknown_action_kind`, counted) so the check can never be skipped."""
        self._engine.refuse_action_when_halted()  # before any decision: nothing runs in a halt
        try:
            kind = ActionKind(kind)
        except ValueError:
            self._refuse_path(
                repr(kind)[:200],
                "unknown_action_kind",
                "the action kind is not a known kind",
                tag=_KIND_REFUSAL,
                action="unknown",
            )
            raise PolicyDenied("the action kind is not a known kind") from None
        if kind is ActionKind.WRITE_FILE and (
            not isinstance(target, str) or not is_canonical_write_target(target)
        ):
            self._refuse_path(
                target if isinstance(target, str) else _NON_TEXT_TARGET,
                "write_path_not_canonical",
                "a write_file path must be workspace-relative with one spelling "
                "(no leading '/', backslash, empty, '.' or '..' segment, control character); "
                "use authorize_write",
            )
            raise PolicyDenied("a write_file path must be workspace-relative with one spelling")
        try:
            action = Action(kind=kind, target=target)
        except ValidationError:
            shown = target[:200] if isinstance(target, str) else ""  # never raises
            if kind is not ActionKind.NETWORK:
                self._refuse_path(
                    shown,
                    "action_target_invalid",
                    "the action target is empty, too long or not allowed for this kind",
                    tag=_TARGET_REFUSAL,
                    action=kind.value,
                )
                raise PolicyDenied("the action target is not valid for this kind") from None
            self._refuse_path(
                target if isinstance(target, str) else "",
                "network_target_not_canonical",
                "a network target must be a bare host with an optional port "
                "(no scheme, userinfo, path, query or trailing dot)",
                tag=_NETWORK_REFUSAL,
                action=ActionKind.NETWORK.value,
            )
            raise PolicyDenied(
                "a network target must be a bare host with an optional port"
            ) from None
        check = check_action(
            self._engine.store,
            self._engine.policies,
            self._engine.org,
            CallContext(run_id=self.run_id, task_id=self.task.id, agent_id=self.agent_id),
            action,
            hasher=self._engine.hashing.bind(None, self._engine.org.id),
            actor=f"agent:{self.agent_id}",
            content=self._engine.content,
            required_suites=required_suites_of(self._engine.evals),
        )
        if action.target is None:  # never true for a validated Action; fail closed (not -O safe)
            raise PolicyDenied("the action target is not valid for this kind")
        key = (kind.value, action.target)  # the validated str (pydantic coerces bytes)
        with self._lock:
            if check.allowed:
                self.refusals.pop(key, None)  # for example an approval answered meanwhile
                return
            if check.decision.decision is Decision.ASK:
                self.refusals[key] = f"waiting for approval {check.approval_id}"
            else:
                self.refusals[key] = f"policy refused a {kind.value} action"
        if check.decision.decision is Decision.ASK:
            assert check.approval_id is not None
            raise ApprovalRequired(check.approval_id, check.decision.reason)
        raise PolicyDenied(check.decision.reason)

    def _refuse_path(
        self,
        path: str,
        code: str,
        why: str,
        *,
        tag: str = _PATH_REFUSAL,
        action: str = ActionKind.WRITE_FILE.value,
    ) -> None:
        """A write path (or action kind) refused before any rule can see it: counted in
        `refusals` first (so a failing ledger write can never leave a refusal uncounted), then
        recorded as a `policy.decided` deny with a fixed reason code (the text is never written, it
        is agent-chosen content), like any other denied action."""
        with self._lock:
            # its own tag: a later allowed `authorize` of the same text never clears it
            self.refusals[(tag, path)] = f"policy refused a {action} action"
        engine = self._engine
        role = engine.org.role_of(self.agent_id).id
        with engine.store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=run_stream(self.run_id),
                    type=EventType.POLICY_DECIDED,
                    actor=f"agent:{self.agent_id}",
                    run_id=self.run_id,
                    task_id=self.task.id,
                    payload={
                        "agent": self.agent_id,
                        "role": role,
                        "policy_contract": POLICY_CONTRACT,
                        "action": action,
                        "decision": Decision.DENY.value,
                        "rule": None,
                        "reason": why,
                        "reason_code": code,
                    },
                )
            )

    def refuse_network_undeclared(self, tool_name: object) -> None:
        """A tool the manifest does not declare network-capable asked for a URL (#1119, C-3):
        counted in `refusals` and recorded as a `policy.decided` deny with reason code
        `tool_not_network_capable` (the tool name is only the count key, never written). Its own
        tag, so graph code that catches the `PolicyDenied` still ends the task with a human and a
        later allowed `authorize` never clears it. Does not raise: the caller raises."""
        self._engine.refuse_action_when_halted()
        self._refuse_path(
            tool_name[:200] if isinstance(tool_name, str) else "",
            "tool_not_network_capable",
            "the tool is not declared network-capable",
            tag=_NETWORK_UNDECLARED_REFUSAL,
            action=ActionKind.NETWORK.value,
        )

    def authorize_write(self, root: Path, path: str) -> str:
        """Check a file write the agent chose: `path` is resolved against the workspace `root`
        (absolute paths, `..`, backslashes and links that leave it are refused) and the
        root-relative name is authorized as a `write_file` action and returned. Team, policy,
        canon, key and budget files are never writable (`is_protected_path`). Raises like
        `authorize`, or `WorkspacePathError` for a path outside the workspace."""
        try:
            target = workspace_target(root, path)
        except WorkspacePathError:
            self._refuse_path(
                path, "write_path_outside_workspace", "the path is not in the workspace"
            )
            raise
        self.authorize(ActionKind.WRITE_FILE, target)
        return target

    def authorize_command(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Check a command, given as the **argument list that will run**, against this agent's
        policy and return that same list. The policy text is `command_text(argv)` (words quoted so
        they split back into the same list), so what is approved is exactly what is executed: run
        the returned tuple as an argument list, never through a shell (policy contract 2, ND54: a
        `run_command` decision does not hold for a shell, where `pytest${IFS}-p${IFS}x` is three
        words). Raises like `authorize`."""
        words = tuple(argv)
        if not words or any(not isinstance(word, str) for word in words):
            raise ValueError("a command is a non-empty list of strings")
        self.authorize(ActionKind.RUN_COMMAND, command_text(words))
        return words

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_output_tokens: int,
        cache_prefix: bool = False,
    ) -> ModelResult:
        """One model call as this agent, with its model, policy, budgets and the ledger.
        `cache_prefix` asks to mark the end of the leading system run as a cacheable prefix; it
        takes effect only for an organization that opted in to prompt caching (R1.34, ND312)."""
        model_ref = self._engine.effective_ref(self.agent_id)
        if model_ref is None:
            raise PolicyDenied(f"agent '{self.agent_id}' has no model")
        provider, endpoint_id, model = parse_model_ref(model_ref)
        with self._lock:
            self._calls += 1
            n = self._calls
        result = self._engine.model_call(
            ModelRequest(
                provider=provider,
                endpoint_id=endpoint_id,
                model=model,
                messages=tuple(messages),
                max_output_tokens=max_output_tokens,
                cache_prefix=cache_prefix,
            ),
            CallContext(
                run_id=self.run_id,
                task_id=self.task.id,
                agent_id=self.agent_id,
                task_max_tokens=self.task.max_tokens,
            ),
            reservation_id=f"{self.task.id}-a{self.attempt}{_epoch_part(self.epoch)}-c{n}",
        )
        if self._engine.settings.runner_calls and (
            result.response.finish_reason == "filtered" or result.response.text == ""
        ):
            # R28: billed and settled as reported; the task goes to a person and no gate runs,
            # also when the worker catches this and goes on (the refusal is remembered)
            with self._lock:
                self.refusals[("model", NO_USABLE_ANSWER)] = NO_USABLE_ANSWER
            raise NoUsableAnswer
        return result


def _epoch_part(epoch: int) -> str:
    """Reservation ids of the first try are unchanged; a step that began again after a crash adds
    its epoch (the in-memory call counter restarts, and `reserve` refuses an id that exists)."""
    return f"-e{epoch}" if epoch else ""


Worker = Callable[[Task, WorkContext], Any]
"""Does the work of a task and returns its output (JSON-compatible)."""

GateRunner = Callable[[GateSpec, Task, Any], GateResult]
"""Runs one gate of a kind the engine doesn't run itself (test-command, custom)."""


@dataclass(frozen=True)
class HumanDecision:
    """A human's decision on a human gate: who, and (verdict, reason) per criterion."""

    decided_by: str
    findings: Mapping[str, tuple[bool, str]]


class Approver(Protocol):
    """Asks a human to decide a human gate; returns None when no decision is available yet."""

    def __call__(self, task: Task, gate: GateSpec, output: Any) -> HumanDecision | None: ...


@dataclass(frozen=True)
class PlannedTask:
    """A task in the plan (status `proposed`, with its assignee) and the tasks it waits for."""

    task: Task
    depends_on: tuple[str, ...] = ()


Planner = Callable[[OrgGraph], Sequence[PlannedTask]]
"""The Chief of Staff's planning step: returns the run's tasks."""


@dataclass(frozen=True)
class GateLine:
    """One recorded gate result in the report (latest round)."""

    gate: str
    kind: str
    outcome: str
    checked_by: str


@dataclass(frozen=True)
class TaskReport:
    """Where a task ended and why."""

    id: str
    title: str
    assignee: str | None
    status: str
    attempts: int
    gates: tuple[GateLine, ...]
    note: str


@dataclass(frozen=True)
class BudgetLine:
    """A budget's balance at the end of the run."""

    id: str
    scope: str
    scope_id: str
    limit: str
    spent: str
    reserved: str
    blocked: bool
    window: str = "none"
    """`none` (lifetime), `day` or `month`. Spent and reserved then count the window that is
    current when the report is built (not necessarily the window the run used: a report on
    yesterday's run shows today's window; the run's own spend is in its ledger events)."""
    window_start: str | None = None


def _plain(amount: Decimal) -> str:
    """Decimal text without trailing zeros or an exponent (`0.007000` -> `0.007`, `0E-6` -> `0`)."""
    return format(amount.normalize(), "f") if amount else "0"


@dataclass(frozen=True)
class UnknownCostCall:
    """A call whose price is known but whose cost is not (the answer had no usable usage): the
    reserved amount was charged to the budgets (spec default-prices 6.1)."""

    model: str
    charged: str | None
    task_id: str | None


@dataclass(frozen=True)
class SpendLine:
    """What the run's model calls cost: the known total, and the calls whose cost is unknown. A run
    with an unknown call shows `at least` the KNOWN total only: what the budgets charged for such a
    call is the reservation, an upper bound, so it is never added (cost spec 3.5 rules 1 and 5)."""

    known_total: str = "0"
    unknown: tuple[UnknownCostCall, ...] = ()
    calls: int = 0

    @property
    def at_least(self) -> str | None:
        return _plain(Decimal(self.known_total)) if self.unknown else None


@dataclass(frozen=True)
class RunReport:
    """The run report (PLAN C.6): tasks, gate verdicts, spend vs budget, approvals, ledger head."""

    run_id: str
    tasks: tuple[TaskReport, ...]
    budgets: tuple[BudgetLine, ...]
    pending_approvals: tuple[str, ...]
    reconciled: int
    ledger_seq: int
    ledger_head: str
    ledger_ok: bool
    canon_commit: str | None
    stopped: bool = False
    stop_requested_by: str | None = None
    spend: SpendLine = SpendLine()

    @property
    def counts(self) -> dict[str, int]:
        """Tasks per final status."""
        out: dict[str, int] = {}
        for t in self.tasks:
            out[t.status] = out.get(t.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        """The report as plain JSON-ready data."""
        return {
            "run_id": self.run_id,
            "counts": self.counts,
            "tasks": [{**t.__dict__, "gates": [g.__dict__ for g in t.gates]} for t in self.tasks],
            "budgets": [b.__dict__ for b in self.budgets],
            "pending_approvals": list(self.pending_approvals),
            "reconciled": self.reconciled,
            "ledger": {"seq": self.ledger_seq, "head": self.ledger_head, "ok": self.ledger_ok},
            "canon_commit": self.canon_commit,
            "stopped": self.stopped,
            "stop_requested_by": self.stop_requested_by,
            "spend": {
                "known_total": self.spend.known_total,
                "unknown_calls": len(self.spend.unknown),
                "at_least": self.spend.at_least,
                "complete": not self.spend.unknown,
                "unknown": [
                    {"model": u.model, "charged": u.charged, "task_id": u.task_id}
                    for u in self.spend.unknown
                ],
            },
        }

    def render_text(self) -> str:
        """A readable summary for a terminal."""
        lines = [f"Run {self.run_id}"]
        if self.stopped:
            lines.append(
                f"STOPPED by {self.stop_requested_by or 'a human'}: unfinished tasks cancelled"
            )
        counts = ", ".join(f"{n} {s}" for s, n in sorted(self.counts.items()))
        lines.append(f"Tasks: {counts}")
        for t in self.tasks:
            who = t.assignee or "-"
            lines.append(f"  [{t.status}] {t.id} ({who}, attempts {t.attempts}): {t.title}")
            for g in t.gates:
                lines.append(f"      gate {g.gate} ({g.kind}): {g.outcome} by {g.checked_by}")
            if t.note:
                lines.append(f"      note: {t.note}")
        lines.append("Budgets:")
        for b in self.budgets:
            flag = " BLOCKED" if b.blocked else ""
            per = (
                f" per {b.window} (the current window, since {b.window_start}, as of the report)"
                if b.window != "none"
                else ""
            )
            lines.append(
                f"  {b.id} ({b.scope} {b.scope_id}): spent {b.spent} of {b.limit}{per}, "
                f"reserved {b.reserved}{flag}"
            )
        if self.spend.calls:
            noun = "call" if self.spend.calls == 1 else "calls"
            if self.spend.unknown:
                lines.append(
                    f"Spend: {at_least_words(self.spend.at_least or '0')} "
                    f"({self.spend.calls} {noun}, {len(self.spend.unknown)} {COST_UNKNOWN_SHORT})"
                )
                for u in self.spend.unknown:
                    lines.append(f"  {u.model}: {cost_unknown_call_words(u.charged)}")
            else:
                lines.append(f"Spend: ${self.spend.known_total} ({self.spend.calls} {noun})")
        if self.pending_approvals:
            lines.append("Waiting for a human: " + ", ".join(self.pending_approvals))
        if self.canon_commit:
            lines.append(f"Canon: {self.canon_commit}")
        verdict = "verified" if self.ledger_ok else "BROKEN"
        lines.append(
            f"Ledger: run:{self.run_id} seq {self.ledger_seq}, head {self.ledger_head[:16]}… "
            f"({verdict})"
        )
        return "\n".join(lines)


@dataclass
class _Outcome:
    status: TaskStatus
    note: str = ""

    @property
    def settled(self) -> Settled:
        return Settled(self.status, WAITING_NOTE in self.note)


@dataclass
class EngineSettings:
    """Tuning for a run engine."""

    max_parallel: int = 4
    reconcile_every: timedelta = timedelta(seconds=60)
    reconcile_max_age: timedelta = timedelta(minutes=15)
    approvals_sweep_every: timedelta = timedelta(minutes=30)
    """The approvals sweep (ND195 a) runs at most this often per engine, not on every task step
    (auth design R34). The first call after the engine starts always sweeps."""
    resume_quiet: timedelta = timedelta(seconds=30)
    """A run whose ledger changed more recently than this is not resumed without
    `previous_engine_gone=True` (its engine may still be alive)."""
    actor: str = REFUSAL_ACTOR
    halt_refusal_window: int = field(default_factory=halt_window_seconds)
    """T154: the summary window W in seconds for refusals by a halt (the setting
    `NURAMESH_HALT_REFUSAL_WINDOW_SECONDS`, 1 to 86400); a bad value raises `HaltWindowError` when
    the settings are made."""
    stop_poll: timedelta = timedelta(milliseconds=250)
    """How often the stop watcher looks while tasks run (so hooks fire during a long step)."""
    interrupt_actor: str | None = None
    """Who a Ctrl-C is recorded as (default: `actor`); the CLI passes `--human` here."""
    human_wait_warn_days: tuple[int, ...] = (14, 3)
    """Durable runs (ND55): the approver of a task waiting for a human is warned this many days
    before its input expires (the 90-day working expiry), in descending order."""
    runner_calls: bool = False
    """The light runner's call rules (real-models design R20.8, R27, R28; never the CLI path, never
    the Temporal worker): a provider call that failed after its request was sent is settled at
    its reservation as unknown, a stop cuts a call in flight, a worker's answer that is a tool
    call or empty sends the task to a person, and the stop path charges left-over reservations
    as unknown instead of releasing them."""
    wind_down: str | None = None
    """R1.37 wind-down mode: the gate code (`org_key_destroyed`, `org_key_missing`,
    `key_tombstone_unavailable`) the worker factory caught. Every activity that ends or reads a run
    still works; `run_task_step` raises the matching error before any model call or spend."""


@dataclass(frozen=True)
class StoredPlan:
    """The plan of a started run, as `run.started` recorded it: task ids and dependency edges only
    (no titles: they are free text). A durable orchestrator reads this, not a copy it was given."""

    tasks: tuple[str, ...]
    depends_on: Mapping[str, tuple[str, ...]]
    ended: bool
    plan_hash: str | None = None
    """The keyed plan hash an API-started run recorded (none for other runs)."""


def open_human_gates(task: Task, events: Sequence[LedgerEvent]) -> list[str]:
    """The required human gates of this round nobody has answered: no recorded result and no
    recorded decision. Empty means the task is not waiting for a person any more (an answer
    is on file and the gates only have to run again)."""
    results: dict[str, str] = {}
    decided: set[str] = set()
    for e in events:
        if e.payload.get("attempt") != task.attempt:
            continue
        if e.type == EventType.GATE_COMPLETED and e.payload.get("submission") == task.submission:
            results[str(e.payload["gate"])] = str(e.payload.get("outcome"))
        elif (
            e.type == EventType.DECISION_RECORDED
            and e.payload.get("kind") == HUMAN_DECISION
            and e.payload.get("submission") == task.submission
        ):
            decided.add(str(e.payload.get("gate")))
    return [
        g.id
        for g in task.definition_of_done.required_gates
        if g.kind is GateKind.HUMAN
        and results.get(g.id) not in ("passed", "failed")
        and g.id not in decided
    ]


@dataclass(frozen=True)
class TaskStepResult:
    """What one per-task entry point did (R1.20 PR 3): ids, a status and flags, never content."""

    task_id: str
    status: TaskStatus
    waiting: bool
    """The task itself is in a durable human wait (a held or cancelled dependent returns False)."""
    stopped: bool
    """A Stop request was seen and applied to this task."""
    waiting_since: datetime | None = None
    """For a task waiting in `verifying` for a human gate: the ledger time of its submit move."""
    attempt: int = 0
    submission: int = 0
    not_due: bool = False
    """`expire_human_wait` refused because the stored deadline has not come under the store clock
    (R1.20 PR D, spec 8, P07): nothing was written and the task still waits."""
    due_at: datetime | None = None
    """With `not_due`: the deadline the engine read from stored times."""

    @property
    def settled(self) -> Settled:
        return Settled(self.status, self.waiting)

    @property
    def awaits_human(self) -> bool:
        """The task sits in `verifying` with only a person's decision missing."""
        return self.status is TaskStatus.VERIFYING and self.waiting


_ENDED_NOT_DONE = (S.CANCELLED, S.NEEDS_HUMAN, S.BLOCKED)
"""A dependency in one of these has ended without `done` and nothing in the run can approve it:
its dependents are cancelled, not held."""


OUTPUT_ERROR_CODE = "output_error"
"""`reason_code` of the ledger entry that cancels a task whose output holds an unsafe number
(ND87): a stable code, so these cancels can be told from a person's cancel and migrated if a real
`error` status is added."""


def _unsafe_number(output: Any) -> tuple[str, str] | None:
    """Why the output can't be written as JSON because of a number (NaN, an infinity, an integer
    beyond +-(2^53 - 1)): (message, masked path) with the data's keys hidden (ND87); None if no
    number is the problem (other problems are found where the output is kept or checked)."""
    try:
        canonical_json_jcs(output)
    except UnsafeNumberError as exc:
        return exc.public_message(), exc.public_path()
    except (TypeError, ValueError, RecursionError):
        return None
    return None


class RunEngine:
    """Runs plans for one organization against one store (module docstring)."""

    def __init__(
        self,
        store: Store,
        org: OrgGraph,
        *,
        provider: ModelProvider,
        prices: PriceBook,
        policies: PolicySet,
        workers: Mapping[str, Worker],
        approver: Approver | None = None,
        evals: EvalConfig | None = None,
        presets: PresetConfig | None = None,
        calibration: CalibrationConfig | None = None,
        canon: CanonSource | None = None,
        overrides: tuple[PriceOverride, ...] = (),
        gate_runners: Mapping[GateKind, GateRunner] | None = None,
        settings: EngineSettings | None = None,
        content: ContentService | None = None,
        content_settings: ContentSettings | None = None,
        governance: Governance | None = None,
        data_class: str = "synthetic",
        breakers: Sequence[BreakerRule] = (),
        hashing: LedgerHashing | None = None,
        hosted_ids: bool = False,
    ) -> None:
        self.store, self.org = store, org
        self.hosted_ids = hosted_ids  # R3.02 F1: hosted mode accepts only `new_run_id()` ids
        self.breakers = check_rules(breakers)  # E19: evaluated before each model call
        self._halt_watch = HaltWatch(store, org.id)
        self._halt_decided: int | None = None
        self._halt_lock = threading.Lock()  # begin_run: one decide-and-record at a time (T154 m4)
        # L5 (ND92): with a `governance` every model call and the start of every run go through
        # the resolver; `data_class` is what this run sends (`synthetic` or `customer`).
        self.governance, self.data_class = governance, data_class
        if data_class not in ("synthetic", "customer"):
            raise ValueError("data_class must be 'synthetic' or 'customer'")
        if any(o.reason_hash is None for o in overrides):
            raise ValueError("price overrides must be keyed first (ContentService.key_overrides)")
        self.provider, self.prices, self.overrides = provider, prices, overrides
        self.policies, self.workers, self.approver = policies, dict(workers), approver
        self.evals, self.canon_source = evals, canon
        self.presets = presets
        self.calibration = calibration
        self.gate_runners = dict(gate_runners or {})
        self.settings = settings or EngineSettings()
        # R1.20 (ND52): a durable run keeps each task output in the content store; an in-process run
        # (library, CLI, the API's live-run endpoint) passes no `content` and stores nothing.
        self.content = content
        self._recapture_at: datetime | None = None  # ND194: the sweep runs at most hourly
        # RET-WIRE R3: a driver that stores content must say how long to keep it (a forgotten
        # setting would silently mean 30 days); without `content` the setting is unused
        if content is not None and content_settings is None:
            raise ValueError("content_settings is required when content is given")
        self.content_settings = content_settings or ContentSettings()
        if content is not None and content.hosted:
            problems = self.content_settings.problems(hosted=True)
            if problems:
                raise ValueError(problems[0])
        if self.settings.max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        if self.settings.reconcile_every <= timedelta(0):
            raise ValueError("reconcile_every must be positive")
        if self.settings.approvals_sweep_every <= timedelta(0):
            raise ValueError("approvals_sweep_every must be positive")
        if self.settings.reconcile_max_age <= timedelta(0):
            raise ValueError("reconcile_max_age must be positive")
        warn = self.settings.human_wait_warn_days
        if list(warn) != sorted(set(warn), reverse=True) or any(
            d < 1 or d >= WORKING_DAYS for d in warn
        ):
            raise ValueError(
                f"human_wait_warn_days must be distinct, descending, from 1 to {WORKING_DAYS - 1}"
            )
        self._hashing = hashing
        self._default_hashing: LedgerHashing | None = None
        self.policy = ModelPolicy(
            store,
            policies,
            org,
            hasher=self.hashing.bind(None, org.id),
            actor=self.settings.actor,
            content=content,
            evals=evals,
        )
        self.chief = next(a.id for a in org.agents if a.reports_to is None)
        self.canon_documents: tuple[CanonDocument, ...] = ()
        self._canon: Canon | None = None
        self._reconciled = 0
        self._reconcile_lock = threading.Lock()
        self._running = threading.Lock()  # one run at a time per engine (its state is per run)
        self._resume_gone = False
        self._run_id = ""
        self._stop: StopWatch | None = None
        self._stop_acted = False
        self._stop_hooks: list[Callable[[], None]] = []
        self._hooks_fired = False
        self._aborted = False
        self._interrupts = 0
        self._hooks_lock = threading.Lock()
        self._in_flight: set[str] = set()  # reservations of model calls that are mid-call
        self._in_flight_lock = threading.Lock()
        self._controls: set[CallControl] = set()  # the runner's provider calls a stop can cut
        self._calls_cut = False

    # -- stop --------------------------------------------------------------------------------

    def on_stop(self, hook: Callable[[], None]) -> None:
        """Call `hook` (once, from the stop watcher or the interrupt) when a stop is found: for
        example to kill a running sandbox container. Hooks must be quick and must not raise."""
        self._stop_hooks.append(hook)

    def _stop_check(self) -> bool:
        return self._stop is not None and self._stop.requested()

    def _checkpoint(self, task_id: str | None = None, role: str | None = None) -> None:
        """Raise `RunStopped` if a human asked the run to stop, or the org is halted. With a task
        (and its agent's role) also `RolePausedStop` if that role is paused or the task was asked
        to stop by a role pause (E19): that task stops, the run goes on."""
        if self._stop_check():
            self._fire_hooks()
            raise RunStopped
        if task_id is not None and role is not None and self._role_stopped(task_id, role):
            raise RolePausedStop

    def _role_stopped(self, task_id: str, role: str) -> bool:
        if self._stop is None:
            return False
        try:
            return self._stop.role_stopped(task_id, role)
        except Exception:  # unreadable: fail closed (the run's own stop check says halted too)
            return True

    def _task_checkpoint(self, task: Task) -> None:
        """`_checkpoint` for a task: its assigned agent's role is the one that counts."""
        role = self._role_of_agent(task.assignee)
        self._checkpoint(task.id, role)

    def _role_of_agent(self, agent_id: str | None) -> str | None:
        if agent_id is None:
            return None
        try:
            return self.org.agent(agent_id).role
        except Exception:
            return None

    def _new_control(self) -> CallControl | None:
        """The control of one provider call under the runner's rules (None elsewhere). It is
        registered before the call's first step, so the step's stop watcher covers steps 5 to 9
        of R20; a stop already seen cancels it at once (nothing is then sent)."""
        if not self.settings.runner_calls:
            return None
        control = CallControl()
        with self._in_flight_lock:
            self._controls.add(control)
            cut = self._calls_cut
        if cut:
            control.signal.cancel()
        return control

    def _drop_control(self, control: CallControl | None) -> None:
        if control is not None:
            with self._in_flight_lock:
                self._controls.discard(control)

    def _cut_calls(self) -> None:
        """R27: a stop request of this run cancels its provider calls in flight. A halt of the
        organization is not a stop request of the run: its calls end as they do today."""
        request = self._stop.user_request if self._stop is not None else None
        if request is None:
            return
        with self._in_flight_lock:
            self._calls_cut = True
            controls = list(self._controls)
        for control in controls:
            control.signal.cancel()

    def _fire_hooks(self) -> None:
        if self.settings.runner_calls:
            self._cut_calls()
        with self._hooks_lock:
            if self._hooks_fired:
                return
            self._hooks_fired = True
        for hook in self._stop_hooks:
            try:
                hook()
            except Exception:  # noqa: S112 (a failing hook must not keep the run from stopping)
                continue

    def _stop_watch_loop(self, done: threading.Event) -> None:
        interval = self.settings.stop_poll.total_seconds()
        halted = (
            False  # a halt was seen: it does not cut calls, so keep looking for a person's stop
        )
        while not done.wait(interval):
            try:
                if halted:
                    if self._stop is not None and self._stop.user_stop() is not None:
                        self._cut_calls()  # R27: a person's stop after a halt still cuts (m2)
                        return
                elif self._stop_check():
                    self._fire_hooks()  # also cuts the runner's calls in flight (R27)
                    request = self._stop.request if self._stop is not None else None
                    if (
                        self.settings.runner_calls
                        and request is not None
                        and (request.via in HALT_VIAS)
                    ):
                        halted = True
                        continue
                    return
            except Exception:  # noqa: S112 (retried on the next tick)
                continue

    def _stopped(self, task_id: str) -> _Outcome:
        """Cancel a task because the run is stopping (recorded with the reason)."""
        self._stop_acted = True
        return self._cancel(task_id, STOP_REASON)

    def _release_open_reservations(self, run_id: str) -> None:
        """A stopped run leaves nothing reserved: all workers are done, so any open reservation
        is a leak (a worker killed mid-call); give it back now, not after the reconciler's age."""
        for stored in self.store.reservations_for_run(run_id, ReservationStatus.OPEN):
            try:
                if self.settings.runner_calls:
                    # real-models design R27 (owner, OQ10 A): a left-over of a call that was sent
                    # is charged, never released. Nothing durable says whether a dead try's
                    # request was sent, so a left-over is taken as sent (it may over-count, it
                    # does not under-count); a call stopped before it was sent released its own
                    # reservation synchronously and is not found here.
                    settle_reservation(
                        self.store,
                        stored.reservation.id,
                        actual=None,
                        cost_basis=CostBasis.UNKNOWN,
                        actor=self.settings.actor,
                        details={"stopped": True, "mode": "charge_unknown"},
                    )
                    continue
                release_reservation(
                    self.store,
                    stored.reservation.id,
                    actor=self.settings.actor,
                    reason="run stopped",
                    details={"stopped": True},
                )
            except Exception:  # noqa: S112 (the reconciler closes what is left)
                continue

    # -- model calls -------------------------------------------------------------------------

    def price_book(self) -> PriceBook:
        """The prices a call uses: the configuration's plus, with governance, the ledger's."""
        return self.governance.price_book() if self.governance is not None else self.prices

    def effective_ref(self, agent_id: str) -> str | None:
        """The model an agent uses now: the ledger's assignments over the configuration when
        governance is wired in (spec section 2.5), else the configuration's."""
        if self.governance is not None:
            return self.governance.effective_ref(agent_id)
        return self.org.model_for(agent_id)

    def model_call(
        self,
        request: ModelRequest,
        context: CallContext,
        *,
        reservation_id: str,
    ) -> ModelResult:
        """A model call through policy, prices, budgets and the ledger. Raises `RunStopped` (before
        anything is reserved) once a human has asked the run to stop. There is no `gate`
        parameter: only the verifier gate marks its own calls, so a worker's calls always count
        as authors of the task (review minor 5)."""
        role = self._role_of_agent(context.agent_id)
        self._checkpoint(context.task_id, role)  # halt and role pause first (spec 9.7)
        if self.breakers:
            self._precall_evaluation(context, role)
            self._checkpoint(context.task_id, role)  # a trip may have halted the org
        control = self._new_control()
        with self._in_flight_lock:
            self._in_flight.add(reservation_id)
        try:
            return self._call_model(request, context, reservation_id, control)
        except CallCancelled:  # R27: a stop cut the call; it is settled or released already
            self._stop_check()
            self._fire_hooks()
            raise RunStopped from None
        except OrgHalted:  # halted between the checkpoint and the call: the same clean stop
            self._stop_on_halt()
        except RolePaused:  # paused between the checkpoint and the call: this task stops
            raise RolePausedStop from None
        finally:
            self._drop_control(control)
            with self._in_flight_lock:
                self._in_flight.discard(reservation_id)

    def _precall_evaluation(self, context: CallContext, role: str | None) -> None:
        """Evaluate the breakers before a model call, only when a halting rule exists. Any failure
        fails closed: the call is refused (`breaker_evaluation_failed`, best effort) and the run
        stops cleanly like a halt (spec 9.7)."""
        try:
            evaluate_breakers(
                self.store, self.org.id, self.breakers, org=self.org, halting_only=True
            )
        except BreakerEvaluationFailed:
            record_unevaluated(
                self.store, context, self.settings.actor, self.store.now(), role or ""
            )
            assert self._stop is not None
            self._stop.record_stop(BREAKER_ACTOR, "breaker_evaluation_failed")
            self._fire_hooks()
            raise RunStopped from None

    def refuse_action_when_halted(self) -> None:
        """Once a halt of the organization is recorded, no tool action and no command executes
        (real-models design R27, case C36s). A model call in flight may end
        and is settled; this is asked at execution time, where a worker has an action checked
        (`WorkContext.authorize`) and right before a gate runner runs. An unreadable halt state
        is a halt (fail closed). It stops the task the way a halt at a model call does."""
        try:
            halted = self._halt_watch.state().halted
        except Exception:
            halted = True
        if halted:
            self._stop_on_halt()

    def _stop_on_halt(self) -> NoReturn:
        """`OrgHalted` escaped from a model call: record the stop request on the watch (so the run
        ends as `stopped` with who asked), fire the hooks and stop the task."""
        self._stop_check()
        self._fire_hooks()
        raise RunStopped

    @property
    def hashing(self) -> LedgerHashing:
        """The ledger hashing of this engine: the one given, else the content service's (read
        when used, so a service set later is honoured), else one over the default key directory."""
        if self._hashing is not None:
            return self._hashing
        if self.content is not None:
            return self.content.ledger_hashing
        if self._default_hashing is None:
            self._default_hashing = default_ledger_hashing(self.store)
        return self._default_hashing

    def _call_model(
        self,
        request: ModelRequest,
        context: CallContext,
        reservation_id: str,
        control: CallControl | None = None,
    ) -> ModelResult:
        try:
            return call_model(
                self.store,
                self.provider,
                request,
                governance=self.governance,
                context=context,
                org=self.org,
                prices=self.price_book(),
                reservation_id=reservation_id,
                actor=self.settings.actor,
                authorize=self.policy.authorize,
                authorize_spend=self.policy.authorize_spend,
                overrides=self.overrides,
                watch=self._halt_watch,
                hasher=self.hashing.bind(None, self.org.id),
                control=control,
            )
        finally:
            self.hashing.flush_alerts()  # a failed ledger hash (PH-KEY R6)

    # -- the run -----------------------------------------------------------------------------

    def run(self, run_id: str, plan: Sequence[PlannedTask] | Planner) -> RunReport:
        """Execute the plan and return the report (module docstring).

        Everything that can be checked is checked **before** `run.started`: the plan's shape,
        every task's gate configuration against the org, and the canon (read at its pinned
        commit). A refused plan writes nothing. `run.ended` is always written once the run has
        started, even if the engine itself fails (`aborted: true`, the error type).
        """
        if not self._running.acquire(blocking=False):
            raise RuntimeError("this engine is already running a run; use one engine per run")
        try:
            return self._run(run_id, plan)
        finally:
            self._finish_run()

    def resume(
        self,
        run_id: str,
        plan: Sequence[PlannedTask] | Planner,
        *,
        previous_engine_gone: bool = False,
    ) -> RunReport:
        """Carry on a run a crash cut short (R1.20 PR 2): the same plan. `run.started` is written
        and `run.ended` is not; tasks created before the crash are kept, the rest are created now.
        Every task goes on from the step the store shows (`engine/steps.py`): a step already
        acknowledged is not repeated, and one the crash interrupted runs again in a new epoch.
        Reservations the crash left open are closed first (T24). Without a content service the
        outputs of submitted tasks are gone, and those tasks go to a human.

        **Precondition:** the engine that was running this run is gone. Nothing here can prove
        that (a lease or an orchestrator can, R1.20 PR 3), so a caller that knows it passes
        `previous_engine_gone=True`; otherwise resume refuses while the run has a model call
        reserved less than `reconcile_max_age` ago (a call that may still be in flight)."""
        if not self._running.acquire(blocking=False):
            raise RuntimeError("this engine is already running a run; use one engine per run")
        try:
            self._resume_gone = previous_engine_gone
            return self._run(run_id, plan, resume=True)
        finally:
            self._finish_run()

    def _finish_run(self) -> None:
        """Release the engine, then flush the last alert (PH-KEY R6, K33). A flush that fails
        (building the hashing reads the store) must neither hide the run's own error nor keep
        the engine locked."""
        self._running.release()
        try:
            self.hashing.flush_alerts()
        except Exception:
            _LOG.warning("the ledger-hash alert flush failed", exc_info=True)

    def _run(
        self, run_id: str, plan: Sequence[PlannedTask] | Planner, *, resume: bool = False
    ) -> RunReport:
        planned = list(plan(self.org) if callable(plan) else plan)
        if self._refuse_before_start():
            return self._halted_start(run_id, planned, resume=resume)
        self._check_run_id(run_id, resume=resume)
        self._check_plan(run_id, planned, resume=resume)
        # E13: the configuration is recorded (with the voids it causes) before it is used; if the
        # record cannot be written the run does not start (the autonomy design 2 and 7 item 4)
        record_config(self.store, self.org, self.policies, self.evals, content=self.content)
        started = self._governance_start(run_id, planned, resume=resume)
        self._reconciled = 0
        self._attach(run_id)
        self._event(
            run_id,
            EventType.RUN_RESUMED if resume else EventType.RUN_STARTED,
            self._started_payload(planned),
            claim=not resume,
        )
        self._governance_started(run_id, started)
        outcomes: dict[str, _Outcome] = {}
        ended: dict[str, Any] = {}
        try:
            if self._canon is not None:
                record_canon_loaded(
                    self.store, run_id, self._canon, org_id=self.org.id, actor=self.settings.actor
                )
            created: list[str] = []
            try:
                if resume:  # nothing is running yet: whatever a crash left reserved is closed now
                    reconcile_stale_reservations(
                        self.store,
                        run_id,
                        max_age=timedelta(microseconds=1),
                        actor=self.settings.actor,
                    )
                for item in planned:
                    if resume and self.store.get_task(item.task.id) is not None:
                        continue  # created before the crash
                    create_task(
                        self.store,
                        item.task,
                        actor=f"agent:{self.chief}",
                        org=self.org,
                        calibration=self.calibration,
                    )
                    created.append(item.task.id)
            except BaseException as exc:
                for tid in created:  # tasks already created must not be left without a reason
                    outcomes[tid] = self._to_human(
                        tid, f"run aborted while creating tasks ({type(exc).__name__})"
                    )
                raise
            stop = threading.Event()
            reconciler = threading.Thread(
                target=self._reconcile_loop, args=(run_id, stop), daemon=True, name="reconciler"
            )
            reconciler.start()
            watcher = threading.Thread(
                target=self._stop_watch_loop, args=(stop,), daemon=True, name="stop-watcher"
            )
            watcher.start()
            try:
                outcomes = self._schedule(run_id, planned)
            finally:
                stop.set()
                reconciler.join()
                watcher.join()
                if self._stop_acted:
                    self._release_open_reservations(run_id)
                try:
                    self._reconcile(run_id)
                except Exception as exc:
                    ended["reconcile_error"] = type(exc).__name__
                self._expire_content()
        except BaseException as exc:  # including KeyboardInterrupt: the record says it aborted
            refused = is_gate_refusal(exc)
            # a key gate refusal is recorded by its fixed code, never by text (R1.37 8.3)
            code = getattr(exc, "code", None) if refused else None
            ended.update({"aborted": True, "error": code or type(exc).__name__})
            if isinstance(exc, KeyboardInterrupt) or refused:
                self._abort_cleanup(
                    planned, outcomes, "cancelled: the run was aborted" if refused else None
                )
                self._expire_content()  # the cancels just set their final expiry
            raise
        finally:
            counts: dict[str, int] = {}
            for o in outcomes.values():
                counts[o.status.value] = counts.get(o.status.value, 0) + 1
            if (
                self._stop_acted
                and not ended.get("aborted")  # one rule: an abort wins over a stop in the status
                and self._stop is not None
                and self._stop.request is not None
            ):
                ended.update({"status": "stopped", "stop_requested_by": self._stop.request.by})
            try:
                self._event(run_id, EventType.RUN_ENDED, {"counts": counts, **ended})
            except Exception:
                if not ended.get("aborted"):
                    raise  # otherwise keep the original error, not this one
        return self.report(run_id, planned, outcomes)

    def _refuse_before_start(self) -> bool:
        """The deny order of `nuramesh try` (R1.37), before anything is written.
        A halt in force comes first and wins over everything: it returns True and the caller
        refuses the start with `_halted_start` (a clean stop that writes ONE audit record and
        nothing else). An unreadable halt state is a halt in force too: fail closed, same refusal
        (E19's checkpoint stop is for a halt that arrives mid-run). Otherwise
        an erased organization id raises `OrgIdRetiredError`, then a lost key `OrphanedContentError`
        (`org_key_missing`). Governance and the plan checks come after."""
        self._halt_decided = None  # E of the deciding read (T154 R0), None = unknown
        denial = deny_checks(
            self.store,
            self.org.id,
            content=self.content,
            halt=self._halt_watch,
            hashing=lambda: self.hashing,
        )
        if denial is None:
            return False
        if denial.is_halt:
            self._halt_decided = denial.halt_seq  # E of the deciding read (None: unreadable)
            return True
        if denial.error is not None:
            raise denial.error
        raise ContentError(denial.text)  # unreachable: every non-halt denial carries its error

    def _record_halt_refusal(self, run_id: str) -> None:
        """The ONE ledger record of a start refused by a halt: `model.blocked` with the reason
        `org_halted` and the stage `start`, on the organization stream (the record governance
        writes for the same refusal). Best effort: a store that is down is why we may be here."""
        record_halt_refusal(
            self.store,
            self.org.id,
            run_id,
            episode=self._halt_decided,  # the seq of the read that refused (None: unreadable)
            actor=self.settings.actor,
            content=self.content,
            window_seconds=self.settings.halt_refusal_window,
        )

    def _halted_start(
        self, run_id: str, planned: Sequence[PlannedTask], *, resume: bool
    ) -> RunReport:
        """`run` or `resume` while the organization is halted (E19): the run
        stops cleanly and does no work. Nothing is written but ONE `model.blocked` record: no
        run, task, budget, seed or reconcile entry. The report says every task is cancelled
        (a run that never started has no stored tasks, so they are named from the plan)."""
        self._record_halt_refusal(run_id)
        if resume:
            report = build_report(self.store, self.org, run_id)
            return dataclasses.replace(report, stopped=True)
        tasks = tuple(
            TaskReport(
                id=p.task.id,
                title=p.task.title,
                assignee=p.task.assignee,
                status=S.CANCELLED.value,
                attempts=0,
                gates=(),
                note=HALTED_TEXT,
            )
            for p in planned
        )
        try:  # the store may be what is down: the stop still reports
            head = self.store.head(org_stream(self.org.id))
            ledger_ok = self.store.verify_stream(org_stream(self.org.id)).ok
        except Exception:
            head, ledger_ok = None, False
        return RunReport(
            run_id=run_id,
            tasks=tasks,
            budgets=(),
            pending_approvals=(),
            reconciled=0,
            ledger_seq=head.seq if head else 0,
            ledger_head=head.hash if head else "",
            ledger_ok=ledger_ok,
            canon_commit=None,
            stopped=True,
        )

    def _attach(self, run_id: str, *, cached: bool = False, with_canon: bool = True) -> None:
        """Per-run state: the stop watch, the hooks, and the canon at its pinned commit. With
        `cached` (the durable entry points, called once per activity) a canon already read at this
        commit is not read from Git again: a commit's content never changes, and a failed read is
        never cached, so the next try reads it again. `with_canon=False` (the activities that close
        or park a run: none of them uses the canon) reads nothing from Git, so a canon failure
        can never keep a run from ending (R3.00)."""
        self._run_id = run_id
        self._stop = StopWatch(self.store, run_id, self.org.id, halt=self._halt_watch)
        self._stop_acted = self._hooks_fired = self._aborted = False
        self._interrupts = 0
        if self.canon_source is None or not with_canon:
            self._canon = None
        else:
            self._canon = (
                _read_canon(self.canon_source) if cached else load_canon(self.canon_source)
            )
        self.canon_documents = self._canon.documents if self._canon else ()

    def keyed_plan_hash(self, plan_text: str) -> str:
        """The keyed hash (ND25, PH-KEY) of a canonical plan under the organization's key: what
        `run.started` records as `plan_hash` and a start request row carries."""
        return self.hashing.bind(None, self.org.id)(LedgerHashKind.PLAN, plan_text.encode())

    def _started_payload(
        self, planned: Sequence[PlannedTask], start: StartInfo | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "org": self.org.id,
            "chief_of_staff": self.chief,
            "tasks": [p.task.id for p in planned],
            "depends_on": {p.task.id: sorted(p.depends_on) for p in planned if p.depends_on},
        }
        if self.governance is not None or start is not None:
            payload["data_class"] = self.data_class
        if start is not None:
            hasher = self.hashing.bind(None, self.org.id)
            payload.update(
                started_by=start.started_by,
                engine=start.engine,
                idempotency_key_hash=start.idempotency_key_hash,
                plan_hash=hasher(LedgerHashKind.PLAN, start.plan_text.encode()),
                **hasher.marker(),
            )
        bound: dict[str, dict[str, str]] = {}
        for p in planned:
            preset = self._gate_preset_of(p.task.assignee)
            if preset is not None:
                bound[p.task.id] = {"id": preset.id, "hash": preset_content_hash(preset)}
        if bound:
            payload["gate_presets"] = bound
        return payload

    def _gate_preset_of(self, assignee: str | None) -> GatePreset | None:
        """The gate preset binding tasks of this agent's role (none when unbound, R5)."""
        if self.presets is None or assignee is None:
            return None
        return gate_preset_for_role(self.presets, self.org.role_of(assignee).id)

    def _preset_gate_problems(self, task: Task) -> list[str]:
        """R1/R2: every gate of the assignee's role preset is present and conforms."""
        preset = self._gate_preset_of(task.assignee)
        if preset is None:
            return []
        have = {g.id: g for g in task.definition_of_done.gates}
        problems: list[str] = []
        for t in preset.gates:
            g = have.get(t.id)
            if g is None:
                problems.append(f"gate '{t.id}': required by gate preset '{preset.id}' but missing")
                continue
            if g.kind is not t.kind:
                what = "kind"
            elif canonical_json(thaw(g.config)) != canonical_json(thaw(t.config)):
                what = "config"
            elif t.required and not g.required:
                what = "required"
            else:
                continue
            problems.append(f"gate '{t.id}': differs from gate preset '{preset.id}' ({what})")
        return problems

    def _eligibility_reasons(self, assignee: str) -> tuple[str, ...]:
        """Why the agent may not take work (empty: it may). Evals from the configuration plus
        the role's gate preset (R11-R13); nothing is checked for a role without suites."""
        role = self.org.role_of(assignee).id
        if self.presets is None:
            if self.evals is None:
                return ()
            return agent_eligibility(self.store, self.evals, self.org, agent_id=assignee).reasons
        if not effective_suites(self.evals, self.presets, role):
            return ()
        return agent_eligibility(
            self.store, self.evals, self.org, agent_id=assignee, presets=self.presets
        ).reasons

    def _governance_start(
        self, run_id: str, planned: Sequence[PlannedTask], *, resume: bool = False
    ) -> tuple[str, Report] | None:
        """The start stage of model governance (spec section 6): before `run.started` (or
        `run.resumed`) every model the run could use is resolved, and any block refuses the run
        (`ModelGovernanceError`, recorded on the organization stream; nothing else is written)."""
        if self.governance is None:
            return None
        data_class = self.governance.data_class_of(run_id) if resume else self.data_class
        plan = [
            PlanTask(
                task=p.task.id,
                assignee=p.task.assignee or "",
                verifier_gates=tuple(
                    (g.id, str(thaw(g.config).get("verifier", "")))
                    for g in p.task.definition_of_done.gates
                    if g.kind is GateKind.VERIFIER
                ),
            )
            for p in planned
        ]
        return data_class, self.governance.begin(
            run_id,
            data_class,
            plan,
            resume=resume,
            plain_provider=not isinstance(self.provider, ProviderSource),
        )

    def _governance_started(self, run_id: str, started: tuple[str, Report] | None) -> None:
        """Right after `run.started`: the connection versions the run resolved against."""
        if started is not None and self.governance is not None:
            data_class, report = started
            self.governance.record_start_use(run_id, report, data_class)

    def _check_plan(
        self, run_id: str, planned: Sequence[PlannedTask], *, resume: bool = False
    ) -> None:
        ids = [p.task.id for p in planned]
        if not ids:
            raise ValueError("a run needs at least one task")
        if len(set(ids)) != len(ids):
            raise ValueError("task ids must be unique in a run")
        if resume:
            self._check_resumable(run_id, planned)
        known = set(ids)
        for p in planned:
            if p.task.run_id != run_id:
                raise ValueError(f"task '{p.task.id}' belongs to run '{p.task.run_id}'")
            exists = self.store.get_task(p.task.id) is not None
            if not exists and p.task.status is not S.PROPOSED:
                raise ValueError(f"task '{p.task.id}' must start as proposed")
            if p.task.assignee is None:
                raise ValueError(f"task '{p.task.id}' has no assignee")
            try:
                self.org.agent(p.task.assignee)
            except KeyError:
                raise PlanRefused(
                    "plan_unknown_role", f"task '{p.task.id}': unknown agent '{p.task.assignee}'"
                ) from None
            if not self.org.can_delegate(self.chief, p.task.assignee):
                raise PlanRefused(
                    "plan_delegation_refused",
                    f"task '{p.task.id}': {self.chief} can't delegate to {p.task.assignee} "
                    "(work goes down the reporting line only)",
                )
            verifier = verifier_problems(
                p.task.definition_of_done, self.org, assignee=p.task.assignee
            )
            problems = definition_problems(p.task.definition_of_done) + verifier
            problems += self._preset_gate_problems(p.task)
            calibration = calibration_problems(
                p.task.definition_of_done,
                self.org,
                self.calibration,
                effective_ref=self.governance.effective_ref if self.governance else None,
            )
            problems += calibration
            reasons = self._eligibility_reasons(p.task.assignee) if self.presets else ()
            if reasons:
                problems.append(f"agent '{p.task.assignee}': not eligible: " + "; ".join(reasons))
            if problems:
                text = f"task '{p.task.id}': " + "; ".join(problems)
                if calibration and len(problems) == len(calibration):
                    raise PlanRefused("plan_calibration_missing", text)
                if verifier and len(problems) == len(verifier):
                    raise PlanRefused("plan_verifier_missing", text)
                raise ValueError(text)
            if not resume and exists:
                raise ValueError(f"task '{p.task.id}' already exists")
            if resume and exists:
                stored = self.store.get_task(p.task.id)
                assert stored is not None
                if (
                    stored.task.run_id != run_id
                    or stored.task.assignee != p.task.assignee
                    or stored.task.definition_of_done != p.task.definition_of_done
                ):
                    raise ValueError(
                        f"task '{p.task.id}' in the store is not the planned one "
                        "(another run, assignee or definition of done)"
                    )
            missing = [d for d in p.depends_on if d not in known or d == p.task.id]
            if missing:
                raise ValueError(f"task '{p.task.id}' depends on unknown tasks {missing}")
        # no cycles: repeatedly remove tasks whose dependencies are all removed
        left = {p.task.id: set(p.depends_on) for p in planned}
        while left:
            free = [t for t, deps in left.items() if not deps & left.keys()]
            if not free:
                raise ValueError(f"the plan has a dependency cycle among {sorted(left)}")
            for t in free:
                del left[t]

    def _check_resumable(self, run_id: str, planned: Sequence[PlannedTask]) -> None:
        """A run can be resumed once it has started and not ended, with the plan it started with,
        and (unless the caller says the old engine is gone) with no call possibly in flight."""
        started: LedgerEvent | None = None
        for event in self.store.read_stream(run_stream(run_id), after_seq=0):
            if event.type == EventType.RUN_STARTED and started is None:
                started = event
            elif event.type == EventType.RUN_ENDED:
                raise ValueError(f"run '{run_id}' has ended; it can't be resumed")
        if started is None:
            raise ValueError(f"run '{run_id}' never started; there is nothing to resume")
        if started.payload.get("org") != self.org.id:
            raise ValueError(f"run '{run_id}' belongs to another organization")
        was = set(started.payload.get("tasks", ()))
        if was != {p.task.id for p in planned}:
            raise ValueError(f"the plan's tasks are not the tasks run '{run_id}' started with")
        deps = started.payload.get("depends_on", {})
        if {p.task.id: sorted(p.depends_on) for p in planned if p.depends_on} != {
            str(k): sorted(v) for k, v in deps.items()
        }:
            raise ValueError(f"the plan's dependencies are not those run '{run_id}' started with")
        if not self._resume_gone and self._may_be_alive(run_id):
            raise ValueError(
                f"run '{run_id}' wrote to its ledger, or has a model call reserved, a moment ago; "
                "the engine that was running it may still be alive. Resume when it is gone "
                "(previous_engine_gone=True) or wait"
            )

    def _may_be_alive(self, run_id: str) -> bool:
        """The run's last ledger entry is younger than `resume_quiet`, or a call may be in flight.
        A heuristic: only an orchestrator or a lease can prove an engine is gone (R1.20 PR 3)."""
        tail = collections.deque(self.store.read_stream(run_stream(run_id)), maxlen=1)
        if tail and self.store.now() - tail[0].at < self.settings.resume_quiet:
            return True
        return self._call_in_flight(run_id)

    def _call_in_flight(self, run_id: str) -> bool:
        """An open reservation younger than `reconcile_max_age`: a call that may still be running
        (the age is that of its `budget.reserved` entry, as the reconciler reads it)."""
        open_ids = {
            r.reservation.id
            for r in self.store.reservations_for_run(run_id, ReservationStatus.OPEN)
        }
        if not open_ids:
            return False
        now = self.store.now()
        return any(
            now - e.at <= self.settings.reconcile_max_age
            for e in self.store.read_stream(run_stream(run_id))
            if e.type == EventType.BUDGET_RESERVED and str(e.payload.get("reservation")) in open_ids
        )

    def _schedule(self, run_id: str, planned: Sequence[PlannedTask]) -> dict[str, _Outcome]:
        deps = {p.task.id: set(p.depends_on) for p in planned}
        outcomes: dict[str, _Outcome] = {}
        running: dict[Future[_Outcome], str] = {}
        pool = ThreadPoolExecutor(max_workers=self.settings.max_parallel)
        try:
            while len(outcomes) < len(planned):
                try:
                    for p in planned:
                        tid = p.task.id
                        if tid in outcomes or tid in running.values():
                            continue
                        if self._stop_check():  # nothing new starts; waiting tasks are cancelled
                            self._fire_hooks()
                            outcomes[tid] = self._stopped(tid)
                            continue
                        bad = blocking_dependency(
                            {d: outcomes[d].settled for d in deps[tid] if d in outcomes}
                        )
                        if bad is not None:
                            outcomes[tid] = self._cancel(
                                tid, self._dependency_reason(bad, outcomes[bad])
                            )
                            continue
                        if self._role_stopped(tid, self._role_of_agent(p.task.assignee) or ""):
                            outcomes[tid] = self._role_cancelled(run_id, tid)
                            continue
                        if all(
                            outcomes.get(d, _Outcome(S.PROPOSED)).status is S.DONE
                            for d in deps[tid]
                        ):
                            running[pool.submit(self._safe_task, run_id, tid)] = tid
                    if not running:
                        continue
                    done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                    for future in done:
                        tid = running.pop(future)
                        try:
                            outcomes[tid] = future.result()
                        except Exception as exc:  # _safe_task catches; this is a last guard
                            if is_gate_refusal(exc):
                                raise
                            outcomes[tid] = self._incident(
                                tid, f"engine error ({type(exc).__name__})"
                            )
                except KeyboardInterrupt:
                    # the first Ctrl-C (anywhere in the loop) asks for the same clean stop
                    # (recorded); the second aborts: stop waiting for workers that don't return
                    self._interrupts += 1
                    if self._interrupts > 1:
                        self._fire_hooks()
                        raise
                    self._interrupted()
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)  # don't wait for a hung worker
            raise
        pool.shutdown(wait=True)
        return outcomes

    @property
    def stop_hooks_fired(self) -> bool:
        """Whether the stop hooks already ran (a resource opened later must cancel itself)."""
        return self._hooks_fired

    def _abort_cleanup(
        self,
        planned: Sequence[PlannedTask],
        outcomes: dict[str, _Outcome],
        reason: str | None = None,
    ) -> None:
        """After a second Ctrl-C, best effort: cancel what is unfinished and rebuild every task's
        outcome from the store (so `run.ended` counts all of them). Open reservations are left
        alone on purpose: a hung worker may still be inside a provider call that will settle
        them; the reconciler closes what is left."""
        self._aborted = True
        for p in planned:
            tid = p.task.id
            try:
                stored = self.store.get_task(tid)
                if stored is not None and stored.task.status not in (
                    S.DONE,
                    S.CANCELLED,
                    S.NEEDS_HUMAN,
                ):
                    self._cancel(tid, reason or "cancelled: the run was aborted (second Ctrl-C)")
                    stored = self.store.get_task(tid)
                if stored is not None:
                    outcomes[tid] = _Outcome(stored.task.status)
            except Exception:  # noqa: S112 (best effort)
                continue

    def _interrupted(self) -> None:
        """Record a Ctrl-C as a stop request by the terminal's user and tell the hooks."""
        assert self._stop is not None
        actor = self.settings.interrupt_actor or "service:cli"  # never a human by default
        self._stop.record_interrupt(actor)  # never raises: the request is kept in memory at worst
        self._fire_hooks()

    def _cancel(
        self, task_id: str, reason: str, payload_extra: Mapping[str, Any] | None = None
    ) -> _Outcome:
        try:
            stored = self.store.get_task(task_id)
            if stored is not None and stored.task.status in (S.CANCELLED, S.DONE):
                return _Outcome(stored.task.status, reason)  # settled before a restart
            move_task(
                self.store,
                task_id,
                S.CANCELLED,
                actor=self.settings.actor,
                reason=reason,
                in_transaction=self._finish_output_hook,
                payload_extra=payload_extra,
                org_id=self.org.id,
                hashing=self.hashing,
            )
        except Exception as exc:
            return self._incident(task_id, f"{reason}; cancelling failed ({type(exc).__name__})")
        return _Outcome(S.CANCELLED, reason)

    def _safe_task(self, run_id: str, task_id: str) -> _Outcome:
        try:
            return self._run_task(run_id, task_id)
        except RolePausedStop:
            return self._role_cancelled(run_id, task_id)
        except RunStopped:
            return self._stopped(task_id)
        except ContentError as exc:
            if is_gate_refusal(exc):
                raise  # the run is aborted by `_run` (R1.37): nobody in a retired org can decide
            return self._to_human(task_id, f"engine error ({type(exc).__name__})")
        except Exception as exc:  # an engine-side failure: put the task in front of a human
            note = f"engine error ({type(exc).__name__})"
            return self._to_human(task_id, note)

    def _role_cancelled(self, run_id: str, task_id: str) -> _Outcome:
        """Cancel a task because its role is paused (E19): its open reservations are given back,
        the reason is `role_paused`. The run goes on. A reservation of a call that is still
        mid-call (a worker that fans out) is left open: that call settles it itself, so its spend
        is recorded normally; the run-end release and the reconciler close it if the call dies."""
        for stored in self.store.reservations_for_run(run_id, ReservationStatus.OPEN):
            if stored.reservation.context.task_id != task_id:
                continue
            with self._in_flight_lock:
                if stored.reservation.id in self._in_flight:
                    continue
            try:
                release_reservation(
                    self.store,
                    stored.reservation.id,
                    actor=self.settings.actor,
                    reason="role paused",
                    details={"role_paused": True},
                )
            except Exception:  # noqa: S112 (the reconciler closes what is left)
                continue
        return self._cancel(task_id, ROLE_PAUSED_REASON, {"reason_code": ROLE_PAUSED_REASON})

    def _dependency_reason(self, dependency: str, outcome: _Outcome) -> str:
        """Why a dependent is cancelled: `role_paused` follows the dependency (spec 5)."""
        if outcome.status is S.CANCELLED and outcome.note == ROLE_PAUSED_REASON:
            return ROLE_PAUSED_REASON
        return dependency_cancel_reason(dependency, outcome.settled)

    # Shortest allowed path to `needs_human` from each status (core.tasks table).
    _PATH_TO_HUMAN: dict[TaskStatus, tuple[TaskStatus, ...]] = {
        S.PROPOSED: (S.NEEDS_HUMAN,),
        S.ASSIGNED: (S.NEEDS_HUMAN,),
        S.IN_PROGRESS: (S.BLOCKED, S.NEEDS_HUMAN),
        S.SUBMITTED: (S.VERIFYING, S.BLOCKED, S.NEEDS_HUMAN),
        S.VERIFYING: (S.BLOCKED, S.NEEDS_HUMAN),
        S.BLOCKED: (S.NEEDS_HUMAN,),
    }

    def _to_human(
        self, task_id: str, note: str, attempts: int = 3, *, reason_code: str | None = None
    ) -> _Outcome:
        """Put a task in front of a human (`needs_human`), with the note as the ledger reason
        (and, if given, a fixed `reason_code` in the entry).

        Each move is retried; if the task still can't be moved, an incident is recorded in the
        run's ledger (`decision.recorded`), so a stuck task always has a recorded reason.
        """
        for _ in range(max(1, attempts)):
            try:
                stored = self.store.get_task(task_id)
                if stored is None:
                    break
                status = stored.task.status
                if status in (S.NEEDS_HUMAN, S.DONE, S.CANCELLED):
                    return _Outcome(status, note)
                for step in self._PATH_TO_HUMAN.get(status, ()):
                    move_task(
                        self.store,
                        task_id,
                        step,
                        actor=self.settings.actor,
                        reason=note,
                        org_id=self.org.id,
                        hashing=self.hashing,
                        payload_extra=None if reason_code is None else {"reason_code": reason_code},
                    )
                return _Outcome(S.NEEDS_HUMAN, note)
            except Exception:  # noqa: S112 (retried; recorded as an incident below)
                continue
        return self._incident(task_id, note)

    def _incident(self, task_id: str, note: str) -> _Outcome:
        """Record that a task couldn't be moved where it should go; return where it is."""
        status: TaskStatus | None = None
        try:
            stored = self.store.get_task(task_id)
            status = stored.task.status if stored else None
        except Exception:  # noqa: S110 (status stays unknown)
            pass
        shown = status.value if status else "unknown"
        if self._run_id:
            try:
                with self.store.transaction() as uow:
                    uow.append(
                        EventDraft(
                            stream=run_stream(self._run_id),
                            type=EventType.DECISION_RECORDED,
                            actor=self.settings.actor,
                            run_id=self._run_id,
                            task_id=task_id,
                            payload={
                                "kind": "engine_incident",
                                "task_status": shown,
                                "note": note,
                                "human_required": True,
                            },
                        )
                    )
            except Exception:  # noqa: S110 (the store itself is down; the report still says it)
                pass
        return _Outcome(status or S.PROPOSED, f"{note} (stuck in {shown}; incident recorded)")

    def _run_task(self, run_id: str, task_id: str) -> _Outcome:
        """Run one task from wherever the store says it is (R1.20 PR 2, `engine/steps.py`): a
        fresh task from `start`, a resumed one from the step it was in. A step the store shows as
        acknowledged is never run again; one a crash cut short runs again in a new epoch."""
        progress = read_progress(self.store, task_id)
        stored = self.store.get_task(task_id)
        assert stored is not None
        task = stored.task
        if task.status is S.BLOCKED:  # a crash cut `_to_human` short: finish the move
            return self._to_human(task_id, "a crash left the task blocked; it waits for a human")
        if progress.next_step is None:  # final, or already in front of a human
            return _Outcome(
                task.status, "" if task.status in (S.DONE, S.CANCELLED) else "waiting for a human"
            )
        if task.status is S.VERIFYING:
            waiting = self._waiting_for_humans(task)
            if waiting:  # its paid gates are done; only a person is missing: nothing to repeat
                return _Outcome(S.VERIFYING, f"waiting for a human: {', '.join(waiting)}")
        assignee = task.assignee
        assert assignee is not None
        cos = self.chief
        self._task_checkpoint(task)  # before the task starts
        if task.status is S.VERIFYING and self.content is not None and self.approver is None:
            routed = self._evidence_route(run_id, task)  # R1.32 G6: None leaves main's path
            if routed is not None:
                routed_outcome, next_feedback = routed
                if routed_outcome is not None:
                    return routed_outcome
                again = self.store.get_task(task_id)  # a rejection: _decide started attempt 2
                assert again is not None
                return self._attempts(
                    run_id, task_id, again.task, self.workers.get(assignee), assignee,
                    next_feedback, 0, 0, None, False,
                )  # fmt: skip
        if task.status is S.PROPOSED:
            refusal = self._delegation_refusal(run_id, task, cos, assignee)
            if refusal:
                return self._to_human(task_id, refusal)
            move_task(
                self.store,
                task_id,
                S.ASSIGNED,
                actor=f"agent:{cos}",
                reason=f"delegated by {cos}",
                assignee=assignee,
                org_id=self.org.id,
                hashing=self.hashing,
            )
        feedback: tuple[str, ...] = ()
        worker = self.workers.get(assignee)
        if worker is None and task.status in (S.PROPOSED, S.ASSIGNED, S.IN_PROGRESS):
            return self._to_human(task_id, f"no worker for agent '{assignee}'")
        work_epoch = 0
        gate_epoch = 0
        output: Any = None
        have_output = False
        if task.status in (S.PROPOSED, S.ASSIGNED):
            current, _ = move_task(
                self.store,
                task_id,
                S.IN_PROGRESS,
                actor=f"agent:{assignee}",
                reason="working",
                org_id=self.org.id,
                hashing=self.hashing,
            )
            task = current.task
        elif task.status is S.IN_PROGRESS:  # a crash cut the work short: again, in a new epoch
            work_epoch = self._record_restart(run_id, task, Step.WORK, progress.epochs)
            feedback_kept = self._stored_feedback(progress.feedback_hash)
            if feedback_kept is None:
                return self._to_human(
                    task_id, "the stored gate feedback is gone; the next attempt can't start"
                )
            feedback = feedback_kept
        else:  # submitted or verifying: the output is stored; the worker is not called again
            loaded = self._stored_output(task, progress.output_hash)
            if loaded is None:
                return self._to_human(task_id, "the stored output is gone; it can't be resumed")
            output, have_output = loaded[0], True
            if task.status is S.SUBMITTED:
                current, _ = move_task(
                    self.store,
                    task_id,
                    S.VERIFYING,
                    actor=self.settings.actor,
                    reason="running gates",
                    org_id=self.org.id,
                    hashing=self.hashing,
                )
                task = current.task
            else:
                gate_epoch = self._record_restart(run_id, task, Step.GATES, progress.epochs)
        return self._attempts(
            run_id, task_id, task, worker, assignee, feedback, work_epoch, gate_epoch, output,
            have_output,
        )  # fmt: skip

    def _attempts(
        self,
        run_id: str,
        task_id: str,
        task: Task,
        worker: Any,
        assignee: str,
        feedback: tuple[str, ...],
        work_epoch: int,
        gate_epoch: int,
        output: Any,
        have_output: bool,
    ) -> _Outcome:
        """The attempt loop of `_run_task`: work, gates, decide, and the next attempt after a
        rejection. Entered with the state the step reached (a resumed step, or a rejection
        decided from the stored evidence, R1.32)."""
        while True:
            self._task_checkpoint(task)  # before each attempt
            if not have_output:
                if worker is None:
                    return self._to_human(task_id, f"no worker for agent '{assignee}'")
                try:
                    shown = self._shown(task)
                except PlanTextUnavailable:
                    return self._text_gone(task_id)
                work = WorkContext(self, run_id, shown, feedback, work_epoch)
                try:
                    output = worker(shown, work)
                except RunStopped:
                    raise
                except BudgetExceededError as exc:
                    budgets = sorted({sh.budget_id for sh in exc.shortfalls})
                    return self._to_human(
                        task_id, f"budget hard stop ({', '.join(budgets) or 'no budget'})"
                    )
                except ApprovalRequired as exc:
                    return self._to_human(task_id, f"waiting for approval {exc.approval_id}")
                except NoUsableAnswer:
                    return self._to_human(
                        task_id, NoUsableAnswer.reason, reason_code=NO_USABLE_ANSWER
                    )
                except NeedsHuman as exc:
                    reason = type(exc).reason
                    if not isinstance(reason, str) or not reason.strip():
                        reason = NeedsHuman.reason
                    return self._to_human(task_id, reason[:200])
                except ModelBlocked as exc:
                    return self._to_human(task_id, f"model blocked [{exc.reason}]: {exc.detail}")
                except PolicyDenied as exc:
                    return self._to_human(task_id, f"policy refused ({type(exc).__name__})")
                except Exception as exc:
                    self._task_checkpoint(task)  # a worker that failed because it was stopped
                    if isinstance(exc, ModelCallError) and exc.code:  # a refused stored key
                        return self._to_human(task_id, f"model key refused ({exc.code})")
                    return self._to_human(task_id, f"worker failed ({type(exc).__name__})")
                self._task_checkpoint(task)  # a worker that swallowed the stop and returned
                if work.refusals:  # the worker caught a refusal and went on: its output can't count
                    first = sorted(work.refusals.values())[0]
                    if NO_USABLE_ANSWER in work.refusals.values():
                        return self._to_human(
                            task_id, NoUsableAnswer.reason, reason_code=NO_USABLE_ANSWER
                        )
                    return self._to_human(task_id, f"the worker went on after: {first}")
                problem = _unsafe_number(output)
                if problem is not None:  # an error, never a person's job (ND87 amends ND78)
                    message, path = problem
                    return self._cancel(
                        task_id,
                        f"output error: {message}",
                        {"reason_code": OUTPUT_ERROR_CODE, "path": path},
                    )
                try:
                    move_task(
                        self.store,
                        task_id,
                        S.SUBMITTED,
                        actor=f"agent:{assignee}",
                        reason="output submitted",
                        in_transaction=self._keep_output_hook(output),
                        org_id=self.org.id,
                        hashing=self.hashing,
                    )
                except ContentError as exc:
                    if is_gate_refusal(exc):
                        raise  # a destroyed or missing key ends the run (R1.37), never a human job
                    # too big, or not JSON: refused, never truncated
                    return self._to_human(task_id, "the output can't be kept (too big or not JSON)")
                current, _ = move_task(
                    self.store,
                    task_id,
                    S.VERIFYING,
                    actor=self.settings.actor,
                    reason="running gates",
                    org_id=self.org.id,
                    hashing=self.hashing,
                )
            else:
                again = self.store.get_task(task_id)
                assert again is not None
                current = again
            try:
                results = self._run_gates(run_id, current.task, output, gate_epoch)
            except _DecisionGone:  # expired or shredded while the run waited: a person decides
                return self._to_human(task_id, DECISION_GONE)
            except PlanTextUnavailable:  # no gate ran against a placeholder (R41)
                return self._text_gone(task_id)
            outcome, feedback = self._decide(current.task, results)
            if outcome is not None:
                return outcome
            again = self.store.get_task(task_id)  # _decide started the next attempt
            assert again is not None
            task = again.task
            have_output, work_epoch, gate_epoch = False, 0, 0

    def _waiting_for_humans(self, task: Task) -> list[str]:
        """The human gates a `verifying` task is waiting for, if that is **all** it is waiting for:
        every other required gate has a **passed** result recorded for this round (so the crash did
        not cut the gates short; running them again would pay the verifiers again for nothing). A
        failed or errored gate is not waiting for a person: the round is decided again. Empty
        otherwise."""
        if self.approver is not None:  # a person may answer right now: ask again (state in T63)
            return []
        latest: dict[str, str] = {}  # the latest recorded outcome of each gate in this round
        for e in self.store.task_events(task.run_id, task.id):
            if (
                e.type == EventType.GATE_COMPLETED
                and e.payload.get("attempt") == task.attempt
                and e.payload.get("submission") == task.submission
            ):
                latest[str(e.payload["gate"])] = str(e.payload.get("outcome"))
        required = task.definition_of_done.required_gates
        if any(latest.get(g.id) == "failed" for g in required):
            return []  # a rejection (a person's too) is acted on: the round is decided again
        decided = self._decided_gates(task)  # a durable run's person answered: gates run again
        humans = [
            g.id
            for g in required
            if g.kind is GateKind.HUMAN
            and latest.get(g.id) not in ("passed", "failed")
            and g.id not in decided
        ]
        others = [
            g.id for g in required if g.kind is not GateKind.HUMAN and latest.get(g.id) != "passed"
        ]
        return humans if humans and not others else []

    def _record_restart(
        self, run_id: str, task: Task, step: Step, epochs: Mapping[Step, int]
    ) -> int:
        """A step a crash cut short begins again: the new epoch is written to the ledger first (so
        a second crash counts it too). Returns the epoch."""
        epoch = epochs.get(step, 0) + 1
        with self.store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=run_stream(run_id),
                    type=EventType.DECISION_RECORDED,
                    actor=self.settings.actor,
                    run_id=run_id,
                    task_id=task.id,
                    payload={
                        "kind": RESTART_KIND,
                        "step": step.value,
                        "attempt": task.attempt,
                        "epoch": epoch,
                        "key": step_key(run_id, task.id, task.attempt, step, epoch),
                    },
                )
            )
        return epoch

    def _stored_output(self, task: Task, output_hash: str | None) -> tuple[Any] | None:
        """The output a durable run kept for this attempt, or None if there is none to read
        (an in-process run keeps nothing, so it can't resume past `submitted`)."""
        if self.content is None or output_hash is None:
            return None
        try:
            text = self.content.read_unledgered(self.org.id, output_hash)
            if text is None:
                return None
            return (read_jcs(text)["output"],)  # 1e20 reads back as the float it was (ND78)
        except (ContentError, ValueError, KeyError, TypeError, RecursionError):
            return None

    def _delegation_refusal(self, run_id: str, task: Task, cos: str, assignee: str) -> str:
        if assignee != cos and not self.org.can_delegate(cos, assignee):
            return f"{cos} can't delegate to {assignee} (not in its reporting line)"
        check = check_action(
            self.store,
            self.policies,
            self.org,
            CallContext(run_id=run_id, task_id=task.id, agent_id=cos),
            Action(kind=ActionKind.DELEGATE, target=assignee),
            hasher=self.hashing.bind(None, self.org.id),
            actor=self.settings.actor,
            content=self.content,
            required_suites=required_suites_of(self.evals),
        )
        if not check.allowed:
            if check.decision.decision is Decision.ASK:
                return f"delegation waits for approval {check.approval_id}"
            return f"delegation refused by policy: {check.decision.reason}"[:300]
        # Eligibility only (R16). The gates are the ones stored at `begin_run`: a step never
        # re-validates them against the current presets (R5, R5a, C60).
        reasons = self._eligibility_reasons(assignee)
        if reasons:
            return "not eligible: " + "; ".join(reasons)[:280]
        return ""

    # -- gates -------------------------------------------------------------------------------

    def _role_name(self, task: Task) -> str | None:
        """The role of an API task's assignee, for its `task.created` entry (none otherwise)."""
        if task.text_ref is None or task.assignee is None:
            return None
        return self.org.role_of(task.assignee).id

    def _shown(self, task: Task) -> Task:
        """The task with its real text for the worker, verifier or human gate (an API task reads
        it from the content store); `PlanTextUnavailable` if it is gone. Never stored."""
        return resolve_task_text(self.content, self.org.id, task)

    def _text_gone(self, task_id: str) -> _Outcome:
        """The plan text of an API task is gone: a person decides, nothing runs (R41)."""
        return self._to_human(
            task_id,
            "the plan text is no longer available",
            reason_code=PLAN_TEXT_UNAVAILABLE,
        )

    def _extend_plan_text(self, task: Task, spec: GateSpec) -> None:
        """When a task's wait begins, every plan-text row of its run lives at least `WORKING_DAYS`
        from that wait's own start (RET-PT): `max(current, start + 90 days)`, never shorter. The
        start is the open `gate.started` of the round, so a re-pass of the same wait (a warning
        pass) computes the same value and extends nothing."""
        content = self.content
        if content is None or task.text_ref is None:
            return
        with self.store.transaction() as uow:
            began = self.store.now()
            key = (spec.id, task.attempt, task.submission)
            for event in uow.task_events(task.run_id, task.id):
                if (
                    event.type == EventType.GATE_STARTED
                    and (
                        event.payload.get("gate"),
                        event.payload.get("attempt"),
                        event.payload.get("submission"),
                    )
                    == key
                ):
                    began = event.at
                elif (
                    event.type == EventType.GATE_COMPLETED
                    and (
                        event.payload.get("gate"),
                        event.payload.get("attempt"),
                        event.payload.get("submission"),
                    )
                    == key
                ):
                    began = self.store.now()
            at = began + timedelta(days=WORKING_DAYS)
            ids: set[str] = set()
            for stored in uow.tasks_for_run(task.run_id):
                if stored.task.text_ref is not None:
                    ids.update(stored.task.text_ref.ids)
            content.extend_plan_text(uow, self.org.id, ids, at=at)

    def _plan_text_at_run_end(self, uow: UnitOfWork, run_id: str) -> None:
        """RET-PT 4.3(a): at `run.ended` every plan-text row of the run lives to
        `max(current, end + output retention)`, so the kept output never outlives its definition
        of done. In the transaction that writes `run.ended`."""
        content = self.content
        if content is None:
            return
        ids: set[str] = set()
        for stored in uow.tasks_for_run(run_id):
            if stored.task.text_ref is not None:
                ids.update(stored.task.text_ref.ids)
        if not ids:
            return
        at = self.store.now() + timedelta(days=self.content_settings.retention_days)
        content.extend_plan_text(uow, self.org.id, ids, at=at)

    def _run_gates(self, run_id: str, task: Task, output: Any, epoch: int = 0) -> list[GateResult]:
        shown = self._shown(task)  # before any gate: a gone text fails closed (R41)
        results: list[GateResult] = []
        cheap_failed = False
        for spec in task.definition_of_done.run_order():
            if spec.kind not in DETERMINISTIC_KINDS and cheap_failed:
                continue  # D.5: don't pay for verifiers when a deterministic gate already failed
            self._task_checkpoint(task)  # between gates
            record_gate_started(  # E3: the span of this gate starts here; an open start is reused
                self.store,
                task,
                spec,
                actor=self.settings.actor,
                role=self._role_of_agent(task.assignee),
            )
            result = self._run_gate(run_id, shown, spec, output, epoch)
            if result is None:
                self._task_checkpoint(task)
                continue
            actor = result.checked_by if spec.kind is GateKind.HUMAN else self.settings.actor
            keep: Callable[[UnitOfWork, GateResult], Mapping[str, Any]] | None = None
            content = self.content
            if content is not None and self._keeps_evidence(task, spec, result):
                text = content.gate_evidence_text(
                    run_id=task.run_id,
                    task_id=task.id,
                    attempt=task.attempt,
                    submission=task.submission,
                    gate=spec.id,
                    result=result,
                )
                if len(text.encode("utf-8")) > MAX_TEXT_BYTES:  # never truncated (G2)
                    result = error_result(
                        spec,
                        checked_by=result.checked_by,
                        message=EVIDENCE_TOO_LARGE,
                        task_id=task.id,
                        attempt=task.attempt,
                        submission=task.submission,
                    )
                else:
                    keep = self._keep_evidence_hook(task, spec)
            record_gate_result(  # as produced, paid or not
                self.store,
                result,
                actor=actor,
                allow_recorded=bool(epoch),
                role=self._role_of_agent(task.assignee),
                org_id=self.org.id,
                hashing=self.hashing,
                in_transaction=keep,
            )
            results.append(result)
            # a stop (or a role pause) that landed during the gate applies after it is recorded
            self._task_checkpoint(task)
            if (
                spec.kind in DETERMINISTIC_KINDS
                and spec.required
                and result.outcome is not GateOutcome.PASSED
            ):
                cheap_failed = True  # failed or errored: the round can't pass, don't pay more
        return results

    def _keeps_evidence(self, task: Task, spec: GateSpec, result: GateResult) -> bool:
        """R1.32 G1: a passed result of a required non-human gate is kept when the round can wait
        for a person (a content service, no approver, a required human gate)."""
        return (
            self.content is not None
            and self.approver is None
            and spec.required
            and spec.kind is not GateKind.HUMAN
            and result.outcome is GateOutcome.PASSED
            and any(g.kind is GateKind.HUMAN and g.required for g in task.definition_of_done.gates)
        )

    def _keep_evidence_hook(
        self, task: Task, spec: GateSpec
    ) -> Callable[[UnitOfWork, GateResult], Mapping[str, Any]]:
        """For `record_gate_result`: keep the result (kind `gate_evidence`) inside the same
        transaction and name it by its keyed content id in the `gate.completed` entry."""
        content = self.content
        assert content is not None

        def keep(uow: UnitOfWork, result: GateResult) -> Mapping[str, Any]:
            kept = content.capture_gate_result(
                uow,
                self.org.id,
                result,
                run_id=task.run_id,
                task_id=task.id,
                attempt=result.attempt,
                submission=result.submission,
                gate=spec.id,
            )
            return {"evidence_hash": kept}

        return keep

    # -- completing a decided round from the stored evidence (R1.32 section 4) -----------------

    def _latest_gate_entries(self, task: Task) -> dict[str, LedgerEvent]:
        """The latest `gate.completed` entry of each gate in this round (ledger order)."""
        latest: dict[str, LedgerEvent] = {}
        for e in self.store.task_events(task.run_id, task.id):
            if (
                e.type == EventType.GATE_COMPLETED
                and e.payload.get("attempt") == task.attempt
                and e.payload.get("submission") == task.submission
            ):
                latest[str(e.payload["gate"])] = e
        return latest

    def _evidence_route(
        self, run_id: str, task: Task
    ) -> tuple[_Outcome | None, tuple[str, ...]] | None:
        """G6: finish a decided round from the stored results, without running a gate again.
        None leaves the step to main's path (a cut-short gates step, a failed or errored latest
        result, a round with no required human gate, a human gate nobody decided): the gates run
        again, as after any crash (ND55). Otherwise `(outcome, feedback)` like `_decide`."""
        content = self.content
        assert content is not None
        required = task.definition_of_done.required_gates
        humans = [g for g in required if g.kind is GateKind.HUMAN]
        others = [g for g in required if g.kind is not GateKind.HUMAN]
        if not humans:
            return None
        content.require_key_present(self.org.id)  # step 0: never "gone"
        latest = self._latest_gate_entries(task)
        for g in others:
            entry = latest.get(g.id)
            if entry is None or entry.payload.get("outcome") != "passed":
                return None  # steps 1 (a) and (b): main's existing path
        if any(g.id not in self._decided_gates(task) for g in humans):
            return None
        return self._complete_from_evidence(run_id, task)

    def _complete_from_evidence(
        self, run_id: str, task: Task
    ) -> tuple[_Outcome | None, tuple[str, ...]]:
        """Steps 2 to 5 of G6. Raises `GateError` unless every required human gate has a stored
        decision (G37)."""
        content = self.content
        assert content is not None
        required = task.definition_of_done.required_gates
        humans = [g for g in required if g.kind is GateKind.HUMAN]
        others = [g for g in required if g.kind is not GateKind.HUMAN]
        decided = self._decided_gates(task)
        if not humans or any(g.id not in decided for g in humans):
            raise GateError("a human gate is open")
        decisions: dict[str, HumanDecision] = {}
        for g in humans:  # step 2: the decision
            try:
                found = self._recorded_decision(task, g, strict=True)
            except LookupError:
                found = None
            if found is None:
                return self._to_human(task.id, DECISION_GONE), ()
            decisions[g.id] = found
        latest = self._latest_gate_entries(task)
        results: list[GateResult] = []
        for g in others:  # step 3: the evidence
            read = self._read_evidence(task, g, latest.get(g.id))
            if read is None:
                return self._to_human(task.id, EVIDENCE_GONE), ()  # step 5: fail closed
            results.append(read)
        try:  # step 4: the stored decision is applied as today, then the round is decided
            for g in humans:
                decision = decisions[g.id]
                human = decide_human_gate(
                    g,
                    decided_by=decision.decided_by,
                    findings=decision.findings,
                    task_id=task.id,
                    attempt=task.attempt,
                    submission=task.submission,
                )
                record_gate_result(
                    self.store,
                    human,
                    actor=human.checked_by,
                    allow_recorded=True,
                    role=self._role_of_agent(task.assignee),
                    org_id=self.org.id,
                    hashing=self.hashing,
                    in_transaction=None,
                )
                results.append(human)
            return self._decide(task, results)
        except (GateError, TaskTransitionError, ConcurrencyError):
            again = self.store.get_task(task.id)  # a second worker got there first
            if again is not None and again.task.status in FINAL:
                return _Outcome(again.task.status), ()
            raise

    def _read_evidence(
        self, task: Task, spec: GateSpec, entry: LedgerEvent | None
    ) -> GateResult | None:
        """G6.3: the stored result of one gate, or None when it is gone (missing, altered,
        another round's, or not what the entry says). A key problem raises its gate refusal."""
        content = self.content
        assert content is not None and entry is not None
        payload = entry.payload
        wanted = payload.get("evidence_hash")
        if not isinstance(wanted, str) or "hash_scheme" not in payload or "hash_org" not in payload:
            return None
        org = self.org.id
        try:  # (i)
            text = content.read_unledgered(org, wanted)
        except ContentError as exc:
            if is_gate_refusal(exc):
                raise
            return None
        if text is None:  # (ii)
            content.require_key_present(org)
            return None
        try:  # (iii) and (iv)
            body = json.loads(text)
            result = GateResult.model_validate(body["result"])
            shape = {
                "run_id": task.run_id,
                "task_id": task.id,
                "attempt": task.attempt,
                "submission": task.submission,
                "gate": spec.id,
            }
            if set(body) != {*shape, "result"} or any(body[k] != v for k, v in shape.items()):
                return None
            if (result.task_id, result.gate_id, result.attempt, result.submission) != (
                task.id,
                spec.id,
                task.attempt,
                task.submission,
            ):
                return None
        except (ValueError, KeyError, TypeError, RecursionError):
            return None
        if content.hash_of(org, ContentKind.GATE_EVIDENCE, text) != wanted:  # (v)
            return None
        master = content.keys.master(org)
        if master is None:
            content.require_key_present(org)
            return None
        if payload.get("hash_key_id") != key_id(master):  # (vi)
            raise OrphanedContentError(org, mismatch=True)
        digest = self.hashing.bind(None, org).use(  # (vii)
            LedgerHashKind.GATE_RESULT, result_bytes(result)
        )
        if digest != payload.get("result_hash"):
            return None
        return result

    def _run_gate(
        self, run_id: str, task: Task, spec: GateSpec, output: Any, epoch: int = 0
    ) -> GateResult | None:
        if spec.kind is GateKind.RULE:
            return run_rule_gate(
                spec, output, task_id=task.id, attempt=task.attempt, submission=task.submission
            )
        if spec.kind is GateKind.SCHEMA:
            return run_schema_gate(
                spec, output, task_id=task.id, attempt=task.attempt, submission=task.submission
            )
        if spec.kind is GateKind.VERIFIER:
            control = self._new_control()
            try:
                return run_verifier_gate(
                    self.store,
                    self.provider,
                    task.id,
                    spec.id,
                    output,
                    org=self.org,
                    prices=self.price_book(),
                    reservation_id=(
                        f"{task.id}-{spec.id}-a{task.attempt}-s{task.submission}"
                        f"{_epoch_part(epoch)}"
                    ),
                    actor=self.settings.actor,
                    authorize=self.policy.authorize,
                    authorize_spend=self.policy.authorize_spend,
                    canon=self.canon_documents,
                    overrides=self.overrides,
                    governance=self.governance,
                    watch=self._halt_watch,
                    hasher=self.hashing.bind(None, self.org.id),
                    calibration=self.calibration,
                    shown=self._shown,
                    control=control,
                )
            except CallCancelled:  # R27: a stop cut the verifier's call; no gate result
                self._stop_check()
                self._fire_hooks()
                raise RunStopped from None
            except OrgHalted:  # halted between the checkpoint and the verifier call (E19, 3.3)
                self._stop_on_halt()  # nothing was reserved or spent: the same clean stop
            finally:
                self._drop_control(control)
        if spec.kind is GateKind.HUMAN:
            decision = self.approver(task, spec, output) if self.approver is not None else None
            if decision is None:
                try:  # a durable run: the person answered while the run waited (T72)
                    decision = self._recorded_decision(task, spec)
                except LookupError:
                    raise _DecisionGone from None
            if decision is None:
                self._extend_plan_text(task, spec)  # the task waits for a person (RET-PT)
                return None
            return decide_human_gate(
                spec,
                decided_by=decision.decided_by,
                findings=decision.findings,
                task_id=task.id,
                attempt=task.attempt,
                submission=task.submission,
            )
        runner = self.gate_runners.get(spec.kind)
        if runner is None:
            return error_result(
                spec,
                checked_by=f"gate:{spec.kind.value}",
                message=f"no runner for {spec.kind.value} gates is configured",
                task_id=task.id,
                attempt=task.attempt,
                submission=task.submission,
            )
        self.refuse_action_when_halted()  # a gate runner executes (a command gate: a command)
        try:
            result = runner(spec, task, output)
            round_ = (result.task_id, result.gate_id, result.attempt, result.submission)
            if round_ != (task.id, spec.id, task.attempt, task.submission):
                return error_result(
                    spec,
                    checked_by=f"gate:{spec.kind.value}",
                    message="the gate runner returned a result for another gate or round",
                    task_id=task.id,
                    attempt=task.attempt,
                    submission=task.submission,
                )
            return result
        except CommandCancelled as cut:  # R27, C37s: a stop or a halt cut the command
            stopped = self._stop_check()
            # R27 is for the light runner only (decision note x-963-pr3-cut-gate): a real-capable
            # runner (`runner_calls`), or a cut of the sandbox service, which only a runner uses
            # (`--fake` included)
            in_scope = self.settings.runner_calls or cut.isolation is Isolation.SERVICE
            if stopped and in_scope:
                self._fire_hooks()
                raise RunStopped from None  # no gate result
            return error_result(  # OCI or host cut (rows 17, 29), or no stop: an error result
                spec,
                checked_by=f"gate:{spec.kind.value}",
                message=(
                    "the command was cut by a stop or a halt"
                    if stopped
                    else "the command was cancelled and no stop or halt is recorded"
                ),
                task_id=task.id,
                attempt=task.attempt,
                submission=task.submission,
            )
        except Exception as exc:
            return error_result(
                spec,
                checked_by=f"gate:{spec.kind.value}",
                message=f"the gate runner failed ({type(exc).__name__})",
                task_id=task.id,
                attempt=task.attempt,
                submission=task.submission,
            )

    def _decide(
        self, task: Task, results: list[GateResult]
    ) -> tuple[_Outcome | None, tuple[str, ...]]:
        """Move the task after its gates; returns (final outcome or None to retry, feedback)."""
        by_gate = {r.gate_id: r for r in results}
        required = task.definition_of_done.required_gates
        failed = [
            g for g in required if g.id in by_gate and by_gate[g.id].outcome is GateOutcome.FAILED
        ]
        errored = [
            g for g in required if g.id in by_gate and by_gate[g.id].outcome is GateOutcome.ERROR
        ]
        missing = [g for g in required if g.id not in by_gate]
        actor = self.settings.actor
        if not failed and not errored and not missing:
            move_task(
                self.store,
                task.id,
                S.DONE,
                actor=actor,
                reason="all required gates passed",
                gate_results=results,
                in_transaction=self._finish_output_hook,
                org_id=self.org.id,
                hashing=self.hashing,
            )
            return _Outcome(S.DONE), ()
        if failed:
            feedback = tuple(
                f"{r.gate_id}: {e.criterion} not met: {e.detail}"[:500]
                for r in results
                if r.outcome is GateOutcome.FAILED
                for e in r.evidence
                if e.verdict is Verdict.NOT_MET
            )
            if task.attempt < task.max_attempts:
                move_task(
                    self.store,
                    task.id,
                    S.IN_PROGRESS,
                    actor=actor,
                    reason="a required gate failed; another attempt",
                    gate_results=results,
                    in_transaction=self._keep_feedback_hook(feedback),
                    org_id=self.org.id,
                    hashing=self.hashing,
                )
                return None, feedback  # the move started the next attempt
            names = ", ".join(g.id for g in failed)
            move_task(
                self.store,
                task.id,
                S.NEEDS_HUMAN,
                actor=actor,
                reason=f"attempts used up; failed: {names}",
                gate_results=results,
                org_id=self.org.id,
                hashing=self.hashing,
            )
            return _Outcome(S.NEEDS_HUMAN, f"failed after {task.attempt} attempts: {names}"), ()
        if errored:
            names = ", ".join(g.id for g in errored)
            reason = f"gate errors: {names}"
            for g in errored:  # a verifier whose model was blocked says so, in the spec's words
                said = by_gate[g.id].error or ""
                if said.startswith("model blocked ["):
                    reason = said
                    break
            move_task(
                self.store,
                task.id,
                S.NEEDS_HUMAN,
                actor=actor,
                reason=reason,
                gate_results=results,
                org_id=self.org.id,
                hashing=self.hashing,
            )
            return _Outcome(S.NEEDS_HUMAN, reason), ()
        names = ", ".join(g.id for g in missing)
        return _Outcome(S.VERIFYING, f"waiting for a human: {names}"), ()

    # -- reconciler --------------------------------------------------------------------------

    # -- the content store for durable runs (R1.20, ND52, ND55) -----------------------------------

    def _keep_output_hook(
        self, output: Any
    ) -> Callable[[UnitOfWork, Task], Mapping[str, Any]] | None:
        """For the submit move: keep the output (kind `task_output`) inside the same transaction and
        name it by its keyed hash in the ledger entry. None when this engine stores nothing."""
        content = self.content
        if content is None:
            return None

        def keep(uow: UnitOfWork, task: Task) -> Mapping[str, Any]:
            kept = content.capture_task_output(
                uow, self.org.id, output, run_id=task.run_id, task_id=task.id, attempt=task.attempt
            )
            return {"output_hash": kept}

        return keep

    def _keep_feedback_hook(
        self, feedback: tuple[str, ...]
    ) -> Callable[[UnitOfWork, Task], Mapping[str, Any]] | None:
        """For the move to another attempt: keep the gate feedback the next attempt starts with
        (kind `gate_evidence`, ND57) in the same transaction, named by its keyed hash. None when
        this engine stores nothing, or there is no feedback."""
        content = self.content
        if content is None or not feedback:
            return None

        def keep(uow: UnitOfWork, task: Task) -> Mapping[str, Any]:
            kept = content.capture_gate_feedback(
                uow,
                self.org.id,
                feedback,
                run_id=task.run_id,
                task_id=task.id,
                attempt=task.attempt,
            )
            return {"feedback_hash": kept}

        return keep

    def _stored_feedback(self, feedback_hash: str | None) -> tuple[str, ...] | None:
        """The feedback a durable run kept for this attempt: empty if the attempt had none (or
        nothing is kept); None if it was kept but can't be read now (expired, shredded, altered)."""
        if feedback_hash is None:
            return ()
        if self.content is None:
            return None
        try:
            text = self.content.read_unledgered(self.org.id, feedback_hash)
            if text is None:
                return None
            return tuple(str(f) for f in json.loads(text)["feedback"])
        except (ContentError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _kept_hashes(events: Sequence[Any]) -> set[str]:
        """Every attempt's output and feedback record of a task, each a record of its own."""
        return {
            str(e.payload[field])
            for e in events
            for field in _KEPT_FIELDS.get(e.type, ())
            if field in e.payload
        }

    def _apply_unfinished_retention(self, run_id: str, plan: StoredPlan) -> int | None:
        """RET-WIRE R10: at the end of a durable run a task still `needs_human` or `blocked` is a
        dead end (nothing resumes it), so its records now expire `retention_days` from now (never
        later than their working expiry; a record already past it is left to the expiry pass).
        Each record changes in its own transaction and the rule is idempotent (`min`), so a
        repeated `finish_run` is safe. Returns the configured days when at least one such task had
        records (written to `run.ended`), else None."""
        content = self.content
        if content is None:
            return None
        days = self.content_settings.retention_days
        found = False
        for tid in plan.tasks:
            stored = self.store.get_task(tid)
            if stored is None or stored.task.status not in (S.NEEDS_HUMAN, S.BLOCKED):
                continue
            kept = self._kept_hashes(self.store.task_events(run_id, tid))
            for content_id in sorted(kept):
                found = True
                content.shorten_unfinished_output(self.org.id, content_id, retention_days=days)
        return days if found else None

    def _finish_output_hook(self, uow: UnitOfWork, task: Task) -> Mapping[str, Any]:
        """For a task's final move (done or cancelled): the output now expires `retention_days`
        from now (the organization's setting; 0 deletes it at the next expiry pass)."""
        content = self.content
        if content is None:
            return {}
        kept = self._kept_hashes(uow.task_events(task.run_id, task.id))
        if not kept:  # never submitted: nothing was kept
            return {}
        days = self.content_settings.retention_days
        for content_id in sorted(kept):
            content.finish_task_output(uow, self.org.id, content_id, retention_days=days)
        return {"output_retention_days": days}

    def _reconcile(self, run_id: str) -> ReconcileReport:
        with self._reconcile_lock:
            report = reconcile_stale_reservations(
                self.store,
                run_id,
                max_age=self.settings.reconcile_max_age,
                actor=self.settings.actor,
            )
            self._reconciled += len(report.closed)
            return report

    def _reconcile_loop(self, run_id: str, stop: threading.Event) -> None:
        interval = self.settings.reconcile_every.total_seconds()
        while not stop.wait(interval):
            try:
                self._reconcile(run_id)
            except Exception:  # noqa: S110 (a failed pass is retried next tick and at the end)
                pass
            self._expire_content()

    _sweep_clock: Callable[[], float] = staticmethod(time.monotonic)
    _last_approvals_sweep: float | None = None

    def _sweep_approvals_if_due(self) -> None:
        """The approvals sweep, at most once per `approvals_sweep_every` (R14). The time is taken
        before the attempt, so a failing sweep is retried at the next interval, not every step."""
        now = self._sweep_clock()
        last = self._last_approvals_sweep
        if last is not None and now - last < self.settings.approvals_sweep_every.total_seconds():
            return
        self._last_approvals_sweep = now
        try:
            sweep_approvals(self.store)
        except Exception as exc:  # retried next interval; the type only, never the payload
            _LOG.warning("the approvals sweep failed: %s", type(exc).__name__)

    def _expire_content(self) -> None:
        """The scheduled expiry (R1.20 PR 1): ledgered, in batches. Also run when a run
        ends, so a short run with retention 0 leaves nothing behind. A failure is retried later.
        Approvals past their 13 months go in the same pass (ND195 a), content service or not."""
        self._sweep_approvals_if_due()
        if self.content is None:
            return
        try:
            self.content.expire()
        except Exception:  # noqa: S110
            pass
        try:  # ND194: the configuration in use is re-captured before its content expires
            at = self.content._clock().astimezone(UTC)
            if self._recapture_at is None or at - self._recapture_at >= RECAPTURE_EVERY:
                self._recapture_at = at  # the window is 30 days: an hourly sweep is plenty
                recapture_configs(self.store, self.content)
        except Exception:  # noqa: S110
            pass
        # the `content.capture_failed` rows queued by a failed capture (R1.37 8.5); no transaction
        # is open here, and the run's end (in-process or a worker's `finish_run`) reaches this
        try:
            self.content.flush_alerts()
        except Exception:  # noqa: S110
            pass

    # -- durable runs: the entry points an orchestrator calls (R1.20 PR 3, T72) -----------------
    #
    # An orchestrator (Temporal, `nuramesh.temporal`) runs the same steps as `run`, one call per
    # task, each from where the store says the task is. Each call is meant for a **fresh engine**
    # (per-run state is set by `_attach`; nothing is shared between calls), and returns ids and
    # statuses only. `run` and `resume` are unchanged.

    def begin_run(
        self,
        run_id: str,
        plan: Sequence[PlannedTask] | Planner,
        *,
        data_class: str | None = None,
        start: StartInfo | None = None,
    ) -> None:
        """Start a durable run: everything `run` does before the first task (checks, model
        governance's start stage, `run.started`, the canon, the tasks), and nothing after. The
        data class (`synthetic` or `customer`) is recorded in `run.started`, so a restarted worker
        keeps it. A run that fails here ends (`aborted`). `start` (a runner starting an API request)
        makes the acting principal the actor of `run.started` and adds `started_by`, `engine`,
        `idempotency_key_hash` and the keyed `plan_hash` to its payload (ND347 4.5)."""
        if data_class is not None:
            if data_class not in ("synthetic", "customer"):
                raise ValueError("data_class must be 'synthetic' or 'customer'")
            self.data_class = data_class
        planned = list(plan(self.org) if callable(plan) else plan)
        with self._halt_lock:  # `_halt_decided` is per call: decide and record under one lock
            refused = self._refuse_before_start()
            if refused:
                # a durable run can't start clean while halted: there is no plan to wait on. One
                # audit record, nothing else, and the caller is told (nothing was started).
                self._record_halt_refusal(run_id)
        if refused:
            raise OrgHalted(HALTED_TEXT)
        self._check_run_id(run_id, resume=False)
        self._check_plan(run_id, planned)
        started = self._governance_start(run_id, planned)
        self._attach(run_id, cached=True)
        with self.store.transaction() as uow:
            try:  # the atomic claim of the run id (R3.02 F1) at expected position 0 (E30)
                uow.append_first(
                    EventDraft(
                        stream=run_stream(run_id),
                        type=EventType.RUN_STARTED,
                        actor=start.started_by if start is not None else self.settings.actor,
                        run_id=run_id,
                        payload=self._started_payload(planned, start),
                    )
                )
            except StreamTaken:
                raise RunIdTaken() from None  # also a ConcurrencyError: a runner backs off (E30)
        self._governance_started(run_id, started)
        created: list[str] = []
        try:
            if self._canon is not None:
                record_canon_loaded(
                    self.store, run_id, self._canon, org_id=self.org.id, actor=self.settings.actor
                )
            for item in planned:
                create_task(
                    self.store,
                    item.task,
                    actor=f"agent:{self.chief}",
                    org=self.org,
                    calibration=self.calibration,
                    role=self._role_name(item.task),
                )
                created.append(item.task.id)
        except BaseException as exc:
            for tid in created:
                self._to_human(tid, f"run aborted while creating tasks ({type(exc).__name__})")
            self._event(run_id, EventType.RUN_ENDED, {"aborted": True, "error": type(exc).__name__})
            raise

    def check_start(self, run_id: str, plan: Sequence[PlannedTask] | Planner) -> list[PlannedTask]:
        """The plan checks `begin_run` starts with, alone and writing nothing (a runner orders
        them before the budget check, ND347 4.6). `PlanRefused` carries a fixed code. Returns the
        planned tasks."""
        planned = list(plan(self.org) if callable(plan) else plan)
        self._check_plan(run_id, planned)
        return planned

    def complete_begin(self, run_id: str, plan: Sequence[PlannedTask] | Planner) -> int:
        """Finish a `begin_run` that a crash cut short (R1.20 PR A): create the tasks of a started
        run that were not created yet. `begin_run` writes `run.started` first and creates the tasks
        after, and it is not idempotent, so a restarted runner calls this instead of it. Writes
        nothing when every task exists. Returns the number of tasks created. Raises
        `DurableRequestError` if the run never started (the caller then calls `begin_run`)."""
        stored = self.stored_plan(run_id)
        planned = list(plan(self.org) if callable(plan) else plan)
        self._attach(run_id, cached=True)
        if self._canon is not None and not any(
            e.type == EventType.CANON_LOADED for e in self.store.read_stream(run_stream(run_id))
        ):  # a crash between `run.started` and the canon entry
            record_canon_loaded(
                self.store, run_id, self._canon, org_id=self.org.id, actor=self.settings.actor
            )
        made = 0
        for item in planned:
            if item.task.id in stored.tasks and self.store.get_task(item.task.id) is None:
                create_task(
                    self.store,
                    item.task,
                    actor=f"agent:{self.chief}",
                    org=self.org,
                    calibration=self.calibration,
                    role=self._role_name(item.task),
                )
                made += 1
        return made

    def stored_plan(self, run_id: str) -> StoredPlan:
        """The plan `run.started` recorded, and whether the run has ended. `ValueError` if the run
        never started or belongs to another organization."""
        started: LedgerEvent | None = None
        ended = False
        for event in self.store.read_stream(run_stream(run_id), after_seq=0):
            if event.type == EventType.RUN_STARTED and started is None:
                started = event
            elif event.type == EventType.RUN_ENDED:
                ended = True
        if started is None:
            raise DurableRequestError(f"run '{run_id}' never started")
        if started.payload.get("org") != self.org.id:
            raise DurableRequestError(f"run '{run_id}' belongs to another organization")
        deps = started.payload.get("depends_on", {})
        return StoredPlan(
            tasks=tuple(str(t) for t in started.payload.get("tasks", ())),
            depends_on={str(k): tuple(str(d) for d in v) for k, v in deps.items()},
            ended=ended,
            plan_hash=(
                str(started.payload["plan_hash"]) if "plan_hash" in started.payload else None
            ),
        )

    def run_task_step(self, run_id: str, task_id: str, *, last_try: bool = True) -> TaskStepResult:
        """Run one task from where the store shows it (module docstring: steps). Safe to call again
        after a crash or a retry: an acknowledged step is never repeated, a cut-short one runs
        again in a new epoch (T63). Reservations a dead try of **this task** left open are closed
        first (T24). A Stop request cancels the task. What the task's worker does never raises.

        An engine-side failure (the store is down, a bug) raises, so the orchestrator retries,
        unless `last_try`: then, as in `run`, the task is put in front of a human with the
        failure's type as the reason."""
        stored = self.store.get_task(task_id)
        if stored is None or stored.task.run_id != run_id:
            raise DurableRequestError(f"task '{task_id}' is not a task of run '{run_id}'")
        if self.settings.wind_down is not None:
            # before any model call or reservation: nothing in this organization can continue
            raise gate_error(self.settings.wind_down, self.org.id)
        self._attach(run_id, cached=True)
        self._refuse_ineligible_step(run_id, stored.task)
        reconcile_stale_reservations(
            self.store,
            run_id,
            max_age=timedelta(microseconds=1),
            actor=self.settings.actor,
            task_id=task_id,
        )
        done = threading.Event()
        watcher = threading.Thread(
            target=self._stop_watch_loop, args=(done,), daemon=True, name="stop-watcher"
        )
        watcher.start()
        try:
            if last_try:
                outcome = self._safe_task(run_id, task_id)
            else:
                try:
                    outcome = self._run_task(run_id, task_id)
                except RolePausedStop:
                    outcome = self._role_cancelled(run_id, task_id)
                except RunStopped:
                    outcome = self._stopped(task_id)
        finally:
            done.set()
            watcher.join()
        return self._step_result(run_id, task_id, outcome)

    def _refuse_ineligible_step(self, run_id: str, task: Task) -> None:
        """R13b: a durable step for a role with eval suites re-checks the assignee's eligibility
        (live records, current presets). If not eligible: one metadata-only `task.refused` entry
        and a refusal; no task event and no model call."""
        if self.presets is None or task.assignee is None:
            return
        if task.status not in (S.PROPOSED, S.ASSIGNED, S.IN_PROGRESS):
            return
        reasons = self._eligibility_reasons(task.assignee)
        if not reasons:
            return
        with self.store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=run_stream(run_id),
                    type=EventType.TASK_REFUSED,
                    actor=self.settings.actor,
                    run_id=run_id,
                    task_id=task.id,
                    payload={
                        "run_id": run_id,
                        "task_id": task.id,
                        "assignee": task.assignee,
                        "reasons": list(reasons),
                    },
                )
            )
        raise DurableRequestError("not eligible: " + "; ".join(reasons))

    def cancel_for_dependency(
        self, run_id: str, task_id: str, dependency: str, settled: Settled
    ) -> TaskStepResult:
        """Cancel a task whose dependency did not finish `done` (the reason says why)."""
        self._attach(run_id, cached=True, with_canon=False)
        reason = dependency_cancel_reason(dependency, settled)
        if self._cancelled_for_role(run_id, dependency):
            reason = ROLE_PAUSED_REASON
        return self._step_result(run_id, task_id, self._cancel(task_id, reason))

    def _cancelled_for_role(self, run_id: str, task_id: str) -> bool:
        """Whether the task's last move was a cancel with the reason `role_paused`."""
        moves = [
            e
            for e in self.store.task_events(run_id, task_id)
            if e.type == EventType.TASK_TRANSITIONED.value
        ]
        return bool(moves) and moves[-1].payload.get("reason") == ROLE_PAUSED_REASON

    def wait_for_dependency(
        self, run_id: str, task_id: str, dependency: str, settled: Settled
    ) -> TaskStepResult:
        """A task it depends on entered a durable run's human wait: the task is held `proposed`
        (R1.20a). Only the durable orchestrator calls this, when the wait begins, because only
        there can the wait end. The reason is written to the ledger once (`decision.recorded`,
        `kind: dependency_waiting`); the task itself does not move."""
        self._attach(run_id, cached=True, with_canon=False)
        return self._step_result(
            run_id, task_id, self._wait_for(run_id, task_id, dependency, settled)
        )

    def _wait_for(self, run_id: str, task_id: str, dependency: str, settled: Settled) -> _Outcome:
        """Write the hold note once per (held task, waiting task), and only while the task is still
        `proposed` and none of its other dependencies has ended without `done`: all of that is
        read inside the transaction that appends the note, so a cancel that lands meanwhile
        (another dependency ended) is not followed by a note, and a task about to be cancelled
        is never noted as held."""
        reason = dependency_wait_reason(dependency)
        edges = self.stored_plan(run_id).depends_on
        ancestors: list[str] = []  # everything it depends on, directly or through others
        pending = list(edges.get(task_id, ()))
        while pending:
            up = pending.pop()
            if up not in ancestors:
                ancestors.append(up)
                pending.extend(edges.get(up, ()))
        with self.store.transaction() as uow:
            stored = uow.get_task(task_id)
            if stored is None or stored.task.status is not S.PROPOSED:
                return _Outcome(stored.task.status if stored else S.PROPOSED, reason)
            if self._stop_in(uow, run_id):  # a Stop cancels it: no hold note
                return _Outcome(S.PROPOSED, reason)
            for other in ancestors:  # one already ended without `done`: this task is cancelled
                found = uow.get_task(other)
                if found is not None and found.task.status in _ENDED_NOT_DONE:
                    return _Outcome(S.PROPOSED, reason)  # not held: no note
            noted = any(
                e.type == EventType.DECISION_RECORDED
                and e.payload.get("kind") == DEPENDENCY_WAITING
                and e.payload.get("waiting_on") == dependency
                for e in uow.task_events(run_id, task_id)
            )  # a retry, a rejection round, a restarted workflow: never twice for the pair
            waiting = uow.get_task(dependency)
            if not noted and waiting is not None:
                uow.append(
                    EventDraft(
                        stream=run_stream(run_id),
                        type=EventType.DECISION_RECORDED,
                        actor=self.settings.actor,
                        run_id=run_id,
                        task_id=task_id,
                        payload={
                            "kind": DEPENDENCY_WAITING,
                            "task_id": task_id,
                            "waiting_on": dependency,
                            "attempt": waiting.task.attempt,
                        },
                    )
                )
        return _Outcome(S.PROPOSED, reason)

    @staticmethod
    def _stop_in(uow: UnitOfWork, run_id: str) -> bool:
        """Whether the run's stream has a Stop request, as this unit of work sees it."""
        return uow.has_event(run_stream(run_id), EventType.RUN_STOP_REQUESTED)

    def cancel_for_stop(self, run_id: str, task_id: str) -> TaskStepResult:
        """Cancel a task because a human stopped the run (as `run` does for tasks still waiting)."""
        self._attach(run_id, cached=True, with_canon=False)
        return self._step_result(run_id, task_id, self._stopped(task_id))

    def stop_requested(self, run_id: str) -> bool:
        """Whether a human asked the run to stop (the request is in its ledger stream)."""
        return StopWatch(self.store, run_id, self.org.id).requested()

    def _step_result(self, run_id: str, task_id: str, outcome: _Outcome) -> TaskStepResult:
        if outcome.status in FINAL:
            # RET-WIRE R4a: a durable worker has no periodic expiry loop, so a task that became
            # final in this step (retention 0 deletes at once) is expired before the step returns;
            # a no-op when nothing is due, and a failure is retried later, never failing the step
            self._expire_content()
        waiting = WAITING_NOTE in outcome.note
        since: datetime | None = None
        attempt = submission = 0
        stored = self.store.get_task(task_id)
        if stored is not None:
            attempt, submission = stored.task.attempt, stored.task.submission
            if outcome.status is S.VERIFYING and waiting:
                since = submitted_at(self.store, run_id, task_id, attempt)
        return TaskStepResult(
            task_id=task_id,
            status=outcome.status,
            waiting=waiting,
            stopped=self._stop_acted,
            waiting_since=since,
            attempt=attempt,
            submission=submission,
        )

    def record_human_decision(
        self,
        run_id: str,
        task_id: str,
        gate_id: str,
        *,
        decided_by: str,
        findings: Mapping[str, tuple[bool, str]],
    ) -> RecordedDecision:
        """A person decides a human gate of a durable run that is waiting for them (days later).

        The decision (who, and a verdict and written reason per criterion) is kept in the content
        store, encrypted, in the same transaction as the `decision.recorded` entry
        (`kind: human_decision`) that names it by its keyed hash; the ledger carries no text. It
        does **not** move the task: the orchestrator is woken, and the gates run again with this
        decision (Q3, option A: the non-human gates are run again, so a verifier is paid once more
        per human wait). Refused (`GateError`, `ValueError`) for a decider who may not decide, a
        bad finding, a gate that isn't a human gate of a task waiting in `verifying`, or a round
        that already has a decision. Also refused, with `SelfApproval`, for the person who
        started the run (the originator rule, R2.13 3a)."""
        try:
            return record_decision(
                self.store,
                self.org,
                self.content,
                run_id,
                task_id,
                gate_id,
                decided_by=decided_by,
                findings=findings,
            )
        except DecisionRefused as exc:
            if exc.code == "not_found":
                raise DurableRequestError(str(exc)) from None
            raise

    def _decision_events(self, task: Task) -> list[LedgerEvent]:
        return [
            e
            for e in self.store.task_events(task.run_id, task.id)
            if e.type == EventType.DECISION_RECORDED
            and e.payload.get("kind") == HUMAN_DECISION
            and e.payload.get("attempt") == task.attempt
            and e.payload.get("submission") == task.submission
        ]

    def _decided_gates(self, task: Task) -> set[str]:
        """The human gates of this round that a person has decided (durable runs)."""
        return {str(e.payload.get("gate")) for e in self._decision_events(task)}

    def _recorded_decision(
        self, task: Task, spec: GateSpec, *, strict: bool = False
    ) -> HumanDecision | None:
        """The person's recorded decision on this gate in this round, if any. `LookupError` if one
        was recorded but can't be read now (expired, shredded, altered). With `strict` (the
        evidence route) a key refusal is raised and a missing record re-checks the key first, so a
        destroyed, missing or replaced key is never taken for "gone" (G6 step 2)."""
        events = [e for e in self._decision_events(task) if e.payload.get("gate") == spec.id]
        if not events:
            return None
        if self.content is None:
            raise LookupError("no content service")
        try:
            text = self.content.read_unledgered(
                self.org.id, str(events[-1].payload["decision_hash"])
            )
            if text is None:
                if strict:
                    self.content.require_key_present(self.org.id)
                raise LookupError("gone")
            body = json.loads(text)
            return HumanDecision(
                decided_by=str(body["decided_by"]),
                findings={str(c): (bool(v[0]), str(v[1])) for c, v in body["findings"].items()},
            )
        except (ContentError, ValueError, KeyError, TypeError, IndexError) as exc:
            if strict and isinstance(exc, ContentError) and is_gate_refusal(exc):
                raise
            raise LookupError("unreadable") from exc

    def _open_gates(self, task: Task, events: Sequence[LedgerEvent]) -> list[str]:
        """See `open_human_gates`."""
        return open_human_gates(task, events)

    def _input_expiry(self, task: Task, events: Sequence[LedgerEvent]) -> datetime | None:
        """When this attempt's output (the task's input to the person) expires: the stored
        record's expiry, or the submit time plus the working days. For the warning payload, so a
        notifier can use the real time (`days_left` is the lead time, which a late warning
        overstates)."""
        for e in reversed(events):
            if (
                e.type == EventType.TASK_TRANSITIONED
                and e.payload.get("attempt_after") == task.attempt
                and "output_hash" in e.payload
            ):
                record = self.store.get_content(self.org.id, str(e.payload["output_hash"]))
                if record is not None and record.expires_at is not None:
                    return record.expires_at
                return e.at + timedelta(days=WORKING_DAYS)
        return None

    def warn_human_wait(self, run_id: str, task_id: str, days_left: int) -> bool:
        """`warn_human_wait_result(...).written`."""
        return self.warn_human_wait_result(run_id, task_id, days_left).written

    def warn_human_wait_result(self, run_id: str, task_id: str, days_left: int) -> WarnResult:
        """Warn the approver that a task waiting for a human loses its input in `days_left` days
        (ND55): a `decision.recorded` entry, `kind: human_wait_warning`, with the task, attempt and
        days left, no text. Once per task, attempt and lead time (a retry writes nothing more).
        False when the task is no longer waiting in `verifying`, when every human gate of the round
        is already answered (a person who has decided is not warned), or when it was warned. A
        refusal because the store clock is before the warning window carries `opens_at`, the store
        time the window opens, so a timer that ran early waits for it instead of asking again."""
        with self.store.transaction() as uow:
            stored = uow.get_task(task_id)
            if (
                stored is None
                or stored.task.run_id != run_id
                or stored.task.status is not S.VERIFYING
            ):
                return WarnResult(False)
            attempt = stored.task.attempt
            events = uow.task_events(run_id, task_id)
            if not self._open_gates(stored.task, events):
                return WarnResult(False)
            expires = self._input_expiry(stored.task, events)
            if expires is not None:
                now = uow.now()  # the store clock decides, never the caller's timer (spec 8)
                if now >= expires:
                    return WarnResult(False)
                opens = expires - timedelta(days=days_left)
                if now < opens:
                    return WarnResult(False, opens_at=opens)
            for e in events:
                if (
                    e.type == EventType.DECISION_RECORDED
                    and e.payload.get("kind") == HUMAN_WAIT_WARNING
                    and e.payload.get("attempt") == attempt
                    and e.payload.get("days_left") == days_left
                ):
                    return WarnResult(False)
            uow.append(
                EventDraft(
                    stream=run_stream(run_id),
                    type=EventType.DECISION_RECORDED,
                    actor=self.settings.actor,
                    run_id=run_id,
                    task_id=task_id,
                    payload={
                        "kind": HUMAN_WAIT_WARNING,
                        "task_id": task_id,
                        "attempt": attempt,
                        "days_left": days_left,
                        "expires_at": format_time(expires) if expires else None,
                    },
                )
            )
        return WarnResult(True)

    def expire_human_wait(self, run_id: str, task_id: str) -> TaskStepResult:
        """A task waiting for a human reached the 90-day limit of its input (ND55): stop it. One
        transaction writes `decision.recorded` (`kind: human_wait_expired`) and moves the task to
        `cancelled` with that reason; the final hook sets the retention of what it kept. A task
        that is no longer waiting in `verifying`, or whose human gates are all answered (the
        answer is on file and the gates only have to run again), is left alone: it is not stopped
        for a wait that is over."""
        self._attach(run_id, cached=True, with_canon=False)
        stored = self.store.get_task(task_id)
        if stored is None or stored.task.run_id != run_id:
            raise DurableRequestError(f"task '{task_id}' is not a task of run '{run_id}'")
        if stored.task.status is not S.VERIFYING:
            return self._step_result(run_id, task_id, _Outcome(stored.task.status))
        attempt = stored.task.attempt
        reason = (
            f"cancelled: the human wait reached {WORKING_DAYS} days and the input expired; "
            "the task was stopped (plan it again as a new task)"
        )

        def hook(uow: UnitOfWork, task: Task) -> Mapping[str, Any]:
            events = uow.task_events(run_id, task_id)
            if not self._open_gates(stored.task, events):
                raise _WaitOver  # an answer arrived: nothing is written
            deadline = self._input_expiry(stored.task, events)
            if deadline is not None and uow.now() < deadline:
                raise _NotDue(deadline)  # a timer ahead of the store clock: nothing is written
            uow.append(
                EventDraft(
                    stream=run_stream(run_id),
                    type=EventType.DECISION_RECORDED,
                    actor=self.settings.actor,
                    run_id=run_id,
                    task_id=task_id,
                    payload={
                        "kind": HUMAN_WAIT_EXPIRED,
                        "task_id": task_id,
                        "attempt": attempt,
                        "days_left": 0,
                    },
                )
            )
            return self._finish_output_hook(uow, task)

        try:
            move_task(
                self.store,
                task_id,
                S.CANCELLED,
                actor=self.settings.actor,
                reason=reason,
                in_transaction=hook,
                org_id=self.org.id,
                hashing=self.hashing,
            )
        except _WaitOver:
            return self._step_result(run_id, task_id, _Outcome(S.VERIFYING))
        except _NotDue as early:
            held = self._step_result(run_id, task_id, _Outcome(S.VERIFYING, WAITING_NOTE))
            return dataclasses.replace(held, not_due=True, due_at=early.deadline)
        except Exception as exc:
            again = self.store.get_task(task_id)
            if again is not None and again.task.status is not S.VERIFYING:
                outcome = _Outcome(again.task.status)  # it moved meanwhile (a decision applied)
            else:
                outcome = self._incident(
                    task_id, f"{reason}; cancelling failed ({type(exc).__name__})"
                )
        else:
            outcome = _Outcome(S.CANCELLED, reason)
        self._expire_content()
        return self._step_result(run_id, task_id, outcome)

    def finish_run(self, run_id: str, *, stop_acted: bool, aborted: str = "") -> dict[str, int]:
        """End a durable run once every task has settled: what `run` does last. Closes open
        reservations (a stopped run releases them), reconciles stale ones, expires content, and
        writes `run.ended` with the counts of the tasks' statuses (and `status: stopped` when a
        Stop request was applied). Writes nothing twice. Returns the counts.

        `aborted` (the type name of what ended the workflow early: a cancel, an activity that
        failed for good) ends the run with `aborted: true` and that `error`, as `run` does, and
        cancels every task that is not final or in front of a human (as a second Ctrl-C does)."""
        plan = self.stored_plan(run_id)
        self._attach(run_id, cached=True, with_canon=False)
        if aborted and not plan.ended:
            for tid in plan.tasks:
                stored = self.store.get_task(tid)
                if stored is not None and stored.task.status not in (
                    S.DONE,
                    S.CANCELLED,
                    S.NEEDS_HUMAN,
                ):
                    self._cancel(tid, "cancelled: the run was aborted (the workflow ended early)")
        counts: dict[str, int] = {}
        for tid in plan.tasks:
            stored = self.store.get_task(tid)
            if stored is not None:
                status = stored.task.status.value
                counts[status] = counts.get(status, 0) + 1
        if plan.ended:
            return counts
        ended: dict[str, Any] = {}
        self._stop_acted = stop_acted
        if stop_acted:
            self._release_open_reservations(run_id)
        try:
            self._reconcile(run_id)
        except Exception as exc:
            ended["reconcile_error"] = type(exc).__name__
        unfinished = self._apply_unfinished_retention(run_id, plan)
        if unfinished is not None:
            ended["unfinished_retention_days"] = unfinished
        self._expire_content()
        if stop_acted and self._stop is not None and self._stop.requested():
            assert self._stop.request is not None
            ended.update({"status": "stopped", "stop_requested_by": self._stop.request.by})
        if aborted:
            ended.update({"aborted": True, "error": aborted})
        self._event(run_id, EventType.RUN_ENDED, {"counts": counts, **ended})
        return counts

    # -- ledger and report -------------------------------------------------------------------

    def _event(
        self, run_id: str, type_: EventType, payload: dict[str, Any], *, claim: bool = False
    ) -> None:
        """Append to the run's stream. `claim` is the first entry of a new run: written with
        `append_first`, so a run id is claimed for one organization, once (R3.02 F1)."""
        draft = EventDraft(
            stream=run_stream(run_id),
            type=type_,
            actor=self.settings.actor,
            run_id=run_id,
            payload=payload,
        )
        with self.store.transaction() as uow:
            if not claim:
                uow.append(draft)
                if type_ is EventType.RUN_ENDED:
                    self._plan_text_at_run_end(uow, run_id)  # RET-PT 4.3(a), same transaction
                return
            try:
                uow.append_first(draft)
            except StreamTaken:
                raise RunIdTaken() from None

    def _check_run_id(self, run_id: str, *, resume: bool) -> None:
        """R3.02 F1, after the halt/retired/key checks and before the plan checks: in hosted mode
        the id must be system-issued (same text for every refused id); a new run's id must have no
        entry yet (a cheap read-only pre-check; the atomic claim is `append_first` at
        `run.started`). `resume` needs the existing stream and is checked by `_check_resumable`."""
        if self.hosted_ids and not _RUN_ID_ISSUED.fullmatch(run_id):
            raise ValueError(RUN_ID_ISSUED_TEXT)
        if not resume and next(iter(self.store.read_stream(run_stream(run_id), limit=1)), None):
            raise RunIdTaken()

    def report(
        self,
        run_id: str,
        planned: Sequence[PlannedTask],
        outcomes: Mapping[str, _Outcome] | None = None,
    ) -> RunReport:
        """Build the run report (from the store and the ledger, with this run's notes)."""
        notes = {tid: o.note for tid, o in (outcomes or {}).items() if o.note}
        return build_report(self.store, self.org, run_id, [p.task.id for p in planned], notes=notes)


def budgets_in_ledger(store: Store, run_id: str) -> set[str]:
    """Budget ids named by a run's budget events (verifier and worker spend alike)."""
    found: set[str] = set()
    for event in store.read_stream(run_stream(run_id)):
        if not event.type.startswith("budget."):
            continue
        for item in [
            *(event.payload.get("budgets") or []),
            *(event.payload.get("shortfalls") or []),
        ]:
            if isinstance(item, str):
                found.add(item)
            elif isinstance(item, Mapping) and isinstance(item.get("budget"), str):
                found.add(item["budget"])
    return found


def unknown_charges(events: Sequence[Any]) -> dict[str, str]:
    """What each call of unknown cost was charged: the `budget_charge` of the settlement that
    carries `cost_unknown`, by reservation id (decimal text, no trailing zeros)."""
    return {
        str(e.payload.get("reservation")): _plain(Decimal(str(e.payload["budget_charge"])))
        for e in events
        if e.type == EventType.BUDGET_SETTLED.value
        and "cost_unknown" in e.payload
        and "budget_charge" in e.payload
    }


def run_spend_line(events: Sequence[Any]) -> SpendLine:
    """The run's model-call spend from its `model.called` entries (a cost is recorded once, there).
    An unknown call's charge is the `budget_charge` of the settlement with the same reservation."""
    charges = unknown_charges(events)
    known = Decimal(0)
    calls = 0
    unknown: list[UnknownCostCall] = []
    for e in events:
        if e.type != EventType.MODEL_CALLED.value:
            continue
        outcome = e.payload.get("outcome")
        settle_failed = outcome == "settle_failed"  # paid, cost not recorded: unknown (sec. 1)
        if not settle_failed and outcome not in ("ok", "settled_late"):
            continue
        if not settle_failed and e.cost is None and getattr(e, "cost_basis", None) is None:
            continue  # no amount and no basis: nothing was spent (costs sec. 1)
        calls += 1
        if e.cost is not None and not settle_failed:
            known += e.cost.amount
        elif settle_failed or getattr(e, "cost_basis", None) is not None:
            payload = e.payload
            unknown.append(
                UnknownCostCall(
                    model=payload_model_ref(payload),
                    charged=charges.get(str(payload.get("reservation"))),
                    task_id=e.task_id,
                )
            )
    return SpendLine(known_total=_plain(known), unknown=tuple(unknown), calls=calls)


def build_report(
    store: Store,
    org: OrgGraph,
    run_id: str,
    task_ids: Sequence[str] | None = None,
    *,
    notes: Mapping[str, str] | None = None,
    redact_reasons: bool = False,
) -> RunReport:
    """The run report, rebuilt from the store and the ledger alone (so it works after the run).

    `task_ids` gives the order (default: the run's tasks as stored). A task's note is the given
    one, else the reason of its last move if it didn't finish `done`. With `redact_reasons` (the
    server's report route) that reason is only used when `system` wrote it (R1.27-H R6).
    """
    stored_tasks = {s.task.id: s.task for s in store.tasks_for_run(run_id)}
    ids = list(task_ids) if task_ids is not None else list(stored_tasks)
    events = list(store.read_stream(run_stream(run_id)))
    tasks = []
    for tid in ids:
        task = stored_tasks[tid]
        task_events = [e for e in events if e.task_id == tid]
        gate_events = [e for e in task_events if e.type == EventType.GATE_COMPLETED.value]
        last_round = max(
            ((int(e.payload["attempt"]), int(e.payload["submission"])) for e in gate_events),
            default=None,
        )
        latest: dict[str, GateLine] = {}
        for e in gate_events:  # only the last verification round: rounds never mix
            if (int(e.payload["attempt"]), int(e.payload["submission"])) == last_round:
                gate_id = str(e.payload["gate"])
                latest[gate_id] = GateLine(
                    gate=gate_id,
                    kind=task.definition_of_done.gate(gate_id).kind.value,
                    outcome=str(e.payload["outcome"]),
                    checked_by=str(e.payload["checked_by"]),
                )
        note = (notes or {}).get(tid, "")
        if not note and task.status is not S.DONE:
            moves = [e for e in task_events if e.type == EventType.TASK_TRANSITIONED.value]
            note = str(moves[-1].payload.get("reason", "")) if moves else ""
            if redact_reasons and moves:
                last_reason = moves[-1].payload.get("reason")
                if not reason_text_visible(moves[-1].actor) or not isinstance(last_reason, str):
                    note = ""
            if not note and task.status is S.PROPOSED:  # held by a dependency (R1.20a)
                held = [
                    e
                    for e in task_events
                    if e.type == EventType.DECISION_RECORDED.value
                    and e.payload.get("kind") == DEPENDENCY_WAITING
                ]
                if held:  # fixed words built here from the note's fields; not stored in the ledger
                    note = dependency_wait_reason(str(held[-1].payload.get("waiting_on", "")))
        tasks.append(
            TaskReport(
                id=task.id,
                title=task.title,
                assignee=task.assignee,
                status=task.status.value,
                attempts=task.attempt,
                gates=tuple(latest.values()),
                note=note,
            )
        )
    now = store.now()  # a windowed budget shows its current window, even if nobody rolled it yet
    balances = [as_of(s.balance, now) for s in store.list_balances(org.id)]
    relevant = budgets_in_ledger(store, run_id)  # everything this run reserved or charged
    for tid in ids:
        context = CallContext(
            run_id=run_id, task_id=tid, agent_id=stored_tasks[tid].assignee or org.agents[0].id
        )
        relevant |= {b.id for b in applicable_budgets([x.budget for x in balances], context, org)}
    budgets = tuple(
        BudgetLine(
            id=b.budget.id,
            scope=b.budget.scope.value,
            scope_id=b.budget.scope_id,
            limit=str(b.budget.limit.amount),
            spent=str(b.spent.amount),
            reserved=str(b.reserved.amount),
            blocked=b.blocked,
            window=b.budget.window.value,
            window_start=None if b.window_start is None else format_time(b.window_start),
        )
        for b in balances
        if b.budget.id in relevant  # only budgets this run spent from or could spend from
    )
    reconciled = sum(
        1
        for e in events
        if e.type in (EventType.BUDGET_SETTLED.value, EventType.BUDGET_RELEASED.value)
        and isinstance(e.payload.get("details"), Mapping)
        and e.payload["details"].get("reconciled") is True
    )
    canon = [e for e in events if e.type == EventType.CANON_LOADED.value]
    ended = [e for e in events if e.type == EventType.RUN_ENDED.value]
    stopped = bool(ended) and ended[-1].payload.get("status") == "stopped"
    requests = [e for e in events if e.type == EventType.RUN_STOP_REQUESTED.value]
    head = store.head(run_stream(run_id))
    check = store.verify_stream(run_stream(run_id))
    return RunReport(
        run_id=run_id,
        spend=run_spend_line(events),
        tasks=tuple(tasks),
        budgets=budgets,
        pending_approvals=tuple(
            f"{a.id} ({a.action.kind.value})" for a in pending_approvals(store, run_id)
        ),
        reconciled=reconciled,
        ledger_seq=head.seq if head else 0,
        ledger_head=head.hash if head else "",
        ledger_ok=check.ok,
        canon_commit=str(canon[-1].payload.get("commit")) if canon else None,
        stopped=stopped,
        stop_requested_by=requests[0].actor if requests else None,
    )
