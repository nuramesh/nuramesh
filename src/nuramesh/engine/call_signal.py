"""What the runner knows about one provider call in flight (spec/real-models R20.8, R27).

Two facts decide what a failed or stopped call costs on the durable runner, and neither can be
seen from outside the HTTP client:

- **sent**: the request was handed to the HTTP client on an open connection. Name lookup, connect
  and TLS come before it; a failure there sent nothing. The default transports of the provider
  adapters mark it when their connection is made (`net_guard.tracked_handlers`, and the guarded
  connections of `engine/net_guard.py`); an adapter with an injected transport marks it when it
  hands the request over (`mark_sent`), which is the careful reading: a transport that cannot
  say is taken to have sent.
- **cancelled**: a stop request was seen. `CallSignal.cancel` closes the call's connections; after
  it a request that was not sent yet is never sent (`mark_sent` refuses).

`CallControl.run` makes the provider call in a helper thread, so the step can return when a stop
cuts the call: it waits `close_grace` seconds (3) for the adapter to notice its closed connection,
then leaves the call behind (**abandoned**). At most `MAX_ABANDONED` (8) abandoned calls are
alive in one process; with none free the stop waits for its call to end. An abandoned call that
still gets an answer hands it to `late` (the existing late path of the model client).

Only the standard library is used here, and no HTTP client or socket module (the connection
classes live in `engine/net_guard.py`, the one place that opens connections); the signal
travels in a context variable, so no adapter
signature changes and a call outside a runner step sees no signal at all.
"""

import contextvars
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Final, TypeVar

_LOG = logging.getLogger(__name__)

T = TypeVar("T")

CLOSE_GRACE: Final = 3.0
"""Seconds a cut call gets to end before it is abandoned (R27: 2 s to see the stop, 3 to close)."""
MAX_ABANDONED: Final = 8
"""Abandoned calls that may be alive in one runner (R27)."""


class CallCancelled(Exception):  # noqa: N818 (named in the spec)
    """A stop cut the call (R27). `sent` says whether the request was handed to the HTTP client:
    false, the reservation was released; true, it was settled at the reserved amount as unknown.
    Fixed text: no provider text, no cause."""

    def __init__(self, sent: bool) -> None:
        super().__init__("the call was stopped " + ("after" if sent else "before") + " it was sent")
        self.sent = sent


class CancelledBeforeSend(Exception):  # noqa: N818 (a signal inside the call)
    """Raised in the adapter's thread when it tries to send after the call was cancelled."""

    def __init__(self) -> None:
        super().__init__("the call was stopped before it was sent")


class CallSignal:
    """The `sent` and `cancelled` facts of one call. Thread-safe; both only ever go to true."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sent = False
        self._cancelled = threading.Event()
        self._closers: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []

    @property
    def sent(self) -> bool:
        return self._sent

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def mark_sent(self) -> None:
        """The request is about to go out. Refused (`CancelledBeforeSend`) once cancelled, so
        `sent` cannot become true after a cancel was answered with "not sent"."""
        with self._lock:
            if self._cancelled.is_set():
                raise CancelledBeforeSend
            self._sent = True

    def attach(self, closer: Callable[[], None]) -> None:
        """Register how to close a connection of this call; closed at once if already cancelled."""
        with self._lock:
            if not self._cancelled.is_set():
                self._closers.append(closer)
                return
        _quietly(closer)

    def on_cancel(self, listener: Callable[[], None]) -> None:
        """Call `listener` when the call is cancelled (at once if it already is)."""
        with self._lock:
            if not self._cancelled.is_set():
                self._listeners.append(listener)
                return
        _quietly(listener)

    def cancel(self) -> bool:
        """Cancel the call and close its connections. Returns `sent` as it stands for good."""
        with self._lock:
            first = not self._cancelled.is_set()
            self._cancelled.set()
            sent = self._sent
            closers, self._closers = self._closers, []
            listeners, self._listeners = self._listeners, []
        if first:
            for act in (*closers, *listeners):
                _quietly(act)
        return sent

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`, cut short by a cancel (the retry wait of a key read, R38.10).
        True when the call was cancelled."""
        return self._cancelled.wait(seconds)


def _quietly(act: Callable[[], None]) -> None:
    try:
        act()
    except Exception:  # noqa: S110 (closing is best effort; the abandon bound still holds)
        pass


_CURRENT: contextvars.ContextVar[CallSignal | None] = contextvars.ContextVar(
    "nuramesh_call_signal", default=None
)


def current() -> CallSignal | None:
    """The signal of the provider call this thread is making, if a runner step makes it."""
    return _CURRENT.get()


def retry_wait(seconds: float) -> None:
    """The wait before the one retry of a stored-key read (`spec/real-models` R33, R38.10). In a
    runner's provider call a stop cuts it short and raises `CancelledBeforeSend`: the store is
    not asked again, no key is read and the call ends as "not sent" (released). Anywhere else
    (no call signal: the CLI path) it is a plain sleep."""
    signal = _CURRENT.get()
    if signal is None:
        time.sleep(seconds)
    elif signal.wait(seconds):
        raise CancelledBeforeSend


def mark_sent() -> None:
    """For an adapter with an injected transport: the request is handed over now."""
    signal = _CURRENT.get()
    if signal is not None:
        signal.mark_sent()


def on_connected(connection: Any) -> None:
    """For a connection class: the connection (and TLS) is made and the request goes out next.
    Registers the socket for a cancel and marks the call sent."""
    signal = _CURRENT.get()
    if signal is None:
        return
    sock = getattr(connection, "sock", None)
    if sock is not None:
        signal.attach(lambda: _close_socket(sock))
    signal.mark_sent()


_SHUT_RDWR: Final = 2  # socket.SHUT_RDWR on every platform (no socket import in this module)


def _close_socket(sock: Any) -> None:
    try:
        sock.shutdown(_SHUT_RDWR)  # wakes a thread blocked in recv on this socket
    except OSError:
        pass
    sock.close()


class _Slots:
    """The process-wide count of abandoned calls that are still alive."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.alive = 0

    def take(self, limit: int) -> bool:
        with self._lock:
            if self.alive >= limit:
                return False
            self.alive += 1
            return True

    def give(self) -> None:
        with self._lock:
            self.alive = max(0, self.alive - 1)


ABANDONED: Final = _Slots()


class _Box:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.wake = threading.Event()
        self.ok = False
        self.value: Any = None
        self.error: BaseException | None = None
        self.abandoned = False


class CallControl:
    """Runs one provider call so that a stop can cut it (module docstring). One per call."""

    def __init__(
        self,
        *,
        close_grace: float = CLOSE_GRACE,
        max_abandoned: int = MAX_ABANDONED,
        slots: _Slots = ABANDONED,
    ) -> None:
        self.signal = CallSignal()
        self.close_grace = close_grace
        self.max_abandoned = max_abandoned
        self._slots = slots
        self.abandoned = False
        """The call was left behind at the close bound; its thread may still be alive."""

    def run(self, call: Callable[[], T], *, late: Callable[[T], None]) -> T:
        """Make the call. Returns its answer (also when the answer arrived before a cut closed
        the connection), raises what it raised, or raises `CallCancelled` when a stop cut it.
        `late` gets the answer of an abandoned call, in the call's own thread."""
        signal = self.signal
        if signal.cancelled:  # stopped during the steps before the call: nothing is sent
            raise CallCancelled(signal.cancel())
        box = _Box()
        context = contextvars.copy_context()  # the fence and the other context of the step

        def body() -> None:
            _CURRENT.set(signal)
            try:
                box.value = call()
                box.ok = True
            except BaseException as exc:  # handed to the waiting step, or dropped if abandoned
                box.error = exc
            with box.lock:
                box.done.set()
                left = box.abandoned
            box.wake.set()
            if left:
                self._slots.give()
                if box.ok:
                    try:
                        late(box.value)
                    except Exception as exc:  # the late path records what it can
                        _LOG.warning("a late answer could not be recorded: %s", type(exc).__name__)

        signal.on_cancel(box.wake.set)
        thread = threading.Thread(
            target=lambda: context.run(body), name="nuramesh-model-call", daemon=True
        )
        thread.start()
        try:
            box.wake.wait()
            if not box.done.is_set() and not box.done.wait(self.close_grace):
                if not self._abandon(box):  # no slot left: as slow as before, never unbounded
                    box.done.wait()
        except BaseException:  # an interrupt of the waiting step: today's release path decides
            self._abandon(box, force=True)
            raise
        if box.abandoned:
            self.abandoned = True
            raise CallCancelled(signal.cancel())
        if box.ok:
            return box.value  # type: ignore[no-any-return]
        if signal.cancelled:
            raise CallCancelled(signal.sent)
        assert box.error is not None
        raise box.error

    def _abandon(self, box: _Box, *, force: bool = False) -> bool:
        """Leave the call behind. True when it just ended (nothing to abandon) or the call was left
        behind; False only when no slot is free."""
        with box.lock:
            if box.done.is_set():
                return True  # it ended: nothing to abandon, the caller reads the result
            if not self._slots.take(self.max_abandoned):
                if not force:
                    return False
                self._slots.take(self._slots.alive + 1)  # an interrupt cannot wait: counted too
            box.abandoned = True
            return True
