"""The run-start port (R1.20, ND347 section 3, as revised): what a server needs from an engine.

The API does **not** build an engine or call `begin_run`. It stores a **durable start request**
(`StartRequest`: ids and references only) and returns; a **runner** (the light engine's runner, or
the Temporal worker) claims the request atomically, builds the engine from the team's code, checks
the plan and starts the run. `begin_run` is not idempotent, so the claim is what keeps two runners
from starting one request twice.

Stdlib only: no Temporal, no pydantic model. An implementation lives next to its engine
(`nuramesh.engine.light.LightStarter`; the Temporal one is `nuramesh.temporal`, PR C).
"""

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nuramesh.stores import Store

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class StartState(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_REQUESTED = "already_requested"


@dataclass(frozen=True)
class PlanItem:
    """One shape-validated task of a plan (ND347 4.1): a local `ref`, a `role` name, the refs it
    depends on, and either an org task-template id or the content-store ids of the title and the
    definition of done. Never text: the text lives in the content store (4.3, PR B)."""

    ref: str
    role: str
    depends_on: tuple[str, ...] = ()
    template: str | None = None
    title_ref: str | None = None
    dod_ref: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("ref", self.ref), ("role", self.role)):
            if not _ID.fullmatch(value):
                raise ValueError(f"a plan item needs a valid {name}")
        for other in (
            *self.depends_on,
            *(x for x in (self.template, self.title_ref, self.dod_ref) if x),
        ):
            if not _ID.fullmatch(other):
                raise ValueError("a plan item holds ids only")
        if self.template is not None and (self.title_ref or self.dod_ref):
            raise ValueError("a plan item names a template or its own text, not both")


def canonical_plan(items: "tuple[PlanItem, ...]") -> str:
    """The plan as canonical JSON (sorted keys, no spaces): what the request row stores and what
    the keyed plan hash covers."""
    return json.dumps(
        [
            {
                "ref": i.ref,
                "role": i.role,
                "depends_on": list(i.depends_on),
                "template": i.template,
                "title_ref": i.title_ref,
                "dod_ref": i.dod_ref,
            }
            for i in items
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_plan(text: str) -> "tuple[PlanItem, ...]":
    """Read `canonical_plan` back; `ValueError` for anything else."""
    try:
        raw = json.loads(text)
        return tuple(
            PlanItem(
                ref=r["ref"],
                role=r["role"],
                depends_on=tuple(r.get("depends_on", ())),
                template=r.get("template"),
                title_ref=r.get("title_ref"),
                dod_ref=r.get("dod_ref"),
            )
            for r in raw
        )
    except (TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("the stored plan is not a plan") from exc


@dataclass(frozen=True)
class StartRequest:
    """A request to start a run (ND347 section 3, revision 3). Ids, hashes and references only."""

    org_id: str
    run_id: str
    """Assigned by the server (derived from the idempotency scope, MA2), never by the client."""
    plan_hash: str
    plan: tuple[PlanItem, ...]
    """The shape-validated plan: refs, roles, edges, content ids; no text."""
    data_class: str
    started_by: str
    """The acting principal's opaque id; the runner makes it the actor of `run.started`."""
    idempotency_key_hash: str | None
    engine: str
    """`light` or `temporal`: the runner or worker of that engine claims it."""


class RunIdConflict(ValueError):
    """The run id exists with another plan hash (the route answers 409 `run_id_conflict`)."""


@dataclass(frozen=True)
class StartResult:
    run_id: str
    state: StartState


@dataclass(frozen=True)
class StarterHealth:
    """Whether the engine can take a start now. `detail` is a fixed code, never free text."""

    engine: str
    ok: bool
    detail: str = ""


@runtime_checkable
class RunStarter(Protocol):
    """What the server asks of an engine."""

    name: str

    def start(self, request: StartRequest) -> StartResult:
        """Record the request durably and return; never waits for the run. Idempotent on the
        request id and on (organization, idempotency key)."""
        ...

    def wake(self, org_id: str, run_id: str, task_id: str = "") -> bool:
        """A decision was recorded: look at the database now. A hint, never trusted. False if the
        engine knows no such unfinished run."""
        ...

    def stop(self, org_id: str, run_id: str) -> bool:
        """The stop request is already recorded (the stop route writes it, with the acting
        human); make the engine look at it now. False if the engine knows no such unfinished
        run."""
        ...

    def health(self) -> StarterHealth: ...


def _first_run_started_org(first: Any) -> str | None:
    """The organization named by a stream's first entry, or None if that entry is not a
    `run.started` (or there is none). The one rule both foreign-run checks share.

    `run.started` is the immutable first entry of a run's stream: it is only ever written via
    `append_first`, and a `StreamTaken` error refuses it on a non-empty stream. So a stream that
    opens with anything else (the local `eval-` and `cal-` streams open with a config entry) can
    never become any organization's run, and it is owned by nobody."""
    from nuramesh.core.ledger import EventType

    if first and first[0].type == EventType.RUN_STARTED.value:
        return str(first[0].payload.get("org"))
    return None


def run_of_another_organization(store: "Store", org_id: str, run_id: str) -> bool:
    """Whether a run with this id already exists in another organization (X10). A starter checks
    it before it writes anything, so a request can never reach into a foreign run's stream. It
    reads the first entry only, the same rule as `refuse_foreign_run`."""
    from nuramesh.stores.base import run_stream

    owner = _first_run_started_org(list(store.read_stream(run_stream(run_id)))[:1])
    return owner is not None and owner != org_id


class ForeignRun(ValueError):
    """The run id belongs to another organization: nothing may be written to its stream (X10)."""


def refuse_foreign_run(uow: Any, org_id: str, run_id: str) -> None:
    """Raise `ForeignRun` if `run_id` was started by an organization other than `org_id`. Call it
    first inside the unit of work that writes to the run's stream, before the first append: the
    first entry of a run's stream is its `run.started` (see `_first_run_started_org`). A run with
    no stream yet, or one that does not open with `run.started`, is nobody's."""
    from nuramesh.stores.base import run_stream

    owner = _first_run_started_org(uow.events_after(run_stream(run_id), 0, 1))
    if owner is not None and owner != org_id:
        raise ForeignRun(run_id)
