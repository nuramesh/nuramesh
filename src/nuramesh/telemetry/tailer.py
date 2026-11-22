"""The export loop: read the ledger, map, hand to a sink, advance the cursor (E3 spec 4, 11).

`Tailer.run_cycle()` is one cycle and never sleeps (tests call it with fakes); `start()` runs it on
a thread named `nuramesh-otel`. The tailer only reads the store. Any exception ends the cycle, is
counted and logged as a class name plus a stream (never a payload, an SDK message or a URL), and
leaves the cursor before the unacknowledged events. The thread never dies of an exception.
"""

import logging
import random
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from nuramesh.core.ledger import LedgerEvent
from nuramesh.telemetry.cursor import CursorFile
from nuramesh.telemetry.mapping import (
    Mapper,
    MappingConfig,
    MetricPoint,
    Span,
    StoreReadError,
)

_LOG = logging.getLogger("nuramesh.telemetry")
MAX_STREAMS_PER_CYCLE = 16
RETRY_SECONDS = (1, 2, 4, 8, 16, 32)
RETRY_CAP_SECONDS = 60
SHUTDOWN_FLUSH_SECONDS = 5.0
THREAD_NAME = "nuramesh-otel"
_BAD_TIME = "BadTimeError"  # the store's error for a stored time that is not real (ND233)


class Head(Protocol):
    """The part of a stream head the tailer reads."""

    @property
    def seq(self) -> int: ...


class TailSource(Protocol):
    """The Store read methods the tailer uses (and nothing else)."""

    def heads(self, only: Collection[str] | None = None) -> Mapping[str, Head]: ...

    def read_stream(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterable[LedgerEvent]: ...

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]: ...


class Sink(Protocol):
    """Receives finished spans and metric points. `export` raises on failure."""

    def export(self, spans: Sequence[Span], metrics: Sequence[MetricPoint]) -> None: ...

    def flush(self, timeout_seconds: float) -> None: ...

    def close(self) -> None: ...


class SinkError(Exception):
    """A failed export. `signals` names the signals that failed (`traces`, `metrics`)."""

    def __init__(self, signals: Sequence[str] = ("traces",)) -> None:
        self.signals = tuple(signals)
        super().__init__("export failed: " + ",".join(self.signals))


@dataclass
class CycleResult:
    """What one cycle did."""

    ok: bool = True
    streams: int = 0
    events: int = 0
    spans: int = 0
    gap_events: int = 0
    lag: int = 0
    error: str | None = None
    served: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # damaged streams skipped (ND233)


def _default_wait(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout)


def _default_jitter(seconds: float) -> float:
    return seconds * (1 + random.uniform(0, 0.2))  # noqa: S311 (spreads retries, not security)


class Tailer:
    """Tails the ledger into a sink (module docstring)."""

    def __init__(
        self,
        source: TailSource,
        sink: Sink,
        cursor_file: CursorFile,
        config: MappingConfig,
        *,
        poll_seconds: float = 2.0,
        export_timeout_seconds: float = 10.0,
        max_lag_events: int = 100000,
        signals: Collection[str] = ("traces", "metrics"),
        only_streams: Collection[str] | None = None,
        wait: Callable[[threading.Event, float], bool] = _default_wait,
        jitter: Callable[[float], float] = _default_jitter,
    ) -> None:
        self.source, self.sink, self.cursor_file = source, sink, cursor_file
        self.config = config
        self.poll_seconds, self.export_timeout = poll_seconds, export_timeout_seconds
        self.max_lag, self.signals = max_lag_events, tuple(sorted(signals))
        self.only_streams = None if only_streams is None else frozenset(only_streams)
        self._wait, self._jitter = wait, jitter
        self.mapper = Mapper(source, config)
        self.cursors: dict[str, int] = cursor_file.load()
        self.consecutive_failures = 0
        self._pending: Counter[tuple[str, str]] = Counter()  # (name, signal or "") -> count
        self._after: str | None = None
        self._current = "-"
        self._damaged: set[str] = set()
        self._lag = 0
        self._lag_sent: int | None = None
        self._inflight: threading.Thread | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_lock = threading.Lock()

    # -- retry schedule ------------------------------------------------------------------------

    def retry_delay(self) -> float:
        """Seconds to wait after the current run of failures: 1, 2, 4, 8, 16, 32, then 60."""
        count = self.consecutive_failures
        if count <= 0:
            return float(self.poll_seconds)
        base = RETRY_SECONDS[count - 1] if count <= len(RETRY_SECONDS) else RETRY_CAP_SECONDS
        return self._jitter(float(base))

    # -- one cycle -----------------------------------------------------------------------------

    def replay(self, streams: Collection[str] | None = None) -> None:
        """Forget the cursors of `streams` (all when None): the next cycles read them from the
        start. A replayed stream is never counted as a gap."""
        for stream in list(self.cursors) if streams is None else list(streams):
            self.cursors.pop(stream, None)

    def run_cycle(self) -> CycleResult:
        """One pass over at most 16 streams with new events. Never raises for an `Exception`."""
        with self._cycle_lock:
            result = CycleResult()
            try:
                self._cycle(result)
            except Exception as exc:
                result.ok = False
                result.error = type(exc).__name__
                self.consecutive_failures += 1
                _LOG.warning("otel export cycle failed: %s %s", type(exc).__name__, self._current)
            else:
                self.consecutive_failures = 0
            return result

    def _heads(self) -> Mapping[str, Head]:
        try:
            heads = self.source.heads()
        except Exception as exc:
            raise StoreReadError(type(exc).__name__) from None
        if self.only_streams is None:
            return heads
        return {s: h for s, h in heads.items() if s in self.only_streams}

    def _cycle(self, result: CycleResult) -> None:
        heads = self._heads()
        order = sorted(s for s, h in heads.items() if h.seq > self.cursors.get(s, 0))
        if self._after is not None:
            order = [s for s in order if s > self._after] + [s for s in order if s <= self._after]
        try:
            for stream in order[:MAX_STREAMS_PER_CYCLE]:
                self._serve(stream, heads[stream].seq, result)
        finally:
            result.lag = sum(max(0, h.seq - self.cursors.get(s, 0)) for s, h in heads.items())
            self._lag = result.lag
        self._send_lag(result.lag)

    def _send_lag(self, lag: int) -> None:
        """The lag gauge at the end of a cycle, sent when it changed. A failure is counted by
        the hand-off and does not fail the cycle."""
        if "metrics" not in self.signals or lag == self._lag_sent:
            return
        point = MetricPoint("nuramesh.export.lag_events", "gauge", "{event}", lag, {})
        try:
            self._hand_off("-", [], [point, *self._pending_points()])
        except SinkError:
            return
        self._pending.clear()
        self._lag_sent = lag

    def _serve(self, stream: str, head_seq: int, result: CycleResult) -> None:
        self._current = stream
        known = stream in self.cursors
        cursor = self.cursors.get(stream, 0)
        if known and head_seq - cursor > self.max_lag:
            skipped_to = head_seq - self.max_lag
            gap = skipped_to - cursor
            _LOG.warning("otel export gap: %s %d %d", stream, cursor + 1, skipped_to)
            self._pending[("nuramesh.export.gap_events", "")] += gap
            result.gap_events += gap
            cursor = self.cursors[stream] = skipped_to
            self._save()
        try:
            events = list(
                self.source.read_stream(stream, after_seq=cursor, limit=self.config.batch_size)
            )
        except Exception as exc:
            if any(c.__name__ == _BAD_TIME for c in type(exc).__mro__):  # no stores import here
                self._skip_damaged(stream, result)
                return
            raise StoreReadError(type(exc).__name__) from None
        result.served.append(stream)
        if not events:
            self._after = stream
            self._damaged.discard(stream)
            return
        try:
            batch = self.mapper.map_batch(stream, events)
        except StoreReadError as exc:
            if str(exc) != _BAD_TIME:
                raise
            self._skip_damaged(stream, result)  # a damaged entry the mapping needed to read
            return
        self._damaged.discard(stream)
        metrics = list(batch.metrics)
        metrics.append(MetricPoint("nuramesh.export.events", "counter", "{event}", len(events), {}))
        for reason, count in sorted(batch.dropped.items()):
            if count:
                metrics.append(
                    MetricPoint(
                        "nuramesh.export.dropped_attributes",
                        "counter",
                        "{attribute}",
                        count,
                        {"reason": reason},
                    )
                )
        pending = self._pending_points()
        self._hand_off(stream, batch.spans, metrics + pending)
        self._pending.clear()
        self.cursors[stream] = events[-1].seq
        self._save()
        self._after = stream
        result.streams += 1
        result.events += len(events)
        result.spans += len(batch.spans)

    def _skip_damaged(self, stream: str, result: CycleResult) -> None:
        """A stream with an entry whose stored time is not real (ND233, ND276, T255): skip it, say
        so once per run of cycles (the stream name only, never a value), keep its cursor where it
        is, and go on to the next stream. The ledger is never touched."""
        if stream not in self._damaged:
            _LOG.warning("otel export skipped a damaged stream: %s", stream)
            self._damaged.add(stream)
        result.skipped.append(stream)
        self._after = stream

    def _pending_points(self) -> list[MetricPoint]:
        points: list[MetricPoint] = []
        for (name, signal), count in sorted(self._pending.items()):
            attrs = {"signal": signal} if signal else {}
            unit = "{attempt}" if name.endswith("failures") else "{event}"
            points.append(MetricPoint(name, "counter", unit, count, attrs))
        return points

    def _save(self) -> None:
        self.cursor_file.save(self.cursors)

    # -- the sink call, under a deadline -------------------------------------------------------

    def _hand_off(self, stream: str, spans: Sequence[Span], metrics: Sequence[MetricPoint]) -> None:
        if self._inflight is not None and self._inflight.is_alive():
            self._failed(self.signals, stream)
            raise SinkError(self.signals)
        outcome: dict[str, BaseException | None] = {}
        done = threading.Event()
        out_spans = list(spans) if "traces" in self.signals else []
        out_metrics = list(metrics) if "metrics" in self.signals else []

        def work() -> None:
            try:
                self.sink.export(out_spans, out_metrics)
                outcome["error"] = None
            except BaseException as exc:  # a worker never raises into the loop
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=work, name=THREAD_NAME + "-export", daemon=True)
        self._inflight = worker
        worker.start()
        if not done.wait(self.export_timeout):
            self._failed(self.signals, stream)
            raise SinkError(self.signals)
        error = outcome.get("error")
        if error is not None:
            signals = error.signals if isinstance(error, SinkError) else self.signals
            self._failed(signals, stream)
            raise SinkError(signals)

    def _failed(self, signals: Iterable[str], stream: str) -> None:
        for signal in signals:
            self._pending[("nuramesh.export.failures", signal)] += 1
        _LOG.warning("otel export failed: SinkError %s", stream)

    # -- the thread ----------------------------------------------------------------------------

    def start(self) -> None:
        """Run cycles on a daemon thread until `stop()`."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
                delay = (
                    self.retry_delay() if self.consecutive_failures else float(self.poll_seconds)
                )
            except BaseException:  # the thread must survive anything a cycle does
                delay = float(self.poll_seconds)
            if self._wait(self._stop, delay):
                break

    def stop(self) -> None:
        """Stop reading, flush the sink with a 5-second deadline and return. The cursor stands
        behind everything acknowledged; a flush that times out loses nothing."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(self.export_timeout + 1)
            self._thread = None
        try:
            self.sink.flush(SHUTDOWN_FLUSH_SECONDS)
        except Exception as exc:
            _LOG.warning("otel export shutdown: %s", type(exc).__name__)
        try:
            self.sink.close()
        except Exception as exc:
            _LOG.warning("otel export shutdown: %s", type(exc).__name__)

    @property
    def running(self) -> bool:
        """Whether the loop thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        """The cursors and the lag of the last cycle (diagnostics, tests)."""
        return {
            "cursors": dict(self.cursors),
            "lag": self._lag,
            "damaged": sorted(self._damaged),
        }
