"""The seam: the one shared implementation of the adapter tool-call contract
. Every tool call of an adapted framework goes through
`ToolGuard.call` (sync) or `ToolGuard.acall` (async) before its body can start (AD-1).

One decision flow (`ToolGuard._flow`, a generator of steps) is run by two small drivers, so the
sync and the async entry points cannot differ in a rule (AD-50): the sync driver does each step
in place, the async driver moves the blocking ledger work to a worker thread and awaits the body
(AD-51). The order is AD-2: the local latch, readable name and arguments, the check (the frozen
set, the gate and the policy are decided by the party that writes the ledger, in one
transaction), the arguments still being the checked ones, the body exactly once, `tool.called`.

The seam holds no rule, no count and no decision: it asks its **transport** (embedded:
`nuramesh.engine.toolcalls.ToolLedger` through `WorkContext.tool_ledger`; the standalone
transport comes with #868) and fails closed on everything else (AD-58). It never logs, raises
or returns arguments, results or a rule's text (AD-65): errors carry a tool name and a reason
code only. No name here approves, rejects or decides anything (AD-64)."""

import asyncio
import contextvars
import threading
import time
from collections.abc import Awaitable, Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from nuramesh.core.policy import Action, Decision
from nuramesh.core.toolcalls import (
    ADAPTERS,
    DEADLINE_WAIT_SECONDS,
    FrozenToolSet,
    ManifestEntry,
    ReasonCode,
    ToolArguments,
    UnreadableArguments,
    UnreadableToolName,
    ask_text,
    call_id_for,
    check_deadline_seconds,
    check_tool_name,
    deny_text,
    freeze_manifest,
    read_arguments,
)
from nuramesh.engine.models import PolicyDenied
from nuramesh.engine.toolcalls import Answer, Wait

POLL_SECONDS = 0.05


class ToolStop(PolicyDenied):
    """A stop of the guarded run (AD-43): raised from the adapter's own entry point, so the
    task goes to a human and its result is never a submission. Carries the reason code only."""

    def __init__(self, code: ReasonCode) -> None:
        self.code = code
        super().__init__(deny_text(code))


class Transport(Protocol):
    """What the seam needs from the party that writes the ledger."""

    @property
    def deadline_seconds(self) -> int:
        """The approval deadline the ledger records (AD-46), in seconds."""
        ...

    def freeze(
        self, frozen: FrozenToolSet, *, framework: str, framework_version: str
    ) -> ReasonCode | None: ...

    def refuse_turn(self, code: ReasonCode) -> None: ...

    def check(
        self,
        tool: str,
        call_id: str,
        *,
        arguments: ToolArguments | None,
        tool_set_hash: str,
        schema: str | None = None,
        pre_deny: ReasonCode | None = None,
        waits: bool = True,
        action: Action | None = None,
        waiting_on: str | None = None,
    ) -> Answer: ...

    def waited(self, approval_id: str) -> "Answer | Wait": ...

    def called(self, tool: str, call_id: str, arguments: ToolArguments, outcome: str) -> None: ...


@dataclass(frozen=True)
class Seen:
    """What the framework gets for one tool call. `ran`: the body returned and `result` is its
    result, unchanged (AD-6). Otherwise `text` is the one fixed text for the model (AD-38), to
    be handed over in the framework's own refusal form, marked as an error (AD-39); `code` is
    the call's own reason code and `stop` the stop this call set or met, if any."""

    call_id: str
    ran: bool
    code: ReasonCode
    result: Any = None
    text: str | None = None
    approval_id: str | None = None
    stop: ReasonCode | None = None


class _Latch:
    """The first stop of one guarded run in this process (AD-44); safe under threads and tasks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._code: ReasonCode | None = None

    def set(self, code: ReasonCode | None) -> None:
        if code is None:
            return
        with self._lock:
            if self._code is None:
                self._code = code

    @property
    def code(self) -> ReasonCode | None:
        with self._lock:
            return self._code


# steps of the one decision flow
@dataclass(frozen=True)
class _Io:
    """Blocking ledger work (the sync driver calls it; the async driver uses a thread)."""

    work: Callable[[], Any]
    shielded: bool = False  # AD-56: completes before a cancellation is re-raised


@dataclass(frozen=True)
class _Sleep:
    seconds: float


@dataclass(frozen=True)
class _Body:
    arguments: Any


_Step = _Io | _Sleep | _Body
_current: contextvars.ContextVar["ToolGuard | None"] = contextvars.ContextVar(
    "nuramesh_tool_guard", default=None
)


def current_guard() -> "ToolGuard | None":
    """The guard of the guarded run this context belongs to (AD-57: a context variable, never a
    module global)."""
    return _current.get()


class ToolGuard:
    """The seam for one guarded run: the manifest, the latch and the transport travel with this
    object (AD-20, AD-57). `wait` is the wait mode of AD-45: seconds a call waits for a person
    (also the approval's deadline, AD-46: the ledger records the deadline, the agent's process
    cannot extend it, so a `wait` that differs from the transport's `deadline_seconds` is refused),
    or `None` for an `ask` that is returned at once (`ask_pending`). No option skips a step or
    widens anything (AD-69)."""

    def __init__(
        self,
        transport: Transport,
        *,
        adapter: str,
        wait: int | None = DEADLINE_WAIT_SECONDS,
        poll: float = POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        asleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if adapter not in ADAPTERS:
            raise ValueError("unknown adapter")
        self._wait = None if wait is None else check_deadline_seconds(wait)
        if self._wait is not None and self._wait != transport.deadline_seconds:
            # AD-46: the approval's deadline is recorded where the ledger is written; a wait that
            # differs would end the call before the approval expires (it stays answerable and
            # consumable by a later identical call) or outlast it
            raise ValueError("wait must equal the transport's approval deadline_seconds (AD-46)")
        self._transport, self._adapter = transport, adapter
        self._poll, self._clock, self._sleep = poll, clock, sleep
        self._asleep = asleep
        self._latch = _Latch()
        self._frozen: FrozenToolSet | None = None

    # -- the latch and the stop -------------------------------------------------------------

    @property
    def stopped(self) -> ReasonCode | None:
        """The first stop of this guarded run in this process, or `None` (AD-44)."""
        return self._latch.code

    def raise_if_stopped(self) -> None:
        """Raise the stop from the adapter's entry point (AD-43 d), and before every model call
        through NuraMesh (AD-44). Also when the framework swallowed the original exception."""
        code = self._latch.code
        if code is not None:
            raise ToolStop(code)

    @contextmanager
    def active(self) -> Iterator["ToolGuard"]:
        """Make this guard the context's guard for the guarded run's duration (AD-57)."""
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    # -- the frozen tool set ----------------------------------------------------------------

    def freeze(
        self, entries: Iterable[ManifestEntry], *, framework: str, framework_version: str
    ) -> FrozenToolSet:
        """Freeze the manifest before the first model call (AD-14 to AD-16, AD-19). A manifest
        that cannot be frozen raises a `ValueError` naming the tool and writes nothing (AD-15);
        a failed write propagates and the run does not start (F5); a second freeze with another
        hash is the stop `tool_set_changed`."""
        _no_running_loop("afreeze")
        return self._frozen_as(entries, framework, framework_version)

    async def afreeze(
        self, entries: Iterable[ManifestEntry], *, framework: str, framework_version: str
    ) -> FrozenToolSet:
        """`freeze` for an async entry point: the write runs on a worker thread (AD-51)."""
        listed = list(entries)
        return await asyncio.to_thread(self._frozen_as, listed, framework, framework_version)

    def _frozen_as(
        self, entries: Iterable[ManifestEntry], framework: str, framework_version: str
    ) -> FrozenToolSet:
        frozen = freeze_manifest(entries)
        refused = self._transport.freeze(
            frozen, framework=framework, framework_version=framework_version
        )
        if refused is not None:
            self._latch.set(refused)
            raise ToolStop(refused)
        self._frozen = frozen
        return frozen

    def check_turn(self, offered: Iterable[ManifestEntry]) -> None:
        """Compare the tools a model turn offers with the frozen set, before that model call
        (AD-17, AD-18): a tool outside the manifest or with another schema is `task.refused`
        and a stop; offering fewer tools is allowed. Raises the stop."""
        self.raise_if_stopped()
        frozen = self._require_frozen()
        refusal: ReasonCode | None = None
        for entry in offered:
            if entry.name not in frozen:
                refusal = ReasonCode.TOOL_SET_CHANGED
                break
            if frozen.schema_of(entry.name) != entry.schema:
                refusal = refusal or ReasonCode.TOOL_REPLACED
        if refusal is None:
            return
        self.refuse(refusal)
        raise ToolStop(refusal)

    def refuse(self, code: ReasonCode) -> None:
        """Latch a stop that has no call to decide and record it as one `task.refused` with the
        fixed `code` (`refuse_turn`): a tool result without a valid run token, a final turn's
        tool calls left unanswered, a tool message that answers no call (#971, LG-1, LG-3). The
        stop holds whether or not the write succeeded; the caller raises. First caller of
        `refuse_turn` outside `check_turn`."""
        self._latch.set(code)
        try:
            self._transport.refuse_turn(code)
        except Exception:  # noqa: BLE001, S110 (the stop holds whether or not it was recorded)
            pass

    @property
    def tool_set_hash(self) -> str | None:
        """The frozen tool set's hash, or `None` before the freeze."""
        return None if self._frozen is None else self._frozen.tool_set_hash

    def _require_frozen(self) -> FrozenToolSet:
        if self._frozen is None:
            raise RuntimeError("the tool set of this guarded run was not frozen (AD-16)")
        return self._frozen

    # -- one tool call ----------------------------------------------------------------------

    def call(
        self,
        name: object,
        arguments: object,
        body: Callable[[Any], Any],
        *,
        call_id: str | None = None,
        schema: str | None = None,
        action: Action | None = None,
    ) -> Seen:
        """One tool call, sync (AD-2). `body` gets the checked arguments and runs at most once;
        its own error propagates unchanged after it was recorded (AD-6, AD-28). `schema` is the
        tool's `schema_hash` now (AD-18); `action` replaces `use_tool:<name>` for a handoff
        (AD-13). Called from a running event loop it raises `RuntimeError` (AD-52)."""
        _no_running_loop("acall")
        flow = self._flow(name, arguments, call_id, schema, action)
        sent: Any = None
        thrown: BaseException | None = None
        while True:
            try:
                step = flow.throw(thrown) if thrown is not None else flow.send(sent)
            except StopIteration as done:
                return done.value  # type: ignore[no-any-return]
            sent, thrown = None, None
            try:
                if isinstance(step, _Io):
                    sent = step.work()
                elif isinstance(step, _Sleep):
                    self._sleep(step.seconds)
                else:
                    sent = body(step.arguments)
            except BaseException as exc:  # noqa: BLE001 (handed to the flow, which decides)
                thrown = exc

    async def acall(
        self,
        name: object,
        arguments: object,
        body: Callable[[Any], Awaitable[Any]],
        *,
        call_id: str | None = None,
        schema: str | None = None,
        action: Action | None = None,
    ) -> Seen:
        """One tool call, async: the same flow as `call`. Ledger work runs on a worker thread,
        so the event loop is never blocked (AD-51); a cancellation propagates unchanged and is
        never a denial (AD-55); `tool.called` of a cancelled body is still written (AD-56)."""
        flow = self._flow(name, arguments, call_id, schema, action)
        sent: Any = None
        thrown: BaseException | None = None
        while True:
            try:
                step = flow.throw(thrown) if thrown is not None else flow.send(sent)
            except StopIteration as done:
                return done.value  # type: ignore[no-any-return]
            sent, thrown = None, None
            try:
                if isinstance(step, _Io):
                    work = asyncio.ensure_future(asyncio.to_thread(step.work))
                    if not step.shielded:
                        sent = await work
                    else:
                        try:
                            sent = await asyncio.shield(work)
                        except asyncio.CancelledError:
                            await asyncio.wait([work])  # the record completes first (AD-56)
                            work.result()  # a failed write is F6, raised into the flow
                            raise
                elif isinstance(step, _Sleep):
                    await self._asleep(step.seconds)
                else:
                    sent = await body(step.arguments)
            except BaseException as exc:  # noqa: BLE001 (handed to the flow, which decides)
                thrown = exc

    def _denied(
        self,
        call_id: str,
        code: ReasonCode,
        stop: ReasonCode | None = None,
        approval_id: str | None = None,
    ) -> Seen:
        self._latch.set(stop)
        return Seen(call_id, False, code, None, deny_text(code), approval_id, stop)

    def _flow(
        self,
        name: object,
        arguments: object,
        framework_id: str | None,
        schema: str | None,
        action: Action | None,
    ) -> Generator[_Step, Any, Seen]:
        """The one decision flow of a tool call (AD-2). Yields the blocking steps; returns what
        the framework sees. Cancellation (a `BaseException` that is not an `Exception`) is
        never caught before the body (AD-55)."""
        transport, stop = self._transport, ReasonCode.STOPPED
        call_id = "c-unreadable"
        if self._latch.code is not None:  # step 1: no check is sent, no entry is written
            return self._denied(_safe_id(framework_id), stop)
        tool = "unreadable"
        try:
            frozen = self._require_frozen()
            call_id = call_id_for(framework_id)
            # step 2: a readable name and readable arguments
            pre: ReasonCode | None = None
            checked: ToolArguments | None = None
            try:
                tool = check_tool_name(name)
            except UnreadableToolName:
                pre = ReasonCode.UNREADABLE_TOOL_NAME
            if pre is None:
                try:
                    checked = read_arguments(arguments)
                except UnreadableArguments:
                    pre = ReasonCode.UNREADABLE_ARGUMENTS
            waiting_on: str | None = None
            waited_until = None
            waited_for: str | None = None
            while True:
                # steps 3 to 5, decided where the ledger is written, in one transaction
                try:
                    answer: Answer = yield _Io(
                        partial(
                            transport.check,
                            tool,
                            call_id,
                            arguments=checked,
                            tool_set_hash=frozen.tool_set_hash,
                            schema=schema,
                            pre_deny=pre,
                            waits=self._wait is not None,
                            action=action,
                            waiting_on=waiting_on,
                        )
                    )
                except Exception:  # noqa: BLE001 (F4: the check's transaction failed)
                    lost = ReasonCode.LEDGER_UNAVAILABLE
                    return self._denied(call_id, lost, lost)
                if answer.decision is Decision.ALLOW:
                    break
                if answer.decision is Decision.DENY:
                    return self._denied(call_id, answer.code, answer.stop, answer.approval_id)
                approval_id = answer.approval_id
                if self._wait is None or approval_id is None:  # AD-45: returned, not awaited
                    self._latch.set(answer.stop)
                    return Seen(
                        call_id,
                        False,
                        ReasonCode.ASK_PENDING,
                        None,
                        ask_text(approval_id or "unknown"),
                        approval_id,
                        answer.stop,
                    )
                # a pause in wait mode (section 10): only this call is held. The wait clock
                # starts after the check that opened the approval and restarts for a new one
                # (AD-45, AD-48 row 5), so it never ends before the store's own deadline.
                if approval_id != waited_for:
                    waited_for = approval_id
                    waited_until = self._clock() + self._wait
                while True:
                    if self._latch.code is not None:  # AD-49: a stop ends the wait
                        return self._denied(call_id, stop, None, approval_id)
                    try:
                        outcome: Answer | Wait = yield _Io(partial(transport.waited, approval_id))
                    except Exception:  # noqa: BLE001 (AD-59: unreachable means deny)
                        lost = ReasonCode.LEDGER_UNAVAILABLE
                        return self._denied(call_id, lost, lost)
                    if isinstance(outcome, Answer):
                        return self._denied(call_id, outcome.code, outcome.stop, approval_id)
                    if outcome is Wait.RECHECK:  # AD-47: only a consumed allow releases
                        waiting_on = approval_id
                        break
                    if waited_until is not None and self._clock() >= waited_until:
                        # the local clock may end the pause only after one more read of the store,
                        # which records the expiry if the store's deadline has passed (AD-46)
                        try:
                            last: Answer | Wait = yield _Io(partial(transport.waited, approval_id))
                        except Exception:  # noqa: BLE001 (AD-59: unreachable means deny)
                            lost = ReasonCode.LEDGER_UNAVAILABLE
                            return self._denied(call_id, lost, lost)
                        if isinstance(last, Answer):
                            return self._denied(call_id, last.code, last.stop, approval_id)
                        if last is Wait.RECHECK:
                            waiting_on = approval_id
                            break
                        # still pending: no answer is a deny, also on the waiting side's own clock
                        return self._denied(call_id, ReasonCode.APPROVAL_TIMEOUT, None, approval_id)
                    yield _Sleep(self._poll)
            if checked is None:  # an allow without readable arguments cannot exist
                raise RuntimeError("allow without arguments")
            # step 6: what was checked is what runs (AD-11)
            passed = arguments if isinstance(arguments, dict) else checked.value
            try:
                same = read_arguments(passed).digest == checked.digest
            except UnreadableArguments:
                same = False
            if not same:
                changed = ReasonCode.ARGUMENTS_CHANGED
                try:
                    answer = yield _Io(
                        lambda: transport.check(
                            tool,
                            call_id,
                            arguments=checked,
                            tool_set_hash=frozen.tool_set_hash,
                            schema=schema,
                            pre_deny=changed,
                        )
                    )
                except Exception:  # noqa: BLE001 (a deny that could not be recorded is a deny)
                    return self._denied(call_id, changed, changed)
                return self._denied(call_id, answer.code, answer.stop or changed)
            if self._latch.code is not None:  # AD-53: a sibling's stop refuses an unstarted body
                return self._denied(call_id, stop)
        except Exception:  # noqa: BLE001 (F1: anything unexpected before the body)
            failed = ReasonCode.ADAPTER_ERROR
            self._latch.set(failed)
            try:  # best effort: one entry, if it can be written
                yield _Io(
                    lambda: transport.check(
                        tool,
                        call_id,
                        arguments=None,
                        tool_set_hash=(self._frozen or _NO_SET).tool_set_hash,
                        pre_deny=failed,
                    )
                )
            except Exception:  # noqa: BLE001, S110
                pass
            return self._denied(call_id, failed, failed)
        # step 7: the body, exactly once (AD-4); step 8: the record (AD-28)
        record = "ok"
        result: Any = None
        error: BaseException | None = None
        try:
            # the body gets the rebuilt copy (AD-11), never the caller's own dict: the caller
            # could change that between the re-read above and the body (TOCTOU)
            result = yield _Body(checked.value)
        except Exception as exc:  # noqa: BLE001 (the body's own error, handed on unchanged)
            record, error = "error", exc
        except BaseException as exc:  # noqa: BLE001 (a cancellation while the body ran)
            record, error = "cancelled", exc
        try:
            yield _Io(lambda: transport.called(tool, call_id, checked, record), shielded=True)
        except Exception:  # noqa: BLE001 (F6: the result is withheld, AD-60)
            if record == "cancelled" and error is not None:
                self._latch.set(ReasonCode.LEDGER_UNAVAILABLE)
                raise error from None
            lost = ReasonCode.LEDGER_UNAVAILABLE
            return self._denied(call_id, lost, lost)
        if error is not None:
            raise error
        return Seen(call_id, True, ReasonCode.ALLOW, result)


_NO_SET = FrozenToolSet((), "0" * 64)


def _safe_id(framework_id: object) -> str:
    try:
        return call_id_for(framework_id)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return "c-unreadable"


def _no_running_loop(async_name: str) -> None:
    """AD-52: a sync entry point called from a running event loop is refused."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"a sync entry point was called inside a running event loop; use `{async_name}`"
    )


__all__ = [
    "POLL_SECONDS",
    "Seen",
    "ToolGuard",
    "ToolStop",
    "Transport",
    "current_guard",
]
