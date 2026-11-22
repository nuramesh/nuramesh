"""The durable queue of the light engine (R1.20 PR A, ND347): start requests, jobs, a leader lease.

Three small tables: two hold **ids, states, owners and times only** (no title, text, address,
host name or process id); `run_start_requests` also keeps the plan text, plan_hash, started_by,
data_class and idempotency_key_hash:

- `run_start_requests`: a durable request to start a run (request id, organization, the acting
  principal's opaque id, a reference to the plan, the idempotency key, the engine). A runner takes
  it with an **atomic claim** (`claim_start_request`): one claimer at a time, a lease, a growing
  attempt count. `begin_run` is not idempotent, so nothing may call it without holding the claim.
- `light_jobs`: the schedule of started runs (`ready`, `waiting_human`, `done`), a due time, a
  lease owner (a random id) and an **epoch** that grows at every claim, so the late write of an
  owner whose lease was taken over is refused.
- `light_leader`: one row per engine name; the holder is the only one that executes.

All rules live here once, over four primitives each store implements (`_rq_get`, `_rq_insert`,
`_rq_update`, `_rq_scan`) on rows as plain dicts; times are canonical text (`format_time`), which
sorts like time.

Two clocks (ND347 5.3a/5.4, ND276a). The FENCE clock (`fence_at`) judges every lease: a start
claim, a job lease (claim, renewal, expiry, the fence of an acknowledging write) and the leader
lease and epoch, because several runners on different hosts must share ONE clock. On PostgreSQL it
is the database's own clock, read inside the unit of work that holds the write lock and the row
locks (so the read and the compare-and-set are one atomic step); on memory and SQLite it is the
store clock. Everything else (a start request's TTL and `created_at`, a job's due time, refusal
timing) keeps the unit of work's own `at`, the store's injectable clock (ND276a).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from nuramesh.core.ledger import format_time

START_PENDING: Final = "pending"
START_CLAIMED: Final = "claimed"
START_STARTED: Final = "started"
START_REFUSED: Final = "refused"
START_STATES: Final = (START_PENDING, START_CLAIMED, START_STARTED, START_REFUSED)

JOB_READY: Final = "ready"
JOB_WAITING: Final = "waiting_human"
JOB_DONE: Final = "done"
JOB_STATES: Final = (JOB_READY, JOB_WAITING, JOB_DONE)
JOB_ACTIVE: Final = (JOB_READY, JOB_WAITING)

UNWOKEN: Final = "1970-01-01T00:00:00.000000Z"
"""The `due_at` of a leased job (claim sets it). `wake_light_job` replaces it with the store time,
so `settle_light_job` can tell a wake that landed during the pass and keep the job due (ND347 5.2:
a wake sets `due_at` to now, and a pass that read the task earlier must not undo it)."""

TABLE_REQUESTS: Final = "run_start_requests"
TABLE_JOBS: Final = "light_jobs"
TABLE_LEADER: Final = "light_leader"

COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    TABLE_REQUESTS: (
        "org_id",
        "run_id",
        "plan_hash",
        "state",
        "created_at",
        "claim_owner",
        "claim_until",
        "refusal_code",
        "engine",
        "started_by",
        "data_class",
        "idempotency_key_hash",
        "plan",
    ),  # fmt: skip
    TABLE_JOBS: ("org_id", "run_id", "state", "due_at", "lease_owner", "lease_until", "epoch"),
    TABLE_LEADER: ("name", "owner", "lease_until", "epoch"),
}
KEYS: Final[dict[str, tuple[str, ...]]] = {
    TABLE_REQUESTS: ("org_id", "run_id"),
    TABLE_JOBS: ("org_id", "run_id"),
    TABLE_LEADER: ("name",),
}


def _t(at: datetime | None) -> str | None:
    return None if at is None else format_time(at)


def _d(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text).astimezone(UTC)


@dataclass(frozen=True)
class StartRequestRow:
    """A durable request to start a run (ids, hashes, times, a state and a fixed refusal code; the
    plan holds task refs, roles and content ids, never text)."""

    org_id: str
    run_id: str
    plan_hash: str
    state: str
    created_at: datetime
    claim_owner: str | None
    claim_until: datetime | None
    refusal_code: str | None
    engine: str
    started_by: str
    data_class: str
    idempotency_key_hash: str | None
    plan: str
    """The shape-validated plan as canonical JSON."""


@dataclass(frozen=True)
class LightJobRow:
    """One started run on the schedule."""

    org_id: str
    run_id: str
    state: str
    due_at: datetime
    lease_owner: str | None
    lease_until: datetime | None
    epoch: int


@dataclass(frozen=True)
class LeaderRow:
    """The holder of an engine's leader lease."""

    name: str
    owner: str
    lease_until: datetime
    epoch: int


def _request(row: dict[str, Any]) -> StartRequestRow:
    return StartRequestRow(
        org_id=row["org_id"],
        run_id=row["run_id"],
        plan_hash=row["plan_hash"],
        state=row["state"],
        created_at=_d(row["created_at"]),  # type: ignore[arg-type]
        claim_owner=row["claim_owner"],
        claim_until=_d(row["claim_until"]),
        refusal_code=row["refusal_code"],
        engine=row["engine"],
        started_by=row["started_by"],
        data_class=row["data_class"],
        idempotency_key_hash=row["idempotency_key_hash"],
        plan=row["plan"],
    )


def _job(row: dict[str, Any]) -> LightJobRow:
    return LightJobRow(
        org_id=row["org_id"],
        run_id=row["run_id"],
        state=row["state"],
        due_at=_d(row["due_at"]),  # type: ignore[arg-type]
        lease_owner=row["lease_owner"],
        lease_until=_d(row["lease_until"]),
        epoch=int(row["epoch"]),
    )


def _leader(row: dict[str, Any]) -> LeaderRow:
    return LeaderRow(
        name=row["name"],
        owner=row["owner"],
        lease_until=_d(row["lease_until"]),  # type: ignore[arg-type]
        epoch=int(row["epoch"]),
    )


class RunQueueOps(ABC):
    """The rules of the three tables over four primitives (see the module docstring)."""

    @property
    @abstractmethod
    def at(self) -> datetime:
        """The unit of work's one time."""

    @property
    def fence_at(self) -> datetime:
        """The time every lease is judged on (module docstring). The store clock here;
        PostgreSQL overrides it with the database clock."""
        return self.at

    @abstractmethod
    def _rq_get(self, table: str, key: tuple[str, ...]) -> dict[str, Any] | None:
        """One row by key, locked for update where the database has row locks."""

    @abstractmethod
    def _rq_insert(self, table: str, row: dict[str, Any]) -> bool:
        """Insert unless the key exists. True if inserted."""

    @abstractmethod
    def _rq_update(self, table: str, row: dict[str, Any]) -> None:
        """Replace the non-key columns of the row with this key."""

    @abstractmethod
    def _rq_scan(self, table: str, state_in: tuple[str, ...] | None) -> list[dict[str, Any]]:
        """Rows whose `state` is in `state_in` (None: all), locked for update where possible."""

    @abstractmethod
    def _rq_count(self, table: str, org_id: str, state_in: tuple[str, ...] | None) -> int:
        """How many rows of one organization have a state in `state_in` (None: any): a plain
        count, no row lock."""

    def count_active_runs(self, org_id: str) -> int:
        """Start requests not yet begun or refused (`pending`, `claimed`) plus light jobs not
        `done`, of one organization: the figure the run cap checks. A count, no row lock, and
        never another organization's rows."""
        return self._rq_count(TABLE_REQUESTS, org_id, (START_PENDING, START_CLAIMED)) + (
            self._rq_count(TABLE_JOBS, org_id, JOB_ACTIVE)
        )

    # -- start requests -------------------------------------------------------------------------

    def put_start_request(
        self,
        org_id: str,
        run_id: str,
        *,
        plan_hash: str,
        plan: str,
        data_class: str,
        started_by: str,
        idempotency_key_hash: str | None,
        engine: str,
    ) -> tuple[StartRequestRow, str]:
        """Store a request, unique on (organization, run id). Returns the stored row and
        `created`, `same` (the row exists with the same plan hash and data class) or `conflict` (it
        exists with another plan hash or data class: nothing is changed)."""
        row = {
            "org_id": org_id,
            "run_id": run_id,
            "plan_hash": plan_hash,
            "state": START_PENDING,
            "created_at": _t(self.at),
            "claim_owner": None,
            "claim_until": None,
            "refusal_code": None,
            "engine": engine,
            "started_by": started_by,
            "data_class": data_class,
            "idempotency_key_hash": idempotency_key_hash,
            "plan": plan,
        }
        if self._rq_insert(TABLE_REQUESTS, row):
            return _request(row), "created"
        found = self._rq_get(TABLE_REQUESTS, (org_id, run_id))
        assert found is not None
        # the plan hash and the data class both identify the request: a reused run id with
        # another data class is a conflict too (ND347 4.4), never a silent "already requested"
        same = found["plan_hash"] == plan_hash and found["data_class"] == data_class
        return _request(found), "same" if same else "conflict"

    def get_start_request(self, org_id: str, run_id: str) -> StartRequestRow | None:
        row = self._rq_get(TABLE_REQUESTS, (org_id, run_id))
        return None if row is None else _request(row)

    def start_requests(self, state: str | None = None) -> list[StartRequestRow]:
        """Requests (oldest first), optionally of one state."""
        rows = self._rq_scan(TABLE_REQUESTS, None if state is None else (state,))
        rows.sort(key=lambda r: (r["created_at"], r["org_id"], r["run_id"]))
        return [_request(r) for r in rows]

    def _claimable(self, engine: str) -> list[dict[str, Any]]:
        now = _t(self.fence_at)
        assert now is not None
        return [
            r
            for r in self._rq_scan(TABLE_REQUESTS, (START_PENDING, START_CLAIMED))
            if r["engine"] == engine
            and (r["state"] == START_PENDING or (r["claim_until"] or "") <= now)
        ]

    def claimable_start_orgs(self, engine: str) -> frozenset[str]:
        """The organizations that have a request a claim could take now (pending, or claimed with
        the claim run out): what the runner checks for a halt before it claims anything."""
        return frozenset(r["org_id"] for r in self._claimable(engine))

    def claim_start_request(
        self,
        owner: str,
        engine: str,
        lease_for: timedelta,
        *,
        skip_orgs: frozenset[str] = frozenset(),
        run: tuple[str, str] | None = None,
    ) -> StartRequestRow | None:
        """Compare-and-set the oldest request of this engine from `pending` to `claimed`, or take
        over a `claimed` one whose `claim_until` passed (its claimer died). One claimer wins.
        Requests of `skip_orgs` (halted organizations) are left untouched. `run=(org_id, run_id)`
        claims that one request only (the Temporal worker's first activity is about one run)."""
        rows = [
            r
            for r in self._claimable(engine)
            if r["org_id"] not in skip_orgs and (run is None or (r["org_id"], r["run_id"]) == run)
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: (r["created_at"], r["org_id"], r["run_id"]))
        row = rows[0]
        row.update(
            state=START_CLAIMED, claim_owner=owner, claim_until=_t(self.fence_at + lease_for)
        )
        self._rq_update(TABLE_REQUESTS, row)
        return _request(row)

    def _held(self, org_id: str, run_id: str, owner: str) -> dict[str, Any] | None:
        row = self._rq_get(TABLE_REQUESTS, (org_id, run_id))
        if row is None or row["state"] != START_CLAIMED or row["claim_owner"] != owner:
            return None
        return row

    def hold_start_request(
        self, org_id: str, run_id: str, owner: str, lease_for: timedelta
    ) -> bool:
        """Keep a claim for `lease_for` more (also how a halted organization's request waits
        without being claimed again every tick). False unless `owner` holds the claim."""
        row = self._held(org_id, run_id, owner)
        if row is None:
            return False
        row["claim_until"] = _t(self.fence_at + lease_for)
        self._rq_update(TABLE_REQUESTS, row)
        return True

    def finish_start_request(
        self, org_id: str, run_id: str, owner: str, state: str, *, code: str | None = None
    ) -> bool:
        """End a claimed request as `started` or `refused` (with a fixed code). False unless
        `owner` holds the claim."""
        if state not in (START_STARTED, START_REFUSED):
            raise ValueError("a request ends started or refused")
        row = self._held(org_id, run_id, owner)
        if row is None:
            return False
        row.update(state=state, refusal_code=code, claim_owner=None, claim_until=None)
        self._rq_update(TABLE_REQUESTS, row)
        return True

    def release_start_request(self, org_id: str, run_id: str, owner: str) -> bool:
        """Give a claim back (another runner began the run, or a transient failure)."""
        row = self._held(org_id, run_id, owner)
        if row is None:
            return False
        row.update(state=START_PENDING, claim_owner=None, claim_until=None)
        self._rq_update(TABLE_REQUESTS, row)
        return True

    # -- jobs -----------------------------------------------------------------------------------

    def put_light_job(self, org_id: str, run_id: str, due_at: datetime | None = None) -> bool:
        """Schedule a run (`ready`, due now unless said). False if the run has a job already."""
        return self._rq_insert(
            TABLE_JOBS,
            {
                "org_id": org_id,
                "run_id": run_id,
                "state": JOB_READY,
                "due_at": _t(due_at or self.at),
                "lease_owner": None,
                "lease_until": None,
                "epoch": 0,
            },
        )

    def get_light_job(self, org_id: str, run_id: str) -> LightJobRow | None:
        row = self._rq_get(TABLE_JOBS, (org_id, run_id))
        return None if row is None else _job(row)

    def light_jobs(self, state: str | None = None) -> list[LightJobRow]:
        rows = self._rq_scan(TABLE_JOBS, None if state is None else (state,))
        rows.sort(key=lambda r: (r["org_id"], r["run_id"]))
        return [_job(r) for r in rows]

    def claim_light_jobs(self, owner: str, lease_for: timedelta, limit: int) -> list[LightJobRow]:
        """Lease up to `limit` jobs that are due and not leased (or whose lease ran out), earliest
        due first. Each claim raises the job's epoch. Due time is the store clock; the lease is
        judged on the fence clock."""
        now = _t(self.at)
        lease_now = _t(self.fence_at)
        assert now is not None and lease_now is not None
        due = [
            r
            for r in self._rq_scan(TABLE_JOBS, (JOB_READY, JOB_WAITING))
            if r["due_at"] <= now
            and (r["lease_owner"] is None or (r["lease_until"] or "") <= lease_now)
        ]
        due.sort(key=lambda r: (r["due_at"], r["org_id"], r["run_id"]))
        claimed: list[LightJobRow] = []
        for row in due[: max(0, limit)]:
            row.update(
                lease_owner=owner,
                lease_until=_t(self.fence_at + lease_for),
                epoch=int(row["epoch"]) + 1,
                due_at=UNWOKEN,  # a wake during the lease writes a real time over this marker
            )
            self._rq_update(TABLE_JOBS, row)
            claimed.append(_job(row))
        return claimed

    def _leased(self, org_id: str, run_id: str, owner: str, epoch: int) -> dict[str, Any] | None:
        row = self._rq_get(TABLE_JOBS, (org_id, run_id))
        if row is None or row["lease_owner"] != owner or int(row["epoch"]) != epoch:
            return None
        return row

    def light_job_held(self, org_id: str, run_id: str, owner: str, epoch: int) -> bool:
        """Whether the job is still this owner's at this epoch with a lease that has not run out,
        read inside this unit of work (the fence of an acknowledging write, ND347 5.3a)."""
        row = self._leased(org_id, run_id, owner, epoch)
        return row is not None and (row["lease_until"] or "") > (_t(self.fence_at) or "")

    def renew_light_job(
        self, org_id: str, run_id: str, owner: str, epoch: int, lease_for: timedelta
    ) -> bool:
        """Extend a lease. False if the job is no longer this owner's at this epoch."""
        row = self._leased(org_id, run_id, owner, epoch)
        if row is None:
            return False
        row["lease_until"] = _t(self.fence_at + lease_for)
        self._rq_update(TABLE_JOBS, row)
        return True

    def settle_light_job(
        self,
        org_id: str,
        run_id: str,
        owner: str,
        epoch: int,
        state: str,
        due_at: datetime | None,
    ) -> bool:
        """End a pass: the new state and due time, the lease released. A `done` job stays done.
        False (nothing written) if the job is no longer this owner's at this epoch."""
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state '{state}'")
        if not self.light_job_held(org_id, run_id, owner, epoch):
            return False  # the lease ran out, or was taken over: a late write is refused
        row = self._leased(org_id, run_id, owner, epoch)
        assert row is not None
        woken = row["due_at"] != UNWOKEN  # a wake landed during the pass: it is not overwritten
        row.update(
            state=state,
            due_at=_t(self.at) if woken else _t(due_at or self.at),
            lease_owner=None,
            lease_until=None,
        )
        self._rq_update(TABLE_JOBS, row)
        return True

    def wake_light_job(self, org_id: str, run_id: str) -> bool:
        """Make the job due now (a hint, never trusted). False for an unknown or finished job."""
        row = self._rq_get(TABLE_JOBS, (org_id, run_id))
        if row is None or row["state"] == JOB_DONE:
            return False
        row["due_at"] = _t(self.at)
        self._rq_update(TABLE_JOBS, row)
        return True

    def free_light_leases(self, keep_owner: str) -> int:
        """Release the leases of every unfinished job held by someone else (a new leader: the
        old one is gone, or its late writes are refused by the epoch). Returns how many."""
        freed = 0
        for row in self._rq_scan(TABLE_JOBS, (JOB_READY, JOB_WAITING)):
            if row["lease_owner"] is not None and row["lease_owner"] != keep_owner:
                row.update(lease_owner=None, lease_until=None)
                self._rq_update(TABLE_JOBS, row)
                freed += 1
        return freed

    # -- leader ---------------------------------------------------------------------------------

    def take_leader(self, name: str, owner: str, lease_for: timedelta) -> int | None:
        """Become, or stay, the leader of `name`: the epoch (it grows when the leader changes), or
        `None` if someone else holds a lease that has not run out."""
        until = _t(self.fence_at + lease_for)
        now = _t(self.fence_at)
        assert now is not None
        for _ in range(2):
            row = self._rq_get(TABLE_LEADER, (name,))
            if row is None:
                row = {"name": name, "owner": owner, "lease_until": until, "epoch": 1}
                if self._rq_insert(TABLE_LEADER, row):
                    return 1
                continue
            if row["owner"] == owner:
                row["lease_until"] = until
                self._rq_update(TABLE_LEADER, row)
                return int(row["epoch"])
            if row["lease_until"] <= now:
                row.update(owner=owner, lease_until=until, epoch=int(row["epoch"]) + 1)
                self._rq_update(TABLE_LEADER, row)
                return int(row["epoch"])
            return None
        return None

    def release_leader(self, name: str, owner: str) -> bool:
        """Give up the lease at once (a clean shutdown); the row stays, the epoch keeps growing."""
        row = self._rq_get(TABLE_LEADER, (name,))
        if row is None or row["owner"] != owner:
            return False
        row["lease_until"] = _t(self.fence_at - timedelta(microseconds=1))
        self._rq_update(TABLE_LEADER, row)
        return True

    def get_leader(self, name: str) -> LeaderRow | None:
        row = self._rq_get(TABLE_LEADER, (name,))
        return None if row is None else _leader(row)
