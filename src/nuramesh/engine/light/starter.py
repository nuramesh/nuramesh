"""The API side of the light engine: store a start request, wake a job, ask for a stop."""

from nuramesh.engine.run_engine import refuse_light_when_hosted
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

ENGINE = "light"


class LightStarter:
    """`RunStarter` over the store alone: it never builds an engine and never runs a step."""

    name = ENGINE

    def __init__(self, store: Store) -> None:
        refuse_light_when_hosted()
        self.store = store

    def start(self, request: StartRequest) -> StartResult:
        if request.engine != ENGINE:
            raise ValueError(f"the light starter takes engine '{ENGINE}' requests only")
        if run_of_another_organization(self.store, request.org_id, request.run_id):
            raise RunIdConflict(request.run_id)  # X10: no write into a foreign run
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
        state = StartState.ACCEPTED if outcome == "created" else StartState.ALREADY_REQUESTED
        return StartResult(request.run_id, state)

    def wake(self, org_id: str, run_id: str, task_id: str = "") -> bool:
        """Make the run's job due now. The runner reads the database before it acts, so a wake
        for a decision that was never recorded changes nothing (E08)."""
        with self.store.transaction() as uow:
            return uow.wake_light_job(org_id, run_id)

    def stop(self, org_id: str, run_id: str) -> bool:
        """The stop request was written by the stop route (it carries the acting human); wake the
        job so the loop finds it before the next step (5.5). False if the run has no live job."""
        return self.wake(org_id, run_id)

    def health(self) -> StarterHealth:
        try:
            with self.store.transaction() as uow:
                uow.get_leader("light")
        except Exception:
            return StarterHealth(ENGINE, False, "store_unreachable")
        return StarterHealth(ENGINE, True)
