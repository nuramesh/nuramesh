"""Starting, waking and stopping a durable run from outside the worker (T72).

The database is the truth; a signal only wakes the workflow so it looks again. A decision is
recorded first (`RunEngine.record_human_decision`), then the signal is sent: if the signal is
lost the workflow still sees the decision at its next timer or step.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence

from temporalio.client import Client, WorkflowHandle

from nuramesh.engine.decision_wake import WAKE_TIMEOUT_SECONDS, WakeFold, wake_gate
from nuramesh.engine.run import PlannedTask, Planner, RunEngine
from nuramesh.engine.run_control import StopOutcome, request_stop
from nuramesh.temporal.converter import data_converter
from nuramesh.temporal.models import RunRef, RunResult
from nuramesh.temporal.security import security_from_env
from nuramesh.temporal.workflow import (
    SIGNAL_HUMAN_DECIDED,
    SIGNAL_STOP,
    RunWorkflow,
)

DEFAULT_TASK_QUEUE = "nuramesh"


def workflow_id(run_id: str) -> str:
    """One workflow per run: the id makes starting twice a no-op, not a second run."""
    return f"nuramesh-run-{run_id}"


async def connect(address: str = "localhost:7233", *, namespace: str = "default") -> Client:
    """A client that scrubs failure text (`converter`). A frontend that is not loopback is reached
    only over TLS with credentials (`security`); a missing or invalid setting raises
    `TemporalSecurityError` before any connection is tried."""
    security = security_from_env(address)
    if security.tls is None:
        return await Client.connect(address, namespace=namespace, data_converter=data_converter())
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=data_converter(),
        tls=security.tls,
        api_key=security.api_key,
    )


async def start_run(
    client: Client,
    engine: RunEngine,
    run_id: str,
    plan: Sequence[PlannedTask] | Planner,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    data_class: str | None = None,
) -> WorkflowHandle[RunWorkflow, RunResult]:
    """Begin the run in the database (once) and start its workflow. If the run already started
    (a second call, or a crash between the two steps) only the workflow is started."""
    try:
        engine.stored_plan(run_id)
    except ValueError:
        engine.begin_run(run_id, plan, data_class=data_class)
    return await client.start_workflow(
        RunWorkflow.run,
        RunRef(run_id=run_id, org_id=engine.org.id),
        id=workflow_id(run_id),
        task_queue=task_queue,
    )


def handle(client: Client, run_id: str) -> WorkflowHandle[RunWorkflow, RunResult]:
    """The handle of a run's workflow."""
    return client.get_workflow_handle_for(RunWorkflow.run, workflow_id(run_id))


async def decide(
    client: Client,
    engine: RunEngine,
    run_id: str,
    task_id: str,
    gate_id: str,
    *,
    decided_by: str,
    findings: Mapping[str, tuple[bool, str]],
) -> None:
    """Record a person's decision on a human gate, then wake the workflow through the same wake
    gate as the API route (deny-checks spec 4): a marker first, and under a halt, an erased
    organization or a lost key the decision is only recorded (held; nothing is signalled until the
    organization is usable and a delivery pass runs)."""
    made = engine.record_human_decision(
        run_id, task_id, gate_id, decided_by=decided_by, findings=findings
    )
    loop = asyncio.get_running_loop()
    target = handle(client, run_id)

    def wake(run: str, task: str) -> None:
        # a `TimeoutError` here means the signal may have gone out (never resent)
        asyncio.run_coroutine_threadsafe(target.signal(SIGNAL_HUMAN_DECIDED, task), loop).result(
            WAKE_TIMEOUT_SECONDS
        )

    org_id = engine.org.id
    await asyncio.to_thread(
        wake_gate,
        engine.store,
        org_id,
        engine.content,
        WakeFold(engine.store, org_id),
        wake,
        run_id,
        made,
    )


def make_waker(
    address: str, *, namespace: str = "default", timeout: float = 3.0
) -> Callable[[str, str], None]:
    """A synchronous waker for the API server (`create_app(on_human_decision=...)`): it sends the
    `human_decided` signal for `(run_id, task_id)`. Meant for a server thread (it runs its own
    event loop); it raises on any failure and the server then says `woken: false`. The signal
    carries a task id only; the decision is already in the database. The transport settings are
    checked here, at creation, so a server with bad ones refuses to start."""
    security_from_env(address)

    async def send(run_id: str, task_id: str) -> None:
        client = await connect(address, namespace=namespace)
        await handle(client, run_id).signal(SIGNAL_HUMAN_DECIDED, task_id)

    def wake(run_id: str, task_id: str) -> None:
        asyncio.run(asyncio.wait_for(send(run_id, task_id), timeout))

    return wake


async def stop(
    client: Client, engine: RunEngine, run_id: str, *, actor: str, via: str = "api"
) -> StopOutcome:
    """Record a Stop request in the ledger, then wake the workflow. The ledger entry is what
    makes the Stop real; the signal only makes the workflow look at it now."""
    outcome = request_stop(engine.store, run_id, actor=actor, via=via)
    await handle(client, run_id).signal(SIGNAL_STOP)
    return outcome
