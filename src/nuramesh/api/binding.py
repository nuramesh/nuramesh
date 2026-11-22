"""The role binding of a standalone agent (#868): `bind(team, role)` and `RoleBinding`.

A standalone agent (a LangChain app, a CrewAI crew, an OpenAI or Claude SDK agent, a plain
script) talks to the NuraMesh server over HTTP as an agent (`nuramesh.api._http`). It never
holds a store, a team folder or a policy file: the server checks the team's rules for the role,
writes the ledger row with the actor its credential names, opens the approval request and keeps
it; a person decides in the app or through the API. Nothing in this module can record a
decision, and a framework's own "resume" payload is never an input: `recheck` asks the server
again (section 6 of `docs/python-api.md`).

Every refusal is a returned `Outcome`; raising (`raise_on_deny=True`) is opt-in. Anything that
goes wrong on the way (the server unreachable or answering badly, an argument that cannot be
read, an unknown kind, the deadline passing) is a deny: fail closed.

The request flow is written once as a generator of steps (`_check_flow`, `_recheck_flow`) and
driven by a sync and an async driver, so both paths send byte-identical bodies (tested).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
import weakref
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from pydantic import ValidationError

from nuramesh.api._http import Answer, HttpSession, Request, load_credential, server_url
from nuramesh.core.policy import Action, ActionKind, Decision
from nuramesh.errors import (
    ApprovalRequired,
    ApprovalTimeout,
    PolicyDenied,
    ServerUnavailable,
)

__all__ = ["Outcome", "RoleBinding", "bind"]

ACTIONS_PATH: Final = "/v1/agents/self/actions"
APPROVAL_PATH: Final = "/v1/agents/self/approvals/{approval_id}"
_APPROVAL_ID_SHAPE: Final = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
"""An approval id is one path segment of plain characters; anything else (a slash, `?`, `#`,
`%`, a space, `.`/`..`) is never sent: it is a missing approval, so the call is denied."""
SESSIONS_PATH: Final = "/v1/agents/self/sessions"
SERVER_LONG_POLL: Final = 30.0
"""The most a server long-poll waits per request; the client loops until its own deadline."""
RETRY_WITHOUT_WAIT: Final = 2.0
"""Without a wait, a 429 is retried once if the server asks for at most this many seconds."""

# Reason codes an adapter may key on (stable strings; `docs/python-api.md`).
ALLOW: Final = "allow"
ASK_PENDING: Final = "ask_pending"
DENIED_BY_RULE: Final = "denied_by_rule"
REJECTED_BY_PERSON: Final = "rejected_by_person"
APPROVAL_TIMEOUT: Final = "approval_timeout"
APPROVAL_EXPIRED: Final = "approval_expired"
SERVER_UNAVAILABLE: Final = "server_unavailable"
ROLE_MISMATCH: Final = "role_mismatch"
UNREADABLE_ARGUMENTS: Final = "unreadable_arguments"
UNKNOWN_ACTION_KIND: Final = "unknown_action_kind"
CREDENTIAL_REJECTED: Final = "credential_rejected"


@dataclass(frozen=True)
class Outcome:
    """What the team's rules say about one call: `allow`, `ask` (a person must approve;
    `approval_id` is the open request) or `deny`. `reason` is the rule's own words (for logs and
    people); `agent_message` is the fixed text an adapter hands to the model (never content)."""

    decision: Decision
    reason: str
    reason_code: str
    approval_id: str | None
    call_id: str

    @property
    def allowed(self) -> bool:
        """True only for `allow`: the tool may run now, exactly once."""
        return self.decision is Decision.ALLOW

    @property
    def agent_message(self) -> str:
        """Fixed text for the model: empty on allow, otherwise why nothing was done."""
        if self.decision is Decision.ALLOW:
            return ""
        if self.decision is Decision.ASK:
            return f"Not done: this action needs a person's approval (approval {self.approval_id})."
        return f"Blocked by the team's rules: {self.reason_code}."


@dataclass(frozen=True)
class _Sleep:
    seconds: float


_Step = Request | _Sleep
_Flow = Generator[_Step, Answer | None, Outcome]
_T = TypeVar("_T")


def _deny(code: str, reason: str, call_id: str, approval_id: str | None = None) -> Outcome:
    return Outcome(Decision.DENY, reason, code, approval_id, call_id)


def _raise_for(outcome: Outcome) -> Outcome:
    if outcome.decision is Decision.ALLOW:
        return outcome
    if outcome.decision is Decision.ASK:
        raise ApprovalRequired(outcome)
    if outcome.reason_code == APPROVAL_TIMEOUT:
        raise ApprovalTimeout(outcome)
    if outcome.reason_code == SERVER_UNAVAILABLE:
        raise ServerUnavailable(outcome)
    raise PolicyDenied(outcome)


def _args_hash(args: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the tool's arguments (sorted keys, no spaces); the
    arguments themselves never leave the process."""
    canonical = json.dumps(
        args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RoleBinding:
    """A standalone agent bound to one role of one team, over HTTP with its agent credential.
    Made by `bind(...)`; thread-safe (parallel tool calls each carry their own `call_id`)."""

    def __init__(
        self,
        team: str,
        role: str,
        *,
        server: str,
        credential: str,
        run: str | None,
        task: str | None,
        transport: Any,
    ) -> None:
        self.team = team
        self.role = role
        self.server = server
        self.run_id = run
        self.task_id = task
        self.agent_id: str | None = None
        """The agent the server says the credential belongs to (after the first answer)."""
        self._http = HttpSession(server, credential, transport=transport)
        self._session_lock = threading.Lock()
        self._async_locks_guard = threading.Lock()
        self._async_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._poll_interval = 1.0  # pacing when a server answers "pending" at once

    # -- public ---------------------------------------------------------------------------------

    def check_action(
        self,
        kind: ActionKind | str,
        target: str,
        *,
        call_id: str | None = None,
        args: Mapping[str, Any] | None = None,
        wait: float | None = None,
        raise_on_deny: bool = False,
    ) -> Outcome:
        """Check one action against the role's rules and record it. `wait=None` returns an
        `ask` at once (the adapter pauses its own way); `wait=seconds` waits for a person's
        decision up to the deadline, then denies (`approval_timeout`; `wait=0` means "only if
        already approved"). With `raise_on_deny` a non-allow raises `PolicyDenied`."""
        prepared = self._prepare(kind, target, call_id, args)
        if isinstance(prepared, Outcome):
            return _raise_for(prepared) if raise_on_deny else prepared
        body, call = prepared
        outcome = self._drive(self._check_flow(body, call, wait), call)
        return _raise_for(outcome) if raise_on_deny else outcome

    async def acheck_action(
        self,
        kind: ActionKind | str,
        target: str,
        *,
        call_id: str | None = None,
        args: Mapping[str, Any] | None = None,
        wait: float | None = None,
        raise_on_deny: bool = False,
    ) -> Outcome:
        """`check_action` for async code (same contract, same request bodies)."""
        prepared = self._prepare(kind, target, call_id, args)
        if isinstance(prepared, Outcome):
            return _raise_for(prepared) if raise_on_deny else prepared
        body, call = prepared
        outcome = await self._adrive(self._check_flow(body, call, wait), call)
        return _raise_for(outcome) if raise_on_deny else outcome

    def recheck(
        self,
        approval_id: str,
        *,
        kind: ActionKind | str,
        target: str,
        call_id: str | None = None,
        raise_on_deny: bool = False,
    ) -> Outcome:
        """The resume path: a framework woke the agent up saying a call may go on. Nothing in
        that payload is trusted; the server's recorded decision is asked for again. Approved
        -> `allow` (the approval is consumed, once); pending -> `ask`; rejected, expired, consumed,
        unknown or unreachable -> deny."""
        prepared = self._prepare(kind, target, call_id, None)
        if isinstance(prepared, Outcome):
            return _raise_for(prepared) if raise_on_deny else prepared
        body, call = prepared
        outcome = self._drive(self._recheck_flow(approval_id, body, call), call)
        return _raise_for(outcome) if raise_on_deny else outcome

    async def arecheck(
        self,
        approval_id: str,
        *,
        kind: ActionKind | str,
        target: str,
        call_id: str | None = None,
        raise_on_deny: bool = False,
    ) -> Outcome:
        """`recheck` for async code."""
        prepared = self._prepare(kind, target, call_id, None)
        if isinstance(prepared, Outcome):
            return _raise_for(prepared) if raise_on_deny else prepared
        body, call = prepared
        outcome = await self._adrive(self._recheck_flow(approval_id, body, call), call)
        return _raise_for(outcome) if raise_on_deny else outcome

    def close(self) -> None:
        """Close the HTTP connections (the binding can still be used; they are reopened)."""
        self._http.close()

    async def aclose(self) -> None:
        """Close the async HTTP connections of the current event loop."""
        await self._http.aclose()

    def __enter__(self) -> RoleBinding:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"RoleBinding(team={self.team!r}, role={self.role!r}, server={self.server!r})"

    # -- preparing a call (client-side refusals; nothing is sent) --------------------------------

    def _prepare(
        self,
        kind: ActionKind | str,
        target: Any,
        call_id: str | None,
        args: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], str] | Outcome:
        call = call_id if isinstance(call_id, str) and call_id else f"call-{uuid.uuid4().hex[:16]}"
        try:
            kind_value = ActionKind(kind)
        except ValueError:
            return _deny(UNKNOWN_ACTION_KIND, f"unknown action kind {str(kind)[:40]!r}", call)
        if not isinstance(target, str):
            return _deny(UNREADABLE_ARGUMENTS, "the target is not text", call)
        try:
            Action(kind=kind_value, target=target)  # the core's own shape rules (length, spend)
        except (ValidationError, ValueError):
            return _deny(UNREADABLE_ARGUMENTS, "the target is not a valid policy target", call)
        body: dict[str, Any] = {
            "team": self.team,
            "role": self.role,
            "kind": kind_value.value,
            "target": target,
            "call_id": call,
        }
        if args is not None:
            try:
                body["args_hash"] = _args_hash(args)
            except (TypeError, ValueError):
                return _deny(UNREADABLE_ARGUMENTS, "the arguments cannot be serialised", call)
        return body, call

    # -- the flows (one generator each; sync and async drivers below) ----------------------------

    def _check_flow(self, body: dict[str, Any], call: str, wait: float | None) -> _Flow:
        body = {**body, "run_id": self.run_id, "task_id": self.task_id}
        deadline = None if wait is None else time.monotonic() + max(0.0, wait)

        def left() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        first = dict(body)
        remaining = left()
        if remaining:
            first["wait_seconds"] = round(min(SERVER_LONG_POLL, remaining), 3)
        outcome = yield from self._post_flow(first, call, left)
        if outcome.decision is not Decision.ASK or deadline is None:
            return outcome
        approval_id = outcome.approval_id
        while True:
            remaining = left()
            if remaining is None or remaining <= 0 or approval_id is None:
                return _deny(APPROVAL_TIMEOUT, "no decision by the deadline", call, approval_id)
            status = yield from self._status_flow(approval_id, min(SERVER_LONG_POLL, remaining))
            if status == "pending":
                yield _Sleep(min(self._poll_interval, remaining))
                continue
            if status == "approved":
                outcome = yield from self._post_flow(body, call, left)  # consumes the approval
                if outcome.decision is Decision.ASK:
                    approval_id = outcome.approval_id  # another rule asks: keep waiting
                    continue
                return outcome
            return self._closed(status, call, approval_id)

    def _recheck_flow(self, approval_id: str, body: dict[str, Any], call: str) -> _Flow:
        status = yield from self._status_flow(approval_id, 0.0)
        if status == "pending":
            return Outcome(
                Decision.ASK, "a person has not decided yet", ASK_PENDING, approval_id, call
            )
        if status == "approved":
            body = {**body, "run_id": self.run_id, "task_id": self.task_id}
            return (yield from self._post_flow(body, call, lambda: None))
        return self._closed(status, call, approval_id)

    @staticmethod
    def _closed(status: str, call: str, approval_id: str | None) -> Outcome:
        if status == "denied":
            return _deny(REJECTED_BY_PERSON, "a person rejected the request", call, approval_id)
        if status in ("expired", "consumed", "missing"):
            return _deny(APPROVAL_EXPIRED, f"the request is {status}", call, approval_id)
        return _deny(SERVER_UNAVAILABLE, "the server gave no usable answer", call, approval_id)

    def _session_flow(self, call: str) -> Generator[_Step, Answer | None, Outcome | None]:
        """Open the binding's session run on first use (`run=` at `bind` time skips it)."""
        if self.run_id is not None:
            return None
        answer = yield Request("POST", SESSIONS_PATH, json={})
        body = answer.body if answer is not None else None
        if (
            answer is None
            or answer.failed
            or not answer.spoke_nuramesh
            or answer.status != 201
            or not isinstance(body, dict)
            or not isinstance(body.get("run_id"), str)
            or not isinstance(body.get("task_id"), str)
        ):
            return self._refusal(answer, call, None)
        if self.run_id is None:
            self.task_id = body["task_id"]  # task_id first: run_id is the flag the fast path reads
            self.run_id = body["run_id"]
        return None

    def _post_flow(
        self, body: dict[str, Any], call: str, left: Any
    ) -> Generator[_Step, Answer | None, Outcome]:
        timeout = 10.0 + float(body.get("wait_seconds", 0.0))
        answer = yield Request("POST", ACTIONS_PATH, json=body, timeout=timeout)
        if (
            answer is not None
            and answer.spoke_nuramesh
            and answer.status == 429
            and answer.retry_after is not None
        ):
            remaining = left()
            budget = RETRY_WITHOUT_WAIT if remaining is None else remaining
            if answer.retry_after <= budget:
                yield _Sleep(answer.retry_after)
                answer = yield Request("POST", ACTIONS_PATH, json=body, timeout=timeout)
        return self._outcome(answer, call)

    def _status_flow(self, approval_id: str, wait: float) -> Generator[_Step, Answer | None, str]:
        if not isinstance(approval_id, str) or not _APPROVAL_ID_SHAPE.fullmatch(approval_id):
            return "missing"
        answer = yield Request(
            "GET",
            APPROVAL_PATH.format(approval_id=approval_id),
            params={"wait": int(wait)},
            timeout=10.0 + wait,
        )
        if answer is None or answer.failed or not answer.spoke_nuramesh:
            return "unavailable"
        if answer.status == 404:
            return "missing"
        body = answer.body
        if answer.status == 200 and isinstance(body, dict):
            status = body.get("status")
            if status in ("pending", "approved", "denied", "consumed", "expired"):
                return str(status)
        return "unavailable"

    # -- mapping answers -------------------------------------------------------------------------

    def _outcome(self, answer: Answer | None, call: str) -> Outcome:
        if answer is None or answer.failed or not answer.spoke_nuramesh:
            return _deny(SERVER_UNAVAILABLE, "the server could not be reached", call)
        body = answer.body
        if answer.status == 200 and isinstance(body, dict):
            raw = body.get("decision")
            try:
                decision = Decision(raw if isinstance(raw, str) else "")
            except ValueError:
                return _deny(SERVER_UNAVAILABLE, "the server gave an unknown decision", call)
            approval = body.get("approval_id")
            reason = body.get("reason")
            code = body.get("reason_code")
            agent = body.get("agent_id")
            if isinstance(agent, str) and agent:
                self.agent_id = agent
            defaults = {
                Decision.ALLOW: ALLOW,
                Decision.ASK: ASK_PENDING,
                Decision.DENY: DENIED_BY_RULE,
            }
            return Outcome(
                decision,
                reason if isinstance(reason, str) else "",
                code if isinstance(code, str) and code else defaults[decision],
                approval if isinstance(approval, str) else None,
                call,
            )
        return self._refusal(answer, call, None)

    @staticmethod
    def _refusal(answer: Answer | None, call: str, approval_id: str | None) -> Outcome:
        """A non-200 answer as a deny with the server's problem code where one exists."""
        if answer is None or answer.failed or not answer.spoke_nuramesh:
            return _deny(SERVER_UNAVAILABLE, "the server could not be reached", call, approval_id)
        detail = ""
        if isinstance(answer.body, dict) and isinstance(answer.body.get("detail"), str):
            detail = answer.body["detail"]
        code = answer.code
        if answer.status == 401:
            return _deny(CREDENTIAL_REJECTED, detail or "credential rejected", call, approval_id)
        if answer.status == 403 and code == "agent_role_mismatch":
            return _deny(ROLE_MISMATCH, detail or "another team or role", call, approval_id)
        if answer.status in (403, 404, 409, 422) and code:
            return _deny(code, detail or code, call, approval_id)
        return _deny(SERVER_UNAVAILABLE, detail or f"HTTP {answer.status}", call, approval_id)

    # -- drivers ---------------------------------------------------------------------------------

    def _drive(self, flow: _Flow, call: str) -> Outcome:
        """Run a flow: the session run first (once per binding, under a lock so parallel first
        calls open one session), then the flow itself with no lock held."""
        if self.run_id is None:
            with self._session_lock:
                if self.run_id is None:
                    failed = self._pump(self._session_flow(call))
                    if failed is not None:
                        return failed
        return self._pump(flow)

    async def _adrive(self, flow: _Flow, call: str) -> Outcome:
        """`_drive` for async code: one lock per event loop around the session step only, so
        parallel first calls of one loop open one session."""
        if self.run_id is None:
            async with self._async_session_lock():
                if self.run_id is None:
                    failed = await self._apump(self._session_flow(call))
                    if failed is not None:
                        return failed
        return await self._apump(flow)

    def _async_session_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._async_locks_guard:
            lock = self._async_locks.get(loop)
            if lock is None:
                lock = self._async_locks[loop] = asyncio.Lock()
            return lock

    def _pump(self, flow: Generator[_Step, Answer | None, _T]) -> _T:
        try:
            step = next(flow)
            while True:
                answer = None
                if isinstance(step, _Sleep):
                    time.sleep(step.seconds)
                else:
                    answer = self._http.send(step)
                step = flow.send(answer)
        except StopIteration as stop:
            result: _T = stop.value
            return result

    async def _apump(self, flow: Generator[_Step, Answer | None, _T]) -> _T:
        try:
            step = next(flow)
            while True:
                answer = None
                if isinstance(step, _Sleep):
                    await asyncio.sleep(step.seconds)
                else:
                    answer = await self._http.asend(step)
                step = flow.send(answer)
        except StopIteration as stop:
            result: _T = stop.value
            return result


def bind(
    team: str,
    role: str,
    *,
    server: str | None = None,
    run: str | None = None,
    task: str | None = None,
    transport: Any | None = None,
) -> RoleBinding:
    """Bind a standalone agent to `role` of `team` (the organization id and role id of the
    team's `nuramesh.yaml`). The agent credential comes from `NURAMESH_AGENT_CREDENTIAL` (or
    `..._FILE`), the server from `server` or `NURAMESH_URL`. `run` and `task` name the run the
    agent takes part in; without them the binding opens a session run on first use. `transport`
    is an `httpx` transport (tests, custom proxies)."""
    for name, value in (("team", team), ("role", role)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if (run is None) != (task is None):
        raise ValueError("run and task go together: pass both or neither")
    credential = load_credential()
    url = server_url(server)
    return RoleBinding(
        team.strip(),
        role.strip(),
        server=url,
        credential=credential,
        run=run,
        task=task,
        transport=transport,
    )
