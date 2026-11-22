"""The Scrum team template (ND64): one sprint as a plan of tasks on the existing task model.

No new engine: a sprint is an ordinary plan. The product owner (Chief of Staff) delegates:

1. **Sprint planning** (scrum master): the sprint goal and the stories.
2. **Two stories** (developer 1 and 2, in parallel): each is checked by a rule gate and by the
   independent reviewer, who sees only the task, the definition of done and the output.
3. **Sprint test** (tester): runs after both stories are done.
4. **Sprint review** (scrum master): after the tests, with a rule gate.

A task is done only when its gates pass. With `--fake` every model answer is scripted below, so a
run needs no key and costs nothing; budgets, prices, policies, gates and the ledger are real.
Replace the prompts and the fake answers with your own work. The tester gate `contains "all tests
passed"` only checks the scripted fake model's output: a reference-run gate, not a real test.
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

ANSWERS = {
    "[sprint-plan]": "Sprint goal: greet users. Stories: story-1 greeting, story-2 farewell.",
    "[story-1]": "Done: story-1 greeting implemented with a unit test.",
    "[story-2]": "Done: story-2 farewell implemented with a unit test.",
    "[sprint-test]": "Result: all tests passed for both stories.",
    "[sprint-review]": "Sprint review: the goal was met and both stories are done.",
}
REVIEW = "The output states that the story was implemented and tested, which is what was asked."


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"story-ok": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    return next((a for marker, a in ANSWERS.items() if marker in text), "No answer.")


def rule(gate: str, criterion: str, value: str) -> GateSpec:
    """A cheap deterministic gate: the output must contain `value`."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": {"type": "contains", "value": value}}]},
    )


def task(run: str, key: str, title: str, assignee: str, done: DefinitionOfDone) -> Task:
    return Task(
        id=f"{run}-{key}",
        run_id=run,
        title=title,
        instructions=f"[{key}] {title}",
        assignee=assignee,
        max_attempts=2,
        definition_of_done=done,
    )


def story(run: str, key: str, title: str, developer: str) -> Task:
    """A story: a rule gate, then the independent reviewer (a different model from the worker)."""
    done = DefinitionOfDone(
        criteria=(
            Criterion(id="implemented", text="The output says the story is done"),
            Criterion(id="story-ok", text="The story is implemented and tested"),
        ),
        gates=(
            rule("is-done", "implemented", "Done:"),
            GateSpec(
                id="review",
                kind=GateKind.VERIFIER,
                checks=("story-ok",),
                config={"verifier": "reviewer", "require_different_model": True},
            ),
        ),
    )
    return task(run, key, title, developer, done)


def single(criterion: str, text: str, value: str) -> DefinitionOfDone:
    return DefinitionOfDone(
        criteria=(Criterion(id=criterion, text=text),), gates=(rule("check", criterion, value),)
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: the sprint plan, a worker per agent and the run budget."""
    run = env.run_id

    def work(task_: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You are a member of a Scrum team. Be brief."),
                Message(role="user", content=f"{task_.instructions}\n{task_.title}"),
            ],
            max_output_tokens=200,
        )
        return {"text": reply.response.text}

    plan = [
        PlannedTask(
            task(
                run,
                "sprint-plan",
                "Plan the sprint: goal and stories",
                "scrum-master",
                single("plan", "The plan names a sprint goal", "Sprint goal"),
            )
        ),
        PlannedTask(
            story(run, "story-1", "Story 1: greet users", "developer-1"),
            depends_on=(f"{run}-sprint-plan",),
        ),
        PlannedTask(
            story(run, "story-2", "Story 2: say goodbye", "developer-2"),
            depends_on=(f"{run}-sprint-plan",),
        ),
        PlannedTask(
            task(
                run,
                "sprint-test",
                "Run the tests for the sprint",
                "tester",
                single("green", "The tests pass", "all tests passed"),
            ),
            depends_on=(f"{run}-story-1", f"{run}-story-2"),
        ),
        PlannedTask(
            task(
                run,
                "sprint-review",
                "Hold the sprint review",
                "scrum-master",
                single("reviewed", "The review says whether the goal was met", "Sprint review"),
            ),
            depends_on=(f"{run}-sprint-test",),
        ),
    ]
    return Team(
        plan=plan,
        workers={
            "scrum-master": work,
            "developer-1": work,
            "developer-2": work,
            "tester": work,
        },
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
