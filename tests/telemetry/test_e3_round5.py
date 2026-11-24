"""E3 round 5: the transport-log silencing is a per-thread filter. A handler on a child logger or
added mid-export never sees a record from the exporting thread; other threads' records are kept;
no handler of other code is removed or replaced."""

import io
import logging
import threading

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
pytest.importorskip("fastapi")

from nuramesh.cli import main  # noqa: E402
from nuramesh.telemetry.otel import quiet_transport  # noqa: E402

from .test_e3_round4 import SECRET_PATH, RawCollector  # noqa: E402

BAD_HEADER = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nX-Foo: a\r\n continuation\r\nNoColonLine\r\n\r\n"
)


def _stream(name: str) -> tuple[logging.Logger, logging.StreamHandler[io.StringIO], io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    log = logging.getLogger(name)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    return log, handler, buf


def test_a_handler_on_a_child_logger_sees_nothing_from_a_real_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = RawCollector(BAD_HEADER)
    monkeypatch.setenv("NURAMESH_OTEL_ENDPOINT", f"http://127.0.0.1:{server.port}/{SECRET_PATH}")
    log, handler, buf = _stream("urllib3.connection")
    try:
        main(["otel", "check"])
    finally:
        log.removeHandler(handler)
        server.sock.close()
    assert buf.getvalue() == ""
    assert handler not in log.filters and not log.filters


def test_a_handler_added_by_another_thread_mid_export_is_kept_and_sees_nothing() -> None:
    log = logging.getLogger("urllib3")
    handler = logging.StreamHandler(io.StringIO())
    inside, proceed = threading.Event(), threading.Event()

    def exporter() -> None:
        with quiet_transport():
            inside.set()
            proceed.wait(5)
            logging.getLogger("urllib3.connectionpool").warning("secret-path-token")

    thread = threading.Thread(target=exporter)
    thread.start()
    inside.wait(5)
    log.addHandler(handler)  # another thread adds it while the window is open
    proceed.set()
    thread.join()
    try:
        assert handler in log.handlers  # never dropped by a restore
        assert handler.stream.getvalue() == ""
    finally:
        log.removeHandler(handler)


def test_another_threads_records_are_kept_during_an_export() -> None:
    log, handler, buf = _stream("urllib3.connectionpool")
    inside, proceed = threading.Event(), threading.Event()

    def exporter() -> None:
        with quiet_transport():
            inside.set()
            proceed.wait(5)

    thread = threading.Thread(target=exporter)
    thread.start()
    inside.wait(5)
    try:
        log.warning("app-own-warning")  # this thread is not exporting
        proceed.set()
        thread.join()
        assert "app-own-warning" in buf.getvalue()
        with quiet_transport():
            log.warning("dropped-here")
        log.warning("after-window")
    finally:
        log.removeHandler(handler)
    assert "dropped-here" not in buf.getvalue() and "after-window" in buf.getvalue()


def _hf(log: logging.Logger) -> tuple[list[object], list[object]]:
    return list(log.handlers), list(log.filters)


def test_filters_and_handlers_are_exactly_restored() -> None:
    names = ["urllib3", "urllib3.connection", "requests", "opentelemetry"]
    before = {
        n: (list(logging.getLogger(n).handlers), list(logging.getLogger(n).filters)) for n in names
    }
    root_before = (list(logging.root.handlers), list(logging.root.filters))
    all_handlers = {h for n in names for h in logging.getLogger(n).handlers} | set(
        logging.root.handlers
    )
    handler_filters = {h: list(h.filters) for h in all_handlers}
    with quiet_transport():
        pass
    assert {h: list(h.filters) for h in all_handlers} == handler_filters
    after = {
        n: (list(logging.getLogger(n).handlers), list(logging.getLogger(n).filters)) for n in names
    }
    assert before == after
    assert root_before == (list(logging.root.handlers), list(logging.root.filters))
