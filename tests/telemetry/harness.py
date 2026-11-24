"""Runs a `cases.json` case through the tailer with a fake reader and sink."""

from decimal import Decimal
from pathlib import Path
from typing import Any

from nuramesh.telemetry.cursor import CursorFile
from nuramesh.telemetry.mapping import MappingConfig
from nuramesh.telemetry.tailer import Tailer

from .cases import build_events
from .fakes import FakeReader, FakeSink


def config_for(case: dict[str, Any]) -> MappingConfig:
    raw = case.get("config", {})
    key = None
    if raw.get("person_ids") == "pseudonym":
        spec = raw["pseudonym_key"]
        key = bytes.fromhex(spec["each_byte_hex"]) * spec["bytes"]
    return MappingConfig(
        event_hash=raw.get("event_hash", True),
        cost_metric=raw.get("cost_metric", False),
        genai_attrs=raw.get("genai_attrs", False),
        sample_ratio=Decimal(raw.get("sample_ratio", "1")),
        batch_size=raw.get("batch_size", 256),
        person_key=key,
    )


def make_tailer(
    case: dict[str, Any],
    tmp_path: Path,
    *,
    sink: FakeSink | None = None,
    reader: FakeReader | None = None,
    cursors: dict[str, int] | None = None,
    **options: Any,
) -> tuple[Tailer, FakeReader, FakeSink]:
    reader = reader or FakeReader(build_events(case))
    sink = sink or FakeSink()
    raw = case.get("config", {})
    options.setdefault("max_lag_events", raw.get("max_lag_events", 100000))
    options.setdefault("export_timeout_seconds", raw.get("export_timeout_seconds", 10))
    options.setdefault("jitter", lambda seconds: seconds)
    tailer = Tailer(
        reader, sink, CursorFile(tmp_path / "cursors.json"), config_for(case), **options
    )
    tailer.cursors = dict(cursors or {})
    return tailer, reader, sink


def drain(tailer: Tailer, limit: int = 50) -> None:
    """Cycle until a cycle serves no stream."""
    for _ in range(limit):
        result = tailer.run_cycle()
        if not result.ok or not result.served:
            return
