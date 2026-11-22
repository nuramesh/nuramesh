"""The DevOps team template (R2.06): a DevOps change desk on the existing task model.

No new engine: a plan is ordinary tasks. Each change has two tasks, **Plan** (Change planner,
checked by a rule gate and by the independent Change reviewer) and **Staging check** (Staging
checker, a rule gate), and the second waits for the first. Add changes by adding names to
`ITEMS`. No policy here allows a production deploy: that action is not listed, so it is denied
and would ask a human.

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

ITEMS = ("change-1", "change-2")
REVIEW = "The plan has ordered steps, a health check and a rollback path."


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"plan-ok": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    if "[stage-1]" in text:
        return (
            "Plan: rollout steps, a health check after each step and a "
            "rollback to the previous version."
        )
    return "Staging check: passed, 3 of 3 health checks green after the rollout."


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
    """Build the run: both tasks per change, a worker per agent and the run budget."""
    run = env.run_id

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You work on a DevOps change desk. Be brief."),
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
            title=f"Plan: {item}",
            instructions=f"[stage-1] Plan {item}",
            assignee="change-planner",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(
                    Criterion(id="planned", text="The output says the change is planned"),
                    Criterion(
                        id="plan-ok",
                        text="The plan has ordered steps, health checks and a rollback",
                    ),
                ),
                gates=(
                    rule("is-planned", "planned", "Plan:"),
                    GateSpec(
                        id="review",
                        kind=GateKind.VERIFIER,
                        checks=("plan-ok",),
                        config={"verifier": "change-reviewer", "require_different_model": True},
                    ),
                ),
            ),
        )
        second = Task(
            id=f"{run}-{item}-2",
            run_id=run,
            title=f"Staging check: {item}",
            instructions=f"[stage-2] Staging check {item}",
            assignee="staging-checker",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(Criterion(id="checked", text="The output says the step is checked"),),
                gates=(
                    outcome_rule(
                        "is-checked",
                        "checked",
                        r"Staging check: passed, (\d+) of \1 health checks green",
                        "rollback",
                    ),
                ),
            ),
        )
        plan.append(PlannedTask(first))
        plan.append(PlannedTask(second, depends_on=(first.id,)))
    return Team(
        plan=plan,
        workers={"change-planner": work, "staging-checker": work},
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
