"""Run a LangGraph graph as a NuraMesh worker (R1.16; install with `nuramesh[langgraph]`).

NuraMesh is the trust layer around the agent, not the agent framework (a hard rule): the team keeps
building agents in LangGraph, and this adapter plugs a compiled graph into the run engine as one
agent's `Worker`. The run engine orchestrates the organization (delegation, gates, budgets,
retries); each agent's work can be a LangGraph graph (T29, T31).

What the adapter guarantees:

- **Model calls through the provided model go through NuraMesh.** The graph gets a chat model
  (`NuraMeshChatModel`, a LangChain `BaseChatModel`) bound to the task's `WorkContext`, so each
  call, sync or async, streamed or batched, is policy-checked, priced, reserved against the
  budgets, settled and recorded as the task's assignee, with the assignee's model. Unsupported
  inputs are refused, never silently changed: non-text content and extra call arguments
  (`max_tokens`, `temperature`, `tool_choice`...) raise `ValueError`.
- **Tool-bound agents and tool-call messages (S4, #971).** `nuramesh_tool_model(model, tools)` and
  `nuramesh_tool_node(model, tools)` together make a tool-using agent: tool definitions and
  tool-call/tool-result messages travel inside the NuraMesh model call as counted text (a fixed
  JSON envelope, so the reservation covers every byte, AD-36); the model's reply in that envelope
  becomes `AIMessage.tool_calls` (arguments that are not one JSON object become
  `invalid_tool_calls`, never executed). The tool node is the only way a bound tool runs: each call
  goes through the shared seam (`adapters/_toolcheck.ToolGuard`, embedded transport): the freeze is
  recorded before the first model call, the name and arguments digest are checked and recorded as a
  `policy.decided` entry (AD-26) before the body starts, `tool.called` follows the body, and the
  engine owns the denial count. A rule deny is a soft denial (the model sees one fixed text, the
  graph goes on, three in a run stop it, AD-41, AD-42); an `ask` waits for a person (`tool_wait`,
  or is returned as `ask_pending` with `tool_wait=None`); a stop raises `ToolStop` (the task goes
  to a human), and a tool that is not in the frozen set, or whose schema changed, is a stop (AD-17,
  AD-18). A bound tool run by a node this adapter did not make is refused before the first model
  call (AD-77) when it can be detected: a `langgraph.prebuilt.ToolNode` (or a subclass) in the
  graph is refused before the run starts, and a tool the adapter does not run is refused at the
  first model call. A tool that a hidden node runs still runs, with no policy check and no ledger
  entry: its side effects stay. The run is refused afterwards only if the tool's result reaches the
  graph's messages (the run-token check on the next model turn, or the last-turn check when the
  run ends); if it does not, nothing refuses it. A custom node that calls a tool's `invoke`
  itself cannot be seen, so graph code must not do that. A turn with only invalid tool calls
  reaches the tool node only with a routing condition that looks at `invalid_tool_calls`
  (LangGraph's `tools_condition` does not).
- **Tool answers are tied to this run, in this process.** Each answer of the tool node carries a
  run token (HMAC-SHA256 over the run id, the call id and the frozen tool set's hash). The key is
  random per attempt and lives only in this process's memory: it is never written to the ledger,
  a checkpoint or the model call, and the token is removed from the final state. A transcript
  restored in another process (a durable resume of a checkpointed graph) therefore can't verify
  the earlier answers and is refused with `tool_token_invalid`: it fails closed, the task goes
  to a human, and no tool answer is trusted across processes. The token
  binds the call, not the answer's content: don't put nodes in the graph that rewrite tool
  messages. When the run ends, every tool call must have a token-valid answer in the final
  state's messages (`tool_calls_unanswered` otherwise, also when the final state has no readable
  message list, so keep the messages key in the graph's output).
- **Actions are policy-checked when the graph asks:** call `authorize(model, kind, target)`
  before a tool, command, file write or message (`authorize_tool_url` for a tool's URL: it
  refuses tools the manifest does not declare network-capable; graph code must call it); it
  records the decision and raises when the role's policy denies (default) or asks a human. The
  adapter can't see tools the graph runs without asking: **don't give graph nodes their own
  provider keys or unchecked tools.**
- **Escalation:** a budget hard stop, a policy refusal or an approval request raised inside the
  graph reaches the engine (the task goes to a human) unless graph code catches it; spending stays
  stopped either way.
- **Pauses are never submissions.** A graph that stops for a human (an `interrupt()`, or a static
  `interrupt_before`/`interrupt_after` breakpoint with a checkpointer) raises `GraphInterrupted`;
  the task goes to a human with a fixed reason (resuming comes with R1.20).
- **Gates decide, not the graph.** The final state's `output_key` is the submission; gate feedback
  from a failed round is passed into the next attempt's input (`feedback`).
- **No tracing side channel:** LangSmith tracing is switched off around the graph, so prompts never
  leave through it even if `LANGSMITH_TRACING` is set (A7, ND25).

The graph is built per attempt by `build(model)`, so no state leaks between attempts or tasks.
Graphs run with `invoke`; `run_async=True` runs one with `ainvoke` on one event loop per attempt,
closed (pending tasks cancelled) before the worker returns, for graphs whose nodes are `async`
(AD-52).
"""

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from importlib.metadata import version
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, InvalidToolCall, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as make_tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.prebuilt import ToolNode
from langsmith.run_helpers import tracing_context
from pydantic import ConfigDict

from nuramesh.adapters._toolcheck import Seen, ToolGuard, ToolStop
from nuramesh.adapters.network import authorize_network
from nuramesh.core.canonical import canonical_json
from nuramesh.core.models import Message
from nuramesh.core.policy import ActionKind
from nuramesh.core.tasks import Task
from nuramesh.core.toolcalls import (
    DEADLINE_WAIT_SECONDS,
    DENIAL_LIMIT,
    FrozenToolSet,
    ManifestEntry,
    ReasonCode,
    UnreadableToolName,
    call_id_for,
    check_tool_name,
    schema_hash,
)
from nuramesh.engine.models import PolicyDenied
from nuramesh.engine.run import NeedsHuman, WorkContext, Worker

_ROLES: dict[str, Literal["system", "user", "assistant"]] = {
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "system": "system",
}
DEFAULT_MAX_OUTPUT_TOKENS = 1_024
RUN_TOKEN_KEY = "nuramesh_run_token"
_SEEDED_TEXT = (
    "tool-call messages are only accepted when the adapter's tool node made them: "
    "the graph input can't carry AIMessage.tool_calls or ToolMessage"
)
_BIND_TEXT = (
    "bind_tools isn't supported on the NuraMesh model: use "
    "nuramesh_tool_model(model, tools) with nuramesh_tool_node(model, tools)"
)
_STRUCTURED_TEXT = "structured output isn't supported on the NuraMesh model (it needs tool-calling)"
_PREBUILT_TEXT = (
    "a prebuilt ToolNode runs tools outside NuraMesh's checks: "
    "use nuramesh_tool_node(model, tools) instead"
)
_ORPHAN_TEXT = "a tool message answers no tool call of this run"
_LANGGRAPH_VERSION = version("langgraph")


class GraphInterrupted(NeedsHuman):
    """The graph paused for a human (an interrupt or a breakpoint); the task needs a human."""

    reason = "the agent's graph paused for a human"


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            kind = block.get("type") if isinstance(block, Mapping) else type(block).__name__
            raise ValueError(f"message content of type '{kind}' can't be sent to the model")
    return "".join(parts)


def _envelope_calls(m: AIMessage) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = [
        {"name": c["name"], "args": c["args"], "id": c.get("id")} for c in m.tool_calls
    ]
    calls += [
        {"name": c.get("name"), "args": c.get("args"), "id": c.get("id")}
        for c in m.invalid_tool_calls
    ]
    return calls


def _to_messages(messages: Sequence[BaseMessage], *, allow_tools: bool = False) -> list[Message]:
    """The model-call messages. Tool-call and tool-result messages are accepted only with the
    adapter's tool node (`allow_tools`, AD-77) and travel as the JSON envelope the model answers
    in, so the reservation counts them (AD-36)."""
    out = []
    for m in messages:
        has_calls = bool(getattr(m, "tool_calls", None) or getattr(m, "invalid_tool_calls", None))
        if m.type == "tool" and allow_tools and isinstance(m, ToolMessage):
            body = {
                "tool_result": {
                    "id": m.tool_call_id,
                    "status": m.status,
                    "content": _text(m),
                }
            }
            out.append(Message(role="user", content=json.dumps(body, sort_keys=True)))
            continue
        role = _ROLES.get(m.type)
        if role is None:
            raise ValueError(f"message type '{m.type}' can't be sent to the model")
        if has_calls:
            if not allow_tools or not isinstance(m, AIMessage):
                raise ValueError("messages with tool calls can't be sent to the model yet")
            calls = json.dumps({"tool_calls": _envelope_calls(m)}, sort_keys=True, default=str)
            text = _text(m)
            out.append(Message(role=role, content=f"{text}\n{calls}" if text else calls))
            continue
        out.append(Message(role=role, content=_text(m)))
    return out


def _tools_message(definitions: Sequence[Mapping[str, Any]]) -> Message:
    """The system message that offers the bound tools to the model (counted bytes, AD-36)."""
    listed = [
        {
            "name": d["function"]["name"],
            "description": d["function"].get("description", ""),
            "parameters": d["function"].get("parameters", {}),
        }
        for d in definitions
    ]
    return Message(
        role="system",
        content=(
            "You may call these tools: "
            + json.dumps(listed, sort_keys=True)
            + '. To call tools reply with ONLY a JSON object {"tool_calls": [{"name": ..., '
            '"args": {...}, "id": ...}]}; otherwise answer normally.'
        ),
    )


def _with_tools(messages: list[Message], tools: Message) -> list[Message]:
    """Put the tool offer right after the leading system messages (the cacheable prefix)."""
    lead = 0
    while lead < len(messages) and messages[lead].role == "system":
        lead += 1
    return [*messages[:lead], tools, *messages[lead:]]


def _tool_calls_of(text: str) -> AIMessage | None:
    """The model's reply in the tool-call envelope as a message with `tool_calls`, or `None` when
    the reply is an ordinary answer. Arguments that are not one JSON object go to
    `invalid_tool_calls`, which are never executed (AD-77)."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        body = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(body, dict) or set(body) != {"tool_calls"}:
        return None
    items = body["tool_calls"]
    if not isinstance(items, list) or not items:
        return None
    good: list[Any] = []
    bad: list[InvalidToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            bad.append(
                InvalidToolCall(
                    name=None,
                    args=None,
                    id=call_id_for(None),
                    error="shape",
                    type="invalid_tool_call",
                )
            )
            continue
        raw_id = item.get("id")
        call_id = call_id_for(raw_id if isinstance(raw_id, str) else None)
        name, args = item.get("name"), item.get("args", {})
        if isinstance(args, dict) and isinstance(name, str):
            good.append({"name": name, "args": args, "id": call_id, "type": "tool_call"})
        else:
            shown = args if isinstance(args, str) else json.dumps(args, default=str)
            bad.append(
                InvalidToolCall(
                    name=name if isinstance(name, str) else None,
                    args=shown,
                    id=call_id,
                    error="unreadable",
                    type="invalid_tool_call",
                )
            )
    return AIMessage(content="", tool_calls=good, invalid_tool_calls=bad)


def _readable_or_marker(name: object) -> str:
    """An invalid call's name for the seam: the name if readable, else the fixed marker, so the
    pre-rule deny is `unreadable_arguments` either way (AD-77, LG-4)."""
    try:
        return check_tool_name(name)
    except UnreadableToolName:
        return "unreadable"


def _parameters(definition: Mapping[str, Any]) -> Any:
    return definition["function"].get("parameters", {})


def _entry(definition: Mapping[str, Any]) -> ManifestEntry:
    return ManifestEntry(definition["function"]["name"], schema_hash(_parameters(definition)))


_WALK_DEPTH = 32


def _not_guarded_text(name: object) -> str:
    return (
        f"bound tool {str(name)[:64]!r} is not run by a nuramesh_tool_node "
        "(a tool node the adapter did not make is refused)"
    )


def _find_tool_node(obj: Any, seen: set[int], depth: int = 0) -> bool:
    """True when a langgraph `ToolNode` (or subclass) is reachable from `obj` through the places a
    graph keeps its steps (see `_refuse_foreign_tool_nodes`)."""
    if depth > _WALK_DEPTH or id(obj) in seen:
        return False
    seen.add(id(obj))
    if isinstance(obj, ToolNode):
        return True
    children: list[Any] = []
    nodes = getattr(obj, "nodes", None)
    if isinstance(nodes, Mapping):  # a compiled graph (also a subgraph)
        children.extend(nodes.values())
    children.append(getattr(obj, "bound", None))  # PregelNode, RunnableBinding
    steps = getattr(obj, "steps", None)  # RunnableSequence
    if isinstance(steps, list | tuple):
        children.extend(steps)
    parallel = getattr(obj, "steps__", None)  # RunnableParallel
    if isinstance(parallel, Mapping):
        children.extend(parallel.values())
    for fn in (getattr(obj, "func", None), getattr(obj, "afunc", None)):
        if callable(fn) and not isinstance(fn, ToolNode):
            try:
                held = inspect.getclosurevars(fn)
            except (TypeError, ValueError):
                continue
            children.extend(held.nonlocals.values())
            children.extend(held.globals.values())
    return any(c is not None and _find_tool_node(c, seen, depth + 1) for c in children)


def _refuse_foreign_tool_nodes(graph: Any) -> None:
    """AD-77: a prebuilt `ToolNode` (or a subclass) would run a bound tool with no policy check and
    no ledger entry, so a graph that holds one is refused before it runs.

    Detected: a `ToolNode` or subclass as a node, inside a `RunnableSequence`, `RunnableParallel`
    or `RunnableBinding`, anywhere in a compiled subgraph (nested), and one held directly in the
    closure or module globals of a function node.

    NOT detected: an arbitrary function node or callable that runs a tool itself (for example by
    calling `tool.invoke`) without holding a `ToolNode`, or one that reaches it through an object
    or attribute the walk does not follow. Those calls are not guarded and are not in the ledger;
    this is the documented limit (the fuller compile-time walk of the tool-bound design is not
    built yet)."""
    if _find_tool_node(graph, set()):
        raise ValueError(_PREBUILT_TEXT)


class ToolRegistry:
    """The tools this attempt's `guarded_tool_node`s run, by name, with the seam's `ToolGuard`
    (the one shared implementation of the S4 contract: the frozen set, the latch, the check, the
    record). The adapter adds only what is LangGraph's: which tools exist, how a body is invoked
    and how the framework is handed a refusal. One per `NuraMeshChatModel`, so no state leaks
    between attempts."""

    def __init__(
        self, context: Any, *, wait: int | None, denial_limit: int, deadline: int | None
    ) -> None:
        self.tools: dict[str, BaseTool] = {}
        self.guard = ToolGuard(
            context.tool_ledger(
                "langgraph",
                denial_limit=denial_limit,
                deadline_seconds=DEADLINE_WAIT_SECONDS if deadline is None else deadline,
            ),
            adapter="langgraph",
            wait=wait,
        )
        self._frozen = False
        self._lock = threading.Lock()
        self._context = context
        self._secret: bytes | None = None  # per-run, process memory only (LG-1)
        self.messages_key = "messages"

    def _make_secret(self) -> None:
        with self._lock:
            if self._secret is None:
                self._secret = os.urandom(32)

    def _digest(self, call_id: str) -> str | None:
        hash_ = self.guard.tool_set_hash
        if self._secret is None or hash_ is None:
            return None
        body = canonical_json(
            {"run_id": self._context.run_id, "call_id": call_id, "tool_set_hash": hash_}
        )
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def token_for(self, call_id: str | None) -> str:
        """The run token for an answer to `call_id` (hex HMAC-SHA256, LG-1)."""
        token = self._digest(call_id_for(call_id))
        if token is None:
            raise RuntimeError("the tool set of this guarded run was not frozen (AD-16)")
        return token

    def valid_token(self, message: ToolMessage) -> bool:
        """True when `message` carries this run's token for its call (constant-time compare)."""
        given = message.additional_kwargs.get(RUN_TOKEN_KEY)
        if not isinstance(given, str):
            return False
        expected = self._digest(call_id_for(message.tool_call_id))
        return expected is not None and hmac.compare_digest(
            expected.encode(), given.encode("utf-8", "replace")
        )

    def register(self, tools: Sequence[BaseTool]) -> None:
        """Called by `guarded_tool_node`: the tools this adapter will run."""
        for t in tools:
            try:
                name = check_tool_name(t.name)
            except UnreadableToolName:
                raise ValueError(f"tool name {str(t.name)[:64]!r} is not a readable name") from None
            if self._frozen and self.tools.get(name) is not t:
                raise ValueError(f"tool '{name}' can't be added after the tool set is frozen")
            if name in self.tools and self.tools[name] is not t:
                raise ValueError(f"tool '{name}' is registered twice")
            self.tools[name] = t

    def schema_of(self, name: str) -> str | None:
        """The registered tool's `schema_hash` now (AD-18), or `None` when there is no such tool."""
        t = self.tools.get(name)
        return None if t is None else schema_hash(_parameters(convert_to_openai_tool(t)))

    def check_offered(self, definitions: Sequence[Mapping[str, Any]]) -> None:
        """Every model turn, before the model call: the tools offered must be the adapter's
        (AD-77); the first turn freezes them and records it (AD-14 to AD-16); every turn is
        compared with the frozen set (AD-17, AD-18). A stop raises `ToolStop` (AD-44)."""
        self.guard.raise_if_stopped()
        if not self.tools:
            raise ValueError(
                "tools are bound to the model but no nuramesh_tool_node runs them: "
                "tool calls are only supported with the adapter's tool node"
            )
        for d in definitions:
            name = d["function"]["name"]
            if name not in self.tools:
                raise ValueError(_not_guarded_text(name))
        self.freeze()
        self.guard.check_turn(_entry(d) for d in definitions)

    def freeze(self) -> None:
        """Freeze the registered tools, once (AD-16)."""
        self._make_secret()
        with self._lock:
            if self._frozen:
                return
            self.guard.freeze(
                (ManifestEntry(n, self.schema_of(n) or "") for n in self.tools),
                framework="langgraph",
                framework_version=_LANGGRAPH_VERSION,
            )
            self._frozen = True

    async def afreeze(self) -> None:
        self._make_secret()
        with self._lock:  # a short critical section; the write itself is on a worker thread
            frozen = self._frozen
        if frozen:
            return
        await asyncio.to_thread(self.freeze)  # one write however many nodes freeze at once

    def body_of(self, name: object) -> Callable[[Any], Any]:
        tool = self.tools.get(name) if isinstance(name, str) else None

        def body(arguments: Any) -> Any:
            if tool is None:  # unreachable: the check denies a tool that is not frozen (AD-17)
                raise RuntimeError("no such tool")
            return tool.invoke(arguments)

        return body

    def abody_of(self, name: object) -> Callable[[Any], Awaitable[Any]]:
        tool = self.tools.get(name) if isinstance(name, str) else None

        async def body(arguments: Any) -> Any:
            if tool is None:
                raise RuntimeError("no such tool")
            return await tool.ainvoke(arguments)

        return body


def _message_of(seen: Seen, *, fallback_id: str) -> ToolMessage:
    """What the graph's messages get: the tool's result, or the one fixed text as an error
    message (AD-38, AD-39). A stop is raised by the caller after this."""
    if seen.ran:
        out = seen.result
        return ToolMessage(
            content=out if isinstance(out, str) else json.dumps(out, default=str),
            tool_call_id=seen.call_id,
        )
    return ToolMessage(
        content=seen.text or "", tool_call_id=seen.call_id or fallback_id, status="error"
    )


def guarded_tool_node(
    model: "NuraMeshChatModel",
    tools: Sequence[BaseTool | Callable[..., Any]],
    *,
    messages_key: str = "messages",
) -> RunnableLambda[Any, Any]:
    """The graph node that runs the tool calls of the last `AIMessage` (AD-1: the only path a bound
    tool runs through). Each call goes through the seam's `ToolGuard`: checked and recorded before
    its body, the body receiving the arguments as checked (AD-11). Sync (`invoke`) and async
    (`ainvoke`) share one decision flow (AD-50); the async path keeps the event loop free
    (AD-51). A soft denial gives the model one fixed text; a stop raises `ToolStop` from the node
    (AD-43)."""
    if not isinstance(model, NuraMeshChatModel):
        raise ValueError("nuramesh_tool_node needs a NuraMeshChatModel")
    built = [t if isinstance(t, BaseTool) else make_tool(t) for t in tools]
    registry = model.registry
    registry.register(built)
    registry.messages_key = messages_key

    def last_of(state: Any) -> AIMessage:
        messages = state[messages_key] if isinstance(state, Mapping) else state
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage):
            raise ValueError("the tool node needs an AI message with tool calls last")
        return last

    def with_ids(last: AIMessage) -> AIMessage | None:
        """The same message (same id, so `add_messages` replaces it) with the AD-8 `c-` id written
        into every call entry that has none, or `None` when none is missing (LG-3): the written
        id survives a checkpointed resume, and the answer then matches an entry."""
        if all(c.get("id") is not None for c in (*last.tool_calls, *last.invalid_tool_calls)):
            return None
        good = [
            c if c.get("id") is not None else {**c, "id": call_id_for(None)}
            for c in last.tool_calls
        ]
        bad = [
            c if c.get("id") is not None else {**c, "id": call_id_for(None)}
            for c in last.invalid_tool_calls
        ]
        return last.model_copy(update={"tool_calls": good, "invalid_tool_calls": bad})

    def calls_of(last: AIMessage) -> list[tuple[str | None, object, object]]:
        items: list[tuple[str | None, object, object]] = [
            (c.get("id"), c["name"], c["args"]) for c in last.tool_calls
        ]
        # AD-77: an invalid tool call is never executed. Its argument text is NOT handed to the
        # seam, which reads text (AD-9) and would run a call whose text happens to be one JSON
        # object (or is empty): the seam gets no arguments, so the call is a pre-rule deny
        # `unreadable_arguments` (LG-4: no readable name is the marker `unreadable`)
        items += [
            (c.get("id"), _readable_or_marker(c.get("name")), None) for c in last.invalid_tool_calls
        ]
        return items

    def schema_for(name: object) -> str | None:
        return registry.schema_of(name) if isinstance(name, str) else None

    def answer(seen: Seen, call_id: str | None) -> ToolMessage:
        message = _message_of(seen, fallback_id=call_id or "")
        message.additional_kwargs[RUN_TOKEN_KEY] = registry.token_for(message.tool_call_id)
        return message

    def run(state: Any) -> dict[str, list[BaseMessage]]:
        out: list[BaseMessage] = []
        registry.freeze()
        last = last_of(state)
        fixed = with_ids(last)
        for call_id, name, args in calls_of(fixed or last):
            seen = registry.guard.call(
                name, args, registry.body_of(name), call_id=call_id, schema=schema_for(name)
            )
            if seen.stop is not None:
                raise ToolStop(seen.stop)
            out.append(answer(seen, call_id))
        return {messages_key: [fixed, *out] if fixed is not None else out}

    async def arun(state: Any) -> dict[str, list[BaseMessage]]:
        out: list[BaseMessage] = []
        await registry.afreeze()
        last = last_of(state)
        fixed = with_ids(last)
        for call_id, name, args in calls_of(fixed or last):
            seen = await registry.guard.acall(
                name, args, registry.abody_of(name), call_id=call_id, schema=schema_for(name)
            )
            if seen.stop is not None:
                raise ToolStop(seen.stop)
            out.append(answer(seen, call_id))
        return {messages_key: [fixed, *out] if fixed is not None else out}

    return RunnableLambda(run, afunc=arun, name="tools")


class NuraMeshChatModel(BaseChatModel):
    """A LangChain chat model whose every call is a NuraMesh model call for the task."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    context: Any  # WorkContext (Any: LangChain's pydantic model can't validate it)
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    cache: bool = False  # never LangChain's global cache: a cached answer would skip the ledger
    cache_prefix: bool = False
    """Ask the provider to cache the leading system messages (R1.34). Unrelated to `cache` above.
    Takes effect only for an organization that opted in to prompt caching."""

    tool_wait: int | None = DEADLINE_WAIT_SECONDS
    """What an `ask` of a tool call does (AD-45): wait up to this many seconds (also the approval's
    deadline, AD-46) for a person, or `None` to return `ask_pending` to the model at once."""
    denial_limit: int = DENIAL_LIMIT
    """Soft denials of one run before it stops: lower than the default only (AD-69)."""

    _registry: ToolRegistry | None = None
    _turns: int = 0

    @property
    def registry(self) -> ToolRegistry:
        """This attempt's tools and seam guard (`ToolRegistry`), made on first use."""
        if self._registry is None:
            self._registry = ToolRegistry(
                self.context,
                wait=self.tool_wait,
                denial_limit=self.denial_limit,
                deadline=self.tool_wait,
            )
        return self._registry

    @property
    def guard(self) -> ToolGuard:
        """This attempt's seam guard (`nuramesh.adapters._toolcheck.ToolGuard`)."""
        return self.registry.guard

    @property
    def _llm_type(self) -> str:
        return "nuramesh"

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[BaseTool | Callable[..., Any]], **kwargs: Any
    ) -> Any:
        """Always refused (LG-1): use `nuramesh_tool_model(model, tools)`, the one way to offer
        tools, so a graph can't hand the model tools some other tool node would run."""
        raise ValueError(_BIND_TEXT)

    def with_structured_output(self, *args: Any, **kwargs: Any) -> Any:
        """Always refused (LG-7): structured output needs provider tool-calling, which
        NuraMesh does not offer; ask for the format in the prompt.
        The owner confirmed this default on 2026-11-22."""
        raise ValueError(_STRUCTURED_TEXT)

    def _check_transcript(self, messages: Sequence[BaseMessage]) -> None:
        """Turn n+1, before the model call (LG-1, LG-3): every `ToolMessage` answers a prior call
        of this transcript (else `tool_message_orphan`) and carries this run's token (else
        `tool_token_invalid`). Each is one `task.refused` and a stop."""
        registry = self.registry
        asked: set[str] = set()
        for m in messages:
            if isinstance(m, AIMessage):
                asked.update(
                    call_id_for(c["id"])
                    for c in (*m.tool_calls, *m.invalid_tool_calls)
                    if c.get("id") is not None
                )
            elif isinstance(m, ToolMessage):
                if call_id_for(m.tool_call_id) not in asked:
                    registry.guard.refuse(ReasonCode.TOOL_MESSAGE_ORPHAN)
                    raise ValueError(_ORPHAN_TEXT)
                if not registry.valid_token(m):
                    registry.guard.refuse(ReasonCode.TOOL_TOKEN_INVALID)
                    raise ToolStop(ReasonCode.TOOL_TOKEN_INVALID)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        definitions = kwargs.pop("tools", None)
        if kwargs:
            raise ValueError(
                f"call arguments aren't supported (got {sorted(kwargs)}); the model, its limits "
                "and its tools come from NuraMesh's configuration"
            )
        if self._registry is not None:
            self._registry.guard.raise_if_stopped()  # AD-44: no model call after a stop
        if self._turns == 0 and _has_tool_messages(messages):
            raise ValueError(_SEEDED_TEXT)  # a graph invoked outside langgraph_worker (LG-3)
        self._turns += 1
        if definitions:
            self.registry.check_offered(definitions)  # before the call: AD-16 to AD-18, AD-77
        allow_tools = self._registry is not None and bool(self._registry.tools)
        if allow_tools:
            self._check_transcript(messages)
        prompt = _to_messages(messages, allow_tools=allow_tools)
        if definitions:
            prompt = _with_tools(prompt, _tools_message(definitions))
        options: dict[str, Any] = {"max_output_tokens": self.max_output_tokens}
        if self.cache_prefix:  # only when set: a context written before the flag still works
            options["cache_prefix"] = True
        result = self.context.complete(prompt, **options)
        text = result.response.text
        if stop:
            cuts = [i for i in (text.find(marker) for marker in stop) if i >= 0]
            if cuts:
                text = text[: min(cuts)]
        meta = {
            "finish_reason": result.response.finish_reason,
            "nuramesh_max_output_tokens": result.effective_max_tokens,
            "nuramesh_cap_hit": result.cap_hit,
        }
        message = (_tool_calls_of(text) if definitions else None) or AIMessage(content=text)
        message.response_metadata = meta
        return ChatResult(generations=[ChatGeneration(message=message)])


def nuramesh_tool_model(
    model: NuraMeshChatModel, tools: Sequence[BaseTool | Callable[..., Any]]
) -> Any:
    """The model offering `tools` (only their definitions; they run through
    `nuramesh_tool_node`). The one way to bind tools to a NuraMesh model (LG-1)."""
    if not isinstance(model, NuraMeshChatModel):
        raise ValueError("nuramesh_tool_model needs a NuraMeshChatModel")
    built = [t if isinstance(t, BaseTool) else make_tool(t) for t in tools]
    return model.bind(tools=[convert_to_openai_tool(t) for t in built])


def nuramesh_tool_node(
    model: NuraMeshChatModel,
    tools: Sequence[BaseTool | Callable[..., Any]],
    *,
    messages_key: str = "messages",
) -> RunnableLambda[Any, Any]:
    """The graph node that runs the tool calls (see `guarded_tool_node`, the same node)."""
    return guarded_tool_node(model, tools, messages_key=messages_key)


def _has_tool_messages(messages: Sequence[BaseMessage]) -> bool:
    return any(
        isinstance(m, ToolMessage)
        or (isinstance(m, AIMessage) and (m.tool_calls or m.invalid_tool_calls))
        for m in messages
    )


def _strip_tokens(state: Any) -> Any:
    """The final state without the run token (LG-1): copies of the messages, the originals stay."""

    def clean(value: Any) -> Any:
        if isinstance(value, ToolMessage) and RUN_TOKEN_KEY in value.additional_kwargs:
            kept = {k: v for k, v in value.additional_kwargs.items() if k != RUN_TOKEN_KEY}
            return value.model_copy(update={"additional_kwargs": kept})
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, tuple):
            return tuple(clean(v) for v in value)
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value

    if isinstance(state, Mapping):
        return {k: clean(v) for k, v in state.items()}
    return state


def nuramesh_tools_condition(
    state: Any, messages_key: str = "messages"
) -> Literal["tools", "__end__"]:
    """Where a graph goes after the model: `"tools"` (the adapter's node) when the last AI message
    has `tool_calls` OR `invalid_tool_calls`, else the end (LG-4). LangGraph's `tools_condition`
    looks at `tool_calls` only, so a turn with only invalid calls would end with no denial."""
    if isinstance(state, Sequence) and not isinstance(state, str | bytes):
        messages = state
    elif isinstance(state, Mapping) and messages_key in state:
        messages = state[messages_key]
    elif getattr(state, messages_key, None) is not None:
        messages = getattr(state, messages_key)
    else:
        raise ValueError(f"no messages found in the input state to route on ('{messages_key}')")
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage) and (last.tool_calls or last.invalid_tool_calls):
        return "tools"
    return "__end__"


def authorize(model: NuraMeshChatModel, kind: ActionKind, target: str) -> None:
    """Check an action against the agent's policy before the graph does it (module docstring)."""
    model.context.authorize(kind, target)


def authorize_url(model: NuraMeshChatModel, url: str) -> str:
    """Check a URL a graph tool is about to fetch (#1119): it becomes a NETWORK action on host:port
    and the approved `host:port` is returned. An unreducible URL is refused, never guessed, and the
    URL text (it may carry a token) is never recorded. Every redirect hop needs its own call."""
    return authorize_network(model.context, url)


def authorize_tool_url(
    model: NuraMeshChatModel, tools: FrozenToolSet, tool_name: object, url: str
) -> str:
    """Check the URL a named tool is about to fetch (#1119, C-3): only a tool the frozen manifest
    declared network-capable gets a NETWORK decision (`authorize_url`). Any other tool is refused:
    the refusal is counted and recorded as a deny (`tool_not_network_capable`) so a graph that
    catches the error still ends with a human; no `use_tool` approval is asked for a call that
    would be refused anyway. The URL text is never recorded or put in an error."""
    if tools.network_capable(tool_name):
        return authorize_url(model, url)
    model.context.refuse_network_undeclared(tool_name)
    raise PolicyDenied("tool is not declared network-capable")


def authorize_command(model: NuraMeshChatModel, argv: Sequence[str]) -> tuple[str, ...]:
    """Check a command (the argument list that will run) and return it: execute that same list,
    never a shell string (`WorkContext.authorize_command`, policy contract 2)."""
    command: tuple[str, ...] = model.context.authorize_command(argv)
    return command


def default_input(task: Task, ctx: WorkContext) -> dict[str, Any]:
    """The graph's input: the task's title and instructions, gate feedback and the canon."""
    return {
        "task": task.title,
        "instructions": task.instructions,
        "feedback": list(ctx.feedback),
        "canon": [{"name": d.name, "text": d.text} for d in ctx.canon],
    }


def _thread_id(run_id: str, task_id: str, attempt: int) -> str:
    return hashlib.sha256(f"{run_id}\x00{task_id}\x00{attempt}".encode()).hexdigest()[:32]


def _last_turn_check(registry: ToolRegistry, state: Any) -> None:
    """On run completion (LG-1): a `tool_calls` or `invalid_tool_calls` entry with no answering
    `ToolMessage` that carries this run's token is `task.refused` `tool_calls_unanswered`, a
    stop. Closes the gap of a final turn, where no turn n+1 checks the token.

    Fails closed: once the tool set is frozen, a final state whose transcript can't be read (the
    state is not a mapping or an object with the tool node's `messages_key`, or that value is not
    a list or tuple, for example an output schema that drops the messages) is refused with the
    same code, because nothing shows that every call was answered by the adapter's node."""
    if registry.guard.tool_set_hash is None:
        return
    if isinstance(state, Mapping):
        transcript = state.get(registry.messages_key)
    else:
        transcript = getattr(state, registry.messages_key, None)
    if not isinstance(transcript, list | tuple):
        registry.guard.refuse(ReasonCode.TOOL_CALLS_UNANSWERED)
        raise ToolStop(ReasonCode.TOOL_CALLS_UNANSWERED)
    # Precedence: a call whose id has no TOKENED answer at all (a tokenless answer is not an
    # answer) is `tool_calls_unanswered`; when every id has one, any extra tokenless ToolMessage in
    # the final transcript (a repeated id answered tokenless, a ghost message) is
    # `tool_token_invalid`. Answers are counted per id: a model-controlled, repeated id must not
    # satisfy a later call.
    answered: Counter[str] = Counter()
    tokenless = False
    for m in transcript:
        if isinstance(m, ToolMessage):
            if registry.valid_token(m):
                answered[call_id_for(m.tool_call_id)] += 1
            else:
                tokenless = True
    calls = [
        c.get("id")
        for m in transcript
        if isinstance(m, AIMessage)
        for c in (*m.tool_calls, *m.invalid_tool_calls)
    ]
    if any(cid is None or answered[call_id_for(cid)] <= 0 for cid in calls):
        registry.guard.refuse(ReasonCode.TOOL_CALLS_UNANSWERED)
        raise ToolStop(ReasonCode.TOOL_CALLS_UNANSWERED)
    if tokenless:
        registry.guard.refuse(ReasonCode.TOOL_TOKEN_INVALID)
        raise ToolStop(ReasonCode.TOOL_TOKEN_INVALID)
    for cid in calls:  # a repeated id with fewer tokened answers than calls
        assert cid is not None
        key = call_id_for(cid)
        answered[key] -= 1
        if answered[key] < 0:
            registry.guard.refuse(ReasonCode.TOOL_CALLS_UNANSWERED)
            raise ToolStop(ReasonCode.TOOL_CALLS_UNANSWERED)


def langgraph_worker(
    build: Callable[[NuraMeshChatModel], Any],
    *,
    make_input: Callable[[Task, WorkContext], Mapping[str, Any]] = default_input,
    output_key: str = "output",
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    recursion_limit: int = 25,
    run_async: bool = False,
    tool_wait: int | None = DEADLINE_WAIT_SECONDS,
    denial_limit: int = DENIAL_LIMIT,
) -> Worker:
    """A `Worker` that runs the graph `build(model)` returns (a compiled LangGraph graph).
    `run_async=True` runs it with `ainvoke` on one event loop of its own, closed before the worker
    returns (AD-52); it refuses to start inside a running event loop."""
    if recursion_limit < 1:
        raise ValueError("recursion_limit must be at least 1")

    def prepare(
        task: Task, ctx: WorkContext
    ) -> tuple[NuraMeshChatModel, Any, bool, dict[str, Any]]:
        model = NuraMeshChatModel(
            context=ctx,
            max_output_tokens=max_output_tokens,
            tool_wait=tool_wait,
            denial_limit=denial_limit,
        )
        graph = build(model)
        _refuse_foreign_tool_nodes(graph)
        saver = getattr(graph, "checkpointer", None)
        has_saver = isinstance(saver, BaseCheckpointSaver)
        breakpoints = [
            *(getattr(graph, "interrupt_before_nodes", None) or []),
            *(getattr(graph, "interrupt_after_nodes", None) or []),
        ]
        if breakpoints and not has_saver:  # without a checkpointer a pause looks like a result
            raise ValueError(
                "a graph with interrupt_before/interrupt_after needs a checkpointer, or its "
                "pause would be taken for a finished result"
            )
        config = {
            "recursion_limit": recursion_limit,
            # one thread per run, task and attempt, for graphs compiled with a checkpointer
            "configurable": {"thread_id": _thread_id(ctx.run_id, task.id, task.attempt)},
        }
        return model, graph, has_saver, config

    def finish(model: NuraMeshChatModel, state: Any, paused: bool) -> Any:
        if model._registry is not None:  # a stop is raised even if the graph swallowed it (AD-43)
            model._registry.guard.raise_if_stopped()
        if paused or (isinstance(state, Mapping) and state.get("__interrupt__")):
            raise GraphInterrupted()
        if model._registry is not None:
            _last_turn_check(model._registry, state)
            state = _strip_tokens(state)
        if not isinstance(state, Mapping) or output_key not in state:
            raise ValueError(f"the graph's final state has no '{output_key}'")
        return state[output_key]

    def stopped(model: NuraMeshChatModel, exc: Exception) -> None:
        if model._registry is not None and not isinstance(exc, ToolStop):
            try:
                model._registry.guard.raise_if_stopped()
            except ToolStop as stop:
                raise stop from None

    def work(task: Task, ctx: WorkContext) -> Any:
        if run_async:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError(
                    "run_async=True can't start inside a running event loop: "
                    "await the graph's own ainvoke instead (AD-52)"
                )
        model, graph, has_saver, config = prepare(task, ctx)
        data = dict(make_input(task, ctx))
        if any(
            _has_tool_messages(list(v)) if isinstance(v, list | tuple) else _has_tool_messages([v])
            for v in data.values()
            if isinstance(v, list | tuple | BaseMessage)
        ):
            raise ValueError(_SEEDED_TEXT)  # before any model call, no exemption (LG-3, T-LG8)

        async def arun() -> tuple[Any, bool]:
            result = await graph.ainvoke(data, config)
            return result, has_saver and bool((await graph.aget_state(config)).next)

        try:
            with tracing_context(enabled=False):
                if run_async:
                    state, paused = asyncio.run(arun())  # one loop, closed with its tasks
                else:
                    state = graph.invoke(data, config)
                    paused = has_saver and bool(graph.get_state(config).next)
        except Exception as exc:
            stopped(model, exc)
            raise
        return finish(model, state, paused)

    return work


__all__ = [
    "GraphInterrupted",
    "NuraMeshChatModel",
    "ToolRegistry",
    "ToolStop",
    "authorize",
    "authorize_tool_url",
    "authorize_url",
    "default_input",
    "guarded_tool_node",
    "langgraph_worker",
    "nuramesh_tool_model",
    "nuramesh_tool_node",
    "nuramesh_tools_condition",
]
