"""Running a worker as a long-lived process (`nuramesh worker`, R1.20 PR 3).

`serve` connects to the Temporal server, starts a worker on one task queue and runs until it is
asked to stop (SIGTERM or SIGINT), then shuts down gracefully (running activities finish or are
cancelled by the SDK's shutdown). A connection failure raises `WorkerConnectionError` with a plain
message; it never includes credentials. A non-loopback address needs TLS and credentials
(`temporal.security`); without them it raises `TemporalSecurityError` before connecting.
**For evaluation only against the dev server; production needs a real cluster (ND53 E).**
"""

import asyncio
import math
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from nuramesh.engine.content import ContentService
from nuramesh.engine.light import RunBuilder
from nuramesh.env_alias import read_renamed
from nuramesh.temporal.activities import EngineFactory
from nuramesh.temporal.client import DEFAULT_TASK_QUEUE, connect
from nuramesh.temporal.health import HealthServer
from nuramesh.temporal.security import TemporalSecurityError
from nuramesh.temporal.worker import build_worker

DEFAULT_NAMESPACE = "default"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)

Connector = Callable[[str, str], Awaitable[Client]]


def force_exit(code: int) -> None:  # pragma: no cover - replaced in tests
    """Leave at once (a second interrupt): the ledger already says what happened."""
    os._exit(code)


def stop_handler(
    stop: asyncio.Event, exit_now: Callable[[int], None] = force_exit
) -> Callable[[], None]:
    """The first signal asks for a graceful stop; a second one exits at once (130)."""

    def handle() -> None:
        if stop.is_set():
            exit_now(130)
        else:
            stop.set()

    return handle


class WorkerConnectionError(RuntimeError):
    """The Temporal server could not be reached; the message says plainly where and why."""


CONNECT_TIMEOUT_ENV = "NURAMESH_WORKER_CONNECT_TIMEOUT_SECONDS"
OLD_CONNECT_TIMEOUT_ENV = "NURAMESH_WORKER_CONNECT_TIMEOUT"  # warning alias for one release (ND421)
DEFAULT_CONNECT_TIMEOUT = 120.0
PER_TRY_SECONDS = 3.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0


def connect_timeout_from_env(environ: Mapping[str, str]) -> float:
    """`NURAMESH_WORKER_CONNECT_TIMEOUT_SECONDS` in seconds (default 120); a bad value raises
    `ValueError` with the setting's name (the worker exits 2)."""
    name, raw = read_renamed(environ, CONNECT_TIMEOUT_ENV, OLD_CONNECT_TIMEOUT_ENV) or (
        CONNECT_TIMEOUT_ENV,
        "",
    )
    text = raw.strip()
    if not text:
        return DEFAULT_CONNECT_TIMEOUT
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{name} must be a number of seconds, 0 or more")
    return value


async def connect_with_backoff(
    address: str,
    namespace: str,
    connector: Connector,
    *,
    timeout: float,
    per_try: float = PER_TRY_SECONDS,
    base: float = BACKOFF_BASE,
    cap: float = BACKOFF_CAP,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Client:
    """Try to connect (`per_try` seconds each), wait 1, 2, 4 ... 30 s between tries, and raise
    `WorkerConnectionError` once `timeout` seconds have passed. A security refusal is not retried.
    With `timeout` 0 there is one try."""
    started = monotonic()
    delay = base
    while True:
        try:
            return await asyncio.wait_for(connector(address, namespace), per_try)
        except TemporalSecurityError:
            raise  # a refusal to start, not a connection failure
        except Exception as exc:  # the SDK raises RuntimeError / OSError variants
            failure = type(exc).__name__ if not isinstance(exc, TimeoutError) else "TimeoutError"
        if monotonic() - started >= timeout:
            raise WorkerConnectionError(
                f"couldn't connect to Temporal at {address} (namespace {namespace}): "
                f"{failure}. Is the server running and the address right?"
            ) from None
        await sleep(delay)
        delay = min(cap, delay * 2)


async def _connect(address: str, namespace: str) -> Client:
    return await connect(address, namespace=namespace)


def worker_is_serving(worker: Any, serving: bool) -> bool:
    """What `/healthz` calls connected: the worker is up and its poll loop still runs (a fatal
    loss of the Temporal connection ends the loop, so this turns false)."""
    return bool(serving and worker is not None and worker.is_running and not worker.is_shutdown)


async def serve(
    address: str,
    factory: EngineFactory,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    task_queue: str = DEFAULT_TASK_QUEUE,
    max_activities: int = 16,
    stop: asyncio.Event | None = None,
    connector: Connector = _connect,
    on_ready: Callable[[], None] | None = None,
    builder: RunBuilder | None = None,
    content: ContentService | None = None,
    connect_timeout: float = 0.0,
    health_port: int | None = None,
) -> None:
    """Connect (retrying with a backoff for up to `connect_timeout` seconds), run a worker until
    `stop` is set (SIGTERM and SIGINT set it), then shut down. With a `builder` the worker also
    begins the runs the API asked for. `health_port` serves `GET /healthz` on loopback."""
    serving = False
    worker: Worker | None = None
    # bind first: a busy port is a quick, coded error (HealthBindError), before any connection
    health = (
        HealthServer(health_port, lambda: worker_is_serving(worker, serving))
        if (health_port is not None)
        else None
    )
    try:
        client = await connect_with_backoff(
            address, namespace, connector, timeout=connect_timeout, per_try=PER_TRY_SECONDS
        )
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        handler = stop_handler(stop)
        installed: list[signal.Signals] = []
        for sig in STOP_SIGNALS:
            try:
                loop.add_signal_handler(sig, handler)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):  # not the main thread / Windows
                pass
        try:
            worker = build_worker(
                client,
                factory,
                task_queue=task_queue,
                max_activities=max_activities,
                builder=builder,
                content=content,
            )
            if health is not None:
                health.start()
            async with worker:
                serving = True
                if on_ready is not None:
                    on_ready()
                await stop.wait()
                serving = False
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
    finally:
        if health is not None:
            health.close()
