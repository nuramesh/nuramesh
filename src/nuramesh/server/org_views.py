"""The answers of the org safety routes (E19, `spec/e19/README.md` section 11).

Pure reads of the `org:<id>` stream (and, for the breakers, the organization's run entries): these
functions write nothing. The routes in `server/app.py` call them after the engine's actions.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from nuramesh.core.breakers import (
    BreakerRule,
    HaltState,
    RoleState,
    breaker_state,
    breaker_statuses,
    halt_state,
    role_states,
)
from nuramesh.engine.killswitch import read_run_window
from nuramesh.engine.org_halt import read_org_events
from nuramesh.stores.base import Store


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def role_object(role: str, state: RoleState | None, in_config: bool) -> dict[str, Any]:
    """One entry of `halt.roles`."""
    paused = state is not None and state.paused
    return {
        "role": role,
        "paused": paused,
        "by": state.by if state is not None and paused else None,
        "at": _iso(state.at) if state is not None and paused else None,
        "cause": state.cause if state is not None and paused else None,
        "rule": state.rule if state is not None and paused else None,
        "ledger_seq": state.seq if state is not None and paused else None,
        "in_config": in_config,
    }


def roles_view(role_ids: Sequence[str], states: Mapping[str, RoleState]) -> list[dict[str, Any]]:
    """Every role of the config in config order, then orphan roles that are still paused."""
    out = [role_object(r, states.get(r), True) for r in role_ids]
    known = set(role_ids)
    out += [
        role_object(r, states[r], False)
        for r in sorted(states)
        if r not in known and states[r].paused
    ]
    return out


def halt_object(state: HaltState, roles: list[dict[str, Any]], can_act: bool) -> dict[str, Any]:
    """The `halt` object (spec 11)."""
    diag = state.cause == "evaluation_unavailable"
    return {
        "halted": state.halted,
        "by": state.by if state.halted else None,
        "at": _iso(state.at) if state.halted else None,
        "cause": state.cause if state.halted else None,
        "rule": state.rule if state.halted else None,
        "ledger_seq": state.seq if state.halted else None,
        "phase": state.phase if state.halted and diag else None,
        "error_class": state.error_class if state.halted and diag else None,
        "failing_rule": state.failing_rule if state.halted and diag else None,
        "failed_ticks": state.failed_ticks if state.halted and diag else None,
        "roles": roles,
        "can_act": can_act,
    }


def halt_view(store: Store, org_id: str, role_ids: Sequence[str], can_act: bool) -> dict[str, Any]:
    """`GET /v1/org/halt`: the halt state and every role, from one read of the org stream."""
    events = read_org_events(store, org_id)
    return halt_object(halt_state(events), roles_view(role_ids, role_states(events)), can_act)


def halt_from_events(
    events: Sequence[Any], role_ids: Sequence[str], can_act: bool
) -> dict[str, Any]:
    """The `halt` object folded from org-stream entries already in hand (the `_in` variants of
    R1.35 return them: a read after the commit would be wrong inside a keyed transaction)."""
    return halt_object(halt_state(events), roles_view(role_ids, role_states(events)), can_act)


def breakers_view(
    store: Store,
    org_id: str,
    role_ids: Sequence[str],
    rules: Sequence[BreakerRule],
    evaluation: dict[str, Any],
    can_act: bool,
    now: datetime,
) -> dict[str, Any]:
    """`GET /v1/org/breakers`. The live numbers are computed now, read-only."""
    events = read_org_events(store, org_id)
    runs: list[Any] = []
    if rules:
        since = now - timedelta(seconds=max(r.window_seconds for r in rules))
        runs = [e for found in read_run_window(store, org_id, since).values() for e in found]
    return {
        "org": org_id,
        "halt": halt_object(halt_state(events), roles_view(role_ids, role_states(events)), can_act),
        "evaluation": evaluation,
        "breakers": breaker_statuses(rules, events, runs, now),
    }


def ever_tripped(store: Store, org_id: str, name: str) -> bool:
    """Whether the org stream holds a `breaker.tripped` for this rule name (any actor)."""
    return any(
        e.type == "breaker.tripped" and e.payload.get("rule") == name
        for e in read_org_events(store, org_id)
    )


def rule_state(store: Store, org_id: str, name: str) -> Any:
    """The folded state of one rule name."""
    return breaker_state(read_org_events(store, org_id), name)
