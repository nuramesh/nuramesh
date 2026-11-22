"""The Temporal worker's first activity: take a start request and begin the run (ND347 6.1, 4.6).

The API (the `TemporalStarter`) only records a durable start request and starts the workflow by
id. The worker, which has the team's code, does the rest here: claim the request atomically,
check its age, have the `RunBuilder` build the engine and plan, run the deny checks and call
`begin_run` once. This is the same sequence as the light runner's (`LightRunner._begin`), with the
same refusal codes, so a request ends the same way on either engine, except that this intake
refuses every real plan (`real_model_not_supported`, `spec/real-models` R2: Temporal runs the fake
model only at launch) and skips the sandbox check (R12). No `temporalio` import: the
function is plain and testable on a store alone.

Outcomes: `started` (the run exists), `refused` (the request ended with a code and one ledger
entry), `held` (another worker holds the claim, or the organization is halted: look again
later) and `none` (nothing for this worker to begin: no request, because the caller began the
run itself, or a request of another engine, or a worker with no builder).
"""

from datetime import timedelta
from typing import Final

from nuramesh.engine.admission import TEMPORAL_ADMISSION
from nuramesh.engine.content import ContentService
from nuramesh.engine.light import RunBuilder
from nuramesh.engine.light.begin import (
    ClaimLost,
    begin_from_request,
    extend_claimed_plan_text,
    org_halted,
)
from nuramesh.stores import ConcurrencyError, Store
from nuramesh.stores.run_queue import START_REFUSED, START_STARTED

ENGINE: Final = "temporal"
INTAKE_STARTED: Final = "started"
INTAKE_REFUSED: Final = "refused"
INTAKE_HELD: Final = "held"
INTAKE_NONE: Final = "none"

DEFAULT_LEASE: Final = timedelta(seconds=60)
DEFAULT_START_TTL: Final = timedelta(hours=1)
DEFAULT_RECHECK: Final = timedelta(seconds=30)


def take_start_request(
    store: Store,
    builder: RunBuilder | None,
    org_id: str,
    run_id: str,
    *,
    owner: str,
    lease: timedelta = DEFAULT_LEASE,
    start_ttl: timedelta = DEFAULT_START_TTL,
    recheck: timedelta = DEFAULT_RECHECK,
    content: ContentService | None = None,
) -> str:
    """Claim the start request of one run and begin it. Safe to call again (a retried or
    duplicated activity): a request that ended is reported, never begun twice."""
    with store.transaction() as uow:
        existing = uow.get_start_request(org_id, run_id)
        if existing is None:
            return INTAKE_NONE
        if existing.state == START_STARTED:
            return INTAKE_STARTED
        if existing.state == START_REFUSED:
            return INTAKE_REFUSED
        if existing.engine != ENGINE or builder is None:
            return INTAKE_NONE  # not this worker's to begin: the plan read then says so
        if org_halted(store, org_id):  # the halt check comes before the claim: nothing is touched
            return INTAKE_HELD
        row = uow.claim_start_request(owner, ENGINE, lease, run=(org_id, run_id))
        if row is None and existing.claim_owner == owner:  # our own claim, a retried activity
            if uow.hold_start_request(org_id, run_id, owner, lease):
                row = uow.get_start_request(org_id, run_id)
        if row is not None and content is not None:  # the plan text lives as long as the wait
            extend_claimed_plan_text(content, uow, row, store.now())
    if row is None:
        return INTAKE_HELD
    try:
        begin_from_request(
            store,
            builder,
            row,
            owner,
            start_ttl=start_ttl,
            recheck=recheck,
            content=content,
            on_started=lambda uow, r: None,  # the workflow needs no job row
            admission=TEMPORAL_ADMISSION,  # spec/real-models R2: the fake model only
        )
    except (
        ConcurrencyError
    ):  # another worker began the run first: back off, it finishes the request
        with store.transaction() as uow:
            uow.release_start_request(org_id, run_id, owner)
        return INTAKE_HELD
    except ClaimLost:  # the claim was taken over (our lease ran out): the new claimer finishes it
        return INTAKE_HELD
    with store.transaction() as uow:
        ended = uow.get_start_request(org_id, run_id)
    if ended is not None and ended.state == START_STARTED:
        return INTAKE_STARTED
    if ended is not None and ended.state == START_REFUSED:
        return INTAKE_REFUSED
    return INTAKE_HELD  # held for a later look (a halt in force)
