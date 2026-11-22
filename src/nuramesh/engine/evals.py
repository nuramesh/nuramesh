"""Run eval suites and decide who may take new work (PLAN D.6; T27).

`run_eval` asks a **responder** for an answer to each case, scores the answers
(`core.evals.score_suite`) and records one `eval.scored` event in the organization's stream
(`org:<org id>`): verdicts, rule details and response hashes, never the answers. The responder is a
plain callable, so the caller decides how answers are produced (a model client with its budget and
policy checks, the fake model in tests); an exception from it stops the eval and records nothing,
while returning `None` records "no response" for that case.

`agent_eligibility` reads the recorded results, re-derives each one from its outcomes against the
current suite (`core.evals.verified`: a forged or inconsistent record counts as a failure) and
applies `core.evals.check_eligibility` to the agent's role and effective model. Response hashes
are keyed (HMAC-SHA256 under the organization's key, PH-KEY, kind `eval_response`) and the payload
carries `hash_scheme`, `hash_key_id` and `hash_org` beside `outcomes`; a row written before that
holds a plain SHA-256 (legacy, `verify-ledger` counts them). The ledger isn't a place for secrets
(A7). Unreadable `eval.scored` events raise rather than being skipped: skipping
could hide a newer failure and let an older pass count.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from nuramesh.core.content import MarkedLedgerHasher
from nuramesh.core.evals import (
    Eligibility,
    EvalCase,
    EvalConfig,
    EvalRecord,
    EvalSuite,
    SubjectKind,
    check_eligibility,
    score_suite,
    verified,
)
from nuramesh.core.ledger import Actor, EventType
from nuramesh.core.org import OrgGraph
from nuramesh.core.presets import PresetConfig, effective_suites
from nuramesh.stores.base import EventDraft, Store

Responder = Callable[[EvalCase], Any]
_ACTOR = TypeAdapter(Actor)  # the ledger's own actor rule, whole string


class EvalLedgerError(ValueError):
    """An `eval.scored` event couldn't be read; eligibility can't be decided safely."""


def org_stream(org_id: str) -> str:
    """The stream for events outside runs."""
    return f"org:{org_id}"


def run_eval(
    store: Store,
    suite: EvalSuite,
    *,
    org: OrgGraph,
    kind: SubjectKind,
    subject_id: str,
    respond: Responder,
    hasher: MarkedLedgerHasher,
    model: str | None = None,
    actor: str = "system",
) -> EvalRecord:
    """Answer every case, score the suite and record the result; returns the stored record.

    Everything that could make the record unwritable is checked **before** the responder runs
    (it may cost money): the subject must exist and match the suite's role, the actor must be
    valid, and an agent's model is the one the org gives it (`model` is only for role-level evals,
    which need one). The payload of any legal suite fits in one ledger event (the suite limits
    in `core.evals` guarantee it), so writing it can't fail for size.
    """
    try:
        _ACTOR.validate_python(actor)
    except ValidationError:
        raise ValueError(f"'{actor}' isn't a valid actor") from None
    if kind is SubjectKind.AGENT:
        agent = org.agent(subject_id)  # raises for an unknown agent
        if org.role_of(subject_id).id != suite.role:
            raise ValueError(f"agent '{subject_id}' isn't in role '{suite.role}'")
        if model is not None and model != org.model_for(subject_id):
            raise ValueError(f"agent '{agent.id}' answers with {org.model_for(subject_id)}")
        model = org.model_for(subject_id)
    else:
        if subject_id != suite.role:
            raise ValueError(f"role evals for suite '{suite.id}' are for role '{suite.role}'")
        org.role(subject_id)
        if model is None:
            raise ValueError("a role-level eval needs the model that answers")
    responses = {case.id: respond(case) for case in suite.cases}
    score = score_suite(suite, {k: v for k, v in responses.items() if v is not None}, hasher)
    payload = EvalRecord.ledger_payload(kind, subject_id, model, score)
    if any(o.response_hash is not None for o in score.outcomes):
        payload.update(hasher.marker())  # top level, beside `outcomes` (PH-KEY R4)
    with store.transaction() as uow:
        event = uow.append(
            EventDraft(
                stream=org_stream(org.id),
                type=EventType.EVAL_SCORED,
                actor=actor,
                payload=payload,
            )
        )
    return EvalRecord.from_event(event)


def load_eval_records(store: Store, org_id: str) -> list[EvalRecord]:
    """Every recorded eval result of an organization, oldest first."""
    records: list[EvalRecord] = []
    for event in store.read_stream(org_stream(org_id)):
        if event.type != EventType.EVAL_SCORED.value:
            continue
        try:
            records.append(EvalRecord.from_event(event))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            raise EvalLedgerError(
                f"eval.scored event {event.seq} of {org_stream(org_id)} is unreadable "
                f"({type(exc).__name__})"
            ) from None
    return records


def agent_eligibility(
    store: Store,
    config: EvalConfig | None,
    org: OrgGraph,
    *,
    agent_id: str,
    presets: PresetConfig | None = None,
    now: datetime | None = None,
) -> Eligibility:
    """Whether the agent may take new work under its role's eval requirements and, when
    `presets` is given, the suites its role's gate preset names (`config` may then be None: every
    preset suite is then not defined, which fails closed)."""
    role = org.role_of(agent_id).id
    if config is None and presets is None:
        raise ValueError("agent_eligibility needs an eval configuration or presets")
    suites = {s.id: s for s in config.suites} if config is not None else {}
    records = [
        verified(r, suites[r.suite_id]) if r.suite_id in suites else r
        for r in load_eval_records(store, org.id)
    ]
    return check_eligibility(
        config,
        role=role,
        agent=agent_id,
        model=org.model_for(agent_id),
        records=records,
        now=now or store.now(),
        suites=effective_suites(config, presets, role) if presets is not None else None,
    )
