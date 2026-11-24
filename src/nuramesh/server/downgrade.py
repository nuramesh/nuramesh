"""The evidence-based downgrade report (ND63): read-only, derived from the ledger.

For every role and model the organization has used, it shows the gate and verifier pass rates next
to the cost per task, and, only where the evidence supports it, a "cheaper model" suggestion. It
reads the store and the ledger and never writes anything or switches a model.

**What is counted.** A task is attributed to the role of its assignee and to the model of the
assignee's own `model.called` entries on it (calls by other agents, for example a verifier's
model, are not the worker's cost). A *gated result* is one `gate.completed` entry of that task,
attributed to the model of the assignee's latest call before it. By the gate's kind it is a
*verifier* result (kind `verifier`), a *gate* result (`test_command`, `rule`, `schema`, `custom`),
or ignored (`human`: a person's decision says nothing about the model). Outcomes `passed` and
`failed` are verdicts; `error` is the infrastructure failing, so it is shown but is neither a pass
nor a fail. The pass rate is passed over passed plus failed, across gate and verifier results.

**Which calls count.** Only calls that produced an answer: outcome `ok` and `settled_late` carry
a cost (or an unknown one) and are attributed. `error` (the reservation was released, nothing was
charged, no answer) is skipped entirely: it is neither cost, nor an unknown price, nor credit for
the task, and it does not change which model later gated results belong to, so a failed call to X
followed by a retry on Y credits only Y. `settle_failed` (the provider answered, the settlement
failed, money may have been spent) is recorded as an unknown cost for that model but credits no
task.

**Repeated gate rounds.** Every `gate.completed` entry counts, once per attempt: a task that failed
its gate and passed on the retry has one failed and one passed result for the model of its latest
answered call. The sample size `min_results` counts these results, not tasks.

**Limits.** The scan is bounded like the runs list (`views.MAX_RUNS_SCANNED`,
`views.MAX_EVENTS_SCANNED`), newest runs first, so a truncated report (`truncated: true`) is biased
toward recent runs. Runs of other organizations are read before they are skipped, so they count
toward `MAX_RUNS_SCANNED` and can crowd this organization's older runs out.

**Cost.** Cost per task is the model's known spend on its tasks divided by the number of those
tasks. If any call of that role and model has no recorded price (or another currency), the cost is
*unknown* and shown as unknown, never guessed (ND3); a model with an unknown price is never
suggested and never compared with.

**The recommendation (R1.34 Part B, core.downgrade_evidence; cases b01..b31 in
the shared token-savings fixtures, choices in T188e).**
One answer per role: `recommendation`, from the evidence of finished tasks in a window (`from`,
`to`, `as_of`), counted per attempt and compared exactly, with no tolerance. The sample threshold
is `min_sample` tasks (default 30). A candidate must also be eligible for the role and of another
model family than the role's verifier models. The report is advice: it writes nothing and never
switches a model. The shipped per-model `suggestion` and `min_results` are retired.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from nuramesh.core.canonical import decimal_text
from nuramesh.core.downgrade_evidence import (
    CallFact,
    Evidence,
    TaskFact,
    build_evidence,
    current_model,
    recommend,
)
from nuramesh.core.gates import GateKind
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.model_catalog import CatalogFold, accepted_classes, effective_ref
from nuramesh.core.models import payload_model_ref
from nuramesh.core.money import Currency, Money
from nuramesh.core.org import OrgGraph, RoleKind
from nuramesh.server import views
from nuramesh.server.scope import MAX_NEW_LOOKUPS
from nuramesh.stores.base import BadTimeError, Store

MIN_SAMPLE = 30
_MONEY_PLACES = Decimal("0.00000001")
_RATE_PLACES = Decimal("0.0001")
T = EventType
_PRICED_OUTCOMES = ("ok", "settled_late")  # `model.called` outcomes that carry a cost


def _round_money(value: Decimal) -> Decimal:
    """Round to 8 decimal places for display, but keep a shorter exact value as it is."""
    places = value.quantize(_MONEY_PLACES, rounding=ROUND_HALF_EVEN)
    return value if places == value else places


@dataclass(frozen=True)
class ModelStats:
    """What the ledger shows for one model in one role."""

    role: str
    model: str
    tasks: int
    gate_passed: int
    gate_failed: int
    verifier_passed: int
    verifier_failed: int
    cost: Money | None  # known spend (None: nothing known)
    gate_errors: int = 0
    verifier_errors: int = 0
    unknown_cost_calls: int = 0
    calls: int = 0

    @property
    def passed(self) -> int:
        """Gate and verifier results that passed."""
        return self.gate_passed + self.verifier_passed

    @property
    def results(self) -> int:
        """Gated results that were verdicts (passed or failed); errors are not."""
        return self.passed + self.gate_failed + self.verifier_failed

    @property
    def errors(self) -> int:
        """Gate and verifier results that errored."""
        return self.gate_errors + self.verifier_errors

    @property
    def pass_rate(self) -> Decimal | None:
        """Passed over passed plus failed, or None with no verdicts."""
        if self.results == 0:
            return None
        with localcontext() as ctx:
            ctx.prec = 60
            return Decimal(self.passed) / Decimal(self.results)

    @property
    def cost_known(self) -> bool:
        """True when every call of this model had a recorded price."""
        return self.cost is not None and self.unknown_cost_calls == 0 and self.tasks > 0

    @property
    def cost_per_task(self) -> Money | None:
        """Known spend per task, or None (unknown) when any call's price is unknown."""
        if not self.cost_known or self.cost is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 60
            return Money.of(_round_money(self.cost.amount / Decimal(self.tasks)))


# -- reading the ledger ---------------------------------------------------------------------


def _counts() -> dict[str, int]:
    return {"passed": 0, "failed": 0, "error": 0}


@dataclass
class _Acc:
    tasks: set[str] = field(default_factory=set)
    gate: dict[str, int] = field(default_factory=_counts)
    verifier: dict[str, int] = field(default_factory=_counts)
    cost: Decimal = Decimal(0)
    unknown: int = 0
    calls: int = 0


def _role_of(org: OrgGraph, agent_id: str) -> str:
    try:
        return org.agent(agent_id).role
    except KeyError:
        return "unknown"


def collect(
    store: Store,
    org: OrgGraph,
    events: Sequence[LedgerEvent],
    acc: dict[tuple[str, str], _Acc],
    run_id: str,
) -> None:
    """Add one run's evidence to `acc` (keyed by role and model)."""
    tasks = {t.task.id: t.task for t in store.tasks_for_run(run_id)}
    last_model: dict[str, str] = {}  # task id -> model of the assignee's latest call
    for e in events:
        task = tasks.get(e.task_id or "")
        if task is None or task.assignee is None:
            continue
        role = _role_of(org, task.assignee)
        if e.type == T.MODEL_CALLED.value:
            if e.payload.get("agent") != task.assignee:
                continue
            model = payload_model_ref(e.payload)
            outcome = str(e.payload.get("outcome"))
            if outcome == "settle_failed":  # the provider answered; money may have been spent
                acc.setdefault((role, model), _Acc()).unknown += 1
                continue
            if outcome not in _PRICED_OUTCOMES:  # `error`: released, nothing charged, no result
                continue
            last_model[task.id] = model
            a = acc.setdefault((role, model), _Acc())
            a.tasks.add(task.id)
            a.calls += 1
            if e.cost is None or e.cost.currency != Currency.USD:
                a.unknown += 1
            else:
                a.cost += e.cost.amount
        elif e.type == T.GATE_COMPLETED.value and task.id in last_model:
            try:
                kind = task.definition_of_done.gate(str(e.payload.get("gate"))).kind
            except KeyError:
                continue
            outcome = str(e.payload.get("outcome"))
            if kind is GateKind.HUMAN or outcome not in ("passed", "failed", "error"):
                continue
            a = acc.setdefault((role, last_model[task.id]), _Acc())
            (a.verifier if kind is GateKind.VERIFIER else a.gate)[outcome] += 1


@dataclass(frozen=True)
class RecommendationInputs:
    """What the recommendation reads besides the ledger, at request time (T188e): the family
    of each model key, the verifier models of each role, and the models the organization allows
    for a role, given the role's current model (None: all)."""

    families: Mapping[str, str] = field(default_factory=dict)
    verifier_models: Mapping[str, Collection[str]] = field(default_factory=dict)
    eligible_for: Callable[[str, str], Collection[str] | None] = lambda role, current: None


def recommendation_inputs(org: OrgGraph, fold: CatalogFold) -> RecommendationInputs:
    """The family, verifier and eligibility inputs of T188e (3), from the folded catalog.

    A model with no catalog entry is its own family. The verifier models of a role are the
    effective models of the verifier agents that check agents of that role. A model is eligible
    for a role when, with a catalog, it is an `approved` entry whose processing scope accepts at
    least the data classes the role's current model accepts (a downgrade never moves data to a
    place the current model is not allowed to send it, ND45); with no catalog every model is.
    """
    families = {e.ref: e.family for e in fold.entries.values()}
    verifiers: dict[str, set[str]] = {}
    for agent in org.agents:
        if org.role(agent.role).kind is not RoleKind.VERIFIER:
            continue
        ref = effective_ref(org, fold.assignments, agent.id).ref
        if ref is None:
            continue
        for checked in agent.checks:
            try:
                verifiers.setdefault(org.agent(checked).role, set()).add(ref)
            except KeyError:
                continue
    by_ref = {e.ref: e for e in fold.entries.values()}

    def eligible_for(role: str, current: str) -> Collection[str] | None:
        del role
        if not fold.exists:
            return None
        here = by_ref.get(current)
        needed = set(accepted_classes(here.processing, fold.customer_regions)) if here else set()
        return {
            ref
            for ref, e in by_ref.items()
            if e.status == "approved"
            and needed <= set(accepted_classes(e.processing, fold.customer_regions))
        }

    return RecommendationInputs(families, verifiers, eligible_for)


def _unit_facts(
    org: OrgGraph, events: Sequence[LedgerEvent], run_id: str, as_of: datetime | None
) -> list[TaskFact]:
    """The (task, role) units of one run as of `as_of`: spend calls of the role (spec 2) and how
    the task stands. Entries after `as_of` do not exist."""
    calls: dict[tuple[str, str], list[tuple[datetime, CallFact]]] = {}
    state: dict[str, tuple[str, bool]] = {}
    closes: dict[str, int] = {}
    ever_done: set[str] = set()
    for e in events:
        if as_of is not None and e.at > as_of:
            continue
        if e.task_id is None:
            continue
        if e.type == T.TASK_TRANSITIONED.value:
            to = e.payload.get("to")
            if not isinstance(to, str):
                continue
            closed = e.payload.get("closed_attempt") is True
            state[e.task_id] = (to, closed)
            closes[e.task_id] = closes.get(e.task_id, 0) + (1 if closed else 0)
            if to == "done":
                ever_done.add(e.task_id)
        elif e.type == T.MODEL_CALLED.value:
            outcome = e.payload.get("outcome")
            if outcome not in (*_PRICED_OUTCOMES, "settle_failed"):
                continue
            known = (
                outcome != "settle_failed"
                and e.cost is not None
                and e.cost.currency == Currency.USD
            )
            if outcome != "settle_failed" and e.cost is None and e.cost_basis is None:
                continue  # nothing was spent
            agent = str(e.payload.get("agent"))
            role = e.payload.get("role")
            if not isinstance(role, str):
                role = _role_of(org, agent)
            fact = CallFact(
                model=payload_model_ref(e.payload),
                cost=e.cost.amount if known and e.cost is not None else None,
                gate=bool(e.payload.get("gate")),
            )
            calls.setdefault((e.task_id, role), []).append((e.at, fact))
    out: list[TaskFact] = []
    for (task, role), found in calls.items():
        to, final_closed = state.get(task, ("open", False))
        worker = [at for at, c in found if not c.gate]
        out.append(
            TaskFact(
                key=f"{run_id}/{task}",
                role=role,
                calls=tuple(c for _, c in found),
                first_call_at=min(worker) if worker else None,
                state=to,
                closes=closes.get(task, 0),
                final_move_closed=final_closed,
                ever_done=task in ever_done,
            )
        )
    return out


def build_report(
    store: Store,
    org: OrgGraph,
    *,
    min_sample: int = MIN_SAMPLE,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    inputs: RecommendationInputs | None = None,
) -> dict[str, Any]:
    """The report for `org`'s runs (bounded like the runs list: see `views`)."""
    inputs = inputs or RecommendationInputs()
    heads = [h for h in store.heads().values() if h.stream.startswith("run:")]
    heads.sort(key=lambda h: (h.at, h.stream), reverse=True)
    budget = views.MAX_EVENTS_SCANNED
    acc: dict[tuple[str, str], _Acc] = {}
    units: list[TaskFact] = []
    scanned = 0
    seen = 0
    truncated = False
    lookups = MAX_NEW_LOOKUPS  # first-entry reads of runs that turn out to be another's
    for head in heads:
        # another organization's run costs one first-entry read, never a full read, and does not
        # count against the caller's window
        if store.run_owner(head.stream) != org.id:
            first = None
        else:
            try:
                first = next(iter(store.read_stream(head.stream, after_seq=0, limit=1)), None)
            except BadTimeError:  # a damaged run is left out of the report (ND238)
                continue
        if first is None or first.type != T.RUN_STARTED.value or first.payload.get("org") != org.id:
            lookups -= 1
            if lookups <= 0:
                truncated = True
                break
            continue
        if seen >= views.MAX_RUNS_SCANNED or head.seq > budget:
            truncated = True
            break
        seen += 1
        budget -= head.seq
        try:
            events = views.read_all(store, head.stream)
        except BadTimeError:  # a damaged run is left out of the report (ND238)
            continue
        started = next((e for e in events if e.type == T.RUN_STARTED.value), None)
        if started is None:
            continue
        scanned += 1
        collect(store, org, events, acc, head.stream[4:])
        units.extend(_unit_facts(org, events, head.stream[4:], as_of))
    stats = [
        ModelStats(
            role=role,
            model=model,
            tasks=len(a.tasks),
            gate_passed=a.gate["passed"],
            gate_failed=a.gate["failed"],
            verifier_passed=a.verifier["passed"],
            verifier_failed=a.verifier["failed"],
            gate_errors=a.gate["error"],
            verifier_errors=a.verifier["error"],
            cost=Money.of(a.cost),
            unknown_cost_calls=a.unknown,
            calls=a.calls,
        )
        for (role, model), a in sorted(acc.items())
    ]
    evidence = build_evidence(units, start=start, end=end, as_of=as_of)
    roles = [
        {
            "role": role,
            "models": [_row(s) for s in mine],
            "evidence": {m: r.to_json() for m, r in evidence.rows.get(role, {}).items()},
            "recommendation": _recommendation(role, evidence, min_sample, inputs),
        }
        for role in sorted({s.role for s in stats} | set(evidence.rows))
        for mine in [[s for s in stats if s.role == role]]
    ]
    return {
        "org": org.id,
        "read_only": True,
        "window": {
            "from": _iso(start),
            "to": _iso(end),
            "as_of": _iso(as_of),
        },
        "min_sample": min_sample,
        "excluded": evidence.excluded,
        "runs_scanned": scanned,
        "truncated": truncated,
        "roles": roles,
    }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat().replace("+00:00", "Z")


def _recommendation(
    role: str, evidence: Evidence, min_sample: int, inputs: RecommendationInputs
) -> dict[str, Any] | None:
    models = evidence.rows.get(role)
    if not models:
        return None
    return recommend(
        role,
        models,
        min_sample=min_sample,
        families=inputs.families,
        verifier_models=inputs.verifier_models.get(role, ()),
        eligible=inputs.eligible_for(role, current_model(models)),
    ).to_json()


def _rate_text(rate: Decimal | None) -> str | None:
    if rate is None:
        return None
    return decimal_text(rate.quantize(_RATE_PLACES, rounding=ROUND_HALF_EVEN))


def _row(s: ModelStats) -> dict[str, Any]:
    return {
        "model": s.model,
        "tasks": s.tasks,
        "calls": s.calls,
        "gate": {"passed": s.gate_passed, "failed": s.gate_failed, "error": s.gate_errors},
        "verifier": {
            "passed": s.verifier_passed,
            "failed": s.verifier_failed,
            "error": s.verifier_errors,
        },
        "results": s.results,
        "pass_rate": _rate_text(s.pass_rate),
        "cost_per_task": views.money(s.cost_per_task),
        "cost_known": s.cost_known,
        "unknown_cost_calls": s.unknown_cost_calls,
    }
