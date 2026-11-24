"""The Incident triage template: alert in, rated, fixed with a human's approval, written up.

No new engine: a plan is ordinary tasks. **Triage** (Triager) reads the sample alert in `alerts/`
and the runbook and rates the incident; a rule gate requires a severity (`SEV1`..`SEV4`) and the
independent Incident reviewer (a different model) checks it against the runbook. **Respond**
(Responder) proposes the fix; a **human must approve** before the action counts (no policy lets any
agent run a production change by itself). **Postmortem** (Postmortem writer) drafts the write-up
with timeline, impact, cause and action items, checked by a rule gate and the Incident reviewer.
Replace `alerts/alert-001.json` and `alerts/runbook.md` with your own.

A task is done only when its gates pass. With `--fake` every model answer is scripted below (built
from the alert file), so a run needs no key, no network and costs nothing; budgets, prices,
policies, gates and the ledger are real. Only run team folders you trust.
"""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from nuramesh.core.budgets import Budget, BudgetScope
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.models import FakeModel, Message, ModelRequest
from nuramesh.core.money import Money
from nuramesh.core.tasks import Task
from nuramesh.engine.run import PlannedTask, WorkContext
from nuramesh.team import Team, TeamEnv

REVIEW = "The write-up is consistent with the alert and the runbook and invents no fact."


def read_alert(root: Path) -> dict[str, Any]:
    """The sample alert."""
    alert: dict[str, Any] = json.loads((root / "alerts" / "alert-001.json").read_text("utf-8"))
    return alert


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"consistent": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    start = text.find("{")
    alert = json.loads(text[start : text.rfind("}") + 1]) if start >= 0 else {}
    service = alert.get("service", "the service")
    change = alert.get("recent_change", "no recent change")
    if "[stage-1]" in text:
        return (
            f"Severity: SEV1. {service} is failing checkouts "
            f"({alert.get('customers_affected', 'customers affected')}); {change}."
        )
    if "[stage-2]" in text:
        return f"Proposed action: rollback-{service} to the previous release, then watch errors."
    return (
        f"Postmortem: {alert.get('id', 'incident')}\n"
        f"Timeline: alert at {alert.get('fired_at', '?')}, rollback approved by a human.\n"
        f"Impact: {alert.get('customers_affected', 'unknown')}.\n"
        f"Cause: {change} exhausted database connections.\n"
        "Action items: add a connection-pool alert; test deploys under load."
    )


def rule(gate: str, criterion: str, checks: list[dict[str, Any]]) -> GateSpec:
    """A cheap deterministic gate on the output text."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": c} for c in checks]},
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: triage, respond (human approval), postmortem, a worker per agent."""
    run = env.run_id
    alert = json.dumps(read_alert(env.root), indent=2)
    runbook = (env.root / "alerts" / "runbook.md").read_text("utf-8")

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You help run an incident. Be brief and factual."),
                Message(
                    role="user", content=f"{task.instructions}\n\nALERT:\n{alert}\n\n{runbook}"
                ),
            ],
            max_output_tokens=300,
        )
        return {"text": reply.response.text}

    def reviewed(criterion: str) -> GateSpec:
        return GateSpec(
            id="incident-review",
            kind=GateKind.VERIFIER,
            checks=(criterion,),
            config={"verifier": "incident-reviewer", "require_different_model": True},
        )

    consistent = Criterion(id="consistent", text="The text agrees with the alert and the runbook")
    triage = Task(
        id=f"{run}-triage",
        run_id=run,
        title="Triage the alert",
        instructions="[stage-1] Rate the incident SEV1 to SEV4 and say why.",
        assignee="triager",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(consistent,),
            gates=(
                rule(
                    "has-severity",
                    "consistent",
                    [{"type": "matches", "pattern": r"Severity: SEV[1-4]\b"}],
                ),
                reviewed("consistent"),
            ),
        ),
    )
    respond = Task(
        id=f"{run}-respond",
        run_id=run,
        title="Propose and approve the fix",
        instructions="[stage-2] Propose one fix from the runbook for a human to approve.",
        assignee="responder",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(
                Criterion(id="proposed", text="The output proposes one concrete action"),
                Criterion(id="approved", text="A human approved the production action"),
            ),
            gates=(
                rule(
                    "has-action",
                    "proposed",
                    [{"type": "contains", "value": "Proposed action:"}],
                ),
                GateSpec(
                    id="approve-action",
                    kind=GateKind.HUMAN,
                    checks=("approved",),
                    config={"instructions": "Approve or refuse the proposed production action."},
                ),
            ),
        ),
    )
    postmortem = Task(
        id=f"{run}-postmortem",
        run_id=run,
        title="Draft the postmortem",
        instructions="[stage-3] Write the postmortem: timeline, impact, cause, action items.",
        assignee="postmortem-writer",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(consistent,),
            gates=(
                rule(
                    "postmortem-sections",
                    "consistent",
                    [
                        {"type": "contains", "value": "Timeline:"},
                        {"type": "contains", "value": "Impact:"},
                        {"type": "contains", "value": "Cause:"},
                        {"type": "contains", "value": "Action items:"},
                    ],
                ),
                reviewed("consistent"),
            ),
        ),
    )
    return Team(
        plan=[
            PlannedTask(triage),
            PlannedTask(respond, depends_on=(triage.id,)),
            PlannedTask(postmortem, depends_on=(respond.id,)),
        ],
        workers={"triager": work, "responder": work, "postmortem-writer": work},
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
