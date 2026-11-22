"""The Temporal `RunStarter` (R1.20 PR C, ND347 section 6.1): the API side of durable runs.

`start` records a durable **start request** (`run_start_requests`, engine `temporal`) and starts
the run's workflow by id; it never builds an engine, imports team code or calls `begin_run`. The
workflow's first activity, in the worker (`nuramesh.temporal.intake`), claims the request and
begins the run. Workflow history carries ids only (`RunRef`); the plan stays in the store.

The org check comes first: a run id that already exists in another organization is refused with
no write at all (X10). The transport rules are those of `security` (a non-loopback address needs
TLS and credentials). Calls are synchronous and each opens its own short-lived event loop and
client (like `client.make_waker`), so a server thread can call them; do not call them from a
running event loop.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from nuramesh.engine.starter import (
    RunIdConflict,
    StarterHealth,
    StartRequest,
    StartResult,
    StartState,
    canonical_plan,
    run_of_another_organization,
)
from nuramesh.stores import Store
from nuramesh.temporal.client import DEFAULT_TASK_QUEUE, connect, workflow_id
from nuramesh.temporal.models import RunRef
from nuramesh.temporal.security import (
    ALLOW_PLAINTEXT,
    TemporalSecurityError,
    plaintext_requested,
    security_for,
)
from nuramesh.temporal.workflow import SIGNAL_HUMAN_DECIDED, SIGNAL_STOP, RunWorkflow

_LOG = logging.getLogger(__name__)

ENGINE: Final = "temporal"
ADDRESS_ENV: Final = "NURAMESH_TEMPORAL_ADDRESS"
NAMESPACE_ENV: Final = "NURAMESH_TEMPORAL_NAMESPACE"
QUEUE_ENV: Final = "NURAMESH_TEMPORAL_TASK_QUEUE"
CALL_TIMEOUT: Final = 5.0

Connector = Callable[[], Awaitable[Client]]


class StarterConfigError(ValueError):
    """The Temporal starter must not start with these settings (the server exits 2)."""


class EngineUnavailable(RuntimeError):
    """The Temporal server could not be reached for this call (the route answers 503
    `engine_unavailable`). The start request, if it was recorded, stays and can be started again."""


class TemporalStarter:
    """`RunStarter` over a store and a Temporal frontend."""

    name = ENGINE

    def __init__(
        self,
        store: Store,
        address: str,
        *,
        namespace: str = "default",
        task_queue: str = DEFAULT_TASK_QUEUE,
        connector: Connector | None = None,
        timeout: float = CALL_TIMEOUT,
    ) -> None:
        from nuramesh.temporal.security import security_from_env

        security_from_env(address)  # a bad transport setting refuses to start, before any call
        self.store = store
        self.address = address
        self.namespace = namespace
        self.task_queue = task_queue
        self.timeout = timeout
        self._connector: Connector = connector or self._default_connector

    async def _default_connector(self) -> Client:
        return await connect(self.address, namespace=self.namespace)

    def _call(self, body: Callable[[Client], Awaitable[Any]]) -> Any:
        async def go() -> Any:
            async def inner() -> Any:
                return await body(await self._connector())

            return await asyncio.wait_for(inner(), self.timeout)

        try:
            return asyncio.run(go())
        except (RPCError, OSError, TimeoutError, RuntimeError) as exc:
            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
                raise
            raise EngineUnavailable(type(exc).__name__) from None

    def start(self, request: StartRequest) -> StartResult:
        if request.engine != ENGINE:
            raise ValueError(f"the Temporal starter takes engine '{ENGINE}' requests only")
        if run_of_another_organization(self.store, request.org_id, request.run_id):
            raise RunIdConflict(request.run_id)  # X10: before any write
        with self.store.transaction() as uow:
            _, outcome = uow.put_start_request(
                request.org_id,
                request.run_id,
                plan_hash=request.plan_hash,
                plan=canonical_plan(request.plan),
                data_class=request.data_class,
                started_by=request.started_by,
                idempotency_key_hash=request.idempotency_key_hash,
                engine=request.engine,
            )
        if outcome == "conflict":
            raise RunIdConflict(request.run_id)
        self._start_workflow(request.org_id, request.run_id)
        state = StartState.ACCEPTED if outcome == "created" else StartState.ALREADY_REQUESTED
        return StartResult(request.run_id, state)

    def _start_workflow(self, org_id: str, run_id: str) -> None:
        from temporalio.exceptions import WorkflowAlreadyStartedError

        async def body(client: Client) -> None:
            try:
                await client.start_workflow(
                    RunWorkflow.run,
                    RunRef(run_id=run_id, org_id=org_id),
                    id=workflow_id(run_id),
                    task_queue=self.task_queue,
                )
            except WorkflowAlreadyStartedError:
                pass  # starting twice is a no-op: one workflow per run

        self._call(body)

    def _signal(self, run_id: str, name: str, *args: str) -> bool:
        async def body(client: Client) -> bool:
            try:
                await client.get_workflow_handle(workflow_id(run_id)).signal(name, *args)
            except RPCError as exc:
                if exc.status == RPCStatusCode.NOT_FOUND:
                    return False
                raise
            return True

        return bool(self._call(body))

    def wake(self, org_id: str, run_id: str, task_id: str = "") -> bool:
        """Signal the run's workflow to look at the database. The signal carries a task id only;
        the decision is already recorded. False if there is no such workflow."""
        return self._signal(run_id, SIGNAL_HUMAN_DECIDED, task_id)

    def stop(self, org_id: str, run_id: str) -> bool:
        """The stop request is already recorded (the stop route writes it, with the acting
        human); signal the workflow so it looks at the ledger now. False if there is no such
        workflow."""
        return self._signal(run_id, SIGNAL_STOP)

    def health(self) -> StarterHealth:
        async def body(client: Client) -> None:
            await client.service_client.check_health()

        try:
            self._call(body)
        except Exception:
            return StarterHealth(ENGINE, False, "unreachable")
        return StarterHealth(ENGINE, True)


def build_temporal_starter(
    store: Store,
    environ: Mapping[str, str],
    *,
    hosted: bool = False,
    connector: Connector | None = None,
) -> TemporalStarter:
    """The starter from the Temporal settings of the environment (S02, S10, S13). Raises
    `StarterConfigError` (the server exits 2) for a missing address, a transport setting that is
    refused, hosted with the plaintext allowance (S10), or hosted with a server that does not
    answer at startup (S13). The server selection (which engine) is the caller's."""
    address = environ.get(ADDRESS_ENV, "").strip()
    if not address:
        raise StarterConfigError(f"set {ADDRESS_ENV} (host:port) to use the Temporal engine")
    if hosted and plaintext_requested(environ):
        raise StarterConfigError(f"{ALLOW_PLAINTEXT} is not allowed on a hosted server")
    try:
        security_for(address, environ, allowance_recorded=True)
    except TemporalSecurityError as exc:
        raise StarterConfigError(str(exc)) from None
    starter = TemporalStarter(
        store,
        address,
        namespace=environ.get(NAMESPACE_ENV, "default"),
        task_queue=environ.get(QUEUE_ENV, DEFAULT_TASK_QUEUE),
        connector=connector,
    )
    if hosted and not starter.health().ok:
        raise StarterConfigError("the Temporal server is unreachable: a hosted server needs it")
    return starter
