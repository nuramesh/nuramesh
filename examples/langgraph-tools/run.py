"""A LangGraph agent with tools, run under NuraMesh on the fake model (no model API is called).

The graph offers two tools with `nuramesh_tool_model` and runs them with `nuramesh_tool_node`.
The team's policy allows `read_file` and denies `drop_db`, so the first runs and the second is
refused: its body never runs and the model is told why. Run it with:

    uv sync --extra langgraph
    uv run python examples/langgraph-tools/run.py
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from nuramesh.adapters.langgraph import (
    NuraMeshChatModel,
    langgraph_worker,
    nuramesh_tool_model,
    nuramesh_tool_node,
    nuramesh_tools_condition,
)
from nuramesh.core.budgets import Budget, BudgetScope
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.models import FakeModel, ModelRequest
from nuramesh.core.money import Money
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.policy import PolicySet
from nuramesh.core.presets import PresetConfig
from nuramesh.core.prices import Price, PriceBook
from nuramesh.core.tasks import Task
from nuramesh.engine.budgets import define_budget
from nuramesh.engine.run import EngineSettings, PlannedTask, RunEngine
from nuramesh.stores import MemoryStore

RAN: list[str] = []
ANSWER = "The notes say: ship on Friday."


@tool
def read_file(path: str) -> str:
    """Read a file."""
    RAN.append(f"read_file:{path}")
    return "ship on Friday"


@tool
def drop_db(name: str) -> str:
    """Drop a database."""
    RAN.append(f"drop_db:{name}")
    return "dropped"


class AgentState(MessagesState):
    output: str


def build(model: NuraMeshChatModel) -> Any:
    """The graph: model, then the adapter's tool node, then the model again."""
    tools = [read_file, drop_db]
    offered = nuramesh_tool_model(model, tools)

    def agent(state: AgentState) -> dict[str, Any]:
        return {"messages": [offered.invoke(state["messages"])]}

    def finish(state: AgentState) -> dict[str, Any]:
        return {"output": str(state["messages"][-1].content)}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", nuramesh_tool_node(model, tools))
    graph.add_node("finish", finish)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent", nuramesh_tools_condition, {"tools": "tools", "__end__": "finish"}
    )
    graph.add_edge("tools", "agent")
    return graph.compile()


def _input(task: Task, ctx: Any) -> dict[str, Any]:
    return {
        "messages": [
            SystemMessage(content="You answer from files."),
            HumanMessage(content=task.title),
        ]
    }


def _verifier(request: ModelRequest) -> str:
    if request.model == "verifier-1":
        evidence = "I compared the answer with the criterion and it states the notes correctly."
        return json.dumps({"criteria": {"reviewed": {"verdict": "met", "evidence": evidence}}})
    return ANSWER


def _calls() -> str:
    """What the fake model answers first: two tool calls (the adapter's call envelope)."""
    body = [
        {"name": "read_file", "args": {"path": "notes.txt"}, "id": "call_1"},
        {"name": "drop_db", "args": {"name": "prod"}, "id": "call_2"},
    ]
    return json.dumps({"tool_calls": body})


def _refused_forms() -> tuple[str, str]:
    """The two ways around the adapter that are refused."""
    model = NuraMeshChatModel(context=object())
    try:
        model.bind_tools([read_file])
    except ValueError as exc:
        bind_error = str(exc)
    try:
        langgraph_worker(
            lambda m: _prebuilt(m)  # a graph holding LangGraph's own ToolNode
        )(Task.model_construct(id="t", run_id="r"), object())  # type: ignore[arg-type]
    except ValueError as exc:
        prebuilt_error = str(exc)
    return bind_error, prebuilt_error


def _prebuilt(model: NuraMeshChatModel) -> Any:
    graph = StateGraph(AgentState)
    graph.add_node("agent", lambda s: {"messages": []})
    graph.add_node("tools", ToolNode([read_file]))
    graph.add_edge(START, "agent")
    graph.add_edge("agent", "tools")
    return graph.compile()


def run_example() -> dict[str, Any]:
    """Run the team once and return what happened."""
    RAN.clear()
    org = OrgGraph(
        id="acme",
        name="Acme",
        roles=(
            Role(id="cos", name="Chief of Staff", kind=RoleKind.CHIEF_OF_STAFF),
            Role(id="dev", name="Developer", default_model="fake/worker-1"),
            Role(
                id="ver", name="Verifier", kind=RoleKind.VERIFIER, default_model="fake/verifier-1"
            ),
        ),
        agents=(
            Agent(id="boss", name="Boss", role="cos"),
            Agent(id="ada", name="Ada", role="dev", reports_to="boss"),
            Agent(id="vera", name="Vera", role="ver", reports_to="boss"),
        ),
    )
    models = [
        {"action": "call_model", "decision": "allow", "target": "fake/*"},
        {"action": "spend", "decision": "allow"},
    ]
    policies = PolicySet.model_validate(
        {
            "policies": [
                {
                    "role": "cos",
                    "rules": [{"action": "delegate", "decision": "allow", "target": "*"}],
                },
                {
                    "role": "dev",
                    "rules": [
                        *models,
                        {"action": "use_tool", "decision": "allow", "target": "read_file"},
                        {"action": "use_tool", "decision": "deny", "target": "drop_db"},
                    ],
                },
                {"role": "ver", "rules": models},
            ]
        }
    )
    prices = PriceBook(
        prices=tuple(
            Price(
                provider="fake",
                model=m,
                input_per_million=Decimal("3"),
                output_per_million=Decimal("15"),
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                source="example price list (fake model)",
            )
            for m in ("worker-1", "verifier-1")
        )
    )
    store = MemoryStore()
    define_budget(
        store,
        Budget(id="run", scope=BudgetScope.RUN, scope_id="r1", limit=Money.of("1")),
        actor="human:owner",
        org_id="acme",
    )
    done = DefinitionOfDone(
        criteria=(
            Criterion(id="answered", text="The answer quotes the notes"),
            Criterion(id="reviewed", text="An independent reviewer agrees it is correct"),
        ),
        gates=(
            GateSpec(
                id="rule",
                kind=GateKind.RULE,
                checks=("answered",),
                config={
                    "rules": [
                        {"criterion": "answered", "check": {"type": "contains", "value": "Friday"}}
                    ]
                },
            ),
            GateSpec(
                id="verify",
                kind=GateKind.VERIFIER,
                checks=("reviewed",),
                config={"verifier": "vera"},
            ),
        ),
    )
    task = Task(
        id="t1",
        run_id="r1",
        title="What do the notes say?",
        definition_of_done=done,
        assignee="ada",
    )
    provider = FakeModel([_calls(), ANSWER], default=_verifier)
    seen: list[Any] = []

    def make_graph(model: NuraMeshChatModel) -> Any:
        graph = build(model)
        seen.append(graph)
        return graph

    worker = langgraph_worker(make_graph, make_input=_input, output_key="output")
    engine = RunEngine(
        store,
        org,
        provider=provider,
        prices=prices,
        policies=policies,
        workers={"ada": worker},
        settings=EngineSettings(max_parallel=1),
        presets=PresetConfig(),  # the example has no configuration: no presets
    )
    report = engine.run("r1", [PlannedTask(task)])
    second_turn = [c for c in provider.calls if c.model == "worker-1"][1]
    blocked = next(
        m.content for m in second_turn.messages if "Blocked by the team's rules" in m.content
    )
    refusal = json.loads(blocked.strip().splitlines()[-1])["tool_result"]["content"]
    bind_error, prebuilt_error = _refused_forms()
    return {
        "counts": report.counts,
        "ran": list(RAN),
        "refusal": refusal,
        "bind_tools_error": bind_error,
        "prebuilt_error": prebuilt_error,
    }


if __name__ == "__main__":
    print(json.dumps(run_example(), indent=2))
