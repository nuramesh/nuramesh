"""The budget headroom read a run start makes (R1.20 PR B, ND347 section 4.5 step 10).

Read only: nothing is reserved, no price is guessed, no team code runs. For **each agent the
organization declares** (in id order) it finds the budgets that would apply to a call of that
agent (`applicable_budgets`) and looks at the balances as a call would see them: a windowed budget
whose window has ended is rolled **virtually** (computed, never written), so a `reset` budget
exhausted in a past window does not refuse a start that a call would allow.

An agent with no applicable budget is skipped. The start passes when at least one agent has a
budget that is not blocked and has an available amount above zero; otherwise the refusal code is
`run_budget_missing` (no agent has a budget), `run_budget_blocked` (every agent with a budget is
blocked) or `run_budget_exhausted`. The exact per-assignee check is the runner's.
"""

from nuramesh.core.budgets import BudgetBalance, CallContext, applicable_budgets, roll
from nuramesh.core.org import OrgGraph
from nuramesh.stores.base import Store

MISSING = "run_budget_missing"
BLOCKED = "run_budget_blocked"
EXHAUSTED = "run_budget_exhausted"


class RunBudgetRefused(Exception):  # noqa: N818 (a decision, said with a code)
    """The start has no budget headroom. `code` is fixed; there is never per-agent text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_start_headroom(store: Store, org: OrgGraph, run_id: str) -> None:
    """Raise `RunBudgetRefused` unless at least one declared agent could still spend."""
    agents = sorted(a.id for a in org.agents)
    if not agents:
        raise RunBudgetRefused(MISSING)
    with store.transaction() as uow:
        now = uow.now()
        stored = {s.balance.budget.id: s.balance for s in uow.list_balances(org.id)}
    states: list[str] = []  # "open", "blocked" or "exhausted", one per agent that has a budget
    for agent_id in agents:
        context = CallContext(run_id=run_id, agent_id=agent_id)
        found = applicable_budgets([b.budget for b in stored.values()], context, org)
        if not found:
            continue
        balances: list[BudgetBalance] = []
        for budget in found:
            balance = stored[budget.id]
            rolled = roll(balance, now)
            balances.append(balance if rolled is None else rolled.balance)
        if any(b.blocked for b in balances):
            states.append("blocked")
        elif all(b.available.amount > 0 for b in balances):
            states.append("open")
        else:
            states.append("exhausted")
    if not states:
        raise RunBudgetRefused(MISSING)
    if "open" in states:
        return
    raise RunBudgetRefused(BLOCKED if all(s == "blocked" for s in states) else EXHAUSTED)
