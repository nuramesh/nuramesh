"""The R1 "Dev team" template on the demo repository (R1.D, R1.28).

What happens in a run:

1. The Chief of Staff delegates three tasks, in order.
2. **Fix** (developer): asks the model for a corrected `calc.py`. Gates, cheapest first:
   a rule (the fix is there), the **tests in the sandbox** (`python -m unittest` in a container,
   no network), then an **independent reviewer** (reviewer) who sees only the task, the definition
   of done and the output, never the developer's reasoning.
3. **Test** (tester): runs the whole suite in the sandbox and reports; a rule gate checks the
   report says everything passed.
4. **Deploy** (DevOps): prepares a staging deploy; a **human must approve** it.

With `--fake` every model answer comes from a script below: no API keys, no cost. Budgets, prices,
policies, gates and the ledger are all real.
"""

import atexit
import hashlib
import json
import re
import shutil
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

from nuramesh.core.budgets import Budget, BudgetScope
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.models import FakeModel, Message, ModelRequest
from nuramesh.core.money import Money
from nuramesh.core.policy import ActionKind
from nuramesh.core.sandbox import SandboxRequest, SandboxResult
from nuramesh.core.tasks import Task
from nuramesh.engine.run import PlannedTask, WorkContext
from nuramesh.engine.sandbox import command_gate_runner
from nuramesh.team import Team, TeamEnv

# python:3.12-alpine, pinned by digest (a tag can move; a digest can't).
IMAGE = (
    "docker.io/library/python@sha256:"
    "92abd2ba1bdfb48b1e2ac224b582dd1558d0d4bb11c903b126b5bf9c448fd656"
)
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
TEST_COMMAND = ["python", "-m", "unittest", "-v"]
FIXED_CALC = '''"""A tiny calculator with one bug, for the NuraMesh demo."""


def add(a: int, b: int) -> int:
    """Return the sum of a and b."""
    return a + b


def multiply(a: int, b: int) -> int:
    """Return the product of a and b."""
    return a * b
'''
REVIEW = (
    "I compared the new calc.py with the definition of done: only the body of add changed, from "
    "a - b to a + b, and multiply and every signature are unchanged."
)


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"safe-change": {"verdict": "met", "evidence": REVIEW}}})
    return FIXED_CALC


class LazySandbox:
    """Opens the run's sandbox on first use, so a missing runtime shows up as a gate error."""

    def __init__(self, env: TeamEnv) -> None:
        self.env = env
        self.opened: Any = None
        self.lock = threading.Lock()

    def run(self, request: SandboxRequest, workspace: str) -> SandboxResult:
        with self.lock:
            if self.opened is None:
                self.opened = self.env.sandbox()  # raises SandboxUnavailable without a runtime
        result: SandboxResult = self.opened.run(request, workspace)
        return result


def setup(env: TeamEnv) -> Team:
    """Build the run: plan, workers, budgets and the sandbox-backed test gate."""
    return build(env)


def build(env: TeamEnv, wrap_workers: Any = None) -> Team:
    """The run. `wrap_workers(workers, fixed, sandbox, workspace)` may replace the plain Python
    workers (the LangGraph variant in examples/dev-team-langgraph does); `fixed` is what the
    developer produced, `workspace(files)` copies the repo with those files into a scratch dir."""
    # the packaged template keeps its sample project in ./repo; the repo's examples share one
    repo = env.root / "repo" if (env.root / "repo").is_dir() else env.root.parent / "demo-repo"
    fixed: dict[str, str] = {}  # what the developer produced, for the tester
    sandbox = LazySandbox(env)
    scratch = Path(tempfile.mkdtemp(prefix="nuramesh-demo-"))
    atexit.register(shutil.rmtree, scratch, True)

    def workspace(files: dict[str, str]) -> str:
        target = Path(tempfile.mkdtemp(dir=scratch)) / "repo"
        shutil.copytree(repo, target)
        for name, text in files.items():
            if not SAFE_NAME.match(name) or ".." in name.split("/"):
                raise ValueError(f"'{name[:60]}' isn't an allowed file name in the output")
            destination = (target / name).resolve()
            if target.resolve() not in destination.parents:
                raise ValueError("an output file would be written outside the workspace")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(str(text), "utf-8")
        return str(target)

    def developer(task: Task, ctx: WorkContext) -> dict[str, Any]:
        ctx.authorize(ActionKind.WRITE_FILE, "calc.py")
        feedback = "\n".join(ctx.feedback)
        prompt = (
            f"Task: {task.title}\n{task.instructions}\n\n"
            f"calc.py now:\n{(repo / 'calc.py').read_text('utf-8')}\n"
            + (f"\nFeedback from the last attempt:\n{feedback}" if feedback else "")
        )
        # A real model must be told the output format: the file text is written to calc.py as is, so
        # prose or a markdown fence around it would be a syntax error (the tests gate catches that).
        system = (
            "You are a careful developer. Reply with ONLY the complete corrected contents of "
            "calc.py as plain text: no markdown fences, no explanation, nothing else."
        )
        reply = ctx.complete(
            [Message(role="system", content=system), Message(role="user", content=prompt)],
            max_output_tokens=400,
        )
        fixed["calc.py"] = reply.response.text
        return {"files": {"calc.py": reply.response.text}, "summary": "fixed add()"}

    def tester(task: Task, ctx: WorkContext) -> dict[str, Any]:
        command = ctx.authorize_command(TEST_COMMAND)  # the same list is what runs
        result = sandbox.run(SandboxRequest(image=IMAGE, command=command), workspace(fixed))
        return {
            "passed": result.ok,
            "exit_code": result.exit_code,
            "isolation": result.isolation.value,
            "summary": result.stderr_tail.strip().splitlines()[-1:] or ["no output"],
        }

    def devops(task: Task, ctx: WorkContext) -> dict[str, Any]:
        if task.text_ref is not None:
            # A run started through the API (`nuramesh start`, `POST /v1/runs`) has no scripted
            # approver and its gates come from the role's preset, so the deploy asks a person
            # first: the role's `merge: ask` rule opens an approval request (Approvals in the app).
            # In `nuramesh try` the plan below carries a human gate for the same decision.
            ctx.authorize(ActionKind.MERGE, "staging")
        return {"deploy": "staging", "change": "calc.add fix", "rollback": "redeploy previous"}

    run = env.run_id
    fix = Task(
        id=f"{run}-fix-add",
        run_id=run,
        title="Fix the failing add() test in calc.py",
        instructions="add(2, 3) must return 5. Change as little as possible.",
        assignee="developer",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(
                Criterion(id="fixed", text="calc.py returns a + b in add()"),
                Criterion(id="tests-pass", text="The project's tests pass in the sandbox"),
                Criterion(id="safe-change", text="Only add() changed; no public API changed"),
            ),
            gates=(
                GateSpec(
                    id="has-fix",
                    kind=GateKind.RULE,
                    checks=("fixed",),
                    config={
                        "rules": [
                            {
                                "criterion": "fixed",
                                "check": {"type": "contains", "value": "return a + b"},
                            }
                        ]
                    },
                ),
                GateSpec(
                    id="tests",
                    kind=GateKind.TEST_COMMAND,
                    checks=("tests-pass",),
                    config={
                        "image": IMAGE,
                        "command": TEST_COMMAND,
                        "limits": {"timeout_seconds": 120},
                    },
                ),
                GateSpec(
                    id="review",
                    kind=GateKind.VERIFIER,
                    checks=("safe-change",),
                    # The verifier must be a different model family from the developer (ND48); the
                    # model gate enforces that independently of this flag (ND92).
                    config={"verifier": "reviewer", "require_different_model": True},
                ),
            ),
        ),
    )
    test = Task(
        id=f"{run}-test-suite",
        run_id=run,
        title="Run the whole test suite on the fix",
        assignee="tester",
        definition_of_done=DefinitionOfDone(
            criteria=(Criterion(id="suite-green", text="Every test passes"),),
            gates=(
                GateSpec(
                    id="green",
                    kind=GateKind.RULE,
                    checks=("suite-green",),
                    config={
                        "rules": [
                            {
                                "criterion": "suite-green",
                                "check": {"type": "path_equals", "path": "passed", "value": True},
                            }
                        ]
                    },
                ),
            ),
        ),
    )
    deploy = Task(
        id=f"{run}-deploy",
        run_id=run,
        title="Deploy the fix to staging",
        assignee="devops",
        definition_of_done=DefinitionOfDone(
            criteria=(
                # "dod" is the one criterion id of a plan started through the API, and the
                # preset's rule names it; the gate here must equal the preset's
                Criterion(id="dod", text="The deploy targets staging"),
                Criterion(id="approved", text="A human approved this deploy"),
            ),
            gates=(
                GateSpec(  # the devops role's gate preset (nuramesh.yaml) requires this gate
                    id="deploy-plan",
                    kind=GateKind.RULE,
                    checks=("dod",),
                    config={
                        "rules": [
                            {
                                "criterion": "dod",
                                "check": {
                                    "type": "path_equals",
                                    "path": "deploy",
                                    "value": "staging",
                                },
                            }
                        ]
                    },
                ),
                GateSpec(
                    id="approve-deploy",
                    kind=GateKind.HUMAN,
                    checks=("approved",),
                    config={"instructions": "Approve the staging deploy of the add() fix."},
                ),
            ),
        ),
    )

    def files_of(task: Task, output: Any) -> str:
        files = output.get("files") if isinstance(output, dict) else None
        return workspace(dict(files) if files else dict(fixed))  # the tester checks the fix

    workers = {"developer": developer, "tester": tester, "devops": devops}
    if wrap_workers is not None:
        workers = wrap_workers(workers, fixed, sandbox, workspace)

    return Team(
        plan=[
            PlannedTask(fix),
            PlannedTask(test, depends_on=(f"{run}-fix-add",)),
            PlannedTask(deploy, depends_on=(f"{run}-test-suite",)),
        ],
        workers=workers,
        fake_provider=FakeModel(default=fake_model),
        run_budgets=[
            Budget(
                id="rb-" + hashlib.sha256(run.encode()).hexdigest()[:12],
                scope=BudgetScope.RUN,
                scope_id=run,
                limit=Money.of(Decimal("2.00")),  # a real run can't spend more than US$2
            )
        ],
        gate_runners={GateKind.TEST_COMMAND: command_gate_runner(sandbox, files_of)},
    )
