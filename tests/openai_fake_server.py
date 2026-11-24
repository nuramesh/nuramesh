"""A fake OpenAI-compatible server on 127.0.0.1 (stdlib, a thread, scripted answers, a log)."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any

from net import QuietHTTPServer


@dataclass
class Fake:
    port: int
    script: list[tuple[int, Any, dict[str, str]]] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def reply(self, status: int, body: Any = None, **headers: str) -> None:
        self.script.append((status, body, headers))


def answer(text: str = "hi", prompt: int = 10, completion: int = 5, **extra: Any) -> dict[str, Any]:
    """A well-formed chat completion."""
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        **extra,
    }


@contextmanager
def fake_server(prefix: str = "/v1") -> Iterator[Fake]:
    fake = Fake(0)

    class Handler(BaseHTTPRequestHandler):
        def _go(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            fake.log.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(body) if body else None,
                }
            )
            status, payload, headers = fake.script.pop(0) if fake.script else (200, answer(), {})
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k.replace("_", "-"), v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _go  # noqa: N815

        def log_message(self, *args: object) -> None:
            pass

    server = QuietHTTPServer(("127.0.0.1", 0), Handler)
    fake.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
