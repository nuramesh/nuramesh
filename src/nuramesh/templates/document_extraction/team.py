"""The Document extraction template: invoices in, a checked CSV out.

No new engine: a plan is ordinary tasks. **Extract** (Extractor) reads the sample invoices in
`invoices/` and returns one row per invoice (number, vendor, date, total). Two rule gates compare
the result with a second, plain-code pass over the same files (the row count and the grand total),
and the independent Invoice checker (a different model) compares each field with its document.
**Export** (Exporter) writes `exports/invoices.csv` once a human approves. Put your own `.txt`
invoices in `invoices/` (same four labelled lines) and run again.

A task is done only when its gates pass. With `--fake` every model answer is scripted below (it
reads the documents in the prompt), so a run needs no key, no network and costs nothing; budgets,
prices, policies, gates and the ledger are real. Only run team folders you trust.
"""

import csv
import hashlib
import io
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

REVIEW = "Each extracted number, vendor, date and total matches its document."
FIELDS = ("invoice_number", "vendor", "date", "total")
LABELS = {"invoice_number": "Invoice No", "vendor": "Vendor", "date": "Date", "total": "Total due"}


def read_invoices(root: Path) -> dict[str, str]:
    """The sample invoices by file name."""
    return {p.stem: p.read_text("utf-8") for p in sorted((root / "invoices").glob("*.txt"))}


def parse_invoice(text: str) -> dict[str, str]:
    """The four fields of one invoice; the total keeps two decimals and has no thousands comma."""
    row: dict[str, str] = {}
    for field, label in LABELS.items():
        found = re.search(rf"(?m)^{label}: (.+?)(?: USD)?$", text)
        row[field] = found.group(1).strip() if found else ""
    row["total"] = f"{Decimal(row['total'].replace(',', '')):.2f}" if row["total"] else ""
    return row


def fake_model(request: ModelRequest) -> str:
    """The scripted answers used with --fake."""
    if request.model == "reviewer-1":
        return json.dumps({"criteria": {"fields-match": {"verdict": "met", "evidence": REVIEW}}})
    text = " ".join(m.content for m in request.messages)
    docs = re.findall(r"(?s)<doc>(.*?)</doc>", text)
    if "[stage-1]" in text:
        return json.dumps({"rows": [parse_invoice(d) for d in docs]})
    return "Export ready: the CSV has one line per invoice."


def rule(gate: str, criterion: str, checks: list[dict[str, Any]]) -> GateSpec:
    """A cheap deterministic gate on the output."""
    return GateSpec(
        id=gate,
        kind=GateKind.RULE,
        checks=(criterion,),
        config={"rules": [{"criterion": criterion, "check": c} for c in checks]},
    )


def to_csv(rows: list[dict[str, str]]) -> str:
    """The export: a header and one line per invoice."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def safe_total(rows: list[dict[str, str]]) -> str | None:
    """The sum of the rows' totals as text, or None if a row has no usable number."""
    try:
        return f"{sum((Decimal(r['total']) for r in rows), Decimal(0)):.2f}"
    except (KeyError, TypeError, ArithmeticError, ValueError):
        return None


def setup(env: TeamEnv) -> Team:
    """Build the run: extract, export (human approval), a worker per agent and the run budget."""
    run = env.run_id
    invoices = read_invoices(env.root)
    # the reference: a separate pass over the same files, never the model's answer
    reference = [parse_invoice(t) for t in invoices.values()]
    expected_total = f"{sum((Decimal(r['total']) for r in reference), Decimal(0)):.2f}"
    prompt_docs = "\n".join(f"<doc>\n{t}\n</doc>" for t in invoices.values())
    extracted: dict[str, list[dict[str, str]]] = {}

    def extractor(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You extract invoice fields. Answer with JSON."),
                Message(role="user", content=f"{task.instructions}\n\n{prompt_docs}"),
            ],
            max_output_tokens=600,
        )
        try:
            rows = [dict(r) for r in json.loads(reply.response.text)["rows"]]
        except (ValueError, KeyError, TypeError):
            return {"count": 0, "total": "0.00", "rows": []}
        total = safe_total(rows)
        if total is None:
            return {"count": 0, "total": "0.00", "rows": []}
        extracted["rows"] = rows
        return {"count": len(rows), "total": total, "rows": rows}

    def exporter(task: Task, ctx: WorkContext) -> dict[str, Any]:
        reply = ctx.complete(
            [
                Message(role="system", content="You prepare accounting exports. Be brief."),
                Message(role="user", content=task.instructions),
            ],
            max_output_tokens=100,
        )
        text = to_csv(extracted.get("rows", []))
        ctx.authorize_write(env.root, "exports/invoices.csv")  # policy check + ledger record
        target = env.root / "exports" / "invoices.csv"
        target.parent.mkdir(exist_ok=True)
        target.write_text(text, "utf-8")
        return {"text": reply.response.text, "csv": text, "path": "exports/invoices.csv"}

    done = Criterion(id="fields-match", text="Every extracted field matches its document")
    extract = Task(
        id=f"{run}-extract",
        run_id=run,
        title="Extract the invoice fields",
        instructions="[stage-1] Extract invoice number, vendor, date and total from each document.",
        assignee="extractor",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(done,),
            gates=(
                rule(
                    "counts-and-total",
                    "fields-match",
                    [
                        {"type": "path_equals", "path": "count", "value": len(reference)},
                        {"type": "path_equals", "path": "total", "value": expected_total},
                    ],
                ),
                GateSpec(
                    id="invoice-check",
                    kind=GateKind.VERIFIER,
                    checks=("fields-match",),
                    config={"verifier": "invoice-checker", "require_different_model": True},
                ),
            ),
        ),
    )
    export = Task(
        id=f"{run}-export",
        run_id=run,
        title="Export the CSV",
        instructions="[stage-2] Prepare the CSV export of the checked rows.",
        assignee="exporter",
        max_attempts=2,
        definition_of_done=DefinitionOfDone(
            criteria=(
                Criterion(id="csv-header", text="The CSV starts with the four column names"),
                Criterion(id="approved", text="A human approved the export"),
            ),
            gates=(
                rule(
                    "csv-header-row",
                    "csv-header",
                    [{"type": "contains", "value": ",".join(FIELDS)}],
                ),
                GateSpec(
                    id="approve-export",
                    kind=GateKind.HUMAN,
                    checks=("approved",),
                    config={"instructions": "Open exports/invoices.csv and approve the export."},
                ),
            ),
        ),
    )
    return Team(
        plan=[PlannedTask(extract), PlannedTask(export, depends_on=(extract.id,))],
        workers={"extractor": extractor, "exporter": exporter},
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
