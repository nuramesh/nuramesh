from collections.abc import Iterator
from typing import Any

import pytest
from content_helpers import pure_hasher
from pydantic import ValidationError

from nuramesh.core.gates import (
    Criterion,
    DefinitionOfDone,
    Evidence,
    GateKind,
    GateOutcome,
    GateResult,
    GateSpec,
    GateState,
    Verdict,
    evidence_problems,
    verify_submission,
)
from nuramesh.core.tasks import (
    ALLOWED,
    Task,
    TaskStatus,
    TaskTransitionError,
    TransitionCode,
    transition,
)

S = TaskStatus


def dod() -> DefinitionOfDone:
    return DefinitionOfDone(
        criteria=(
            Criterion(id="tests-pass", text="All tests pass"),
            Criterion(id="matches-spec", text="Behaviour matches the spec"),
            Criterion(id="style", text="Follows the style guide"),
        ),
        gates=(
            GateSpec(id="verifier", kind=GateKind.VERIFIER, checks=("matches-spec",)),
            GateSpec(id="tests", kind=GateKind.TEST_COMMAND, checks=("tests-pass",)),
            GateSpec(id="lint", kind=GateKind.RULE, checks=("style",)),
            GateSpec(id="nice", kind=GateKind.VERIFIER, checks=("style",), required=False),
        ),
    )


def task(**overrides: Any) -> Task:
    data: dict[str, Any] = {
        "id": "t1",
        "run_id": "r1",
        "title": "Add login",
        "definition_of_done": dod(),
    }
    data.update(overrides)
    return Task.model_validate(data)


def result(
    gate: str,
    outcome: GateOutcome,
    *criteria: str,
    attempt: int = 1,
    submission: int = 1,
    task_id: str = "t1",
) -> GateResult:
    verdict = Verdict.MET if outcome is GateOutcome.PASSED else Verdict.NOT_MET
    return GateResult(
        task_id=task_id,
        attempt=attempt,
        submission=submission,
        gate_id=gate,
        outcome=outcome,
        evidence=()
        if outcome is GateOutcome.ERROR
        else tuple(Evidence(criterion=c, verdict=verdict, detail="checked") for c in criteria),
        checked_by="agent:rev",
        error="timed out" if outcome is GateOutcome.ERROR else None,
    )


def passed(gate: str, *criteria: str, **kw: Any) -> GateResult:
    return result(gate, GateOutcome.PASSED, *criteria, **kw)


def failed(gate: str, criterion: str, **kw: Any) -> GateResult:
    return result(gate, GateOutcome.FAILED, criterion, **kw)


def errored(gate: str, **kw: Any) -> GateResult:
    return result(gate, GateOutcome.ERROR, **kw)


def all_passed(**kw: Any) -> list[GateResult]:
    return [
        passed("verifier", "matches-spec", **kw),
        passed("tests", "tests-pass", **kw),
        passed("lint", "style", **kw),
    ]


def go(t: Task, to: TaskStatus, **kw: Any) -> Task:
    return transition(t, to, reason=kw.pop("reason", "because"), **kw).task


def to_verifying(t: Task | None = None) -> Task:
    t = go(t or task(), S.ASSIGNED, assignee="dev-1")
    t = go(t, S.IN_PROGRESS)
    t = go(t, S.SUBMITTED)
    return go(t, S.VERIFYING)


def resubmit(t: Task) -> Task:
    return go(go(t, S.SUBMITTED), S.VERIFYING)


def refused(code: TransitionCode, t: Task, to: TaskStatus, **kwargs: Any) -> TaskTransitionError:
    with pytest.raises(TaskTransitionError) as caught:
        transition(t, to, reason=kwargs.pop("reason", "because"), **kwargs)
    assert caught.value.code is code
    return caught.value


def test_happy_path_to_done_and_move_record() -> None:
    t = to_verifying()
    assert (t.status, t.attempt, t.submission, t.assignee) == (S.VERIFYING, 1, 1, "dev-1")
    move = transition(t, S.DONE, reason="all gates passed", gate_results=all_passed())
    assert move.task.status is S.DONE
    assert (move.from_status, move.to_status, move.attempt_before, move.attempt_after) == (
        S.VERIFYING,
        S.DONE,
        1,
        1,
    )
    assert move.verification is not None and move.verification.passed
    payload = move.ledger_payload(pure_hasher())
    assert payload["from"] == "verifying" and payload["to"] == "done"
    assert {v["gate"]: v["state"] for v in payload["verification"]} == {
        "verifier": "passed",
        "tests": "passed",
        "lint": "passed",
        "nice": "missing",
    }
    assert t.status is S.VERIFYING  # the input is unchanged


def test_done_needs_every_required_gate() -> None:
    t = to_verifying()
    err = refused(TransitionCode.VERIFICATION_INCOMPLETE, t, S.DONE, gate_results=all_passed()[:2])
    assert [p.gate_id for p in err.problems] == ["lint"]
    refused(TransitionCode.VERIFICATION_INCOMPLETE, t, S.DONE)


def test_done_refused_when_a_required_gate_failed() -> None:
    t = to_verifying()
    results = [*all_passed()[:2], failed("lint", "style")]
    err = refused(TransitionCode.VERIFICATION_FAILED, t, S.DONE, gate_results=results)
    assert err.problems[0].state is GateState.FAILED


def test_failure_wins_over_missing_on_done() -> None:
    t = to_verifying()
    refused(TransitionCode.VERIFICATION_FAILED, t, S.DONE, gate_results=[failed("lint", "style")])


def test_error_outcome_never_counts_as_passed() -> None:
    t = to_verifying()
    results = [all_passed()[0], errored("tests"), all_passed()[2]]
    refused(TransitionCode.VERIFICATION_INCOMPLETE, t, S.DONE, gate_results=results)


def test_rerun_after_error_counts() -> None:
    t = to_verifying()
    results = [errored("tests"), *all_passed()]
    assert go(t, S.DONE, gate_results=results).status is S.DONE


def test_a_failure_is_final_for_the_submission_reruns_cant_overturn_it() -> None:
    t = to_verifying()
    for order in (
        [failed("lint", "style"), *all_passed()],
        [*all_passed(), failed("lint", "style")],
    ):
        refused(TransitionCode.VERIFICATION_FAILED, t, S.DONE, gate_results=order)


def test_passed_result_without_evidence_for_a_checked_criterion_counts_as_failed() -> None:
    two = DefinitionOfDone(
        criteria=(Criterion(id="a", text="A"), Criterion(id="b", text="B")),
        gates=(GateSpec(id="g", kind=GateKind.VERIFIER, checks=("a", "b")),),
    )
    t = to_verifying(task(definition_of_done=two))
    refused(TransitionCode.VERIFICATION_FAILED, t, S.DONE, gate_results=[passed("g", "a")])
    assert go(t, S.DONE, gate_results=[passed("g", "a", "b")]).status is S.DONE


def test_passed_result_with_evidence_for_unchecked_criterion_counts_as_failed() -> None:
    t = to_verifying()
    bad = [*all_passed()[:2], passed("lint", "style", "tests-pass")]
    refused(TransitionCode.VERIFICATION_FAILED, t, S.DONE, gate_results=bad)


def test_optional_gate_failure_does_not_block() -> None:
    t = to_verifying()
    assert go(t, S.DONE, gate_results=[*all_passed(), failed("nice", "style")]).status is S.DONE


def test_results_for_other_attempts_or_submissions_are_ignored() -> None:
    t = to_verifying()
    t = go(t, S.IN_PROGRESS, gate_results=[failed("tests", "tests-pass")])
    assert (t.attempt, t.submission) == (2, 1)
    t = resubmit(t)
    assert t.submission == 2
    refused(TransitionCode.VERIFICATION_INCOMPLETE, t, S.DONE, gate_results=all_passed())
    ok = all_passed(attempt=2, submission=2)
    assert go(t, S.DONE, gate_results=ok).status is S.DONE


def test_stale_passes_dont_count_after_block_and_resubmission() -> None:
    t = to_verifying()
    t = go(t, S.BLOCKED, reason="verifier budget exhausted")
    t = go(t, S.NEEDS_HUMAN, reason="raise budget?")
    t = go(t, S.ASSIGNED, assignee="dev-2", reason="budget raised")
    t = go(t, S.IN_PROGRESS, reason="resume")
    assert (t.attempt, t.submission) == (1, 1)  # same attempt continues
    t = resubmit(t)
    assert t.submission == 2
    old = all_passed()  # recorded for submission 1, before the block
    refused(TransitionCode.VERIFICATION_INCOMPLETE, t, S.DONE, gate_results=old)
    assert go(t, S.DONE, gate_results=all_passed(submission=2)).status is S.DONE


def test_foreign_and_unknown_gate_results_are_refused() -> None:
    t = to_verifying()
    other = passed("tests", "tests-pass", task_id="t2")
    refused(TransitionCode.FOREIGN_RESULT, t, S.DONE, gate_results=[*all_passed(), other])
    ghost = passed("ghost", "style")
    refused(TransitionCode.UNKNOWN_GATE, t, S.DONE, gate_results=[*all_passed(), ghost])


def test_generator_input_is_read_once_and_works() -> None:
    t = to_verifying()

    def gen() -> Iterator[GateResult]:
        yield from all_passed()

    assert go(t, S.DONE, gate_results=gen()).status is S.DONE


def test_non_result_items_are_refused() -> None:
    t = to_verifying()
    refused(TransitionCode.UNEXPECTED_ARGUMENT, t, S.DONE, gate_results=[{"gate_id": "x"}])


def test_results_only_where_they_are_used() -> None:
    t = to_verifying()
    refused(TransitionCode.UNEXPECTED_ARGUMENT, t, S.BLOCKED, gate_results=all_passed())
    refused(TransitionCode.UNEXPECTED_ARGUMENT, t, S.CANCELLED, gate_results=all_passed())
    refused(TransitionCode.UNEXPECTED_ARGUMENT, task(), S.NEEDS_HUMAN, gate_results=all_passed())


def test_retry_until_attempts_run_out_then_human_grants_more() -> None:
    t = to_verifying(task(max_attempts=2))
    t = go(t, S.IN_PROGRESS, gate_results=[failed("tests", "tests-pass")])
    assert (t.attempt, t.attempt_closed) == (2, False)
    t = resubmit(t)
    fail2 = [failed("tests", "tests-pass", attempt=2, submission=2)]
    refused(TransitionCode.ATTEMPTS_EXHAUSTED, t, S.IN_PROGRESS, gate_results=fail2)
    t = go(t, S.NEEDS_HUMAN, reason="out of attempts", gate_results=fail2)
    assert (t.status, t.attempt_closed) == (S.NEEDS_HUMAN, True)
    refused(TransitionCode.ATTEMPTS_EXHAUSTED, go(t, S.ASSIGNED), S.IN_PROGRESS)
    t = go(t, S.ASSIGNED, extra_attempts=1, assignee="dev-2", reason="human approved")
    assert (t.max_attempts, t.assignee, t.attempt_closed) == (3, "dev-2", True)
    t = go(t, S.IN_PROGRESS, reason="third try")
    assert (t.attempt, t.attempt_closed) == (3, False)


def test_retry_and_escalation_rules() -> None:
    t = to_verifying()
    refused(TransitionCode.VERIFICATION_PASSED, t, S.IN_PROGRESS, gate_results=all_passed())
    refused(TransitionCode.VERIFICATION_PASSED, t, S.NEEDS_HUMAN, gate_results=all_passed())
    refused(TransitionCode.VERIFICATION_INCOMPLETE, t, S.IN_PROGRESS, gate_results=all_passed()[:1])
    refused(TransitionCode.VERIFICATION_INCOMPLETE, t, S.NEEDS_HUMAN)
    some_missing_one_failed = [failed("tests", "tests-pass")]
    assert go(t, S.IN_PROGRESS, gate_results=some_missing_one_failed).attempt == 2


def test_infrastructure_errors_dont_cost_the_agent_an_attempt() -> None:
    t = to_verifying()
    refused(
        TransitionCode.VERIFICATION_INCOMPLETE, t, S.IN_PROGRESS, gate_results=[errored("tests")]
    )
    escalated = transition(
        t, S.NEEDS_HUMAN, reason="verifier down", gate_results=[errored("tests")]
    )
    assert not escalated.closed_attempt
    t = escalated.task
    assert (t.attempt, t.attempt_closed) == (1, False)
    t = go(go(t, S.ASSIGNED), S.IN_PROGRESS)
    assert t.attempt == 1


def test_block_during_work_resumes_same_attempt() -> None:
    t = go(task(), S.ASSIGNED, assignee="dev-1")
    t = go(t, S.IN_PROGRESS)
    t = go(go(go(t, S.BLOCKED), S.NEEDS_HUMAN), S.ASSIGNED)
    assert go(t, S.IN_PROGRESS).attempt == 1


def test_block_during_verification_keeps_attempt_open() -> None:
    t = go(to_verifying(), S.BLOCKED, reason="verifier budget exhausted")
    assert (t.attempt, t.attempt_closed) == (1, False)


def test_cancel_and_final_states() -> None:
    t = to_verifying()
    cancelled = go(t, S.CANCELLED, reason="run cancelled")
    refused(TransitionCode.FINAL, cancelled, S.ASSIGNED)
    done = go(t, S.DONE, gate_results=all_passed())
    refused(TransitionCode.FINAL, done, S.CANCELLED)
    refused(TransitionCode.MISSING_REASON, done, S.CANCELLED, reason=" ")  # reason checked first


def _task_in(status: TaskStatus) -> Task:
    started = status in (S.IN_PROGRESS, S.SUBMITTED, S.VERIFYING, S.DONE)
    submitted = status in (S.SUBMITTED, S.VERIFYING, S.DONE)
    return task(
        status=status,
        assignee=None if status is S.PROPOSED else "dev-1",
        attempt=1 if started else 0,
        submission=1 if submitted else 0,
    )


@pytest.mark.parametrize("source", list(S))
@pytest.mark.parametrize("target", list(S))
def test_moves_outside_the_table_are_refused(source: TaskStatus, target: TaskStatus) -> None:
    if target in ALLOWED[source]:
        return
    code = TransitionCode.FINAL if source in (S.DONE, S.CANCELLED) else TransitionCode.NOT_ALLOWED
    refused(code, _task_in(source), target)


def test_every_open_status_can_be_cancelled() -> None:
    for source, targets in ALLOWED.items():
        assert (S.CANCELLED in targets) == (source not in (S.DONE, S.CANCELLED))


def test_argument_rules() -> None:
    t = task()
    refused(TransitionCode.MISSING_REASON, t, S.ASSIGNED, assignee="dev-1", reason="  ")
    refused(TransitionCode.MISSING_ASSIGNEE, t, S.ASSIGNED)
    refused(TransitionCode.UNEXPECTED_ARGUMENT, t, S.NEEDS_HUMAN, assignee="dev-1")
    refused(TransitionCode.UNEXPECTED_ARGUMENT, t, S.ASSIGNED, assignee="dev-1", extra_attempts=1)
    human = go(t, S.NEEDS_HUMAN, reason="policy ask")
    refused(TransitionCode.UNEXPECTED_ARGUMENT, human, S.ASSIGNED, assignee="d", extra_attempts=-1)
    refused(TransitionCode.MISSING_ASSIGNEE, human, S.ASSIGNED, extra_attempts=1)


def test_extra_attempts_are_capped_and_refused_at_the_cap() -> None:
    t = go(task(max_attempts=49), S.NEEDS_HUMAN, reason="ask")
    assert go(t, S.ASSIGNED, assignee="d", extra_attempts=10).max_attempts == 50
    at_cap = go(task(max_attempts=50), S.NEEDS_HUMAN, reason="ask")
    refused(TransitionCode.UNEXPECTED_ARGUMENT, at_cap, S.ASSIGNED, assignee="d", extra_attempts=1)


def test_refused_move_changes_nothing() -> None:
    t = to_verifying(task(max_attempts=1))
    fail = [failed("tests", "tests-pass")]
    refused(TransitionCode.ATTEMPTS_EXHAUSTED, t, S.IN_PROGRESS, gate_results=fail)
    assert (t.status, t.attempt_closed) == (S.VERIFYING, False)


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "assigned"},  # no assignee
        {"status": "in_progress", "assignee": "d", "attempt": 0},
        {"status": "verifying", "assignee": "d", "attempt": 1, "submission": 0},
        {
            "status": "verifying",
            "assignee": "d",
            "attempt": 1,
            "submission": 1,
            "attempt_closed": True,
        },
        {"status": "done", "assignee": "d", "attempt": 1, "submission": 1, "attempt_closed": True},
        {"attempt": 4, "max_attempts": 3},
        {"parent_id": "t1"},
        {"max_attempts": 0},
        {"attempt_closed": True},
    ],
)
def test_task_invariants(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        task(**bad)


def test_definition_of_done_rules() -> None:
    c = (Criterion(id="a", text="A"), Criterion(id="b", text="B"))
    g = GateSpec(id="g", kind=GateKind.RULE, checks=("a",))
    with pytest.raises(ValidationError, match="not checked by any required gate"):
        DefinitionOfDone(criteria=c, gates=(g,))
    optional_b = GateSpec(id="o", kind=GateKind.RULE, checks=("b",), required=False)
    with pytest.raises(ValidationError, match="not checked by any required gate"):
        DefinitionOfDone(criteria=c, gates=(g, optional_b))
    unknown = GateSpec(id="g", kind=GateKind.RULE, checks=("z",))
    with pytest.raises(ValidationError, match="unknown criteria"):
        DefinitionOfDone(criteria=c[:1], gates=(unknown,))
    with pytest.raises(ValidationError, match="gate ids must be unique"):
        DefinitionOfDone(criteria=c[:1], gates=(g, g))
    with pytest.raises(ValidationError, match="criterion ids must be unique"):
        DefinitionOfDone(criteria=(c[0], c[0]), gates=(g,))
    with pytest.raises(ValidationError):
        GateSpec(id="g", kind=GateKind.RULE, checks=("a", "a"))
    with pytest.raises(ValidationError):
        GateSpec(id="g", kind=GateKind.RULE, checks=("a",), config={"ratio": 0.5})


def test_gate_config_is_read_only_and_round_trips() -> None:
    spec = GateSpec(
        id="g", kind=GateKind.TEST_COMMAND, checks=("a",), config={"cmd": ["pytest", "-q"]}
    )
    with pytest.raises(TypeError):
        spec.config["cmd"] = ["rm", "-rf", "/"]  # type: ignore[index]
    assert spec.model_dump(mode="json")["config"] == {"cmd": ["pytest", "-q"]}
    assert GateSpec.model_validate_json(spec.model_dump_json()) == spec
    assert hash(spec) == hash(GateSpec.model_validate(spec.model_dump()))
    assert isinstance(hash(task()), int)  # tasks and definitions of done are hashable


def test_run_order_puts_deterministic_gates_first() -> None:
    assert [g.id for g in dod().run_order()] == ["tests", "lint", "verifier", "nice"]


@pytest.mark.parametrize(
    ("outcome", "evidence", "error"),
    [
        (GateOutcome.PASSED, (), None),
        (GateOutcome.PASSED, (("a", Verdict.NOT_MET),), None),
        (GateOutcome.PASSED, (("a", Verdict.MET),), "oops"),
        (GateOutcome.FAILED, (("a", Verdict.MET),), None),
        (GateOutcome.ERROR, (), None),
        (GateOutcome.PASSED, (("a", Verdict.MET), ("a", Verdict.MET)), None),
    ],
)
def test_gate_result_consistency(
    outcome: GateOutcome, evidence: tuple[tuple[str, Verdict], ...], error: str | None
) -> None:
    with pytest.raises(ValidationError):
        GateResult(
            task_id="t1",
            attempt=1,
            submission=1,
            gate_id="g",
            outcome=outcome,
            evidence=tuple(Evidence(criterion=c, verdict=v, detail="d") for c, v in evidence),
            checked_by="x",
            error=error,
        )


def test_evidence_problems() -> None:
    spec = GateSpec(id="g", kind=GateKind.VERIFIER, checks=("a", "b"))
    assert evidence_problems(spec, passed("g", "a", "b")) == []
    assert "passed without evidence" in evidence_problems(spec, passed("g", "a"))[0]
    assert "doesn't check" in evidence_problems(spec, passed("g", "a", "b", "c"))[0]
    assert "not 'g'" in evidence_problems(spec, passed("h", "a", "b"))[0]


def test_verify_submission_is_order_independent() -> None:
    results = [errored("tests"), *all_passed(), failed("nice", "style"), errored("lint")]
    first = verify_submission(dod(), results, task_id="t1", attempt=1, submission=1)
    second = verify_submission(dod(), results[::-1], task_id="t1", attempt=1, submission=1)
    assert [(g.gate_id, g.state) for g in first.gates] == [
        (g.gate_id, g.state) for g in second.gates
    ]
    assert first.passed


def test_ledger_payload_ties_verdicts_to_result_hashes() -> None:
    from nuramesh.core.gates import result_hash

    results = all_passed()
    payload = transition(to_verifying(), S.DONE, reason="ok", gate_results=results).ledger_payload(
        pure_hasher()
    )
    by_gate = {v["gate"]: v["deciding_results"] for v in payload["verification"]}
    assert by_gate["tests"] == [result_hash(results[1], pure_hasher())]
    assert by_gate["nice"] == []
