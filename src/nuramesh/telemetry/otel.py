"""The OpenTelemetry sink: the ONLY module that imports `opentelemetry` (E3 spec sections 9, 10).

Imported lazily, and only when an endpoint is set and valid. Everything here uses PRIVATE
providers: the global tracer and meter providers are never touched, and the standard `OTEL_*`
variables are never read (the exporters get their endpoint, headers and timeout explicitly).

Spans are built finished, with the ids and times the mapper computed (`ReadableSpan` with an
explicit `SpanContext`), and handed straight to the OTLP span exporter so a failed call is seen
by the tailer (a batch processor would hide it). Metrics go through a private `MeterProvider` and
an in-memory reader with delta temporality; each `export` call collects and sends them.
No exception text from the SDK is kept: a failure becomes a `SinkError` naming only signals.
"""

import contextlib
import functools
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import requests
from opentelemetry.exporter.http.transport._requests import RequestsHTTPTransport
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram, NoOpMeterProvider
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics import Counter as SdkCounter
from opentelemetry.sdk.metrics import Histogram as SdkHistogram
from opentelemetry.sdk.metrics._internal.export._metric_reader_metrics import (
    create_metric_reader_metrics,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
    MetricExporter,
)
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, TraceFlags
from opentelemetry.trace.status import Status, StatusCode

from nuramesh.telemetry.mapping import LATENCY_BOUNDS, MetricPoint, Span
from nuramesh.telemetry.settings import OtelSettings
from nuramesh.telemetry.tailer import SinkError

_LOG = logging.getLogger("nuramesh.telemetry")
SCOPE = "nuramesh.telemetry"
_STATUS = {
    "UNSET": Status(StatusCode.UNSET),
    "OK": Status(StatusCode.OK),
    "ERROR": Status(StatusCode.ERROR),
}


def package_version() -> str:
    """The installed `nuramesh` version, `unknown` from a source tree."""
    try:
        return version("nuramesh")
    except PackageNotFoundError:
        return "unknown"


def make_resource(deployment: str | None) -> Resource:
    """`service.name`, `service.version` and the optional deployment. No detectors, no host."""
    attrs: dict[str, str] = {"service.name": "nuramesh", "service.version": package_version()}
    if deployment:
        attrs["nuramesh.deployment"] = deployment
    return Resource(attrs)


def _context(trace_hex: str, span_hex: str) -> SpanContext:
    return SpanContext(
        trace_id=int(trace_hex, 16),
        span_id=int(span_hex, 16),
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )


def to_readable(span: Span, resource: Resource) -> ReadableSpan:
    """A finished SDK span with exactly the mapper's ids, times, attributes and events."""
    return ReadableSpan(
        name=span.name,
        context=_context(span.trace_id, span.span_id),
        parent=None
        if span.parent_span_id is None
        else _context(span.trace_id, span.parent_span_id),
        resource=resource,
        attributes=dict(span.attributes),
        events=[Event(e.name, dict(e.attributes), e.at_ns) for e in span.events],
        status=_STATUS.get(span.status, _STATUS["UNSET"]),
        start_time=span.start_ns,
        end_time=span.end_ns,
    )


class _PrivateMeterProvider(MeterProvider):
    """A `MeterProvider` that the process environment cannot weaken or extend.

    The SDK reads `OTEL_SDK_DISABLED` in its constructor and then hands out no-op meters: metrics
    would be dropped, the cursor would advance and `otel check` would report OK without ever
    contacting the collector. `__init__` resets that private flag (the version is pinned exactly,
    ND133, and a test fails if the attribute disappears) instead of refusing the setting, because
    refusing would let an unrelated ambient variable stop an export the owner asked for. The fork
    hook is replaced by one that resets the locks only: the SDK's own adds `process.*` resource
    attributes (host and process detectors) that the E3 spec forbids."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(exemplar_filter=AlwaysOffExemplarFilter(), **kwargs)
        self._disabled = False

    def _handle_fork(self) -> None:
        self._lock = threading.Lock()
        self._meter_lock = threading.Lock()
        type(self)._all_metric_readers_lock = threading.Lock()


class _QuietReader(InMemoryMetricReader):
    """The SDK reader records its own collection metrics on the provider it is registered with
    when `OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED` is set; those would reach the collector under
    the scope `opentelemetry-sdk`. Give it a no-op provider and the flag off instead."""

    def _set_meter_provider(self, meter_provider: Any) -> None:
        self._metrics = create_metric_reader_metrics(
            self._otel_component_type, NoOpMeterProvider(), False
        )


# The SDK exporters log the request URL, the collector's reason phrase and (through `requests`)
# host and port. Spec 10 and 11.1: none of it may reach any output. A handler on the SDK's parent
# logger that does nothing, with propagation off, swallows every record of the `opentelemetry.*`
# loggers (and keeps the "last resort" stderr handler from printing them).
_SDK_LOGGER = logging.getLogger("opentelemetry")


def silence_sdk_logs() -> None:
    """Swallow every log record of the OpenTelemetry SDK (idempotent)."""
    if not any(isinstance(h, logging.NullHandler) for h in _SDK_LOGGER.handlers):
        _SDK_LOGGER.addHandler(logging.NullHandler())
    _SDK_LOGGER.propagate = False


silence_sdk_logs()

# The transport libraries log the full URL, host, port and the collector's raw reply (urllib3:
# header-parse defects, TLS mismatch, retries, DEBUG request lines). While the exporter path runs
# (export, probe, flush, close) a filter drops, on the CURRENT thread only, every record of these
# loggers. No handler or propagation setting is touched, so other code's handlers keep working and
# other threads' records are kept. The filter sits on (1) every existing logger under these names,
# which runs on the logger a record is created on, before any of its or its parents' handlers
# (so a handler added mid-export, on any of them, never sees the record), and (2) every existing
# handler of any logger (and the root's), which covers a logger created mid-export. Residual gap:
# a NEW logger plus a handler added to it or to any parent (the root included) during the window
# by another thread is not covered; the exporter's own libraries cannot reach that case.
_TRANSPORT_LOGGERS = (
    "opentelemetry",
    "urllib3",
    "requests",
    "http.client",  # never logs (only print() with debuglevel>0); listed for completeness
    "charset_normalizer",
)
_quiet_lock = threading.Lock()
_quiet_depth = 0
_quiet_local = threading.local()
_quiet_attached: list[logging.Filterer] = []


def _is_transport(name: str) -> bool:
    return any(name == n or name.startswith(n + ".") for n in _TRANSPORT_LOGGERS)


class _ThreadQuiet(logging.Filter):
    """Drops a transport-library record when this thread is inside `quiet_transport`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(_quiet_local, "depth", 0) <= 0:
            return True
        return not _is_transport(record.name)


_THREAD_QUIET = _ThreadQuiet()


def _attach_filters() -> None:
    targets: list[logging.Filterer] = []
    loggers = [logging.root] + [
        lg
        for lg in list(logging.root.manager.loggerDict.values())
        if isinstance(lg, logging.Logger)
    ]
    for lg in loggers:
        if lg is not logging.root and _is_transport(lg.name):
            targets.append(lg)
        targets.extend(list(lg.handlers))
    targets.extend(logging.getLogger(n) for n in _TRANSPORT_LOGGERS)
    for target in targets:
        if _THREAD_QUIET not in target.filters:
            target.addFilter(_THREAD_QUIET)
            _quiet_attached.append(target)


@contextlib.contextmanager
def quiet_transport() -> Iterator[None]:
    """No record of the transport libraries made on this thread reaches any handler while this
    block runs (re-entrant and thread-safe: the first entry attaches, the last exit detaches)."""
    global _quiet_depth
    with _quiet_lock:
        if _quiet_depth == 0:
            _attach_filters()
        _quiet_depth += 1
    _quiet_local.depth = getattr(_quiet_local, "depth", 0) + 1
    try:
        yield
    finally:
        _quiet_local.depth -= 1
        with _quiet_lock:
            _quiet_depth -= 1
            if _quiet_depth == 0:
                for target in _quiet_attached:
                    target.removeFilter(_THREAD_QUIET)
                _quiet_attached.clear()


def _quiet[F: Callable[..., Any]](func: F) -> F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with quiet_transport():
            return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


class _DropRecords(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


class OtelSink:
    """Hands spans and metric points to the SDK exporters. Raises `SinkError` on any failure."""

    def __init__(
        self,
        span_exporter: SpanExporter,
        metric_exporter: MetricExporter,
        resource: Resource,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._spans = span_exporter
        self._metrics = metric_exporter
        self._resource = resource
        self._timeout = timeout_seconds
        temporality: dict[type, AggregationTemporality] = {
            SdkCounter: AggregationTemporality.DELTA,
            SdkHistogram: AggregationTemporality.DELTA,
        }
        self._reader = _QuietReader(preferred_temporality=temporality)
        views = [
            View(
                instrument_name="nuramesh.model.latency",
                aggregation=ExplicitBucketHistogramAggregation(
                    tuple(float(b) for b in LATENCY_BOUNDS)
                ),
            )
        ]
        self._provider = _PrivateMeterProvider(
            resource=resource, metric_readers=[self._reader], views=views
        )
        self._meter = self._provider.get_meter(SCOPE, package_version())
        self._instruments: dict[tuple[str, str], Any] = {}
        self._closed = False

    def _instrument(self, point: MetricPoint) -> Any:
        key = (point.name, point.kind)
        found = self._instruments.get(key)
        if found is None:
            if point.kind == "histogram":
                found = self._meter.create_histogram(point.name, unit=point.unit)
            elif point.kind == "gauge":
                found = self._meter.create_gauge(point.name, unit=point.unit)
            else:
                found = self._meter.create_counter(point.name, unit=point.unit)
            self._instruments[key] = found
        return found

    @_quiet
    def export(self, spans: Sequence[Span], metrics: Sequence[MetricPoint]) -> None:
        """Send both signals; `SinkError.signals` names those that failed."""
        failed: list[str] = []
        if spans:
            try:
                result = self._spans.export([to_readable(s, self._resource) for s in spans])
                if result is not SpanExportResult.SUCCESS:
                    failed.append("traces")
            except Exception as exc:
                _LOG.warning("otel traces export failed: %s", type(exc).__name__)
                failed.append("traces")
        if metrics:
            try:
                self._record(metrics)
                if not self._send_metrics():
                    failed.append("metrics")
            except Exception as exc:
                _LOG.warning("otel metrics export failed: %s", type(exc).__name__)
                failed.append("metrics")
        if failed:
            raise SinkError(failed)

    def _record(self, metrics: Sequence[MetricPoint]) -> None:
        for point in metrics:
            instrument = self._instrument(point)
            attrs = dict(point.attributes)
            if isinstance(instrument, Counter):
                instrument.add(point.value, attrs)
            elif isinstance(instrument, Histogram):
                instrument.record(point.value, attrs)
            else:
                instrument.set(point.value, attrs)

    def _send_metrics(self) -> bool:
        data = self._reader.get_metrics_data()
        if data is None or not data.resource_metrics:
            return True
        result = self._metrics.export(data, timeout_millis=self._timeout * 1000)
        return bool(getattr(result, "name", "") == "SUCCESS")

    @_quiet
    def probe(self, signals: Sequence[str]) -> None:
        """One empty request per signal (`nuramesh otel check`). Raises `SinkError`."""
        failed: list[str] = []
        if "traces" in signals:
            try:
                if self._spans.export([]) is not SpanExportResult.SUCCESS:
                    failed.append("traces")
            except Exception:
                failed.append("traces")
        if "metrics" in signals:
            try:
                self._record([MetricPoint("nuramesh.export.lag_events", "gauge", "{event}", 0, {})])
                if not self._send_metrics():
                    failed.append("metrics")
            except Exception:
                failed.append("metrics")
        if failed:
            raise SinkError(failed)

    @_quiet
    def flush(self, timeout_seconds: float) -> None:
        """Flush what the exporters hold (they hold nothing between calls); best effort."""
        for exporter in (self._spans, self._metrics):
            try:
                if isinstance(exporter, SpanExporter):
                    exporter.force_flush(int(timeout_seconds * 1000))
                else:
                    exporter.force_flush(timeout_seconds * 1000)
            except Exception as exc:
                _LOG.warning("otel flush failed: %s", type(exc).__name__)

    @_quiet
    def close(self) -> None:
        """Shut the providers and exporters down once; errors are logged by class only."""
        if self._closed:
            return
        self._closed = True
        for call in (self._provider.shutdown, self._spans.shutdown, self._metrics.shutdown):
            try:
                call()
            except Exception as exc:
                _LOG.warning("otel shutdown failed: %s", type(exc).__name__)


_HEADERS_BASE = {"Content-Type": "application/x-protobuf"}


def _session() -> requests.Session:
    """A session that reads nothing from the process: no `REQUESTS_CA_BUNDLE`, proxy variables or
    `.netrc`; TLS verification on with the library's own CA bundle."""
    session = requests.Session()
    session.trust_env = False
    session.verify = True
    return session


def _explicit_headers(configured: dict[str, str]) -> dict[str, str]:
    """The only request headers: the protobuf type, the compression and the configured ones."""
    return {**_HEADERS_BASE, "Content-Encoding": "gzip", **configured}


def build_sink(settings: OtelSettings) -> OtelSink:
    """The production sink: OTLP/HTTP protobuf, gzip, TLS verification on, headers from the
    settings only. No standard `OTEL_*` variable is read: the transport is built here (so the
    TLS-file variables are never consulted), the headers are replaced after construction (the SDK
    merges `OTEL_EXPORTER_OTLP_*HEADERS` into them), and the temporality and aggregation are
    passed explicitly. The process environment is never changed."""
    headers = _explicit_headers(dict(settings.headers))
    base = settings.endpoint.rstrip("/")
    timeout = float(settings.export_timeout_seconds)
    # The SDK parses `OTEL_EXPORTER_OTLP_*HEADERS` while constructing and logs each malformed
    # piece verbatim (a header value is a credential): drop that logger's records meanwhile.
    quiet = _DropRecords()
    header_log = logging.getLogger("opentelemetry.util.re")
    header_log.addFilter(quiet)
    try:
        return _build(settings, headers, base, timeout)
    finally:
        header_log.removeFilter(quiet)


def _build(settings: OtelSettings, headers: dict[str, str], base: str, timeout: float) -> OtelSink:
    span_exporter = OTLPSpanExporter(
        endpoint=base + "/v1/traces",
        timeout=timeout,
        compression=Compression.Gzip,
        _transport=RequestsHTTPTransport(verify=True, cert=None, session=_session()),
        meter_provider=NoOpMeterProvider(),
    )
    metric_exporter = OTLPMetricExporter(
        endpoint=base + "/v1/metrics",
        timeout=timeout,
        compression=Compression.Gzip,
        preferred_temporality={
            SdkCounter: AggregationTemporality.DELTA,
            SdkHistogram: AggregationTemporality.DELTA,
        },
        preferred_aggregation={SdkHistogram: ExplicitBucketHistogramAggregation()},
        _transport=RequestsHTTPTransport(verify=True, cert=None, session=_session()),
        meter_provider=NoOpMeterProvider(),
    )
    span_exporter._client._headers = dict(headers)
    metric_exporter._client._headers = dict(headers)
    return OtelSink(span_exporter, metric_exporter, make_resource(settings.deployment), timeout)
