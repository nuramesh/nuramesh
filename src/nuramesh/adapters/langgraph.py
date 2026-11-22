"""Run a LangGraph graph as a NuraMesh worker (R1.16; install with `nuramesh[langgraph]`).

NuraMesh is the trust layer around the agent, not the agent framework: the team keeps
building agents in LangGraph, and this adapter plugs a compiled graph into the run engine as one
agent's `Worker`. The run engine orchestrates the organization (delegation, gates, budgets,
retries); each agent's work can be a LangGraph graph (T29, T31).

What the adapter guarantees:

- **Model calls through the provided model go through NuraMesh.** The graph gets a chat model
  (`NuraMeshChatModel`, a LangChain `BaseChatModel`) bound to the task's `WorkContext`, so each
  call, sync or async, streamed or batched, is policy-checked, priced, reserved against the
  budgets, settled and recorded as the task's assignee, with the assignee's model. Unsupported
  inputs are refused, never silently changed: non-text content, tool-call messages and extra call
  arguments (`max_tokens`, `temperature`, bound tools...) raise `ValueError`.
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
Graphs run with `invoke`: graphs whose nodes are all `async` aren't supported yet.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.base import BaseCheckpointSaver
from langsmith.run_helpers import tracing_context
from pydantic import ConfigDict

from nuramesh.adapters.network import authorize_network
from nuramesh.core.models import Message
from nuramesh.core.policy import ActionKind
from nuramesh.core.tasks import Task
from nuramesh.core.toolcalls import FrozenToolSet
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


def _to_messages(messages: Sequence[BaseMessage]) -> list[Message]:
    out = []
    for m in messages:
        role = _ROLES.get(m.type)
        if role is None:
            raise ValueError(f"message type '{m.type}' can't be sent to the model")
        if getattr(m, "tool_calls", None) or getattr(m, "invalid_tool_calls", None):
            raise ValueError("messages with tool calls can't be sent to the model yet")
        out.append(Message(role=role, content=_text(m)))
    return out


class NuraMeshChatModel(BaseChatModel):
    """A LangChain chat model whose every call is a NuraMesh model call for the task."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    context: Any  # WorkContext (Any: LangChain's pydantic model can't validate it)
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    cache: bool = False  # never LangChain's global cache: a cached answer would skip the ledger
    cache_prefix: bool = False
    """Ask the provider to cache the leading system messages (R1.34). Unrelated to `cache` above.
    Takes effect only for an organization that opted in to prompt caching."""

    @property
    def _llm_type(self) -> str:
        return "nuramesh"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if kwargs:
            raise ValueError(
                f"call arguments aren't supported (got {sorted(kwargs)}); the model, its limits "
                "and its tools come from NuraMesh's configuration"
            )
        options: dict[str, Any] = {"max_output_tokens": self.max_output_tokens}
        if self.cache_prefix:  # only when set: a context written before the flag still works
            options["cache_prefix"] = True
        result = self.context.complete(_to_messages(messages), **options)
        text = result.response.text
        if stop:
            cuts = [i for i in (text.find(marker) for marker in stop) if i >= 0]
            if cuts:
                text = text[: min(cuts)]
        message = AIMessage(
            content=text,
            response_metadata={
                "finish_reason": result.response.finish_reason,
                "nuramesh_max_output_tokens": result.effective_max_tokens,
                "nuramesh_cap_hit": result.cap_hit,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


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


def langgraph_worker(
    build: Callable[[NuraMeshChatModel], Any],
    *,
    make_input: Callable[[Task, WorkContext], Mapping[str, Any]] = default_input,
    output_key: str = "output",
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    recursion_limit: int = 25,
) -> Worker:
    """A `Worker` that runs the graph `build(model)` returns (a compiled LangGraph graph)."""
    if recursion_limit < 1:
        raise ValueError("recursion_limit must be at least 1")

    def work(task: Task, ctx: WorkContext) -> Any:
        model = NuraMeshChatModel(context=ctx, max_output_tokens=max_output_tokens)
        graph = build(model)
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
        with tracing_context(enabled=False):
            state = graph.invoke(dict(make_input(task, ctx)), config)
            paused = has_saver and bool(graph.get_state(config).next)
        if paused or (isinstance(state, Mapping) and state.get("__interrupt__")):
            raise GraphInterrupted()
        if not isinstance(state, Mapping) or output_key not in state:
            raise ValueError(f"the graph's final state has no '{output_key}'")
        return state[output_key]

    return work


__all__ = [
    "GraphInterrupted",
    "NuraMeshChatModel",
    "authorize",
    "authorize_tool_url",
    "authorize_url",
    "default_input",
    "langgraph_worker",
]
