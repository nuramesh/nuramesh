"""The Support team template (R2.06): a customer support desk on the existing task model.

No new engine: a plan is ordinary tasks. Each ticket has two tasks, **Triage** (Triager, checked
by a rule gate and by the independent Reply checker) and **Reply** (Responder, a rule gate), and
the second waits for the first. Add tickets by adding names to `ITEMS`. The reply draft is only
a draft: sending it to a customer is not allowed by any policy here.

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

ITEMS = ("ticket-1", "ticket-2")
REVIEW = "The triage names a category and priority and makes no promise outside the support policy."


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"reply-ok": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    if "[stage-1]" in text:
        return "Triaged: billing question, priority normal, no policy exception needed."
    return (
        "Reply drafted: answers the question, cites the help "
        "article, promises nothing outside policy."
    )


def rule(gate: str, criterion: str, value: str) -> GateSpec:
    """A cheap deterministic gate: the output must contain `value`."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": {"type": "contains", "value": value}}]},
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: both tasks per ticket, a worker per agent and the run budget."""
    run = env.run_id

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You work on a customer support desk. Be brief."),
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
            title=f"Triage: {item}",
            instructions=f"[stage-1] Triage {item}",
            assignee="triager",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(
                    Criterion(id="triaged", text="The output says the ticket is triaged"),
                    Criterion(
                        id="reply-ok",
                        text=(
                            "The reply answers the customer and promises nothing the "
                            "support policy forbids"
                        ),
                    ),
                ),
                gates=(
                    rule("is-triaged", "triaged", "Triaged:"),
                    GateSpec(
                        id="review",
                        kind=GateKind.VERIFIER,
                        checks=("reply-ok",),
                        config={"verifier": "reply-checker", "require_different_model": True},
                    ),
                ),
            ),
        )
        second = Task(
            id=f"{run}-{item}-2",
            run_id=run,
            title=f"Reply: {item}",
            instructions=f"[stage-2] Reply {item}",
            assignee="responder",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(Criterion(id="sent", text="The output says the step is sent"),),
                gates=(rule("is-sent", "sent", "Reply drafted:"),),
            ),
        )
        plan.append(PlannedTask(first))
        plan.append(PlannedTask(second, depends_on=(first.id,)))
    return Team(
        plan=plan,
        workers={"triager": work, "responder": work},
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
