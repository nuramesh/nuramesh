"""Builds the exporter from settings (E3 spec sections 1, 2, 10). Used by the server lifespan and
the `nuramesh otel` commands. The OpenTelemetry modules are imported here only through
`nuramesh.telemetry.otel`, lazily, when the settings are valid and enabled.

A refusal never reaches a run: the caller gets `None` plus one closed code, and one log line
`otel export disabled: <code>` is written (never a URL, a header or a key)."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from nuramesh.telemetry.cursor import CursorFile
from nuramesh.telemetry.mapping import MappingConfig
from nuramesh.telemetry.people import PseudonymKeyError, load_key
from nuramesh.telemetry.settings import OtelSettings, OtelSettingsError, load_settings
from nuramesh.telemetry.tailer import Sink, Tailer, TailSource

_LOG = logging.getLogger("nuramesh.telemetry")
MISSING_EXTRA = "missing_extra"
BAD_KEY = "bad_setting"


@dataclass(frozen=True)
class Outcome:
    """`tailer` is set when the exporter is on; otherwise `code` says why not (None = not asked)."""

    tailer: Tailer | None
    code: str | None
    variable: str | None = None


def mapping_config(settings: OtelSettings) -> MappingConfig:
    """The mapper's options. Loads (and on first use creates) the key only for `pseudonym`."""
    key = load_key(settings.pseudonym_key_file) if settings.person_ids == "pseudonym" else None
    return MappingConfig(
        event_hash=settings.event_hash,
        cost_metric=settings.cost_metric,
        genai_attrs=settings.genai_attrs,
        sample_ratio=settings.sample_ratio,
        batch_size=settings.batch_size,
        person_key=key,
    )


def build_tailer(
    source: TailSource,
    settings: OtelSettings,
    sink: Sink,
    *,
    only_streams: frozenset[str] | None = None,
) -> Tailer:
    """A tailer over `source` with the settings' cursor file and limits."""
    return Tailer(
        source,
        sink,
        CursorFile(settings.state_file),
        mapping_config(settings),
        poll_seconds=settings.poll_seconds,
        export_timeout_seconds=settings.export_timeout_seconds,
        max_lag_events=settings.max_lag_events,
        signals=settings.signals,
        only_streams=only_streams,
    )


def make_sink(settings: OtelSettings) -> Sink:
    """The production sink. Raises `ImportError` when the `otel` extra is not installed."""
    from nuramesh.telemetry.otel import build_sink

    return build_sink(settings)


def start_exporter(source: TailSource, env: Mapping[str, str] | None) -> Outcome:
    """Start the exporter when `NURAMESH_OTEL_ENDPOINT` is set in `env` (None = not asked, which
    is what every test and the read-only inspector pass). Never raises."""
    if env is None:
        return Outcome(None, None)
    try:
        settings = load_settings(env)
        if settings is None:
            return Outcome(None, None)
        sink = make_sink(settings)
        tailer = build_tailer(source, settings, sink)
    except OtelSettingsError as exc:
        _LOG.warning("otel export disabled: %s", exc.code)
        return Outcome(None, exc.code, exc.variable)
    except PseudonymKeyError:
        _LOG.warning("otel export disabled: %s", BAD_KEY)
        return Outcome(None, BAD_KEY)
    except ImportError:
        _LOG.warning("otel export disabled: %s", MISSING_EXTRA)
        return Outcome(None, MISSING_EXTRA)
    except Exception as exc:  # the server must start whatever the exporter does
        _LOG.warning("otel export disabled: %s", type(exc).__name__)
        return Outcome(None, "failed")
    tailer.start()
    return Outcome(tailer, None)
