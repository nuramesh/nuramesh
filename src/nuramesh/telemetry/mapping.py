"""Ledger events to spans, span events and metric points (E3 spec sections 4.3 and 5 to 8).

Pure and deterministic: the output depends only on ledger entries (the cursor decides which are
new), never on the clock or on memory of earlier batches. A span is built when its closing event
is mapped, by re-reading the ledger up to that event. Every attribute comes from a named payload
key through the value guard; no payload is ever iterated, and nothing here writes anywhere.
Money is plain decimal text; no float is produced.
"""

import hashlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any, Protocol

from nuramesh.core.gates import GateKind, Verdict
from nuramesh.core.ledger import CostBasis, EventType, LedgerEvent
from nuramesh.core.policy import ActionKind
from nuramesh.core.tasks import TaskStatus
from nuramesh.telemetry.guard import closed, is_safe_string, safe_decimal, whole_number
from nuramesh.telemetry.people import describe_actor

AttrValue = str | int | bool
MAX_SPAN_EVENTS = 128
LATENCY_BOUNDS = (50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)
_INT64_MAX = 2**63 - 1
_ZERO_SPAN = "0000000000000001"
_ZERO_TRACE = "00000000000000000000000000000001"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

TASK_STATUSES = tuple(s.value for s in TaskStatus)
GATE_KINDS = tuple(k.value for k in GateKind)
ACTION_KINDS = tuple(k.value for k in ActionKind)
_VERDICTS = (Verdict.MET.value, Verdict.NOT_MET.value)
_COST_BASES = tuple(b.value for b in CostBasis)
_MODEL_OUTCOMES = ("ok", "error", "settled_late", "settle_failed")
_FINISH = ("stop", "length", "filtered")
_CAP_STAGES = ("applied", "hit", "not_applied")
_BLOCK_REASONS = ("unknown_price", "org_halted", "role_paused", "breaker_evaluation_failed")
_GATE_OUTCOMES = ("passed", "failed", "error")
_DECISIONS = ("allow", "ask", "deny")
_STOP_VIA = ("api", "cli", "org_halt")
_VIA = ("api", "cli", "breaker")
_CAUSE = ("human", "breaker", "evaluation_unavailable")
_SIGNALS = ("policy_denied", "gate_failed", "model_blocked", "model_called", "spend")
_BREAKER_ACTIONS = ("halt_org", "pause_role")
_DISABLE_REASONS = ("false_positive", "planned_work", "rule_being_replaced", "other")
_SCOPES = ("run", "task", "agent", "team", "org")
_SUBJECTS = ("agent", "role")
_BUDGET_REASONS = ("hard_stop", "no_budget", "overrun")
_BREACH_REASONS = ("settlement_overrun", "overrun_after_reconciliation")
_CLOSING = (TaskStatus.DONE.value, TaskStatus.CANCELLED.value)
_ORG_ERROR_TYPES = ("org.halted", "breaker.tripped")


class LedgerReader(Protocol):
    """The Store read methods the exporter uses (and nothing else)."""

    def read_stream(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterable[LedgerEvent]: ...

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]: ...


class StoreReadError(Exception):
    """A read from the store failed: the cycle fails and the cursor stays where it was."""


@dataclass(frozen=True)
class SpanEvent:
    """A timed note on a span."""

    name: str
    at_ns: int
    attributes: Mapping[str, AttrValue]


@dataclass(frozen=True)
class Span:
    """A finished span in neutral form."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_ns: int
    end_ns: int
    status: str
    attributes: Mapping[str, AttrValue]
    events: tuple[SpanEvent, ...] = ()


@dataclass(frozen=True)
class MetricPoint:
    """One observation of an instrument: a counter increment or a histogram sample."""

    name: str
    kind: str  # counter | histogram
    unit: str
    value: int
    attributes: Mapping[str, str]


@dataclass(frozen=True)
class MappingConfig:
    """What the mapper needs from the settings."""

    event_hash: bool = True
    cost_metric: bool = False
    genai_attrs: bool = False
    sample_ratio: Decimal = Decimal(1)
    batch_size: int = 256
    person_key: bytes | None = None


@dataclass
class MappedBatch:
    """The output of mapping one stream's events."""

    spans: list[Span] = field(default_factory=list)
    metrics: list[MetricPoint] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)
    events_read: int = 0
    skipped: int = 0


def trace_id(stream: str) -> str:
    """First 32 hex characters of sha256("nuramesh/trace/" + stream)."""
    value = hashlib.sha256(("nuramesh/trace/" + stream).encode()).hexdigest()[:32]
    return _ZERO_TRACE if int(value, 16) == 0 else value


def span_id(stream: str, parts: Sequence[str | int]) -> str:
    """First 16 hex characters of sha256("nuramesh/span/" + stream + "/" + parts joined by /)."""
    text = "nuramesh/span/" + stream + "/" + "/".join(str(p) for p in parts)
    value = hashlib.sha256(text.encode()).hexdigest()[:16]
    return _ZERO_SPAN if int(value, 16) == 0 else value


def is_sampled(trace: str, ratio: Decimal) -> bool:
    """Head sampling by trace id: kept when `int(trace[-16:], 16) < floor(ratio * 2**64)`."""
    with localcontext() as context:
        context.prec = 60
        threshold = int((ratio * (1 << 64)).to_integral_value(rounding=ROUND_FLOOR))
    return int(trace[-16:], 16) < threshold


def to_ns(moment: datetime) -> int:
    """Unix nanoseconds, exact."""
    delta = moment - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def _items(value: object) -> tuple[Any, ...] | None:
    return tuple(value) if isinstance(value, list | tuple) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class Mapper:
    """Maps batches of one stream. Keeps only a cache of each stream's first event, which never
    changes (losing it changes nothing)."""

    def __init__(self, reader: LedgerReader, config: MappingConfig) -> None:
        self._reader = reader
        self._config = config
        self._first: dict[str, LedgerEvent | None] = {}

    def map_batch(self, stream: str, events: Sequence[LedgerEvent]) -> MappedBatch:
        """Map `events` (new entries of `stream`, in `seq` order). A store read error propagates;
        any other error on one event skips that event and counts a `type` drop."""
        work = _Work(self._reader, self._config, self._first, stream, events)
        return work.run()


class _Work:
    def __init__(
        self,
        reader: LedgerReader,
        config: MappingConfig,
        first_cache: dict[str, LedgerEvent | None],
        stream: str,
        events: Sequence[LedgerEvent],
    ) -> None:
        self.reader, self.config, self.first_cache = reader, config, first_cache
        self.stream, self.events = stream, events
        self.fresh = {e.seq for e in events}
        self.counted: set[tuple[int, str]] = set()
        self.out = MappedBatch(events_read=len(events))
        self.trace = trace_id(stream)

    # -- reads (a failure here is a failed cycle, not a skipped event) -------------------------

    def _read(self, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Exception as exc:
            raise StoreReadError(type(exc).__name__) from None

    def first_event(self) -> LedgerEvent | None:
        if self.stream not in self.first_cache:
            page = self._read(
                lambda: list(self.reader.read_stream(self.stream, after_seq=0, limit=1))
            )
            self.first_cache[self.stream] = page[0] if page else None
        return self.first_cache[self.stream]

    def task_events(self, ev: LedgerEvent) -> list[LedgerEvent]:
        """The task's events up to and including `ev`, in order."""
        if ev.run_id is None or ev.task_id is None:
            return [ev]
        found = self._read(lambda: self.reader.task_events(ev.run_id or "", ev.task_id or ""))
        return [e for e in found if e.seq <= ev.seq]

    def _page_reader(self, after: int) -> Callable[[], list[LedgerEvent]]:
        def read() -> list[LedgerEvent]:
            return list(
                self.reader.read_stream(self.stream, after_seq=after, limit=self.config.batch_size)
            )

        return read

    def scan_stream(self, upto: int) -> Iterable[LedgerEvent]:
        """Every event of the stream up to `upto`, read in pages of `batch_size`."""
        after = 0
        while True:
            page = self._read(self._page_reader(after))
            if not page:
                return
            for e in page:
                if e.seq > upto:
                    return
                yield e
            after = page[-1].seq

    # -- value guard with counting -------------------------------------------------------------

    def drop(self, ev: LedgerEvent, label: str, reason: str) -> None:
        if (
            ev.seq in self.fresh
            and ev.stream == self.stream
            and (ev.seq, label) not in self.counted
        ):
            self.counted.add((ev.seq, label))
            self.out.dropped[reason] += 1

    def text(self, ev: LedgerEvent, label: str, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            self.drop(ev, label, "type")
            return None
        if not is_safe_string(value):
            self.drop(ev, label, "value_shape")
            return None
        return value

    def whole(self, ev: LedgerEvent, label: str, value: object) -> int | None:
        if value is None:
            return None
        number = whole_number(value)
        if number is None:
            self.drop(ev, label, "type")
        return number

    def flag(self, ev: LedgerEvent, label: str, value: object) -> bool | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            self.drop(ev, label, "type")
            return None
        return value

    def decimal(self, ev: LedgerEvent, label: str, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
            self.drop(ev, label, "type")
            return None
        text = safe_decimal(value)
        if text is None:
            self.drop(ev, label, "value_shape")
        return text

    def pick(
        self, ev: LedgerEvent, label: str, value: object, allowed: Sequence[str]
    ) -> str | None:
        if value is None:
            return None
        picked = closed(value, allowed)
        if picked is None:
            self.drop(ev, label, "type")
        return picked

    # -- shared attribute groups ---------------------------------------------------------------

    def actor_group(self, ev: LedgerEvent) -> dict[str, AttrValue]:
        view = describe_actor(ev.actor, self.config.person_key)
        attrs: dict[str, AttrValue] = {"nuramesh.actor.kind": view.kind}
        ident = self.text(ev, "actor.id", view.id)
        if ident is not None:
            attrs["nuramesh.actor.id"] = ident
        if view.pseudonym is not None and view.pseudonym_key_id is not None:
            attrs["nuramesh.actor.pseudonym"] = view.pseudonym
            attrs["nuramesh.actor.pseudonym_key_id"] = view.pseudonym_key_id
        return attrs

    def org_name(self) -> str | None:
        first = self.first_event()
        if first is None or first.type != EventType.RUN_STARTED.value:
            return None
        return self.text(first, "org", first.payload.get("org"))

    def base(
        self, ev: LedgerEvent, *, end_seq: int | None = None, run_scope: bool = True
    ) -> dict[str, AttrValue]:
        stream = self.text(ev, "stream", ev.stream)
        if stream is None:  # key-shaped slug: export the kind only
            kind = ev.stream.partition(":")[0] if ":" in ev.stream else ""
            stream = kind if kind and is_safe_string(kind) else "unknown"
        attrs: dict[str, AttrValue] = {"nuramesh.stream": stream, "nuramesh.seq": ev.seq}
        if end_seq is not None:
            attrs["nuramesh.end_seq"] = end_seq
        if self.config.event_hash:
            attrs["nuramesh.event_hash"] = ev.hash
        if run_scope:
            org = self.org_name()
            if org is not None:
                attrs["nuramesh.org"] = org
            if ev.run_id is not None:
                run = self.text(ev, "run_id", ev.run_id)
                if run is not None:
                    attrs["nuramesh.run.id"] = run
            if ev.task_id is not None:
                task = self.text(ev, "task_id", ev.task_id)
                if task is not None:
                    attrs["nuramesh.task.id"] = task
        return attrs

    @staticmethod
    def put(attrs: dict[str, AttrValue], key: str, value: AttrValue | None) -> None:
        if value is not None:
            attrs[key] = value

    def span_ids(self, ev: LedgerEvent) -> tuple[str, str]:
        """(run span id, parent id of a span that belongs to a task or else the run)."""
        run = span_id(self.stream, ["run"])
        if ev.task_id is not None:
            return run, span_id(self.stream, ["task", ev.task_id])
        return run, run

    # -- the loop ------------------------------------------------------------------------------

    def run(self) -> MappedBatch:
        for ev in self.events:
            marks = (len(self.out.spans), len(self.out.metrics), self.out.dropped.copy())
            try:
                self.map_event(ev)
            except StoreReadError:
                raise
            except Exception:
                del self.out.spans[marks[0] :]
                del self.out.metrics[marks[1] :]
                self.out.dropped = marks[2]
                self.out.dropped["type"] += 1
                self.out.skipped += 1
        if not self.stream.startswith("org:"):
            self.out.spans = [
                s for s in self.out.spans if is_sampled(s.trace_id, self.config.sample_ratio)
            ]
        return self.out

    def map_event(self, ev: LedgerEvent) -> None:
        if self.stream.startswith("org:"):
            self.org_event(ev)
            return
        handler = {
            EventType.RUN_STARTED.value: self.run_started,
            EventType.RUN_ENDED.value: self.run_ended,
            EventType.TASK_TRANSITIONED.value: self.task_transitioned,
            EventType.MODEL_CALLED.value: self.model_called,
            EventType.MODEL_BLOCKED.value: self.model_blocked,
            EventType.MODEL_OUTPUT_CAPPED.value: self.model_output_capped,
            EventType.GATE_COMPLETED.value: self.gate_completed,
            EventType.APPROVAL_DECIDED.value: self.approval_decided,
            EventType.POLICY_DECIDED.value: self.policy_decided,
            EventType.BUDGET_BREACHED.value: self.budget_breached_metric,
        }.get(ev.type)
        if handler is not None:
            handler(ev)

    def metric(
        self,
        name: str,
        unit: str,
        value: int,
        attrs: Mapping[str, str | None],
        kind: str = "counter",
    ) -> None:
        kept = {k: v for k, v in attrs.items() if v is not None}
        self.out.metrics.append(MetricPoint(name, kind, unit, value, kept))

    def add_span(
        self,
        name: str,
        ev: LedgerEvent,
        *,
        sid: str,
        parent: str | None,
        start_ns: int,
        end_ns: int,
        status: str,
        attrs: dict[str, AttrValue],
        events: tuple[SpanEvent, ...] = (),
    ) -> None:
        self.out.spans.append(
            Span(name, self.trace, sid, parent, start_ns, end_ns, status, attrs, events)
        )

    # -- run -----------------------------------------------------------------------------------

    def run_started(self, ev: LedgerEvent) -> None:
        self.metric(
            "nuramesh.runs.started",
            "{run}",
            1,
            {"org": self.text(ev, "org", ev.payload.get("org"))},
        )

    @staticmethod
    def run_status(ev: LedgerEvent) -> str:
        if ev.payload.get("aborted") is True:
            return "aborted"
        return "stopped" if ev.payload.get("status") == "stopped" else "finished"

    def run_ended(self, ev: LedgerEvent) -> None:
        status = self.run_status(ev)
        self.metric("nuramesh.runs.ended", "{run}", 1, {"status": status})
        first = self.first_event()
        if first is None or first.type != EventType.RUN_STARTED.value:
            return
        events: list[SpanEvent] = []
        tasks: dict[str, list[Any]] = {}  # id -> [created seq, status, closed]
        approvals: dict[str, list[Any]] = {}  # id -> [request seq, action, decided]
        for e in self.scan_stream(ev.seq - 1):
            if e.task_id is None:
                events.extend(self.span_events(e, {}))
            if e.type == EventType.TASK_CREATED.value and e.task_id is not None:
                tasks.setdefault(e.task_id, [e.seq, e.payload.get("status"), False, e])
            elif e.type == EventType.TASK_TRANSITIONED.value and e.task_id in tasks:
                record = tasks[e.task_id]
                if not record[2]:
                    record[1] = e.payload.get("to")
                    record[2] = e.payload.get("to") in _CLOSING
            elif e.type == EventType.APPROVAL_REQUESTED.value:
                key = str(e.payload.get("approval"))
                approvals.setdefault(key, [e.seq, e.payload.get("action"), False, e])
            elif e.type == EventType.APPROVAL_DECIDED.value:
                key = str(e.payload.get("approval"))
                if key in approvals:
                    approvals[key][2] = True
        at = to_ns(ev.at)
        for task_id, record in sorted(tasks.items(), key=lambda item: item[1][0]):
            if record[2]:
                continue
            attrs: dict[str, AttrValue] = {"nuramesh.seq": ev.seq}
            self.put(attrs, "nuramesh.task.id", self.text(record[3], "task_id", task_id))
            self.put(
                attrs,
                "nuramesh.task.status",
                self.pick(ev, "task_status", record[1], TASK_STATUSES),
            )
            events.append(SpanEvent("nuramesh.task.open_at_end", at, attrs))
        for key, record in sorted(approvals.items(), key=lambda item: item[1][0]):
            if record[2]:
                continue
            attrs = {"nuramesh.seq": ev.seq}
            self.put(attrs, "nuramesh.approval.id", self.text(record[3], "approval", key))
            self.put(
                attrs,
                "nuramesh.approval.action",
                self.pick(record[3], "action", record[1], ACTION_KINDS),
            )
            events.append(SpanEvent("nuramesh.approval.pending_at_end", at, attrs))
        attrs = self.base(first, end_seq=ev.seq)
        attrs["nuramesh.run.status"] = status
        listed = _items(first.payload.get("tasks"))
        if listed is not None:
            attrs["nuramesh.run.task_count"] = len(listed)
        elif first.payload.get("tasks") is not None:
            self.drop(first, "tasks", "type")
        attrs.update(self.actor_group(first))
        self.add_span(
            "nuramesh.run",
            first,
            sid=span_id(self.stream, ["run"]),
            parent=None,
            start_ns=to_ns(first.at),
            end_ns=at,
            status="ERROR" if status == "aborted" else "UNSET",
            attrs=attrs,
            events=self.capped(events, attrs),
        )

    @staticmethod
    def capped(events: list[SpanEvent], attrs: dict[str, AttrValue]) -> tuple[SpanEvent, ...]:
        if len(events) > MAX_SPAN_EVENTS:
            attrs["nuramesh.span_events_dropped"] = len(events) - MAX_SPAN_EVENTS
        return tuple(events[:MAX_SPAN_EVENTS])

    # -- tasks ---------------------------------------------------------------------------------

    def task_transitioned(self, ev: LedgerEvent) -> None:
        if ev.payload.get("to") not in _CLOSING or ev.task_id is None:
            return
        history = self.task_events(ev)
        if any(
            e.type == EventType.TASK_TRANSITIONED.value and e.payload.get("to") in _CLOSING
            for e in history
            if e.seq < ev.seq
        ):
            return  # only the first closing transition closes a task
        final = self.pick(ev, "to", ev.payload.get("to"), _CLOSING)
        self.metric("nuramesh.tasks.finished", "{task}", 1, {"status": final})
        created = next((e for e in history if e.type == EventType.TASK_CREATED.value), None)
        if created is None:
            return
        attrs = self.base(created, end_seq=ev.seq)
        self.put(attrs, "nuramesh.task.final_status", final)
        self.put(
            attrs,
            "nuramesh.task.attempts",
            self.whole(ev, "attempt_after", ev.payload.get("attempt_after")),
        )
        attrs.update(self.actor_group(created))
        starts = self.match_gates(history)
        events: list[SpanEvent] = []
        for e in history:
            events.extend(self.span_events(e, starts))
        self.add_span(
            "nuramesh.task",
            created,
            sid=span_id(self.stream, ["task", ev.task_id]),
            parent=span_id(self.stream, ["run"]),
            start_ns=to_ns(created.at),
            end_ns=to_ns(ev.at),
            status="UNSET",
            attrs=attrs,
            events=self.capped(events, attrs),
        )

    # -- model calls ---------------------------------------------------------------------------

    @staticmethod
    def valid_latency(value: object) -> int | None:
        number = whole_number(value)
        return number if number is not None and number >= 0 else None

    def model_called(self, ev: LedgerEvent) -> None:
        p = ev.payload
        agent = self.text(ev, "agent", p.get("agent"))
        role = self.text(ev, "role", p.get("role"))
        provider = self.text(ev, "provider", p.get("provider"))
        model = self.text(ev, "model", p.get("model"))
        outcome = self.pick(ev, "outcome", p.get("outcome"), _MODEL_OUTCOMES)
        usage = _mapping(p.get("usage"))
        if p.get("usage") is not None and not isinstance(p.get("usage"), Mapping):
            self.drop(ev, "usage", "type")
        tokens = {
            "input": self.whole(ev, "usage.input_tokens", usage.get("input_tokens")),
            "output": self.whole(ev, "usage.output_tokens", usage.get("output_tokens")),
            "cached_input": self.whole(
                ev, "usage.cached_input_tokens", usage.get("cached_input_tokens")
            ),
            "cache_write": self.whole(
                ev, "usage.cache_write_tokens", usage.get("cache_write_tokens")
            ),
        }
        latency = self.valid_latency(p.get("latency_ms"))
        if p.get("latency_ms") is not None and latency is None:
            self.drop(ev, "latency_ms", "type")
        basis = self.cost_basis(ev)
        amount_attrs = self.cost_attrs(ev, basis)
        # metrics
        self.metric(
            "nuramesh.model.calls",
            "{call}",
            1,
            {"provider": provider, "model": model, "outcome": outcome, "role": role},
        )
        for kind, count in tokens.items():
            if count:
                self.metric(
                    "nuramesh.model.tokens",
                    "{token}",
                    count,
                    {"token_type": kind, "provider": provider, "model": model},
                )
        if latency is not None:
            self.metric(
                "nuramesh.model.latency",
                "ms",
                latency,
                {"provider": provider, "model": model},
                kind="histogram",
            )
        if self.config.cost_metric:
            nano = self.nano_usd(ev, basis)
            if nano > _INT64_MAX:  # the OTLP integer is signed 64-bit
                self.drop(ev, "cost", "value_shape")
            elif nano:
                self.metric(
                    "nuramesh.cost.nano_usd",
                    "nUSD",
                    nano,
                    {"provider": provider, "model": model, "basis": basis},
                )
        # span
        attrs = self.base(ev)
        self.put(attrs, "nuramesh.agent.id", agent)
        self.put(attrs, "nuramesh.role", role)
        self.put(attrs, "nuramesh.model.provider", provider)
        self.put(attrs, "nuramesh.model.name", model)
        self.put(attrs, "nuramesh.model.outcome", outcome)
        names = {
            "input": "input_tokens",
            "output": "output_tokens",
            "cached_input": "cached_input_tokens",
            "cache_write": "cache_write_tokens",
        }
        for kind, count in tokens.items():
            self.put(attrs, f"nuramesh.model.{names[kind]}", count)
        if outcome == "ok":
            self.put(
                attrs,
                "nuramesh.model.finish_reason",
                self.pick(ev, "finish_reason", p.get("finish_reason"), _FINISH),
            )
        attrs.update(amount_attrs)
        attrs.update(self.actor_group(ev))
        if self.config.genai_attrs:
            self.put(attrs, "gen_ai.system", provider)
            self.put(attrs, "gen_ai.request.model", model)
            self.put(attrs, "gen_ai.usage.input_tokens", tokens["input"])
            self.put(attrs, "gen_ai.usage.output_tokens", tokens["output"])
        end = to_ns(ev.at)
        _, parent = self.span_ids(ev)
        self.add_span(
            "nuramesh.model_call",
            ev,
            sid=span_id(self.stream, ["call", ev.seq]),
            parent=parent,
            start_ns=end - (latency or 0) * 1_000_000,
            end_ns=end,
            status="UNSET" if outcome in ("ok", "settled_late") else "ERROR",
            attrs=attrs,
        )

    @staticmethod
    def cost_basis(ev: LedgerEvent) -> str:
        if ev.cost_basis is None:
            return "unknown"
        return ev.cost_basis if ev.cost_basis in _COST_BASES else "other"

    def cost_attrs(self, ev: LedgerEvent, basis: str) -> dict[str, AttrValue]:
        attrs: dict[str, AttrValue] = {"nuramesh.cost.basis": basis}
        if ev.cost is not None and basis != "unknown":
            self.put(attrs, "nuramesh.cost.amount", self.decimal(ev, "cost.amount", ev.cost.amount))
            self.put(
                attrs,
                "nuramesh.cost.currency",
                self.text(ev, "cost.currency", str(ev.cost.currency)),
            )
        return attrs

    @staticmethod
    def nano_usd(ev: LedgerEvent, basis: str) -> int:
        if ev.cost is None or basis not in ("actual", "provisional"):
            return 0
        if str(ev.cost.currency) != "USD" or ev.cost.amount <= 0:
            return 0
        with localcontext() as context:
            context.prec = 60
            return int((ev.cost.amount * 10**9).to_integral_value(rounding=ROUND_FLOOR))

    def model_blocked(self, ev: LedgerEvent) -> None:
        reason = self.pick(ev, "reason", ev.payload.get("reason"), _BLOCK_REASONS)
        self.metric("nuramesh.model.blocked", "{call}", 1, {"reason": reason})

    def model_output_capped(self, ev: LedgerEvent) -> None:
        stage = self.pick(ev, "stage", ev.payload.get("stage"), _CAP_STAGES)
        self.metric("nuramesh.model.output_capped", "{call}", 1, {"stage": stage})

    # -- gates ---------------------------------------------------------------------------------

    @staticmethod
    def gate_key(ev: LedgerEvent) -> tuple[str, ...]:
        p = ev.payload
        return (
            repr(ev.task_id),
            repr(p.get("gate")),
            repr(p.get("attempt")),
            repr(p.get("submission")),
        )

    def match_gates(self, history: Iterable[LedgerEvent]) -> dict[int, LedgerEvent | None]:
        """For each `gate.completed` seq, the `gate.started` it closes (None when there is no
        match): the latest start of its key with no completion of that key in between."""
        open_starts: dict[tuple[str, ...], LedgerEvent] = {}
        matched: dict[int, LedgerEvent | None] = {}
        for e in history:
            if e.type == EventType.GATE_STARTED.value:
                open_starts[self.gate_key(e)] = e
            elif e.type == EventType.GATE_COMPLETED.value:
                matched[e.seq] = open_starts.pop(self.gate_key(e), None)
        return matched

    def gate_group(self, ev: LedgerEvent) -> dict[str, AttrValue]:
        p = ev.payload
        attrs: dict[str, AttrValue] = {}
        self.put(attrs, "nuramesh.gate.id", self.text(ev, "gate", p.get("gate")))
        self.put(
            attrs,
            "nuramesh.gate.outcome",
            self.pick(ev, "outcome", p.get("outcome"), _GATE_OUTCOMES),
        )
        self.put(attrs, "nuramesh.gate.attempt", self.whole(ev, "attempt", p.get("attempt")))
        self.put(
            attrs, "nuramesh.gate.submission", self.whole(ev, "submission", p.get("submission"))
        )
        met = not_met = 0
        verdicts = p.get("verdicts")
        listed = _items(verdicts)
        if listed is None:
            if verdicts is not None:
                self.drop(ev, "verdicts", "type")
        else:
            for item in listed:
                verdict = _mapping(item).get("verdict")
                met += verdict == Verdict.MET.value
                not_met += verdict == Verdict.NOT_MET.value
        attrs["nuramesh.gate.verdicts_met"] = met
        attrs["nuramesh.gate.verdicts_not_met"] = not_met
        self.put(attrs, "nuramesh.role", self.text(ev, "role", p.get("role")))
        return attrs

    def gate_completed(self, ev: LedgerEvent) -> None:
        p = ev.payload
        outcome = self.pick(ev, "outcome", p.get("outcome"), _GATE_OUTCOMES)
        start = (
            self.match_gates(self.task_events(ev)).get(ev.seq) if ev.task_id is not None else None
        )
        # R2.11 section 6: the kind of the matching start, from the same source as the span
        # attribute; closed set, so `unknown` covers no match or a value that is not a GateKind.
        raw_kind = start.payload.get("kind") if start is not None else None
        kind = raw_kind if isinstance(raw_kind, str) and raw_kind in GATE_KINDS else "unknown"
        self.metric(
            "nuramesh.gate.results",
            "{result}",
            1,
            {
                "gate": self.text(ev, "gate", p.get("gate")),
                "outcome": outcome,
                "kind": kind,
            },
        )
        if start is None or ev.task_id is None:
            return
        attrs = self.base(start, end_seq=ev.seq)
        attrs.update(self.gate_group(ev))
        self.put(
            attrs,
            "nuramesh.gate.kind",
            self.pick(start, "kind", start.payload.get("kind"), GATE_KINDS),
        )
        attrs.update(self.actor_group(start))
        self.add_span(
            "nuramesh.gate",
            start,
            sid=span_id(self.stream, ["gate", start.seq]),
            parent=span_id(self.stream, ["task", ev.task_id]),
            start_ns=to_ns(start.at),
            end_ns=to_ns(ev.at),
            status="UNSET" if outcome == "passed" else "ERROR",
            attrs=attrs,
        )

    # -- approvals and policy ------------------------------------------------------------------

    def approval_decided(self, ev: LedgerEvent) -> None:
        p = ev.payload
        approved = p.get("approved")
        status: str | None = None
        if isinstance(approved, bool):
            status = "approved" if approved else "denied"
        elif approved is not None:
            self.drop(ev, "approved", "type")
        else:
            self.drop(ev, "approved", "type")
        self.metric("nuramesh.approvals.decided", "{approval}", 1, {"status": status})
        approval = p.get("approval")
        if approval is None:
            return
        pool = self.task_events(ev) if ev.task_id is not None else self.scan_stream(ev.seq - 1)
        request = next(
            (
                e
                for e in pool
                if e.type == EventType.APPROVAL_REQUESTED.value
                and e.payload.get("approval") == approval
            ),
            None,
        )
        if request is None:
            return
        rp = request.payload
        attrs = self.base(request, end_seq=ev.seq)
        self.put(attrs, "nuramesh.approval.id", self.text(request, "approval", approval))
        self.put(
            attrs,
            "nuramesh.approval.action",
            self.pick(request, "action", rp.get("action"), ACTION_KINDS),
        )
        self.put(attrs, "nuramesh.approval.status", status)
        self.put(attrs, "nuramesh.agent.id", self.text(request, "agent", rp.get("agent")))
        self.put(attrs, "nuramesh.role", self.text(request, "role", rp.get("role")))
        attrs.update(self.actor_group(ev))
        _, parent = self.span_ids(request)
        self.add_span(
            "nuramesh.approval",
            request,
            sid=span_id(self.stream, ["approval", str(approval)]),
            parent=parent,
            start_ns=to_ns(request.at),
            end_ns=to_ns(ev.at),
            status="UNSET",
            attrs=attrs,
        )

    def policy_decided(self, ev: LedgerEvent) -> None:
        decision = self.pick(ev, "decision", ev.payload.get("decision"), _DECISIONS)
        self.metric("nuramesh.policy.decisions", "{decision}", 1, {"decision": decision})

    def budget_breached_metric(self, ev: LedgerEvent) -> None:
        items = _items(ev.payload.get("budgets"))
        if items is None or not all(isinstance(i, Mapping) for i in items):
            self.drop(ev, "budgets", "type")
            return
        for index, item in enumerate(items):
            self.metric(
                "nuramesh.budget.breaches",
                "{breach}",
                1,
                {"budget": self.text(ev, f"budgets.{index}.budget", item.get("budget"))},
            )

    # -- span events ---------------------------------------------------------------------------

    def span_events(
        self, ev: LedgerEvent, starts: Mapping[int, LedgerEvent | None]
    ) -> list[SpanEvent]:
        """The span events one ledger entry gives its owner span (section 5.3)."""
        t, at = ev.type, to_ns(ev.at)
        p = ev.payload

        def one(name: str, attrs: dict[str, AttrValue]) -> list[SpanEvent]:
            return [SpanEvent(name, at, {"nuramesh.seq": ev.seq, **attrs})]

        if t == EventType.TASK_TRANSITIONED.value:
            attrs: dict[str, AttrValue] = {}
            self.put(
                attrs, "nuramesh.task.from", self.pick(ev, "from", p.get("from"), TASK_STATUSES)
            )
            self.put(attrs, "nuramesh.task.to", self.pick(ev, "to", p.get("to"), TASK_STATUSES))
            self.put(
                attrs,
                "nuramesh.task.attempt",
                self.whole(ev, "attempt_after", p.get("attempt_after")),
            )
            return one("nuramesh.task.transition", attrs)
        if t == EventType.POLICY_DECIDED.value:
            attrs = {}
            self.put(
                attrs,
                "nuramesh.policy.decision",
                self.pick(ev, "decision", p.get("decision"), _DECISIONS),
            )
            self.put(attrs, "nuramesh.policy.rule", self.whole(ev, "rule", p.get("rule")))
            self.put(
                attrs,
                "nuramesh.policy.action",
                self.pick(ev, "action", p.get("action"), ACTION_KINDS),
            )
            self.put(attrs, "nuramesh.agent.id", self.text(ev, "agent", p.get("agent")))
            self.put(attrs, "nuramesh.approval.id", self.text(ev, "approval", p.get("approval")))
            return one("nuramesh.policy.decided", attrs)
        if t == EventType.MODEL_BLOCKED.value:
            attrs = {}
            self.put(
                attrs,
                "nuramesh.model.block_reason",
                self.pick(ev, "reason", p.get("reason"), _BLOCK_REASONS),
            )
            self.put(attrs, "nuramesh.agent.id", self.text(ev, "agent", p.get("agent")))
            self.put(attrs, "nuramesh.role", self.text(ev, "role", p.get("role")))
            self.put(attrs, "nuramesh.model.provider", self.text(ev, "provider", p.get("provider")))
            self.put(attrs, "nuramesh.model.name", self.text(ev, "model", p.get("model")))
            return one("nuramesh.model.blocked", attrs)
        if t == EventType.BUDGET_EXCEEDED.value:
            return self.budget_exceeded_events(ev, at)
        if t == EventType.BUDGET_BREACHED.value:
            return self.budget_breached_events(ev, at)
        if t == EventType.RUN_STOP_REQUESTED.value:
            attrs = {}
            self.put(attrs, "nuramesh.run.stop_via", self.pick(ev, "via", p.get("via"), _STOP_VIA))
            attrs.update(self.actor_group(ev))
            return one("nuramesh.run.stop_requested", attrs)
        if t == EventType.RUN_RESUMED.value:
            return one("nuramesh.run.resumed", {})
        if t == EventType.GATE_COMPLETED.value and ev.seq in starts and starts[ev.seq] is None:
            return one("nuramesh.gate.completed", self.gate_group(ev))
        return []

    def budget_exceeded_events(self, ev: LedgerEvent, at: int) -> list[SpanEvent]:
        p = ev.payload
        reason = self.pick(ev, "reason", p.get("reason"), _BUDGET_REASONS)
        common: dict[str, AttrValue] = {}
        self.put(common, "nuramesh.budget.reason", reason)
        self.put(common, "nuramesh.agent.id", self.text(ev, "agent", p.get("agent")))
        source = p.get("budgets") if reason == "overrun" else p.get("shortfalls")
        label = "budgets" if reason == "overrun" else "shortfalls"
        items = _items(source) if source is not None else ()
        if items is None or not all(isinstance(i, Mapping) for i in items):
            self.drop(ev, label, "type")
            return []
        events: list[SpanEvent] = []

        def emit(attrs: dict[str, AttrValue]) -> None:
            events.append(
                SpanEvent("nuramesh.budget.exceeded", at, {"nuramesh.seq": ev.seq, **attrs})
            )

        if not items:
            attrs = dict(common)
            if reason != "overrun":
                self.put(
                    attrs,
                    "nuramesh.budget.requested",
                    self.decimal(ev, "requested", p.get("requested")),
                )
            emit(attrs)
            return events
        for index, item in enumerate(items):
            prefix = f"{label}.{index}."
            attrs = {}
            self.put(
                attrs, "nuramesh.budget.id", self.text(ev, prefix + "budget", item.get("budget"))
            )
            self.put(
                attrs,
                "nuramesh.budget.limit",
                self.decimal(ev, prefix + "limit", item.get("limit")),
            )
            if reason == "overrun":
                self.put(
                    attrs,
                    "nuramesh.budget.spent",
                    self.decimal(ev, prefix + "spent", item.get("spent")),
                )
                self.put(
                    attrs,
                    "nuramesh.budget.overrun",
                    self.decimal(ev, prefix + "overrun", item.get("overrun")),
                )
            else:
                self.put(
                    attrs,
                    "nuramesh.budget.available",
                    self.decimal(ev, prefix + "available", item.get("available")),
                )
                self.put(
                    attrs,
                    "nuramesh.budget.spent",
                    self.decimal(ev, prefix + "spent", item.get("spent")),
                )
                self.put(
                    attrs,
                    "nuramesh.budget.requested",
                    self.decimal(ev, "requested", p.get("requested")),
                )
            attrs.update(common)
            emit(attrs)
        return events

    def budget_breached_events(self, ev: LedgerEvent, at: int) -> list[SpanEvent]:
        p = ev.payload
        items = _items(p.get("budgets"))
        if items is None or not all(isinstance(i, Mapping) for i in items):
            self.drop(ev, "budgets", "type")
            return []
        reason = self.pick(ev, "reason", p.get("reason"), _BREACH_REASONS)
        agent = self.text(ev, "agent", p.get("agent"))
        events: list[SpanEvent] = []
        for index, item in enumerate(items):
            prefix = f"budgets.{index}."
            attrs: dict[str, AttrValue] = {"nuramesh.seq": ev.seq}
            self.put(
                attrs, "nuramesh.budget.id", self.text(ev, prefix + "budget", item.get("budget"))
            )
            self.put(
                attrs,
                "nuramesh.budget.limit",
                self.decimal(ev, prefix + "limit", item.get("limit")),
            )
            self.put(
                attrs,
                "nuramesh.budget.spent",
                self.decimal(ev, prefix + "spent", item.get("spent")),
            )
            self.put(
                attrs,
                "nuramesh.budget.over_by",
                self.decimal(ev, prefix + "over_by", item.get("over_by")),
            )
            self.put(attrs, "nuramesh.budget.reason", reason)
            self.put(attrs, "nuramesh.agent.id", agent)
            events.append(SpanEvent("nuramesh.budget.breached", at, attrs))
        return events

    # -- organization stream -------------------------------------------------------------------

    def org_event(self, ev: LedgerEvent) -> None:
        p = ev.payload
        t = ev.type
        attrs = self.base(ev, run_scope=False)
        self.put(attrs, "nuramesh.org", self.text(ev, "org", self.stream[4:]))

        def via() -> str | None:
            return self.pick(ev, "via", p.get("via"), _VIA)

        def rule() -> str | None:
            return self.text(ev, "rule", p.get("rule"))

        def role() -> str | None:
            return self.text(ev, "role", p.get("role"))

        def cause() -> str | None:
            return self.pick(ev, "cause", p.get("cause"), _CAUSE)

        put = self.put
        if t == EventType.ORG_HALTED.value:
            halt_cause = cause()
            put(attrs, "nuramesh.org.cause", halt_cause)
            put(attrs, "nuramesh.org.via", via())
            put(attrs, "nuramesh.breaker.rule", rule())
            self.metric("nuramesh.org.halts", "{halt}", 1, {"cause": halt_cause})
        elif t == EventType.ORG_RESUMED.value:
            put(attrs, "nuramesh.org.via", via())
        elif t == EventType.ORG_ROLE_PAUSED.value:
            put(attrs, "nuramesh.org.cause", cause())
            put(attrs, "nuramesh.org.via", via())
            put(attrs, "nuramesh.role", role())
            put(attrs, "nuramesh.breaker.rule", rule())
        elif t == EventType.ORG_ROLE_RESUMED.value:
            put(attrs, "nuramesh.org.via", via())
            put(attrs, "nuramesh.role", role())
        elif t == EventType.BREAKER_TRIPPED.value:
            rule_id = rule()
            action = self.pick(ev, "action", p.get("action"), _BREAKER_ACTIONS)
            put(attrs, "nuramesh.breaker.rule", rule_id)
            put(
                attrs, "nuramesh.breaker.signal", self.pick(ev, "signal", p.get("signal"), _SIGNALS)
            )
            put(attrs, "nuramesh.breaker.action", action)
            put(attrs, "nuramesh.breaker.dry_run", self.flag(ev, "dry_run", p.get("dry_run")))
            put(attrs, "nuramesh.role", role())
            self.metric("nuramesh.breaker.trips", "{trip}", 1, {"rule": rule_id, "action": action})
        elif t in (EventType.BREAKER_RESET.value, EventType.BREAKER_ENABLED.value):
            put(attrs, "nuramesh.breaker.rule", rule())
            put(attrs, "nuramesh.org.via", via())
        elif t == EventType.BREAKER_DISABLED.value:
            put(attrs, "nuramesh.breaker.rule", rule())
            put(attrs, "nuramesh.org.via", via())
            put(
                attrs,
                "nuramesh.breaker.disable_reason",
                self.pick(ev, "reason", p.get("reason"), _DISABLE_REASONS),
            )
        elif t == EventType.BUDGET_DEFINED.value:
            put(attrs, "nuramesh.budget.id", self.text(ev, "budget", p.get("budget")))
            put(attrs, "nuramesh.budget.scope", self.pick(ev, "scope", p.get("scope"), _SCOPES))
            put(attrs, "nuramesh.budget.limit", self.decimal(ev, "limit", p.get("limit")))
            put(attrs, "nuramesh.cost.currency", self.text(ev, "currency", p.get("currency")))
        elif t == EventType.EVAL_SCORED.value:
            put(attrs, "nuramesh.eval.suite_id", self.text(ev, "suite_id", p.get("suite_id")))
            put(
                attrs,
                "nuramesh.eval.suite_version",
                self.whole(ev, "suite_version", p.get("suite_version")),
            )
            put(
                attrs,
                "nuramesh.eval.subject_kind",
                self.pick(ev, "subject_kind", p.get("subject_kind"), _SUBJECTS),
            )
            put(attrs, "nuramesh.eval.subject_id", self.text(ev, "subject_id", p.get("subject_id")))
            put(attrs, "nuramesh.model.name", self.text(ev, "model", p.get("model")))
            put(attrs, "nuramesh.eval.score", self.decimal(ev, "score", p.get("score")))
            put(attrs, "nuramesh.eval.passed", self.flag(ev, "passed", p.get("passed")))
        else:
            return
        attrs.update(self.actor_group(ev))
        moment = to_ns(ev.at)
        self.add_span(
            "nuramesh." + t,
            ev,
            sid=span_id(self.stream, ["event", ev.seq]),
            parent=None,
            start_ns=moment,
            end_ns=moment,
            status="ERROR" if t in _ORG_ERROR_TYPES else "UNSET",
            attrs=attrs,
        )
