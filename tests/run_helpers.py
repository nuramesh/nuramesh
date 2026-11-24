"""A tiny organization, prices and policies for tests that need a run in progress."""

from datetime import UTC, datetime
from decimal import Decimal

from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.policy import PolicySet
from nuramesh.core.prices import Price, PriceBook
from nuramesh.core.tasks import Task

ORG = OrgGraph(
    id="devteam-example",
    name="Demo",
    roles=(
        Role(id="cos", name="Chief of Staff", kind=RoleKind.CHIEF_OF_STAFF),
        Role(id="dev", name="Developer", default_model="fake/worker-1"),
    ),
    agents=(
        Agent(id="boss", name="Boss", role="cos"),
        Agent(id="ada", name="Ada", role="dev", reports_to="boss"),
    ),
)
PRICES = PriceBook(
    prices=(
        Price(
            provider="fake",
            model="worker-1",
            input_per_million=Decimal("3"),
            output_per_million=Decimal("15"),
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            source="test price list",
        ),
    )
)
_ALLOW = [
    {"action": "call_model", "decision": "allow", "target": "fake/*"},
    {"action": "spend", "decision": "allow"},
]
POLICIES = PolicySet.model_validate(
    {
        "policies": [
            {"role": "cos", "rules": [{"action": "delegate", "decision": "allow", "target": "*"}]},
            {"role": "dev", "rules": _ALLOW},
        ]
    }
)


def task_for(run_id: str, tid: str) -> Task:
    """A one-gate task for `ada` (the rule wants `return a + b` in the output)."""
    rule = GateSpec(
        id="rule",
        kind=GateKind.RULE,
        checks=("fixed",),
        config={
            "rules": [
                {"criterion": "fixed", "check": {"type": "contains", "value": "return a + b"}}
            ]
        },
    )
    done = DefinitionOfDone(
        criteria=(Criterion(id="fixed", text="The function returns the sum"),), gates=(rule,)
    )
    return Task(
        id=f"{run_id}-{tid}",
        run_id=run_id,
        title=f"Task {tid}",
        definition_of_done=done,
        assignee="ada",
    )
