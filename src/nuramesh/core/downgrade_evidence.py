"""Evidence and recommendation for the downgrade report (R1.34 Part B).

The cases are b01..b31; the choices are in T188e.

Pure functions over facts the ledger reader produces: which calls a task made (a verifier gate's
calls are marked and never count), how the task ended and how many attempts it closed. Money is
`Decimal` and every comparison is exact (`Fraction`); an unknown cost stays unknown and keeps the
cost per accepted result `None`, never a guess. Nothing here reads a store or writes anything.
"""

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any


def round6(value: Decimal | Fraction) -> str:
    """A ratio as text with 6 places, rounded half away from zero (exact arithmetic)."""
    exact = Fraction(value)
    scaled = abs(exact) * 1_000_000
    units = int(scaled + Fraction(1, 2))  # floor of a non-negative number
    sign = "-" if exact < 0 and units else ""
    return f"{sign}{units // 1_000_000}.{units % 1_000_000:06d}"


def _text(value: Decimal) -> str:
    """Plain text of an exact sum (`1E+1` is `10`, `0.50` is `0.5`)."""
    return format(value.normalize() if value else Decimal(0), "f")


@dataclass(frozen=True)
class CallFact:
    """One spend call of a task: its model key, its known cost (None: unknown) and whether it
    is a verifier gate's own call."""

    model: str
    cost: Decimal | None
    gate: bool = False


@dataclass(frozen=True)
class TaskFact:
    """One task as of `as_of`, for one role: the role's spend calls on it and how it stands.

    `state` is the task state (`done`, `needs_human`, `cancelled` or anything else);
    `closes` counts its moves that closed an attempt; `final_move_closed` says whether the move
    that put it in its present state closed an attempt; `ever_done` that it reached `done`.
    """

    key: str
    role: str
    calls: tuple[CallFact, ...]
    first_call_at: datetime | None
    state: str
    closes: int
    final_move_closed: bool
    ever_done: bool


@dataclass(frozen=True)
class Row:
    """The evidence for one (role, model)."""

    tasks: int
    attempts: int
    passed: int
    first_try: int
    known_total: Decimal
    unknown_calls: int

    @property
    def cost_per_accepted(self) -> Fraction | None:
        """Known total over passed tasks, only when no call is unknown and something passed."""
        if self.unknown_calls or self.passed == 0:
            return None
        return Fraction(self.known_total) / self.passed

    def to_json(self) -> dict[str, Any]:
        """The API and `cases.json` form."""
        cpa = self.cost_per_accepted
        return {
            "tasks": self.tasks,
            "attempts": self.attempts,
            "passed": self.passed,
            "first_try": self.first_try,
            "pass_rate": round6(Fraction(self.passed, self.attempts)),
            "first_try_rate": round6(Fraction(self.first_try, self.tasks)),
            "known_total": _text(self.known_total),
            "unknown_calls": self.unknown_calls,
            "cost_per_accepted": None if cpa is None else round6(cpa),
        }


@dataclass(frozen=True)
class Evidence:
    """Rows keyed by role then model key, and the tasks left out (counted in tasks)."""

    rows: dict[str, dict[str, Row]]
    excluded: dict[str, int] = field(default_factory=lambda: {"mixed_model": 0, "not_evidence": 0})


def _outcome(task: TaskFact) -> str | None:
    """`pass`, `fail` or None (not evidence): cases.json, T188e."""
    if task.state == "done":
        return "pass"
    if task.state == "needs_human" and task.final_move_closed:
        return "fail"
    if task.state == "cancelled" and task.closes >= 1 and not task.ever_done:
        return "fail"
    return None


def build_evidence(
    tasks: Sequence[TaskFact],
    *,
    start: datetime | None,
    end: datetime | None,
    as_of: datetime | None,
) -> Evidence:
    """Rows per (role, model) from the tasks whose first worker call is in `[start, end)` and by
    `as_of`. A missing bound is no limit."""
    acc: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    excluded = {"mixed_model": 0, "not_evidence": 0}
    for task in tasks:
        calls = [c for c in task.calls if not c.gate]
        if not calls:
            continue
        at = task.first_call_at
        if at is not None and (
            (start is not None and at < start)
            or (end is not None and at >= end)
            or (as_of is not None and at > as_of)
        ):
            continue
        models = {c.model for c in calls}
        if len(models) > 1:
            excluded["mixed_model"] += 1
            continue
        outcome = _outcome(task)
        if outcome is None:
            excluded["not_evidence"] += 1
            continue
        model = calls[0].model
        row = acc[task.role].setdefault(
            model,
            {
                "tasks": 0,
                "attempts": 0,
                "passed": 0,
                "first_try": 0,
                "total": Decimal(0),
                "unknown": 0,
            },
        )
        passed = outcome == "pass"
        row["tasks"] += 1
        row["attempts"] += task.closes + (1 if passed else 0)
        row["passed"] += 1 if passed else 0
        row["first_try"] += 1 if passed and task.closes == 0 else 0
        for c in calls:
            if c.cost is None:
                row["unknown"] += 1
            else:
                row["total"] += c.cost
    rows = {
        role: {
            model: Row(
                r["tasks"], r["attempts"], r["passed"], r["first_try"], r["total"], r["unknown"]
            )
            for model, r in sorted(models.items())
        }
        for role, models in sorted(acc.items())
    }
    return Evidence(rows, excluded)


@dataclass(frozen=True)
class Recommendation:
    """The answer for one role (cases.json, T188e)."""

    status: str
    current: str
    candidate: str | None
    refused: tuple[tuple[str, str], ...]
    saving: str | None = None
    projected_saving: str | None = None
    pass_rate_current: str | None = None
    pass_rate_candidate: str | None = None
    tasks_current: int | None = None
    tasks_candidate: int | None = None

    def to_json(self) -> dict[str, Any]:
        """The API form: the extra fields only for `recommend`."""
        out: dict[str, Any] = {
            "status": self.status,
            "current": self.current,
            "candidate": self.candidate,
            "refused": [{"model": m, "reason": r} for m, r in self.refused],
        }
        if self.status == "recommend":
            out.update(
                saving=self.saving,
                projected_saving=self.projected_saving,
                pass_rate_current=self.pass_rate_current,
                pass_rate_candidate=self.pass_rate_candidate,
                tasks_current=self.tasks_current,
                tasks_candidate=self.tasks_candidate,
            )
        return out


def current_model(models: Mapping[str, Row]) -> str:
    """The current model of a role: the row with the most tasks, a tie to the smaller key."""
    return min(models, key=lambda k: (-models[k].tasks, k))


def recommend(
    role: str,
    models: Mapping[str, Row],
    *,
    min_sample: int,
    families: Mapping[str, str],
    verifier_models: Collection[str],
    eligible: Collection[str] | None,
) -> Recommendation:
    """Is there a cheaper model of this role that the evidence supports (cases.json, T188e)?

    `families` maps a model key to its family (a model without one is its own family),
    `verifier_models` are the models of the verifiers that check this role, `eligible` the models
    the organization allows for the role (None: all). The pass rate is compared exactly per
    attempt, with no tolerance; an equal rate qualifies.
    """
    del role  # the rows are already this role's
    current_key = current_model(models)
    cur = models[current_key]
    others = [k for k in sorted(models) if k != current_key]
    if cur.tasks < min_sample or not any(models[k].tasks >= min_sample for k in others):
        return Recommendation("insufficient_evidence", current_key, None, ())
    cur_cpa = cur.cost_per_accepted
    verifier_families = {families.get(m, m) for m in verifier_models}
    refused: list[tuple[str, str]] = []
    best: tuple[Fraction, str] | None = None
    for key in others:
        cand = models[key]
        cand_cpa = cand.cost_per_accepted
        if cand.tasks < min_sample or cur_cpa is None or cand_cpa is None or cand_cpa >= cur_cpa:
            continue
        if cand.passed * cur.attempts < cur.passed * cand.attempts:
            continue
        if eligible is not None and key not in eligible:
            refused.append((key, "not_eligible"))
        elif families.get(key, key) in verifier_families:
            refused.append((key, "same_family_as_verifier"))
        else:
            saving = (cur_cpa - cand_cpa) / cur_cpa
            if best is None or saving > best[0]:
                best = (saving, key)
    if best is None:
        return Recommendation("none", current_key, None, tuple(refused))
    saving, key = best
    cand = models[key]
    assert cur_cpa is not None and cand.cost_per_accepted is not None
    return Recommendation(
        "recommend",
        current_key,
        key,
        tuple(refused),
        saving=round6(saving),
        projected_saving=round6((cur_cpa - cand.cost_per_accepted) * cur.passed),
        pass_rate_current=round6(Fraction(cur.passed, cur.attempts)),
        pass_rate_candidate=round6(Fraction(cand.passed, cand.attempts)),
        tasks_current=cur.tasks,
        tasks_candidate=cand.tasks,
    )
