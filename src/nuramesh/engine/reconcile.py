"""Reconcile reservations a crash left open (R1.14a; T24).

A reservation holds budget for one model call. Normally it is settled or released within moments.
If the process dies after reserving, or settling fails after the provider answered (the model
client's documented windows, T18), the reservation stays **open** and keeps holding budget.
`reconcile_stale_reservations` closes the ones that have been open longer than `max_age`:

- `Mode.CHARGE_UNKNOWN` (the default, the safe one): charge the reservation as an **unknown cost**
  (T12: the reservation is an upper bound, recorded as such). The call may have reached the provider
  and been paid for, and a budget must never be under-charged silently.
- `Mode.RELEASE`: give the hold back (nothing charged), for an operator who knows no call was made.

Both go through the budget service (`settle_reservation` / `release_reservation`), so they are one
transaction each and appear in the ledger (`budget.settled` or `budget.released`) with
`details: {reconciled: true, reason, age_seconds, mode}`. Safe against races: closing an open
reservation checks its status inside the transaction, so a reservation that settles normally at
the same moment is skipped, never double charged.

`max_age` must be comfortably longer than the slowest provider call plus retries. A call that is
still in flight when its reservation is reconciled finds the reservation closed: the model client
then records the late result
(`model.called` with `outcome: settled_late` and the real usage and cost) and, if the real cost is
above what was charged (or nothing was charged because the reservation was released), **adds the
difference to the budgets' spent** (`budget.adjusted`, reason `overrun_after_reconciliation`; an
unknown cost after a release is charged at the reservation, an upper bound). If that leaves a budget
over its limit, `budget.breached` is recorded and the budget is blocked until a human lifts it
(ND23). All of it is one transaction, so the balance and the ledger always agree.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from nuramesh.core.budgets import ReservationStatus
from nuramesh.core.ledger import CostBasis, EventType
from nuramesh.engine.budgets import release_reservation, settle_reservation
from nuramesh.stores.base import ConcurrencyError, Store, run_stream


class Mode(StrEnum):
    """What to do with a stale reservation."""

    CHARGE_UNKNOWN = "charge_unknown"
    RELEASE = "release"


@dataclass(frozen=True)
class Reconciled:
    """One reservation that was closed, and how."""

    reservation_id: str
    mode: Mode
    age_seconds: int


@dataclass(frozen=True)
class ReconcileReport:
    """What a pass did: closed reservations, ones skipped because they settled meanwhile, and ones
    whose reservation event couldn't be found (never touched: a damaged ledger, for a human), and
    ones that failed with an error (the pass goes on; a later pass retries them)."""

    closed: tuple[Reconciled, ...] = ()
    raced: tuple[str, ...] = ()
    unknown_age: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()


def reconcile_stale_reservations(
    store: Store,
    run_id: str,
    *,
    max_age: timedelta,
    mode: Mode = Mode.CHARGE_UNKNOWN,
    actor: str = "system",
    now: Callable[[], datetime] | None = None,
    task_id: str | None = None,
) -> ReconcileReport:
    """Close the run's reservations that have been open longer than `max_age` (module docstring).
    With `task_id`, only that task's (a retried Temporal activity closes what its own dead try
    left, R1.20 PR 3, without touching the calls other tasks have in flight)."""
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    open_ones = store.reservations_for_run(run_id, ReservationStatus.OPEN)
    if not open_ones:
        return ReconcileReport()
    current = (now or store.now)()
    if current.tzinfo is None:
        raise ValueError("now() must return a timezone-aware time")
    wanted = {stored.reservation.id for stored in open_ones}
    reserved_at: dict[str, datetime] = {}
    reserved_for: dict[str, str | None] = {}
    for event in store.read_stream(run_stream(run_id)):
        if event.type == EventType.BUDGET_RESERVED:
            rid = str(event.payload.get("reservation"))
            if rid in wanted:
                reserved_at.setdefault(rid, event.at)
                reserved_for.setdefault(rid, event.task_id)
                if len(reserved_at) == len(wanted):
                    break  # every open reservation's start is known
    if task_id is not None:
        open_ones = [r for r in open_ones if reserved_for.get(r.reservation.id) == task_id]
    closed: list[Reconciled] = []
    raced: list[str] = []
    unknown: list[str] = []
    failed: list[tuple[str, str]] = []
    for stored in open_ones:
        rid = stored.reservation.id
        opened = reserved_at.get(rid)
        if opened is None:
            unknown.append(rid)
            continue
        age = current - opened
        if age <= max_age:
            continue
        seconds = int(age.total_seconds())
        reason = f"open for {seconds}s (limit {int(max_age.total_seconds())}s)"
        details = {"reconciled": True, "reason": reason, "age_seconds": seconds, "mode": mode.value}
        try:
            if mode is Mode.CHARGE_UNKNOWN:
                settle_reservation(
                    store,
                    rid,
                    actual=None,
                    cost_basis=CostBasis.UNKNOWN,
                    actor=actor,
                    details=details,
                )
            else:
                release_reservation(store, rid, actor=actor, reason=reason, details=details)
        except ConcurrencyError:
            # Usually: settled or released by someone else in the meantime. Check before saying so.
            try:
                now_stored = store.get_reservation(rid)
            except Exception as exc:
                failed.append((rid, type(exc).__name__))
                continue
            if now_stored is not None and now_stored.reservation.status is ReservationStatus.OPEN:
                failed.append((rid, "ConcurrencyError"))
            else:
                raced.append(rid)
            continue
        except Exception as exc:  # one bad reservation must not stop the others
            failed.append((rid, type(exc).__name__))
            continue
        closed.append(Reconciled(reservation_id=rid, mode=mode, age_seconds=seconds))
    return ReconcileReport(
        closed=tuple(closed),
        raced=tuple(raced),
        unknown_age=tuple(unknown),
        failed=tuple(failed),
    )
