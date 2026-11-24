"""Task service: every task change and its ledger event are written in one transaction (A1)."""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from nuramesh.core.calibration import CalibrationConfig, calibration_problems
from nuramesh.core.gate_config import definition_problems, verifier_problems
from nuramesh.core.gates import GateKind, GateResult
from nuramesh.core.ledger import EventType
from nuramesh.core.org import OrgGraph
from nuramesh.core.tasks import Task, TaskMove, TaskStatus, transition
from nuramesh.engine.content import LedgerHashing
from nuramesh.engine.result_match import recorded_hash_of
from nuramesh.engine.starter import refuse_foreign_run
from nuramesh.stores.base import (
    ConcurrencyError,
    EventDraft,
    NotFoundError,
    Store,
    StoredTask,
    UnitOfWork,
    run_stream,
)


class GateConfigError(ValueError):
    """A gate's configuration is wrong; the task is not created."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


class UnrecordedGateResultError(ValueError):
    """A move used a gate result that isn't in the ledger (nothing counts unrecorded)."""


def create_task(
    store: Store,
    task: Task,
    *,
    actor: str,
    org: OrgGraph | None = None,
    calibration: CalibrationConfig | None = None,
    role: str | None = None,
    org_id: str | None = None,
) -> StoredTask:
    """Store a new task and record `task.created` with its definition of done.

    Refused with `GateConfigError` (nothing written) if a rule, schema or human gate is
    misconfigured, so a bad gate fails now and not when the work is already done. With `org`, the
    verifier gates are also checked against it (the agent exists, has a verifier role and a model);
    their calibration suites are checked against `calibration` (None means no suites: a gate that
    lists one is refused, the same fail-closed reading as the gate itself).

    A task with a `text_ref` (made from an API plan) is recorded by hashes and content ids; `role`
    names the role the runner resolved for it.

    The task is refused with `ForeignRun` (nothing written, #1587) if its run belongs to another
    organization: the caller's organization is `org_id`, or `org.id` when only `org` is given. A
    run with no stream, or whose first entry is not `run.started`, is nobody's (same rule as
    `check_action` and `record_canon_loaded`). With neither `org` nor `org_id` there is no caller
    organization to compare, so the check is skipped (engine callers always pass `org`).
    """
    caller_org = org_id if org_id is not None else (org.id if org is not None else None)
    problems = definition_problems(task.definition_of_done)
    has_verifier = any(g.kind is GateKind.VERIFIER for g in task.definition_of_done.gates)
    if org is not None:
        problems += verifier_problems(task.definition_of_done, org, assignee=task.assignee)
        # No configuration fails closed (spec rule 21): every listed suite is then unknown.
        problems += calibration_problems(
            task.definition_of_done, org, calibration or CalibrationConfig(suites=())
        )
    elif has_verifier:
        problems.append("verifier gates can only be checked with the org: pass `org`")
    if problems:
        raise GateConfigError(problems)
    payload: dict[str, Any] = {
        "title": task.title,
        "parent_id": task.parent_id,
        "status": task.status.value,
        "max_attempts": task.max_attempts,
        "definition_of_done": task.definition_of_done.model_dump(mode="json"),
    }
    if task.text_ref is not None:
        # text_ref mode (R1.20 PR B, ND347 4.3): the title and the definition of done are in the
        # content store. The entry names them by their keyed hashes (a content id IS the keyed
        # hash of the stored text, ND25) and the role; the table row and the entry hold a fixed
        # placeholder, never client text.
        ref = task.text_ref
        payload["role"] = role
        if ref.template_id is not None:
            payload["template_hash"] = ref.template_id
            payload["template_content"] = ref.template_id
        else:
            payload["title_hash"] = ref.title_id
            payload["dod_hash"] = ref.dod_id
            payload["title_content"] = ref.title_id
            payload["dod_content"] = ref.dod_id
    with store.transaction() as uow:
        if caller_org is not None:
            refuse_foreign_run(uow, caller_org, task.run_id)  # before any write (#1587)
        version = uow.put_task(task, expected_version=None)
        uow.append(
            EventDraft(
                stream=run_stream(task.run_id),
                type=EventType.TASK_CREATED,
                actor=actor,
                run_id=task.run_id,
                task_id=task.id,
                payload=payload,
            )
        )
    return StoredTask(task=task, version=version)


def move_task(
    store: Store,
    task_id: str,
    to: TaskStatus,
    *,
    actor: str,
    reason: str,
    expected_version: int | None = None,
    assignee: str | None = None,
    extra_attempts: int = 0,
    gate_results: Iterable[GateResult] = (),
    in_transaction: Callable[[UnitOfWork, Task], Mapping[str, Any]] | None = None,
    payload_extra: Mapping[str, Any] | None = None,
    org_id: str,
    hashing: LedgerHashing,
) -> tuple[StoredTask, TaskMove]:
    """Apply a status move and record `task.transitioned`, atomically.

    The task is read inside the transaction. If `expected_version` is given and differs, the move
    is refused with `ConcurrencyError` (someone changed the task since the caller looked).
    Refused moves (`TaskTransitionError`) write nothing. Every gate result given must already be
    recorded (`gates.record_gate_result`), or the move is refused with
    `UnrecordedGateResultError`: nothing counts as verified unless it is in the ledger.

    `in_transaction(uow, new_task)`, if given, runs **inside the same transaction** after the move
    is decided and before it is recorded; the fields it returns are added to the ledger payload
    (R1.20: the keyed hash of a task output kept in the content store, so the content and the entry
    that names it commit or roll back together). If it raises, nothing is written.

    `payload_extra` adds fixed fields to the ledger payload of this one entry (for example
    `reason_code`); entries written without it keep the payload they always had, so their hashes
    do not change.

    `hashing` (required) makes the keyed digests of the deciding gate results: it is bound to
    `org_id` inside this transaction and used only when the move has a `verification` (a cancel or
    wind-down move never touches the key ring). A recorded legacy row (plain SHA-256) still counts
    as recorded (PH-KEY R10); the transition row always holds the keyed digest.
    """
    with store.transaction() as uow:
        hasher = hashing.bind(uow, org_id)
        current = uow.get_task(task_id)
        if current is None:
            raise NotFoundError(f"task '{task_id}' doesn't exist")
        if expected_version is not None and current.version != expected_version:
            raise ConcurrencyError(
                f"task '{task_id}' is at version {current.version}, not {expected_version}"
            )
        version = current.version
        results = tuple(gate_results)
        if results:
            rows = [
                e
                for e in uow.task_events(current.task.run_id, task_id)
                if e.type == EventType.GATE_COMPLETED
            ]
            unrecorded = [
                r.gate_id for r in results if recorded_hash_of(rows, r, hasher, org_id) is None
            ]
            if unrecorded:
                raise UnrecordedGateResultError(
                    f"gate results not in the ledger for gates: {sorted(set(unrecorded))}"
                )
        move = transition(
            current.task,
            to,
            reason=reason,
            assignee=assignee,
            extra_attempts=extra_attempts,
            gate_results=results,
        )
        new_version = uow.put_task(move.task, expected_version=version)
        extra = {} if in_transaction is None else dict(in_transaction(uow, move.task))
        extra.update(payload_extra or {})
        base = move.ledger_payload(hasher)
        if move.verification is not None and any(g.deciding for g in move.verification.gates):
            base.update(hasher.marker())
        if overlap := base.keys() & extra.keys():
            raise ValueError(f"a transaction hook can't set {sorted(overlap)}")
        uow.append(
            EventDraft(
                stream=run_stream(move.task.run_id),
                type=EventType.TASK_TRANSITIONED,
                actor=actor,
                run_id=move.task.run_id,
                task_id=task_id,
                payload={**base, **extra},
            )
        )
    return StoredTask(task=move.task, version=new_version), move
