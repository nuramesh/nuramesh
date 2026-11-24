"""Loads `spec/e3-otel/cases.json` and turns its events and expectations into test inputs."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from nuramesh.core.ledger import LedgerEvent
from nuramesh.telemetry.mapping import MetricPoint, Span

SPEC = Path(__file__).resolve().parents[2] / "spec" / "e3-otel" / "cases.json"


def load_cases() -> dict[str, dict[str, Any]]:
    data = json.loads(SPEC.read_text("utf-8"))
    return {case["id"]: case for case in data["cases"]}


CASES = load_cases()
Key = tuple[Any, ...]


def build_events(case: dict[str, Any]) -> list[LedgerEvent]:
    """The case's events as stored entries with the given (placeholder) hashes."""
    return [LedgerEvent.model_validate(raw) for raw in case.get("events", [])]


def span_dict(span: Span) -> dict[str, Any]:
    return {
        "name": span.name,
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "start_unix_nano": str(span.start_ns),
        "end_unix_nano": str(span.end_ns),
        "status": span.status,
        "attributes": dict(span.attributes),
        "events": [
            {"name": e.name, "at_unix_nano": str(e.at_ns), "attributes": dict(e.attributes)}
            for e in span.events
        ],
    }


def aggregate(points: list[MetricPoint]) -> set[Key]:
    """Counters summed per (name, attributes); histograms as (count, sum). The event counter and
    the lag gauge are left out: the case file lists them separately."""
    sums: dict[Key, int] = defaultdict(int)
    counts: dict[Key, int] = defaultdict(int)
    units: dict[str, str] = {}
    for point in points:
        key = (point.name, point.kind, tuple(sorted(point.attributes.items())))
        units[point.name] = point.unit
        sums[key] += point.value
        counts[key] += 1
    found: set[Key] = set()
    for key, value in sums.items():
        name, kind, attrs = key
        if name in ("nuramesh.export.events", "nuramesh.export.lag_events"):
            continue
        if kind == "histogram":
            found.add((name, "histogram", units[name], attrs, counts[key], value))
        else:
            found.add((name, "counter", units[name], attrs, value))
    return found


def expected_metrics(raw: list[dict[str, Any]]) -> set[Key]:
    found: set[Key] = set()
    for item in raw:
        attrs = tuple(sorted(item["attributes"].items()))
        if item["type"] == "histogram":
            found.add((item["name"], "histogram", item["unit"], attrs, item["count"], item["sum"]))
        else:
            found.add((item["name"], "counter", item["unit"], attrs, item["value"]))
    return found


def total(points: list[MetricPoint], name: str) -> int:
    return sum(p.value for p in points if p.name == name)
