"""The Multi-framework org template: two teams on two frameworks, one set of rules, one ledger.

No new engine: a plan is ordinary tasks. **Research** (Researcher, a native NuraMesh agent in the
Research team) writes findings; **Draft** (Drafter, a compiled LangGraph graph in the Operations
team, run through `nuramesh.adapters.langgraph`) turns them into a short brief. The Central
reviewer (a different model, one verifier for both teams) checks each, rule gates check the shape
and a human approves the brief. The graph's model calls and its file write go through NuraMesh
(policy, price, budget, ledger) like any native agent's; gates, not the graph, decide the task is
done. Nothing here claims more than the adapter does: only model calls and the actions the graph
asks `authorize` about are governed.

Needs the LangGraph extra: `pip install "nuramesh[langgraph]"`. With `--fake` every model answer
is scripted below, so a run needs no key, no network and costs nothing. Only run team folders you
trust.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any, TypedDict

from nuramesh.core.budgets import Budget, BudgetScope
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.models import FakeModel, Message, ModelRequest
from nuramesh.core.money import Money
from nuramesh.core.policy import ActionKind
from nuramesh.core.tasks import Task
from nuramesh.engine.run import PlannedTask, WorkContext
from nuramesh.team import Team, TeamEnv, TeamError

REVIEW = "The text is consistent with the findings and adds no claim of its own."
TOPIC = "How do we shorten customer onboarding?"


class State(TypedDict, total=False):
    task: str
    instructions: str
    feedback: list[str]
    draft: str
    output: Any


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"consistent": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    if "[stage-1]" in text:
        return (
            "Findings: onboarding takes 14 days; 9 of them wait on account set-up; "
            "a checklist for set-up removes most of the wait."
        )
    return (
        "Brief: cut onboarding by automating account set-up with a checklist (14 days to about 5)."
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
    """Build the run: research (native), draft (LangGraph), a worker per agent, the run budget."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langgraph.graph import END, START, StateGraph

        from nuramesh.adapters.langgraph import NuraMeshChatModel, authorize, langgraph_worker
    except ImportError as exc:
        raise TeamError(
            'this template runs one agent as a LangGraph graph: pip install "nuramesh[langgraph]"'
        ) from exc
    run = env.run_id

    def researcher(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You are a careful researcher. Be brief."),
                Message(role="user", content=f"{task.instructions}\nTopic: {TOPIC}"),
            ],
            max_output_tokens=200,
        )
        return {"text": reply.response.text}

    def drafter_graph(model: NuraMeshChatModel) -> Any:
        def draft(state: State) -> State:
            authorize(model, ActionKind.WRITE_FILE, "drafts/brief.md")  # the policy decides
            reply = model.invoke(
                [
                    SystemMessage(content="Write a one-sentence brief from the findings."),
                    HumanMessage(content=f"{state['instructions']}\nTopic: {TOPIC}"),
                ]
            )
            return {"output": {"text": str(reply.content)}}

        graph = StateGraph(State)
        graph.add_node("draft", draft)
        graph.add_edge(START, "draft")
        graph.add_edge("draft", END)
        return graph.compile()

    reviewed = GateSpec(
        id="central-review",
        kind=GateKind.VERIFIER,
        checks=("consistent",),
        config={"verifier": "central-reviewer", "require_different_model": True},
    )
    consistent = Criterion(id="consistent", text="The text matches the topic and adds no new claim")
    research = Task(
        id=f"{run}-research",
        run_id=run,
        title="Research the topic",
        instructions="[stage-1] List the findings.",
        assignee="researcher",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(consistent,),
            gates=(rule("has-findings", "consistent", "Findings:"), reviewed),
        ),
    )
    draft_task = Task(
        id=f"{run}-draft",
        run_id=run,
        title="Draft the brief (LangGraph agent)",
        instructions="[stage-2] Write the brief from the research findings.",
        assignee="graph-drafter",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(consistent, Criterion(id="approved", text="A human approved the brief")),
            gates=(
                rule("has-brief", "consistent", "Brief:"),
                reviewed,
                GateSpec(
                    id="approve-brief",
                    kind=GateKind.HUMAN,
                    checks=("approved",),
                    config={"instructions": "Read the brief and approve it for sharing."},
                ),
            ),
        ),
    )
    return Team(
        plan=[PlannedTask(research), PlannedTask(draft_task, depends_on=(research.id,))],
        workers={
            "researcher": researcher,
            "graph-drafter": langgraph_worker(drafter_graph, max_output_tokens=200),
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
