"""Test doubles for the exporter: a ledger reader over plain events, and recording sinks."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nuramesh.core.ledger import LedgerEvent
from nuramesh.telemetry.mapping import MetricPoint, Span
from nuramesh.telemetry.tailer import SinkError


@dataclass(frozen=True)
class FakeHead:
    seq: int


class FakeReader:
    """Committed events by stream. `cap` limits what is visible (the store at an earlier time)."""

    def __init__(self, events: Sequence[LedgerEvent]) -> None:
        self.by_stream: dict[str, list[LedgerEvent]] = {}
        for event in events:
            self.by_stream.setdefault(event.stream, []).append(event)
        self.cap: dict[str, int] = {}
        self.reads = 0
        self.fail_reads = False

    def visible(self, stream: str) -> list[LedgerEvent]:
        limit = self.cap.get(stream)
        found = self.by_stream.get(stream, [])
        return [e for e in found if limit is None or e.seq <= limit]

    def heads(self, only: Any = None) -> dict[str, FakeHead]:
        if self.fail_reads:
            raise OSError("boom")
        return {
            s: FakeHead(self.visible(s)[-1].seq)
            for s in sorted(self.by_stream)
            if self.visible(s) and (only is None or s in only)
        }

    def read_stream(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[LedgerEvent]:
        self.reads += 1
        if self.fail_reads:
            raise OSError("boom")
        found = [e for e in self.visible(stream) if e.seq > after_seq]
        return found if limit is None else found[:limit]

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        self.reads += 1
        if self.fail_reads:
            raise OSError("boom")
        return [e for e in self.visible(f"run:{run_id}") if e.task_id == task_id]


class FakeSink:
    """Records everything it is handed."""

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self.metrics: list[MetricPoint] = []
        self.calls = 0
        self.flushed: list[float] = []
        self.closed = False

    def export(self, spans: Sequence[Span], metrics: Sequence[MetricPoint]) -> None:
        self.calls += 1
        self.spans.extend(spans)
        self.metrics.extend(metrics)

    def flush(self, timeout_seconds: float) -> None:
        self.flushed.append(timeout_seconds)

    def close(self) -> None:
        self.closed = True


class FailingSink(FakeSink):
    """Raises on every export."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error or SinkError(("traces",))

    def export(self, spans: Sequence[Span], metrics: Sequence[MetricPoint]) -> None:
        self.calls += 1
        raise self.error


class HangingSink(FakeSink):
    """Blocks until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def export(self, spans: Sequence[Span], metrics: Sequence[MetricPoint]) -> None:
        self.calls += 1
        self.release.wait(30)
