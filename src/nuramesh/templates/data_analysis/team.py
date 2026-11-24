"""The Data analysis template: a question answered from a sample database, numbers rechecked.

No new engine: a plan is ordinary tasks. **Query** (Analyst) asks the model for one SQL query,
runs it on a read-only SQLite database built in memory from `data/sales.sql` (any statement that
is not a plain read is refused by SQLite itself) and returns revenue per region. A rule gate
recomputes every number by a second route (plain Python over all rows, not SQL), and the
independent Number checker (a different model) judges the output. **Summarize** (Summarizer)
writes the findings; rule gates require the top region and the grand total, which are again
recomputed, and the Number checker checks the text. Replace `data/sales.sql` with your own table
and change the question in `QUESTION`.

A task is done only when its gates pass. With `--fake` every model answer is scripted below, so a
run needs no key, no network and costs nothing; budgets, prices, policies, gates and the ledger
are real. Only run team folders you trust.
"""

import hashlib
import json
import re
import sqlite3
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

QUESTION = "Which region earned the most revenue, and what is the total?"
QUERY = (
    "SELECT region, SUM(units * unit_price_cents) AS cents FROM orders "
    "GROUP BY region ORDER BY cents DESC"
)
REVIEW = "Every figure in the output matches the data and nothing is invented."
_READ_ONLY = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def open_database(root: Path) -> sqlite3.Connection:
    """The sample database: `data/sales.sql` loaded into memory, then locked to reads."""
    db = sqlite3.connect(":memory:")
    db.executescript((root / "data" / "sales.sql").read_text("utf-8"))
    db.set_authorizer(
        lambda action, *_: sqlite3.SQLITE_OK if action in _READ_ONLY else sqlite3.SQLITE_DENY
    )
    return db


def money(cents: int) -> str:
    """Whole cents as a two-decimal amount."""
    return f"{Decimal(cents) / 100:.2f}"


def reference(db: sqlite3.Connection) -> dict[str, int]:
    """Revenue per region in cents by a second route: Python over every row, no SQL aggregate."""
    totals: dict[str, int] = {}
    for region, units, price in db.execute("SELECT region, units, unit_price_cents FROM orders"):
        totals[region] = totals.get(region, 0) + units * price
    return totals


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"numbers-ok": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    if "[stage-1]" in text:
        return QUERY
    figures = re.findall(
        r"(?m)^(\w+): (\d+\.\d{2})$", "\n".join(m.content for m in request.messages)
    )
    top, best = max(figures, key=lambda f: Decimal(f[1]))
    grand = sum((Decimal(v) for _, v in figures), Decimal(0))
    return f"Summary: {top} earned the most revenue ({best} USD). The total is {grand:.2f} USD."


def rule(gate: str, criterion: str, checks: list[dict[str, Any]]) -> GateSpec:
    """A cheap deterministic gate on the output."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": c} for c in checks]},
    )


def setup(env: TeamEnv) -> Team:
    """Build the run: query, summarize, a worker per agent and the run budget."""
    run = env.run_id
    truth = reference(open_database(env.root))
    top = max(truth, key=lambda r: truth[r])
    grand = money(sum(truth.values()))
    figures: dict[str, str] = {}

    def analyst(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="Answer with one read-only SQLite query."),
                Message(
                    role="user",
                    content=f"{task.instructions}\nQuestion: {QUESTION}\nTable: orders(region, "
                    "product, units, unit_price_cents)",
                ),
            ],
            max_output_tokens=200,
        )
        db = open_database(env.root)
        try:
            rows = db.execute(reply.response.text.strip().rstrip(";")).fetchall()
        except sqlite3.Error as exc:  # a write or a bad query is refused, never run
            return {
                "error": f"the query was refused ({type(exc).__name__})",
                "query": reply.response.text,
            }
        try:
            by_region = {region: money(cents) for region, cents in rows}
            total = money(sum(cents for _, cents in rows))
        except (ValueError, TypeError, ArithmeticError):  # not (region, cents): refused
            return {
                "error": "the query must return two columns: region and revenue in cents",
                "query": reply.response.text,
            }
        figures.clear()  # a retry must not keep a failed attempt's regions
        figures.update(by_region)
        return {
            "query": reply.response.text.strip(),
            "revenue_by_region": by_region,
            "top_region": rows[0][0] if rows else "",
            "total": total,
        }

    def summarizer(task: Task, ctx: WorkContext) -> dict[str, Any]:
        lines = "\n".join(f"{region}: {amount}" for region, amount in figures.items())
        reply = ctx.complete(
            [
                Message(
                    role="system", content="Write a two-sentence summary. Use only the figures."
                ),
                Message(role="user", content=f"{task.instructions}\n{lines}"),
            ],
            max_output_tokens=200,
        )
        return {"text": reply.response.text}

    done = Criterion(id="numbers-ok", text="Every figure matches the data and none is invented")
    verifier = GateSpec(
        id="number-check",
        kind=GateKind.VERIFIER,
        checks=("numbers-ok",),
        config={"verifier": "number-checker", "require_different_model": True},
    )
    recomputed = [
        {"type": "path_equals", "path": f"revenue_by_region.{region}", "value": money(cents)}
        for region, cents in sorted(truth.items())
    ] + [
        {"type": "path_equals", "path": "top_region", "value": top},
        {"type": "path_equals", "path": "total", "value": grand},
    ]
    query = Task(
        id=f"{run}-query",
        run_id=run,
        title="Run the revenue query",
        instructions="[stage-1] Write the query that answers the question.",
        assignee="analyst",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(done,), gates=(rule("recomputed-numbers", "numbers-ok", recomputed), verifier)
        ),
    )
    summarize = Task(
        id=f"{run}-summarize",
        run_id=run,
        title="Summarize the findings",
        instructions="[stage-2] Summarize which region earned the most and the total.",
        assignee="summarizer",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(done,),
            gates=(
                rule(
                    "summary-figures",
                    "numbers-ok",
                    [
                        {"type": "contains", "value": f"{top} earned the most"},
                        {"type": "contains", "value": grand},
                    ],
                ),
                verifier,
            ),
        ),
    )
    return Team(
        plan=[PlannedTask(query), PlannedTask(summarize, depends_on=(query.id,))],
        workers={"analyst": analyst, "summarizer": summarizer},
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
