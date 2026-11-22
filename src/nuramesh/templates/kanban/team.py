"""The Kanban team template (ND64): a flow of cards on the existing task model.

No new engine: a board is an ordinary plan. Each card has two tasks, **Doing** (the developer,
checked by a rule gate and by the independent reviewer) and **Testing** (the tester). The
work-in-progress limit is one: a card's Doing waits for the previous card's Testing, so only one
card is in flight at a time. Add cards by adding names to `CARDS`.

A task is done only when its gates pass. With `--fake` every model answer is scripted below, so a
run needs no key and costs nothing; budgets, prices, policies, gates and the ledger are real.
The tester gate `contains "all tests passed"` only checks the scripted fake model's output: a
reference-run gate, not a real test. Only run team folders you trust.
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

CARDS = ("card-1", "card-2")
REVIEW = "The output says the card was built and what changed, which is what was asked."


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"card-ok": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    if "[doing]" in text:
        return "Built: the change for this card is made and committed."
    return "Result: all tests passed for this card."


def rule(gate: str, criterion: str, value: str) -> GateSpec:
    """A cheap deterministic gate: the output must contain `value`."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": {"type": "contains", "value": value}}]},
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: the cards in flow order, a worker per agent and the run budget."""
    run = env.run_id

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You work on a Kanban board. Be brief."),
                Message(role="user", content=task.instructions),
            ],
            max_output_tokens=200,
        )
        return {"text": reply.response.text}

    plan: list[PlannedTask] = []
    previous: tuple[str, ...] = ()  # the WIP limit: the last task of the card before
    for card in CARDS:
        doing = Task(
            id=f"{run}-{card}-doing",
            run_id=run,
            title=f"Doing: {card}",
            instructions=f"[doing] Build {card}",
            assignee="developer",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(
                    Criterion(id="built", text="The output says the card is built"),
                    Criterion(id="card-ok", text="The card is built as described"),
                ),
                gates=(
                    rule("is-built", "built", "Built:"),
                    GateSpec(
                        id="review",
                        kind=GateKind.VERIFIER,
                        checks=("card-ok",),
                        config={"verifier": "reviewer", "require_different_model": True},
                    ),
                ),
            ),
        )
        testing = Task(
            id=f"{run}-{card}-testing",
            run_id=run,
            title=f"Testing: {card}",
            instructions=f"[testing] Test {card}",
            assignee="tester",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(Criterion(id="green", text="The tests pass"),),
                gates=(rule("tests", "green", "all tests passed"),),
            ),
        )
        plan.append(PlannedTask(doing, depends_on=previous))
        plan.append(PlannedTask(testing, depends_on=(doing.id,)))
        previous = (testing.id,)
    return Team(
        plan=plan,
        workers={"developer": work, "tester": work},
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
