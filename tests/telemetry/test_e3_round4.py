"""E3 round 4: no host, port, path or reply text through ANY logger while the exporter runs, and
the key refusals of `otel check|export` never raise (exit 2, the fixed line)."""

import base64
import logging
import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
pytest.importorskip("fastapi")

from nuramesh.cli import main  # noqa: E402

SECRET_PATH = "secret-path-token"


class RawCollector:
    """A loopback socket server that answers every request with fixed raw bytes."""

    def __init__(self, reply: bytes) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.reply = reply
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.sendall(self.reply)
            conn.close()


BAD_HEADER = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nX-Foo: a\r\n continuation\r\nNoColonLine\r\n"
    b"Content-Type: application/x-protobuf\r\n\r\n"
)
SERVER_ERROR = b"HTTP/1.1 500 reply-text-marker\r\nContent-Length: 0\r\n\r\n"


def _collector(reply: bytes) -> Iterator[RawCollector]:
    server = RawCollector(reply)
    yield server
    server.sock.close()


@pytest.fixture
def bad_header() -> Iterator[RawCollector]:
    yield from _collector(BAD_HEADER)


@pytest.fixture
def server_error() -> Iterator[RawCollector]:
    yield from _collector(SERVER_ERROR)


def _point(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    monkeypatch.setenv("NURAMESH_OTEL_ENDPOINT", f"http://127.0.0.1:{port}/{SECRET_PATH}")


@pytest.mark.parametrize("fixture", ["bad_header", "server_error"])
def test_root_logging_on_no_endpoint_or_reply_text_in_any_log_or_output(
    fixture: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    collector: RawCollector = request.getfixturevalue(fixture)
    _point(monkeypatch, collector.port)
    caplog.set_level(logging.DEBUG)  # a root handler at DEBUG, as a deployer's log config gives
    code = main(["otel", "check"])
    out = capsys.readouterr()
    text = caplog.text + "".join(r.getMessage() for r in caplog.records) + out.out + out.err
    for forbidden in (
        str(collector.port),
        SECRET_PATH,
        "127.0.0.1",
        "reply-text-marker",
        "NoColon",
    ):
        assert forbidden not in text, forbidden
    if fixture == "server_error":
        assert code == 1  # an answer that fails (OD-7b: was 3)
    if code != 0:
        assert "otel check ok" not in out.out


def _state(names: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
    loggers = {n: logging.getLogger(n) for n in names}
    return {
        n: (g.propagate, list(g.handlers), list(g.filters), g.level) for n, g in loggers.items()
    }


def test_the_loggers_are_restored_after_the_export(
    bad_header: RawCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point(monkeypatch, bad_header.port)
    names = ("opentelemetry", "urllib3", "requests", "http.client")
    before = _state(names)
    main(["otel", "check"])
    assert _state(names) == before


def _key_env(monkeypatch: pytest.MonkeyPatch, key: Path) -> None:
    monkeypatch.setenv("NURAMESH_OTEL_ENDPOINT", "http://127.0.0.1:9/x")
    monkeypatch.setenv("NURAMESH_OTEL_PERSON_IDS", "pseudonym")
    monkeypatch.setenv("NURAMESH_OTEL_PSEUDONYM_KEY_FILE", str(key))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
@pytest.mark.parametrize("case", ["unreadable", "missing_rodir", "missing"])
def test_check_refuses_a_bad_or_missing_key_with_exit_2_and_never_creates_it(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "k"
    folder.mkdir()
    key = folder / "otel.key"
    if case == "unreadable":
        key.write_text(base64.b64encode(b"a" * 32).decode() + "\n")
        key.chmod(0o000)
    elif case == "missing_rodir":
        folder.chmod(0o500)
    _key_env(monkeypatch, key)
    try:
        assert main(["otel", "check"]) == 2
    finally:
        folder.chmod(0o700)
        if key.exists():
            key.chmod(0o600)
    err = capsys.readouterr().err
    assert "Traceback" not in err and str(folder) not in err
    if case != "unreadable":
        assert not key.exists()  # check is read-only: it never creates key material


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_export_refuses_an_unreadable_key_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    team = tmp_path / "team"
    team.mkdir()
    key = tmp_path / "otel.key"
    key.write_text(base64.b64encode(b"a" * 32).decode() + "\n")
    key.chmod(0o000)
    _key_env(monkeypatch, key)
    try:
        code = main(["otel", "export", "--once", "--team", str(team)])
    finally:
        key.chmod(0o600)
    assert code == 2 and "Traceback" not in capsys.readouterr().err
