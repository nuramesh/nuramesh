"""Pins for `nuramesh.telemetry.runtime` found by the mutation farm (issue #1197): each outcome
of `start_exporter` and what `build_tailer` hands the tailer. Fake sinks only; nothing leaves the
machine. Pseudonym key material is generated when the test runs."""

import logging
from pathlib import Path

import pytest

from nuramesh.telemetry import runtime
from nuramesh.telemetry.people import PseudonymKeyError
from nuramesh.telemetry.settings import load_settings

from .fakes import FakeReader, FakeSink


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "NURAMESH_OTEL_ENDPOINT": "https://collector.example.test",
        "NURAMESH_OTEL_STATE_FILE": str(tmp_path / "cursors.json"),
        "NURAMESH_KEY_DIR": str(tmp_path / "keys"),
        **extra,
    }


def test_start_exporter_returns_a_started_tailer_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sink = FakeSink()
    monkeypatch.setattr(runtime, "make_sink", lambda settings: sink)
    outcome = runtime.start_exporter(FakeReader([]), _env(tmp_path))
    try:
        assert outcome.tailer is not None and outcome.code is None and outcome.variable is None
        assert outcome.tailer.sink is sink and outcome.tailer.running
    finally:
        if outcome.tailer is not None:
            outcome.tailer.stop()


def test_start_exporter_is_silent_when_not_asked_or_not_configured(tmp_path: Path) -> None:
    assert runtime.start_exporter(FakeReader([]), None) == runtime.Outcome(None, None)
    off = {"NURAMESH_KEY_DIR": str(tmp_path)}
    assert runtime.start_exporter(FakeReader([]), off) == runtime.Outcome(None, None)


def test_a_bad_setting_gives_its_code_and_variable(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    env = _env(tmp_path, NURAMESH_OTEL_PERSON_IDS="everyone")
    with caplog.at_level(logging.WARNING, logger="nuramesh.telemetry"):
        outcome = runtime.start_exporter(FakeReader([]), env)
    assert outcome == runtime.Outcome(None, "bad_setting", "NURAMESH_OTEL_PERSON_IDS")
    assert [r.getMessage() for r in caplog.records] == ["otel export disabled: bad_setting"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PseudonymKeyError("x"), runtime.BAD_KEY),
        (ImportError("x"), runtime.MISSING_EXTRA),
        (RuntimeError("x"), "failed"),
    ],
)
def test_each_start_failure_has_its_closed_code_and_one_log_line(
    error: Exception,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    def boom(settings: object) -> FakeSink:
        raise error

    monkeypatch.setattr(runtime, "make_sink", boom)
    with caplog.at_level(logging.WARNING, logger="nuramesh.telemetry"):
        outcome = runtime.start_exporter(FakeReader([]), _env(tmp_path))
    assert outcome == runtime.Outcome(None, code)
    shown = code if code != "failed" else "RuntimeError"
    assert [r.getMessage() for r in caplog.records] == [f"otel export disabled: {shown}"]


def test_the_pseudonym_key_is_loaded_only_for_pseudonym_ids(tmp_path: Path) -> None:
    none = load_settings(_env(tmp_path))
    assert none is not None and none.person_ids == "none"
    assert runtime.mapping_config(none).person_key is None
    assert not (tmp_path / "keys").exists()  # `none` never creates a key
    pseudo = load_settings(_env(tmp_path, NURAMESH_OTEL_PERSON_IDS="pseudonym"))
    assert pseudo is not None
    key = runtime.mapping_config(pseudo).person_key
    assert isinstance(key, bytes) and len(key) == 32
    assert (tmp_path / "keys" / "otel-pseudonym.key").exists()


def test_the_mapping_config_and_tailer_carry_the_settings(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        NURAMESH_OTEL_EVENT_HASH="false",
        NURAMESH_OTEL_COST_METRIC="true",
        NURAMESH_OTEL_GENAI_ATTRS="true",
        NURAMESH_OTEL_BATCH_SIZE="7",
        NURAMESH_OTEL_POLL_SECONDS="9",
        NURAMESH_OTEL_EXPORT_TIMEOUT_SECONDS="4",
        NURAMESH_OTEL_MAX_LAG_EVENTS="123",
        NURAMESH_OTEL_SIGNALS="traces",
    )
    settings = load_settings(env)
    assert settings is not None
    config = runtime.mapping_config(settings)
    assert (config.event_hash, config.cost_metric, config.genai_attrs) == (False, True, True)
    assert config.batch_size == 7
    assert (
        config.genai_attrs == settings.genai_attrs and config.sample_ratio == settings.sample_ratio
    )
    sink = FakeSink()
    tailer = runtime.build_tailer(FakeReader([]), settings, sink, only_streams=frozenset({"run:a"}))
    assert tailer.sink is sink and tailer.config == config
    assert (tailer.poll_seconds, tailer.export_timeout, tailer.max_lag) == (9, 4, 123)
    assert tailer.signals == ("traces",) and tailer.only_streams == frozenset({"run:a"})
    assert tailer.cursor_file.path == settings.state_file


def test_make_sink_hands_the_settings_to_the_otel_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    sink = FakeSink()
    seen: list[object] = []
    import nuramesh.telemetry.otel as otel

    def build(settings: object) -> FakeSink:
        seen.append(settings)
        return sink

    monkeypatch.setattr(otel, "build_sink", build)
    settings = load_settings(_env(tmp_path))
    assert runtime.make_sink(settings) is sink and seen == [settings]  # type: ignore[arg-type]


def test_an_outcome_cannot_be_changed_after_it_is_returned() -> None:
    import dataclasses

    outcome = runtime.Outcome(None, runtime.BAD_KEY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.code = None  # type: ignore[misc]
