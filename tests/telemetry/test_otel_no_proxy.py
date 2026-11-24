"""ND225 (T239): the OpenTelemetry exporter sends configured header credentials, so it is a
key-sending path. A local listener named as every proxy variable must receive nothing, and a 307
must be reported, never followed (the redirect target would get the configured header)."""

import contextlib

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http")

from proxy_trap import no_dns, proxy_listener, redirecting_server  # noqa: E402

from nuramesh.telemetry.otel import build_sink  # noqa: E402
from nuramesh.telemetry.settings import load_settings  # noqa: E402
from nuramesh.telemetry.tailer import SinkError  # noqa: E402

HEADER = "x-token=obviously-fake-test-key-not-a-secret"


def probe(endpoint: str) -> None:
    """One export request; the result (failed, or a 3xx the exporter does not call a failure) is
    not what these tests are about, only where the request goes."""
    settings = load_settings(
        {
            "NURAMESH_OTEL_ENDPOINT": endpoint,
            "NURAMESH_OTEL_HEADERS": HEADER,
            "NURAMESH_OTEL_EXPORT_TIMEOUT_SECONDS": "2",
            "NURAMESH_OTEL_ALLOW_INSECURE": "1",
        }
    )
    assert settings is not None
    sink = build_sink(settings)
    try:
        with contextlib.suppress(SinkError):
            sink.probe(["traces"])
    finally:
        sink.close()


def test_the_exporter_never_offers_its_header_to_a_proxy_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_dns(monkeypatch)
    with proxy_listener(monkeypatch) as seen:
        probe("http://nuramesh-otel-test.invalid")
        probe("https://nuramesh-otel-test.invalid")
    assert seen == []


def test_the_exporter_never_follows_a_redirect() -> None:
    with redirecting_server(any_path=True) as (port, hits):
        probe(f"http://127.0.0.1:{port}")
    assert hits == ["/v1/traces"]  # asked once; the redirect target was never requested
