"""Recording a person's decision on a human gate of a durable run (R1.20, T81).

One code path for the API and for the orchestrator (`RunEngine.record_human_decision`): it needs
only a store, the organization and the content service, never an engine. The order of checks and
the cases are the table in the design notes ("Human decisions in a durable run").

The decision text is kept in the content store (kind `reason`); the ledger entry names it by its
keyed hash and carries no text. The task is not moved here: the next step of the task applies the
decision (T72).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from nuramesh.core.gates import GateKind, GateOutcome
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.org import OrgGraph
from nuramesh.core.policy import ApprovalMode
from nuramesh.core.solo import SoloProof
from nuramesh.core.tasks import TaskStatus
from nuramesh.engine.content import WORKING_DAYS, ContentService
from nuramesh.engine.gate_runners import decide_human_gate
from nuramesh.engine.originator import SelfApproval, originator_of
from nuramesh.engine.solo import SoloRefused, SoloTerms, mode_without_solo, standing_of
from nuramesh.engine.steps import HUMAN_DECISION, HUMAN_DECISION_REFUSED
from nuramesh.stores.base import EventDraft, Store, UnitOfWork, run_stream

__all__ = ["DecisionRefused", "RecordedDecision", "attempt_output_hash", "record_decision"]


class DecisionRefused(ValueError):
    """A decision that was not recorded. `code` is stable (the API's problem code); `why` is a
    fixed word for the refusals that carry one. The message never quotes a reason."""

    def __init__(self, code: str, message: str, *, why: str | None = None) -> None:
        super().__init__(message)
        self.code, self.why = code, why


@dataclass(frozen=True)
class RecordedDecision:
    """What was recorded (or found already recorded). No text."""

    task_id: str
    gate: str
    attempt: int
    submission: int
    outcome: str
    decision_hash: str
    already_recorded: bool


def attempt_output_hash(attempt: int, events: Sequence[LedgerEvent]) -> str | None:
    """The keyed hash of the output this attempt's submit move kept, if it kept one."""
    for e in reversed(events):
        if (
            e.type == EventType.TASK_TRANSITIONED
            and e.payload.get("attempt_after") == attempt
            and "output_hash" in e.payload
        ):
            return str(e.payload["output_hash"])
    return None


def _decisions(events: Sequence[LedgerEvent], gate: str) -> list[LedgerEvent]:
    return [
        e
        for e in events
        if e.type == EventType.DECISION_RECORDED
        and e.payload.get("kind") == HUMAN_DECISION
        and e.payload.get("gate") == gate
    ]


def _ended(uow: UnitOfWork, run_id: str) -> bool:
    return uow.has_event(run_stream(run_id), EventType.RUN_ENDED)


def _stop_requested(uow: UnitOfWork, run_id: str) -> bool:
    """A `run.stop_requested` is in the run's stream, read inside the writing transaction."""
    return uow.has_event(run_stream(run_id), EventType.RUN_STOP_REQUESTED)


def _mode(
    uow: UnitOfWork,
    org_id: str,
    run_id: str,
    decided_by: str,
    *,
    passes: bool,
    solo: SoloTerms | None,
) -> ApprovalMode:
    """SA-12 steps 6 and 7a for a gate decision, before any state check of the gate; returns the
    mode the entry will carry (SA-14). The solo condition is `D(org) == {decider}`, read in this
    transaction: while it holds the decision is solo whoever started the run, and one that passes
    the gate must carry the acknowledgement. Otherwise (another decide-holder exists, the caller
    gave no terms, or the decider holds no `decide` here) the person who started the run never
    decides it (R2.13 3a)."""
    originator = originator_of(uow, org_id, run_id)
    if solo is not None and standing_of(uow, org_id, decided_by).solo:
        if passes and solo.acknowledge is not True:
            raise SoloRefused("acknowledge_required")
        return ApprovalMode.SOLO
    if originator == decided_by:
        raise SelfApproval
    return mode_without_solo(originator)


def record_decision(
    store: Store,
    org: OrgGraph,
    content: ContentService | None,
    run_id: str,
    task_id: str,
    gate_id: str,
    *,
    decided_by: str,
    findings: Mapping[str, tuple[bool, str]],
    round: tuple[int, int] | None = None,
    dry_run: bool = False,
    solo: SoloTerms | None = None,
) -> RecordedDecision:
    """Record the decision, or recognise it as one already recorded.

    `round` is `(attempt, submission)` the person looked at; with it an identical decision already
    on file answers `already_recorded` (replay), and a round that is not the current one is
    refused (`stale_round`). Without it the current round is meant and a repeat is refused
    (`already_decided`). Raises `DecisionRefused` (codes in the spec), `GateError` subclasses for
    the gate's own rules (`DeciderNotAllowed`, a bad finding). With `dry_run` every check runs
    (including the ones inside the writing transaction) and nothing is written: the result's
    outcome is `dry_run`.

    `solo` (SA-2, SA-19) defaults to "never solo": without it the run's
    originator is refused (`SelfApproval`) even when nobody else could decide, and the mode
    written is `peer` or `no_originator`. Only the server route passes the terms, for a person in
    a cookie session; then the decision of the organization's only active decide-holder is a
    solo decision, whoever started the run, and one that passes the gate needs the
    acknowledgement (`SoloRefused` otherwise). A failing one is free. A gate has no digest, no
    fresh proof and no rate limit."""
    if content is None:
        raise DecisionRefused(
            "content_store_unavailable",
            "a decision is kept in the content store, which isn't configured",
        )
    stored = store.get_task(task_id)
    if stored is None or stored.task.run_id != run_id:
        raise DecisionRefused("not_found", "no such task or gate")
    task = stored.task
    spec = next((g for g in task.definition_of_done.gates if g.id == gate_id), None)
    if spec is None or spec.kind is not GateKind.HUMAN:
        raise DecisionRefused("no_human_gate", "this task has no human gate with that id")
    named = round or (task.attempt, task.submission)
    # the same checks the gate applies when it runs (who may decide, a reason per criterion)
    result = decide_human_gate(
        spec,
        decided_by=decided_by,
        findings=findings,
        task_id=task_id,
        attempt=named[0],
        submission=named[1],
    )
    body = {
        "decided_by": decided_by,
        "findings": {c: [ok, why] for c, (ok, why) in findings.items()},
    }
    wanted = content.human_decision_hash(
        org.id,
        body,
        run_id=run_id,
        task_id=task_id,
        attempt=named[0],
        submission=named[1],
        gate_id=gate_id,
    )
    passes = result.outcome is GateOutcome.PASSED
    refused = False
    kept = ""
    with store.transaction() as uow:
        # everything is checked again inside the transaction that writes
        now = uow.get_task(task_id)
        if now is None:
            raise DecisionRefused("not_found", "no such task or gate")
        task = now.task
        mode = _mode(uow, org.id, run_id, decided_by, passes=passes, solo=solo)
        events = uow.task_events(run_id, task_id)
        current = (task.attempt, task.submission)
        on_file = [
            e
            for e in _decisions(events, gate_id)
            if (e.payload.get("attempt"), e.payload.get("submission")) == named
        ]
        if round is not None and wanted is not None:
            for e in on_file:
                if e.payload.get("decision_hash") == wanted:  # a replay: nothing is written
                    return RecordedDecision(
                        task_id,
                        gate_id,
                        named[0],
                        named[1],
                        str(e.payload.get("outcome")),
                        wanted,
                        True,
                    )
        if _ended(uow, run_id):
            raise DecisionRefused("run_ended", "the run has ended; a decision would change nothing")
        if _stop_requested(uow, run_id):
            raise DecisionRefused(
                "run_stopping", "the run was asked to stop; a decision would change nothing"
            )
        if task.status is not TaskStatus.VERIFYING:
            raise DecisionRefused(
                "not_waiting",
                f"the task is {task.status.value}, not waiting for a decision",
            )
        if named != current:
            raise DecisionRefused(
                "stale_round",
                "this decision names a round that is not the task's current one; the task "
                f"is at attempt {current[0]}, submission {current[1]}",
            )
        if on_file:
            raise DecisionRefused(
                "already_decided", "the gate already has a decision in this round"
            )
        output_hash = attempt_output_hash(task.attempt, events)
        expired = output_hash is not None and content.describe(org.id, output_hash) is None
        if dry_run and not expired:
            return RecordedDecision(
                task_id, gate_id, named[0], named[1], "dry_run", wanted or "", False
            )
        if expired:
            # the input the person was asked about is gone (now >= its expiry, no legal hold,
            # T78): no decision is kept and nothing moves; the refusal is one ledger fact (ids
            # and one fixed word), written before the error is raised to the person
            refused = True
            if dry_run:  # the real call refuses; a dry run says so and writes nothing
                raise DecisionRefused(
                    "human_decision_refused",
                    "the input of this task expired before this decision; the "
                    f"{WORKING_DAYS}-day rule stops the task (ND55), so a decision would "
                    "change nothing",
                    why="input_expired",
                )
            uow.append(
                EventDraft(
                    stream=run_stream(run_id),
                    type=EventType.DECISION_RECORDED,
                    actor=decided_by,
                    run_id=run_id,
                    task_id=task_id,
                    payload={
                        "kind": HUMAN_DECISION_REFUSED,
                        "task_id": task_id,
                        "attempt": task.attempt,
                        "why": "input_expired",
                    },
                )
            )
        else:
            kept = content.capture_human_decision(
                uow,
                org.id,
                body,
                run_id=run_id,
                task_id=task_id,
                attempt=task.attempt,
                submission=task.submission,
                gate_id=gate_id,
            )
            # R1.32 S2: the person joins every live row of the run (same transaction)
            content.tag_run_rows(uow, org.id, run_id, decided_by)
            uow.append(
                EventDraft(
                    stream=run_stream(run_id),
                    type=EventType.DECISION_RECORDED,
                    actor=decided_by,
                    run_id=run_id,
                    task_id=task_id,
                    payload={
                        "kind": HUMAN_DECISION,
                        "gate": gate_id,
                        "attempt": task.attempt,
                        "submission": task.submission,
                        "outcome": result.outcome.value,
                        "decision_hash": kept,
                        "approval_mode": mode.value,  # SA-19: on every new entry, every caller
                        **(
                            {"solo_proof": SoloProof.NONE.value}  # only a solo passing decision
                            if mode is ApprovalMode.SOLO and passes
                            else {}
                        ),
                    },
                )
            )
    if refused:
        raise DecisionRefused(
            "human_decision_refused",
            "the input of this task expired before this decision; the "
            f"{WORKING_DAYS}-day rule stops the task (ND55), so a decision would change nothing",
            why="input_expired",
        )
    return RecordedDecision(
        task_id,
        gate_id,
        task.attempt,
        task.submission,
        result.outcome.value,
        kept,
        False,
    )
