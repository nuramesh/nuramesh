"""The dashboard formulas (spec/r211 section 3): pure, no I/O, shared by the API and the tests.

`compute(route, streams, window, cap, organization)` reads run streams (ledger entries, each
stream in seq order, streams in ascending stream-id order) and returns the figures of one
dashboard. It reads only named ledger fields and payload keys, never a payload as a whole; every
free-text value goes through `is_safe_string` and a failing value is dropped and counted.
Nothing from a prompt, reason, target, person id or path ever reaches the result.
"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from nuramesh.core.gates import GateKind
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.money import Money
from nuramesh.core.policy import ActionKind

ROUTES = ("verification", "verifiers", "cost-latency", "approvals")
MAX_WINDOW_DAYS = 400
UNKNOWN_KIND = "unknown"
_KINDS = frozenset(k.value for k in GateKind)
_ACTIONS = frozenset(a.value for a in ActionKind)
_OUTCOMES = ("passed", "failed", "error")
_COST_BASES = frozenset({"actual", "provisional"})

_SHAPE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,128}$")
_EMAIL = re.compile(r"^[^@]+@[^@]+\.[A-Za-z]{2,}$")
_FILE_END = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_KEY_RUN = re.compile(r"[A-Za-z0-9+/]{32,}")


def is_safe_string(value: object) -> bool:
    """The value guard of E3 section 6.1: an id-shaped string, not an email, path, person or key."""
    if not isinstance(value, str) or not _SHAPE.match(value):
        return False
    if _EMAIL.match(value) or value.startswith(("human:", "operator:")):
        return False
    if "/" in value:
        bad = (
            value.startswith(("/", "."))
            or "//" in value
            or ".." in value
            or value.count("/") != 1
            or _FILE_END.search(value) is not None
        )
        if bad:
            return False
    return _KEY_RUN.search(value) is None


@dataclass(frozen=True)
class Window:
    """`[start, end)` for window figures; `as_of` for state figures (approvals waiting)."""

    start: datetime
    end: datetime
    as_of: datetime


def iso(moment: datetime) -> str:
    """UTC text with a `Z`, as the cost views write it."""
    utc = moment.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    return text + (f".{utc.microsecond:06d}" if utc.microsecond else "") + "Z"


def ratio(top: int, bottom: int) -> str | None:
    """`top / bottom` as text with 4 places, half-even; None when there is no denominator."""
    if bottom == 0:
        return None
    return str((Decimal(top) / Decimal(bottom)).quantize(Decimal("0.0001"), ROUND_HALF_EVEN))


def nearest_rank(values: Sequence[int], q: int) -> int | None:
    """Nearest rank with integers only: k = ceil(q * n / 100), over the ascending values."""
    if not values:
        return None
    k = (q * len(values) + 99) // 100
    return sorted(values)[k - 1]


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


class _Tally:
    __slots__ = ("error", "failed", "passed")

    def __init__(self) -> None:
        self.passed = self.failed = self.error = 0

    def add(self, outcome: str) -> None:
        if outcome == "passed":
            self.passed += 1
        elif outcome == "failed":
            self.failed += 1
        else:
            self.error += 1

    def row(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "error": self.error,
            "pass_rate": ratio(self.passed, self.passed + self.failed),
        }


class _Money:
    __slots__ = ("calls", "total", "unknown")

    def __init__(self) -> None:
        self.total: Money | None = None
        self.calls = self.unknown = 0

    def add(self, amount: Decimal | None) -> None:
        self.calls += 1
        if amount is None:
            self.unknown += 1
        else:
            # `Money` addition is exact (60 digits, a trap on any rounding): a sum that would
            # round raises instead of returning a figure that is not the exact sum.
            money = Money(amount=amount)
            self.total = money if self.total is None else self.total + money

    def row(self) -> dict[str, Any]:
        total = Decimal(0) if self.total is None else self.total.amount
        return {
            "total": format(total, "f"),
            "calls": self.calls,
            "unknown_cost_calls": self.unknown,
        }


def _match_kinds(events: Sequence[LedgerEvent]) -> dict[int, str]:
    """For each `gate.completed` (by seq) the kind of its matching `gate.started`, else unknown.

    The match is the start with the largest seq below the completion that has its key
    (task, gate, attempt, submission), with no other completion of that key between (E3 5.5)."""
    open_starts: dict[tuple[object, ...], object] = {}
    kinds: dict[int, str] = {}
    for e in events:
        if e.type not in (EventType.GATE_STARTED.value, EventType.GATE_COMPLETED.value):
            continue
        p = e.payload
        key = (e.task_id, p.get("gate"), p.get("attempt"), p.get("submission"))
        if e.type == EventType.GATE_STARTED.value:
            open_starts[key] = p.get("kind")
        else:
            raw = open_starts.pop(key, None)
            kinds[e.seq] = raw if isinstance(raw, str) and raw in _KINDS else UNKNOWN_KIND
    return kinds


class Scan:
    """Accumulates one dashboard over streams fed in ascending stream-id order."""

    def __init__(self, route: str, window: Window, cap: int) -> None:
        if route not in ROUTES:
            raise ValueError(f"unknown dashboard '{route}'")
        self.route, self.window, self.cap = route, window, cap
        self.scanned = 0
        self.partial = False
        self.dropped: Counter[str] = Counter()
        self._overall = _Tally()
        self._gates: dict[str, _Tally] = {}
        self._roles: dict[str | None, _Tally] = {}
        self._v_passed = self._v_failed = self._unknown_kind = 0
        self._rounds: Counter[str] = Counter()
        self._done_without = 0
        self._money: dict[str, _Money] = {}
        self._model_money: dict[tuple[str, str], _Money] = {}
        self._role_money: dict[tuple[str | None, str], _Money] = {}
        self._latency: list[int] = []
        self._latency_skipped = 0
        self._waiting: list[tuple[datetime, str]] = []
        self._approved = self._denied = 0

    def add_stream(self, events: Sequence[LedgerEvent]) -> None:
        """Take one run stream (all its entries, in seq order)."""
        w = self.window
        window_events = [e for e in events if w.start <= e.at < w.end]
        room = max(self.cap - self.scanned, 0)
        if len(window_events) > room:
            self.partial = True
        scanned = window_events[:room]
        self.scanned += len(scanned)
        if self.route == "verification":
            self._verification(scanned)
        elif self.route == "verifiers":
            self._verifiers(events, scanned)
        elif self.route == "cost-latency":
            self._cost_latency(scanned)
        else:
            self._approvals(events, scanned)

    def _safe(self, value: str | None) -> bool:
        """True for a missing value or one that passes the guard; a failure is counted."""
        if value is None or is_safe_string(value):
            return True
        self.dropped["value_shape"] += 1
        return False

    def _verification(self, scanned: Sequence[LedgerEvent]) -> None:
        for e in scanned:
            if e.type != EventType.GATE_COMPLETED.value:
                continue
            outcome = _text(e.payload.get("outcome"))
            if outcome not in _OUTCOMES:
                outcome = "error"
            assert outcome is not None
            self._overall.add(outcome)
            gate, role = _text(e.payload.get("gate")), _text(e.payload.get("role"))
            if gate is not None and self._safe(gate):
                self._gates.setdefault(gate, _Tally()).add(outcome)
            if self._safe(role):
                self._roles.setdefault(role, _Tally()).add(outcome)

    def _verifiers(self, events: Sequence[LedgerEvent], scanned: Sequence[LedgerEvent]) -> None:
        kinds = _match_kinds(events)
        in_scan = {e.seq for e in scanned}
        for e in scanned:
            if e.type != EventType.GATE_COMPLETED.value:
                continue
            kind = kinds.get(e.seq, UNKNOWN_KIND)
            if kind == UNKNOWN_KIND:
                self._unknown_kind += 1
            elif kind == GateKind.VERIFIER.value:
                outcome = e.payload.get("outcome")
                if outcome == "passed":
                    self._v_passed += 1
                elif outcome == "failed":
                    self._v_failed += 1
        # rounds to pass: a task whose closing transition is `done` in the scanned window; the
        # position counts the task's verifier-kind results over the whole stream
        closing: dict[str, LedgerEvent] = {}
        for e in events:
            if e.type == EventType.TASK_TRANSITIONED.value and e.task_id is not None:
                closing[e.task_id] = e
        results: dict[str, list[str]] = {}
        for e in events:
            if (
                e.type == EventType.GATE_COMPLETED.value
                and e.task_id is not None
                and kinds.get(e.seq) == GateKind.VERIFIER.value
            ):
                results.setdefault(e.task_id, []).append(str(e.payload.get("outcome")))
        for task, move in closing.items():
            if move.payload.get("to") != "done" or move.seq not in in_scan:
                continue
            position = next((i for i, o in enumerate(results.get(task, []), 1) if o == "passed"), 0)
            if position == 0:
                self._done_without += 1
            else:
                self._rounds["4+" if position >= 4 else str(position)] += 1

    def _cost_latency(self, scanned: Sequence[LedgerEvent]) -> None:
        for e in scanned:
            if e.type != EventType.MODEL_CALLED.value:
                continue
            basis = str(e.cost_basis) if e.cost_basis is not None else None
            amount = e.cost.amount if e.cost is not None and basis in _COST_BASES else None
            currency = str(e.cost.currency) if e.cost is not None else "USD"
            self._money.setdefault(currency, _Money()).add(amount)
            model, role = _text(e.payload.get("model")), _text(e.payload.get("role"))
            if model is not None and self._safe(model):
                self._model_money.setdefault((model, currency), _Money()).add(amount)
            if self._safe(role):
                self._role_money.setdefault((role, currency), _Money()).add(amount)
            latency = _int(e.payload.get("latency_ms"))
            if latency is None or latency < 0:
                self._latency_skipped += 1
            else:
                self._latency.append(latency)

    def _approvals(self, events: Sequence[LedgerEvent], scanned: Sequence[LedgerEvent]) -> None:
        w = self.window
        ended = any(e.type == EventType.RUN_ENDED.value and e.at <= w.as_of for e in events)
        decided = {
            _text(e.payload.get("approval"))
            for e in events
            if e.type == EventType.APPROVAL_DECIDED.value and e.at <= w.as_of
        }
        if not ended:
            for e in events:
                if e.type != EventType.APPROVAL_REQUESTED.value or e.at > w.as_of:
                    continue
                ident = _text(e.payload.get("approval"))
                if ident is None or ident in decided:
                    continue
                action = _text(e.payload.get("action"))
                self._waiting.append((e.at, action if action in _ACTIONS else "other"))
        for e in scanned:
            if e.type == EventType.APPROVAL_DECIDED.value:
                if e.payload.get("approved") is True:
                    self._approved += 1
                elif e.payload.get("approved") is False:
                    self._denied += 1

    def result(self, organization: str) -> dict[str, Any]:
        w = self.window
        head: dict[str, Any] = {
            "as_of": iso(w.as_of),
            "from": iso(w.start),
            "to": iso(w.end),
            "organization": organization,
            "partial": self.partial,
            "scanned_events": self.scanned,
            "dropped_values": dict(self.dropped),
        }
        return {**head, **getattr(self, "_" + self.route.replace("-", "_") + "_figures")()}

    def _verification_figures(self) -> dict[str, Any]:
        o = self._overall.row()
        return {
            "passed": o["passed"],
            "failed": o["failed"],
            "error": o["error"],
            "pass_rate": o["pass_rate"],
            "gates": [{"gate": g, **t.row()} for g, t in sorted(self._gates.items())],
            "roles": [
                {"role": r, **t.row()}
                for r, t in sorted(
                    self._roles.items(), key=lambda kv: (kv[0] is not None, kv[0] or "")
                )
            ],
        }

    def _verifiers_figures(self) -> dict[str, Any]:
        rounds = {k: self._rounds.get(k, 0) for k in ("1", "2", "3", "4+")}
        return {
            "verifier_passed": self._v_passed,
            "verifier_failed": self._v_failed,
            "catch_rate": ratio(self._v_failed, self._v_passed + self._v_failed),
            "unknown_kind_results": self._unknown_kind,
            "rounds_to_pass": rounds,
            "done_without_verifier_pass": self._done_without,
        }

    def _cost_latency_figures(self) -> dict[str, Any]:
        return {
            "currencies": {c: m.row() for c, m in sorted(self._money.items())},
            "models": [
                {"model": m, "currency": c, **v.row()}
                for (m, c), v in sorted(self._model_money.items())
            ],
            "roles": [
                {"role": r, "currency": c, **v.row()}
                for (r, c), v in sorted(
                    self._role_money.items(),
                    key=lambda kv: (kv[0][0] is not None, kv[0][0] or "", kv[0][1]),
                )
            ],
            "p50": nearest_rank(self._latency, 50),
            "p95": nearest_rank(self._latency, 95),
            "max": max(self._latency) if self._latency else None,
            "latency_skipped": self._latency_skipped,
        }

    def _approvals_figures(self) -> dict[str, Any]:
        by_action = Counter(a for _, a in self._waiting)
        oldest = min((t for t, _ in self._waiting), default=None)
        return {
            "waiting": len(self._waiting),
            "oldest_waiting_seconds": None
            if oldest is None
            else int((self.window.as_of - oldest).total_seconds()),
            "waiting_by_action": dict(sorted(by_action.items())),
            "decided_approved": self._approved,
            "decided_denied": self._denied,
        }


def compute(
    route: str,
    streams: Iterable[tuple[str, Sequence[LedgerEvent]]],
    window: Window,
    cap: int,
    organization: str,
) -> dict[str, Any]:
    """The figures of one dashboard over `(stream id, entries)` pairs in ascending id order."""
    scan = Scan(route, window, cap)
    for _, events in streams:
        scan.add_stream(events)
    return scan.result(organization)


__all__ = [
    "MAX_WINDOW_DAYS",
    "ROUTES",
    "Scan",
    "Window",
    "compute",
    "is_safe_string",
    "iso",
    "nearest_rank",
    "ratio",
]
