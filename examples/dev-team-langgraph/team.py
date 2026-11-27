"""The dev-team demo with its workers as LangGraph graphs (R1.D, R1.16).

Same plan, gates, budgets, policies and ledger as `examples/dev-team`; the difference is that
Developer, Tester and DevOps each run as a compiled LangGraph graph through
`nuramesh.adapters.langgraph`.
NuraMesh stays the trust layer: the graph's model calls go through `NuraMeshChatModel` (policy,
price, budget, ledger) and its actions ask `authorize` / `authorize_command` first. Gates, not the
graphs, decide whether a task is done. On `--fake` no model API is called.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from nuramesh.adapters.langgraph import (
    NuraMeshChatModel,
    authorize,
    authorize_command,
    langgraph_worker,
)
from nuramesh.core.policy import ActionKind
from nuramesh.core.sandbox import SandboxRequest
from nuramesh.team import Team, TeamEnv


def _load_plain() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "dev-team" / "team.py"
    spec = importlib.util.spec_from_file_location("nuramesh_demo_dev_team", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plain = _load_plain()
fake_model = plain.fake_model  # the scripted answers, shared with the plain demo
IMAGE, TEST_COMMAND = plain.IMAGE, plain.TEST_COMMAND

SYSTEM = (
    "You are a careful developer. Reply with ONLY the complete corrected contents of "
    "calc.py as plain text: no markdown fences, no explanation, nothing else."
)


class State(TypedDict, total=False):
    task: str
    instructions: str
    feedback: list[str]
    canon: list[dict[str, str]]
    draft: str
    output: Any


def setup(env: TeamEnv) -> Team:
    """The plain demo's run, with the three workers replaced by LangGraph graphs."""
    repo = env.root.parent / "demo-repo"

    def graph_workers(
        workers: dict[str, Any], fixed: dict[str, str], sandbox: Any, workspace: Any
    ) -> dict[str, Any]:

        def developer_graph(model: NuraMeshChatModel) -> Any:
            def draft(state: State) -> State:
                authorize(model, ActionKind.WRITE_FILE, "calc.py")
                feedback = "\n".join(state.get("feedback", []))
                prompt = (
                    f"Task: {state['task']}\n{state['instructions']}\n\n"
                    f"calc.py now:\n{(repo / 'calc.py').read_text('utf-8')}\n"
                    + (f"\nFeedback from the last attempt:\n{feedback}" if feedback else "")
                )
                reply = model.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
                return {"draft": str(reply.content)}

            def submit(state: State) -> State:
                fixed["calc.py"] = state["draft"]  # what the tester will check
                return {"output": {"files": {"calc.py": state["draft"]}, "summary": "fixed add()"}}

            g = StateGraph(State)
            g.add_node("draft", draft)
            g.add_node("submit", submit)
            g.add_edge(START, "draft")
            g.add_edge("draft", "submit")
            g.add_edge("submit", END)
            return g.compile()

        def tester_graph(model: NuraMeshChatModel) -> Any:
            def run_tests(state: State) -> State:
                command = authorize_command(model, TEST_COMMAND)  # the same list is what runs
                result = sandbox.run(SandboxRequest(image=IMAGE, command=command), workspace(fixed))
                return {
                    "output": {
                        "passed": result.ok,
                        "exit_code": result.exit_code,
                        "isolation": result.isolation.value,
                        "summary": result.stderr_tail.strip().splitlines()[-1:] or ["no output"],
                    }
                }

            g = StateGraph(State)
            g.add_node("run_tests", run_tests)
            g.add_edge(START, "run_tests")
            g.add_edge("run_tests", END)
            return g.compile()

        def devops_graph(model: NuraMeshChatModel) -> Any:
            def prepare(state: State) -> State:
                return {
                    "output": {
                        "deploy": "staging",
                        "change": "calc.add fix",
                        "rollback": "redeploy previous",
                    }
                }

            g = StateGraph(State)
            g.add_node("prepare", prepare)
            g.add_edge(START, "prepare")
            g.add_edge("prepare", END)
            return g.compile()

        return {
            "developer": langgraph_worker(developer_graph, max_output_tokens=400),
            "tester": langgraph_worker(tester_graph),
            "devops": langgraph_worker(devops_graph),
        }

    return plain.build(env, graph_workers)
