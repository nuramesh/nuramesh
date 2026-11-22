"""The code-review team template (R1.28): independent reviewers on the existing model.

No new engine: the review is one task with several gates. The change summarizer writes a summary of
the change; then **two verifier gates**, one per reviewer (code, security), each on a different
model and each seeing only the task, the definition of done and the output, never the
summarizer's reasoning. The summary is accepted only when both
pass. A second task records the decision after the first is done.

With `--fake` every model answer is scripted below, so a run needs no key and costs nothing;
budgets, prices, policies, gates and the ledger are real. Only run team folders you trust.
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

# reviewer agent -> (criterion it judges, the fake model that reviewer runs on)
BOARD = {
    "code-reviewer": ("correct", "reviewer-1"),
    "security-reviewer": ("secure", "reviewer-2"),
}
CRITERIA = {
    "correct": "The summary says what the change does and that it behaves as intended",
    "secure": "The summary shows no security risk in the change",
}


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    for criterion, model in BOARD.values():
        if request.model == model:
            evidence = f"The summary addresses {criterion} and names the files that were checked."
            return json.dumps({"criteria": {criterion: {"verdict": "met", "evidence": evidence}}})
    text = " ".join(m.content for m in request.messages)
    if "[decision]" in text:
        return "Decision: approved by both reviewers."
    return "Summary: renames one helper, adds a test, touches no configuration."


def rule(gate: str, criterion: str, value: str) -> GateSpec:
    """A cheap deterministic gate: the output must contain `value`."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": {"type": "contains", "value": value}}]},
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: the summary with its two reviewer gates, then the decision record."""
    run = env.run_id

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You support a code review. Be brief."),
                Message(role="user", content=task.instructions),
            ],
            max_output_tokens=200,
        )
        return {"text": reply.response.text}

    summary = Task(
        id=f"{run}-summary",
        run_id=run,
        title="Summarize the change for the review",
        instructions="[summary] Summarize the change under review",
        assignee="change-summarizer",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(
                Criterion(id="summarized", text="The output is a summary"),
                *(Criterion(id=c, text=CRITERIA[c]) for c, _ in BOARD.values()),
            ),
            gates=(
                rule("is-summary", "summarized", "Summary:"),
                *(
                    GateSpec(
                        id=f"{reviewer}-review",
                        kind=GateKind.VERIFIER,
                        checks=(criterion,),
                        config={"verifier": reviewer, "require_different_model": True},
                    )
                    for reviewer, (criterion, _) in BOARD.items()
                ),
            ),
        ),
    )
    decision = Task(
        id=f"{run}-decision",
        run_id=run,
        title="Record the review decision",
        instructions="[decision] Record the decision of the review",
        assignee="change-summarizer",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(Criterion(id="decided", text="The decision is recorded"),),
            gates=(rule("is-decision", "decided", "Decision:"),),
        ),
    )
    return Team(
        plan=[PlannedTask(summary), PlannedTask(decision, depends_on=(summary.id,))],
        workers={"change-summarizer": work},
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
