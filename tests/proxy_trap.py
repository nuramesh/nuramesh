"""Local stand-ins for a proxy and a redirecting host (ND225): no real network."""

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

PROXY_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


@contextmanager
def proxy_listener(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bytes]]:
    """A listener named as every proxy variable; yields what it received (it must stay empty)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.5)
    seen: list[bytes] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            conn.settimeout(0.5)
            try:
                seen.append(conn.recv(4096) or b"<connected>")
            except OSError:
                seen.append(b"<connected>")
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    for name in PROXY_VARS:
        monkeypatch.setenv(name, f"http://127.0.0.1:{listener.getsockname()[1]}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield seen
    finally:
        stop.set()
        thread.join()
        listener.close()


def no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a numeric loopback address resolves: target hosts fail locally, never on a network."""
    real = socket.getaddrinfo

    def only_numeric(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host in ("127.0.0.1", "localhost"):
            return real(host, *args, **kwargs)
        raise socket.gaierror("no network in tests")

    monkeypatch.setattr(socket, "getaddrinfo", only_numeric)


@contextmanager
def redirecting_server(any_path: bool = False) -> Iterator[tuple[int, list[str]]]:
    """A server that answers 307 for `/start` (with `any_path`: every path but `/stolen`)."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            hits.append(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            if self.path == "/start" or (any_path and self.path != "/stolen"):
                self.send_response(307)
                self.send_header("Location", "/stolen")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _answer  # noqa: N815

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], hits
    finally:
        server.shutdown()
        server.server_close()
