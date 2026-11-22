"""The Sales team template (R2.06): a sales pipeline on the existing task model.

No new engine: a plan is ordinary tasks. Each lead has two tasks, **Qualify** (Qualifier,
checked by a rule gate and by the independent Claims checker) and **Outreach** (Outreach writer,
a rule gate), and the second waits for the first. Add leads by adding names to `ITEMS`. The
outreach is only a draft: sending email or touching the CRM is not allowed by any policy here.

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

ITEMS = ("lead-1", "lead-2")
REVIEW = "The qualification states its reasons and invents nothing about the lead."


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"claims-ok": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    if "[stage-1]" in text:
        return (
            "Qualified: fits the target profile, budget owner "
            "identified, next step is a short intro email."
        )
    return "Outreach drafted: short intro email that makes no claim we cannot back up."


def rule(gate: str, criterion: str, value: str) -> GateSpec:
    """A cheap deterministic gate: the output must contain `value`."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": {"type": "contains", "value": value}}]},
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: both tasks per lead, a worker per agent and the run budget."""
    run = env.run_id

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You work on a sales pipeline. Be brief."),
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
            title=f"Qualify: {item}",
            instructions=f"[stage-1] Qualify {item}",
            assignee="qualifier",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(
                    Criterion(id="qualified", text="The output says the lead is qualified"),
                    Criterion(
                        id="claims-ok",
                        text=(
                            "The qualification rests on stated facts and invents no "
                            "claim about the lead"
                        ),
                    ),
                ),
                gates=(
                    rule("is-qualified", "qualified", "Qualified:"),
                    GateSpec(
                        id="review",
                        kind=GateKind.VERIFIER,
                        checks=("claims-ok",),
                        config={"verifier": "claims-checker", "require_different_model": True},
                    ),
                ),
            ),
        )
        second = Task(
            id=f"{run}-{item}-2",
            run_id=run,
            title=f"Outreach: {item}",
            instructions=f"[stage-2] Outreach {item}",
            assignee="outreach-writer",
            max_attempts=2,
            definition_of_done=DefinitionOfDone(
                criteria=(Criterion(id="drafted", text="The output says the step is drafted"),),
                gates=(rule("is-drafted", "drafted", "Outreach drafted:"),),
            ),
        )
        plan.append(PlannedTask(first))
        plan.append(PlannedTask(second, depends_on=(first.id,)))
    return Team(
        plan=plan,
        workers={"qualifier": work, "outreach-writer": work},
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
