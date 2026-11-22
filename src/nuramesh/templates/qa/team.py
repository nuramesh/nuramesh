"""The QA and testing team template (R2.06): a QA and testing group on the existing task model.

No new engine: a plan is ordinary tasks. Each feature has two tasks, **Design tests** (Test
designer, checked by a rule gate and by the independent Test reviewer) and **Run tests** (Test
runner, a rule gate), and the second waits for the first. Add features by adding names to
`ITEMS`. The runner gate checks the reported result (passes, zero failures, no error), so a
failing result is refused; with `--fake` the result itself is scripted.

A task is done only when its gates pass. With `--fake` every model answer is scripted below, so a
run needs no key and costs nothing; budgets, prices, policies, gates and the ledger are real.
Only run team folders you trust.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any

from nuramesh.core.budgets import Budget, BudgetScope
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.models import FakeModel, Message, ModelRequest
from nuramesh.core.money import Money
from nuramesh.core.tasks import Task
from nuramesh.engine.run import PlannedTask, WorkContext
from nuramesh.team import Team, TeamEnv

ITEMS = ("feature-1", "feature-2")
REVIEW = "The plan covers the happy path, an edge case and a failure case with expected results."


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"plan-ok": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    if "[stage-1]" in text:
        return (
            "Test plan: covers the happy path, an empty input and a "
            "failure case, each with an expected result."
        )
    return "Result: 12 passed, 0 failed for this feature."


def rule(gate: str, criterion: str, value: str) -> GateSpec:
    """A cheap deterministic gate: the output must contain `value`."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": {"type": "contains", "value": value}}]},
    )


def outcome_rule(gate: str, criterion: str, pattern: str, forbidden: str) -> GateSpec:
    """A deterministic gate on the result itself: it must match `pattern` and must not contain
    `forbidden`, so a wrong or failing result is refused, not only a missing marker."""
    checks = [
        {"type": "matches", "pattern": pattern},
        {"type": "not_contains", "value": forbidden, "case_sensitive": False},
    ]
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": c} for c in checks]},
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: both tasks per feature, a worker per agent and the run budget."""
    run = env.run_id

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You work on a QA and testing group. Be brief."),
                Message(role="user", content=task.instructions),
            ],
            max_output_tokens=200,
        )
        return {"text": reply.response.text}

    plan: list[PlannedTask] = []
    for item in ITEMS:
        first = Task(
            id=f"{run}-{item}-1",
            run_id=run,
            title=f"Design tests: {item}",
            instructions=f"[stage-1] Design tests {item}",
            assignee="test-designer",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(
                    Criterion(id="designed", text="The output says the feature is designed"),
                    Criterion(
                        id="plan-ok",
                        text="The test plan covers the feature including edge and failure cases",
                    ),
                ),
                gates=(
                    rule("is-designed", "designed", "Test plan:"),
                    GateSpec(
                        id="review",
                        kind=GateKind.VERIFIER,
                        checks=("plan-ok",),
                        config={"verifier": "test-reviewer", "require_different_model": True},
                    ),
                ),
            ),
        )
        second = Task(
            id=f"{run}-{item}-2",
            run_id=run,
            title=f"Run tests: {item}",
            instructions=f"[stage-2] Run tests {item}",
            assignee="test-runner",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(Criterion(id="run", text="The output says the step is run"),),
                gates=(outcome_rule("is-run", "run", r"Result: \d+ passed, 0 failed", "error"),),
            ),
        )
        plan.append(PlannedTask(first))
        plan.append(PlannedTask(second, depends_on=(first.id,)))
    return Team(
        plan=plan,
        workers={"test-designer": work, "test-runner": work},
        fake_provider=FakeModel(default=fake_model),
        run_budgets=[
            Budget(
                id="rb-" + hashlib.sha256(run.encode()).hexdigest()[:12],
                scope=BudgetScope.RUN,
                scope_id=run,
                limit=Money.of(Decimal("2.00")),
            )
        ],
    )
