"""Recording a run-start request (R1.20 PR B, ND347 4.4 and 4.5 step 11), shared by the API route
and the host CLI (`nuramesh start`): the plan text into the content store (a row per run and
text, subjects per ND157), the keyed plan hash, and the request row, all in the caller's
transaction. No team code and no ledger entry of its own."""

from collections.abc import Sequence

from nuramesh.core.content import ContentKind, LedgerHashKind
from nuramesh.core.idempotency import human_subjects
from nuramesh.engine.content import ContentService
from nuramesh.engine.run_request import PlanItem, plan_text_envelope
from nuramesh.engine.starter import PlanItem as StoredPlanItem
from nuramesh.engine.starter import canonical_plan
from nuramesh.stores import UnitOfWork


def record_start_request(
    uow: UnitOfWork,
    service: ContentService,
    *,
    org_id: str,
    run_id: str,
    actor_id: str,
    tasks: Sequence[PlanItem],
    data_class: str,
    engine: str,
    idempotency_key_hash: str | None,
    plan_text_days: int,
) -> str:
    """Store the request and its plan text. Returns `created`, `same` or `conflict` (the row's
    outcome); the text is written only for `created`."""
    texts = [
        text for item in tasks for text in (item.title, item.definition_of_done) if text is not None
    ]
    subjects = frozenset(human_subjects(*texts)) | {org_id, actor_id}
    stored_items: list[StoredPlanItem] = []
    envelopes: list[str] = []
    for item in tasks:
        pair: list[str | None] = [None, None]
        for slot, (name, text) in enumerate(
            (("title", item.title), ("dod", item.definition_of_done))
        ):
            if text is None:
                continue
            envelope = plan_text_envelope(run_id, name, text)
            pair[slot] = service.hash_in(uow, org_id, ContentKind.PLAN_TEXT, envelope)
            envelopes.append(envelope)
        stored_items.append(
            StoredPlanItem(
                ref=item.ref,
                role=item.role,
                depends_on=item.depends_on,
                template=item.template,
                title_ref=pair[0],
                dod_ref=pair[1],
            )
        )
    plan = canonical_plan(tuple(stored_items))
    # the runner refuses any other value (plan_hash_mismatch): the engine's own keyed plan hash,
    # made here with the same ledger hashing (PH-KEY)
    plan_hash = service.ledger_hashing.bind(uow, org_id)(LedgerHashKind.PLAN, plan.encode())
    _, outcome = uow.put_start_request(
        org_id,
        run_id,
        plan_hash=plan_hash,
        plan=plan,
        data_class=data_class,
        started_by=actor_id,
        idempotency_key_hash=idempotency_key_hash,
        engine=engine,
    )
    if outcome == "created":
        for envelope in envelopes:
            service.capture(
                uow,
                org_id,
                ContentKind.PLAN_TEXT,
                envelope,
                retention_days=plan_text_days,
                run_id=run_id,
                subjects=subjects,
            )
    return str(outcome)
