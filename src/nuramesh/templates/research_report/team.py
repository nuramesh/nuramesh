"""The Research report template: a cited report from bundled sources, checked before it counts.

No new engine: a plan is ordinary tasks. **Gather** (Researcher) reads the sample documents in
`sources/` and lists findings with a source tag such as `[S1]`; **Write** (Writer) turns them into
a short report. Both are checked by a rule gate (every claim line carries a source tag) and by the
independent Fact checker, who runs on a different model and sees only the task, the definition of
done and the output. A human approves the report last. Add your own documents to `sources/` (one
file per source, named `s<number>-<topic>.md`).

A task is done only when its gates pass. With `--fake` every model answer is scripted below (built
from the sources' `Key finding:` lines), so a run needs no key, no network and costs nothing;
budgets, prices, policies, gates and the ledger are real. Only run team folders you trust.
"""

import hashlib
import json
import re
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

REVIEW = "Every claim carries a source tag and matches what that source says."
CITED = re.compile(r"\[S\d+\]")


def read_sources(root: Path) -> dict[str, str]:
    """The bundled sources by tag: `s1-remote-work-survey.md` becomes `S1`."""
    found: dict[str, str] = {}
    for path in sorted((root / "sources").glob("s[0-9]*-*.md")):
        found["S" + path.name[1:].split("-", 1)[0]] = path.read_text("utf-8")
    return found


def findings_of(sources: dict[str, str]) -> list[str]:
    """One `- finding [S#]` line per `Key finding:` line in the sources."""
    lines = []
    for tag, text in sources.items():
        for match in re.finditer(r"(?m)^Key finding: (.+)$", text):
            lines.append(f"- {match.group(1).rstrip('.')} [{tag}]")
    return lines


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"cited": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    sources = {m.group(1): m.group(2) for m in re.finditer(r"(?s)<(S\d+)>(.*?)</S\d+>", text)}
    found = findings_of(sources)
    if "[stage-1]" in text:
        return "Findings:\n" + "\n".join(found)
    return (
        "Report: what the sources say.\n"
        + "\n".join(found)
        + "\nSources: "
        + ", ".join(sources)
        + "."
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
    """Build the run: gather, write, approve; a worker per agent and the run budget."""
    run = env.run_id
    sources = read_sources(env.root)
    block = "\n".join(f"<{tag}>\n{text}\n</{tag}>" for tag, text in sources.items())

    def work(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You write cited research. Use only the sources."),
                Message(role="user", content=f"{task.instructions}\n\n{block}"),
            ],
            max_output_tokens=400,
        )
        return {"text": reply.response.text}

    cited_rules = [
        {"type": "matches", "pattern": r"\[S\d+\]"},
        {"type": "not_contains", "value": "according to experts", "case_sensitive": False},
    ]
    verifier = GateSpec(
        id="fact-check",
        kind=GateKind.VERIFIER,
        checks=("cited",),
        config={"verifier": "fact-checker", "require_different_model": True},
    )
    criterion = Criterion(
        id="cited", text="Every claim names the source it came from, and no claim goes beyond it"
    )
    gather = Task(
        id=f"{run}-gather",
        run_id=run,
        title="Gather findings from the sources",
        instructions="[stage-1] List each finding with its source tag.",
        assignee="researcher",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(criterion,),
            gates=(rule("has-source-tags", "cited", cited_rules), verifier),
        ),
    )
    write = Task(
        id=f"{run}-write",
        run_id=run,
        title="Write the report",
        instructions="[stage-2] Write a short report from the findings. End with a Sources line.",
        assignee="writer",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(criterion,),
            gates=(
                rule(
                    "report-shape",
                    "cited",
                    [*cited_rules, {"type": "contains", "value": "Sources:"}],
                ),
                verifier,
            ),
        ),
    )
    publish = Task(
        id=f"{run}-publish",
        run_id=run,
        title="Approve the report",
        instructions="[stage-3] Hand the finished report to a person for approval.",
        assignee="writer",
        definition_of_done=DefinitionOfDone(
            criteria=(Criterion(id="approved", text="A human approved the report"),),
            gates=(
                GateSpec(
                    id="approve-report",
                    kind=GateKind.HUMAN,
                    checks=("approved",),
                    config={"instructions": "Read the report and approve it for sharing."},
                ),
            ),
        ),
    )
    return Team(
        plan=[
            PlannedTask(gather),
            PlannedTask(write, depends_on=(gather.id,)),
            PlannedTask(publish, depends_on=(write.id,)),
        ],
        workers={"researcher": work, "writer": work},
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
