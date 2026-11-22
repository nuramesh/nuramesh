"""Verified Merge signer limits (spec `spec/verified-merge` SV-10, SV-8 d, SV-11; cases S-05,
S-06, S-14, S-17, S-18, S-21, S-23): a pure evaluator over what the ledger already holds.

No table and no counter of its own (the ledger is the truth, LA-5): the history is read from
the org stream's `merge.review_requested`, `merge.verdict_issued` and `merge.review_failed`
entries, exactly as `core.vm_signer` writes them.

- A **run** is one admitted review (`merge.review_requested`). It ends with its decision
  (`merge.verdict_issued` without `rebound_from`) or with a refusal that can only happen after
  the request was admitted (`RUN_END_REASONS`, and `head_moved` with detail `during_review`).
  An open run is `run_in_flight` for its PR until `run_timeout_seconds` passes (a crashed
  signer must not block a PR for ever; the hourly limits still count the run). The timeout is
  **not in the spec** (a deviation recorded in the decision file).
- A **decision** is a well-formed, signed ACCEPT or REJECT that is not a rebind. The task key
  is always present: the issue linked at the base, or the PR author's id when there is none
  (SV-10; `core.vm_signer.task_key_of` resolves it and refuses a request with neither). Per
  (repository, task key): at most `task_rejects` REJECTs with no time window (`reject_limit`;
  only a human reset clears it) and at most `task_decisions` decisions in `task_window_days`
  (`task_ask_limit`). A decision voided by a `merge.review_reset` of a higher sequence number
  (its task, its change hash, or a mode flip) does not count: SV-10 "by ledger order".
  A malformed proposal, a failed model step or any other refusal is never a decision (SV-11).
- A **rebind ask** is a rebind that was answered (a rebound verdict or `rebind_refused`); at most
  `rebinds_per_pr_per_hour`. Rebinds are in no other count (SV-8 d).

Order of the answers: the limits only a human can lift first (`reject_limit`,
`task_ask_limit`), then `run_in_flight`, then `rate_limited` (PR, then repository). "Within an
hour" and "in 30 days" are half-open: an entry exactly that old is outside.

- A **reset** counts only when `resets_from_ledger` made it: a `merge.review_reset` entry
  appended by a `human:<id>` actor (the F-8 actor table), carrying a 6.8 payload that the
  `admin_signed` port verified (an enrolled admin's approver-key signature; VM-3 supplies the
  check), signed by that same actor, for this org, each reset id once. Anything else in the
  ledger is not a reset: the `resets` port is a trust boundary, because one `mode_flip` reset
  voids every decision of the org.

**Not one transaction:** the limit check and the `merge.review_requested` append that follows it
in `Signer.admit` are two steps. The spec has one signer service per org; the adapter that hosts
the signer must hold a per-org lock from the check to the append (required of the adapter PR,
recorded in the decision file), or two signer processes could both admit a run.

A merge entry of this org that does not have the shape the signer writes is refused loudly
(`ValueError` with a fixed text): the limits never guess, and `core.vm_signer` turns any failure
of this port into `limits_unavailable` with nothing requested and nothing signed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nuramesh.core.vm_events import check_merge_actor
from nuramesh.core.vm_formats import ResetPayload
from nuramesh.core.vm_signer import ReviewRequest
from nuramesh.core.vm_verdicts import ResetRecord

REVIEW_REQUESTED = "merge.review_requested"
VERDICT_ISSUED = "merge.verdict_issued"
REVIEW_FAILED = "merge.review_failed"
REVIEW_RESET = "merge.review_reset"
RUN_END_REASONS = frozenset({"malformed_proposal", "model_failed", "budget_exceeded"})
"""Refusals that only happen after `merge.review_requested` was written for the run."""
HEAD_MOVED_DURING_REVIEW = ("head_moved", "during_review")
_MALFORMED = "limits: a merge entry is malformed"
_ORDER = "limits: ledger entries are not in sequence order"


@dataclass(frozen=True)
class LimitConfig:
    """SV-10 defaults. `run_timeout_seconds` is not in the spec: it bounds `run_in_flight`
    after a signer crash (decision file of this PR)."""

    repo_runs_per_hour: int = 6
    pr_runs_per_hour: int = 3
    task_rejects: int = 2
    task_decisions: int = 4
    task_window_days: int = 30
    rebinds_per_pr_per_hour: int = 6
    run_timeout_seconds: int = 1800


@dataclass(frozen=True)
class LedgerEntry:
    """One entry of the org stream as the limits read it. `actor` is the ledger's own record of
    who appended the entry (never a payload field); the default names nobody, so an adapter that
    does not pass it can never produce a reset."""

    seq: int
    at: datetime
    type: str
    payload: Mapping[str, Any]
    actor: str = ""


@dataclass(frozen=True)
class Run:
    seq: int
    at: datetime
    repo: str
    pr: int
    open: bool


@dataclass(frozen=True)
class Decision:
    seq: int
    at: datetime
    repo: str
    pr: int
    change_hash: str
    task_key: str
    decision: str


@dataclass(frozen=True)
class RebindAsk:
    seq: int
    at: datetime
    repo: str
    pr: int


@dataclass(frozen=True)
class History:
    runs: tuple[Run, ...] = ()
    decisions: tuple[Decision, ...] = ()
    rebinds: tuple[RebindAsk, ...] = ()


def _text(payload: Mapping[str, Any], name: str, *, optional: bool = False) -> str | None:
    value = payload.get(name)
    if value is None and optional:
        return None
    if type(value) is not str or not value:
        raise ValueError(_MALFORMED)
    return value


def _where(payload: Mapping[str, Any]) -> tuple[str, int]:
    repo = _text(payload, "repo")
    pr = payload.get("pr")
    if repo is None or type(pr) is not int or pr < 1:
        raise ValueError(_MALFORMED)
    return repo, pr


def history_from_ledger(entries: Iterable[LedgerEntry], org_id: str) -> History:
    """The limit history of one org from its ledger entries in sequence order. Entries of other
    types or of another org are ignored; a merge entry of this org with a wrong shape, or
    entries out of order, raise `ValueError` (fixed text)."""
    runs: list[Run] = []
    decisions: list[Decision] = []
    rebinds: list[RebindAsk] = []
    open_runs: dict[tuple[str, int], list[int]] = {}
    last_seq: int | None = None

    def end_run(where: tuple[str, int]) -> None:
        waiting = open_runs.get(where)
        if waiting:
            index = waiting.pop(0)
            run = runs[index]
            runs[index] = Run(run.seq, run.at, run.repo, run.pr, open=False)

    for entry in entries:
        if last_seq is not None and entry.seq <= last_seq:
            raise ValueError(_ORDER)
        last_seq = entry.seq
        if entry.type not in (REVIEW_REQUESTED, VERDICT_ISSUED, REVIEW_FAILED):
            continue
        payload = entry.payload
        if payload.get("org_id") != org_id:
            continue
        where = _where(payload)
        if entry.type == REVIEW_REQUESTED:
            open_runs.setdefault(where, []).append(len(runs))
            runs.append(Run(entry.seq, entry.at, where[0], where[1], open=True))
        elif entry.type == VERDICT_ISSUED:
            verdict = payload.get("decision")
            change_hash = _text(payload, "change_hash")
            if verdict not in ("ACCEPT", "REJECT") or change_hash is None:
                raise ValueError(_MALFORMED)
            if _text(payload, "rebound_from", optional=True) is not None:
                rebinds.append(RebindAsk(entry.seq, entry.at, where[0], where[1]))
                continue
            task_key = _text(payload, "task_key")
            if task_key is None:
                raise ValueError(_MALFORMED)
            decisions.append(
                Decision(
                    seq=entry.seq,
                    at=entry.at,
                    repo=where[0],
                    pr=where[1],
                    change_hash=change_hash,
                    task_key=task_key,
                    decision=verdict,
                )
            )
            end_run(where)
        else:
            reason = _text(payload, "reason")
            detail = _text(payload, "detail", optional=True)
            if reason == "rebind_refused":
                rebinds.append(RebindAsk(entry.seq, entry.at, where[0], where[1]))
            elif reason in RUN_END_REASONS or (reason, detail) == HEAD_MOVED_DURING_REVIEW:
                end_run(where)
    return History(tuple(runs), tuple(decisions), tuple(rebinds))


def _voided(decision: Decision, resets: Sequence[ResetRecord]) -> bool:
    for reset in resets:
        if reset.seq <= decision.seq:
            continue
        if reset.kind == "mode_flip":
            return True
        if reset.kind == "change_hash" and reset.value == decision.change_hash:
            return True
        if reset.kind == "task" and reset.value == decision.task_key:
            return True
    return False


AdminSigned = Callable[[LedgerEntry], ResetPayload | None]
"""The 6.8 signature check (VM-3): the reset payload embedded in a `merge.review_reset` entry,
returned only when its signature (domain `nuramesh-vm-reset-v1`) verifies against the approver
key of an enrolled admin; None for anything else. An exception is a failure of the port."""


def resets_from_ledger(
    entries: Iterable[LedgerEntry], org_id: str, *, admin_signed: AdminSigned
) -> tuple[ResetRecord, ...]:
    """The `ResetRecord`s of one org from its ledger entries in sequence order (SV-10). A
    `merge.review_reset` entry counts iff all hold: its ledger actor is a `human:<id>` (F-8: an
    agent, the signer, the gate or any other service can never reset); `admin_signed` returns
    its verified 6.8 payload; the payload's `approver_id` is that same actor (an admin's signed
    reset re-appended by somebody else is a replay); the payload names this org; and its
    `reset_id` was not counted before (the same reset at a higher sequence is a replay too).
    Every other entry is ignored: it voids nothing. Entries out of order, an `admin_signed` that
    answers anything but a `ResetPayload` or None, or one that raises, are loud (the caller
    turns that into `limits_unavailable` or `side_unavailable`)."""
    records: list[ResetRecord] = []
    seen: set[str] = set()
    last_seq: int | None = None
    for entry in entries:
        if last_seq is not None and entry.seq <= last_seq:
            raise ValueError(_ORDER)
        last_seq = entry.seq
        if entry.type != REVIEW_RESET:
            continue
        if check_merge_actor(REVIEW_RESET, entry.actor) is not None:
            continue
        signed = admin_signed(entry)
        if signed is None:
            continue
        if type(signed) is not ResetPayload:
            raise ValueError(_MALFORMED)
        if signed.approver_id != entry.actor or signed.org_id != org_id:
            continue
        if signed.reset_id in seen:
            continue
        seen.add(signed.reset_id)
        records.append(ResetRecord(entry.seq, signed.scope.kind, signed.scope.value))
    return tuple(records)


def check_run(
    history: History,
    *,
    repo: str,
    pr: int,
    task_key: str,
    now: datetime,
    resets: Sequence[ResetRecord],
    config: LimitConfig,
) -> str | None:
    """None when a new signer run may start for (repo, PR) under `task_key`, else one of
    `reject_limit`, `task_ask_limit`, `run_in_flight`, `rate_limited`. `task_key` is the linked
    issue or, with none, the PR author's id (SV-10): there is no shared counter for requests
    without an issue. A run that timed out (`run_timeout_seconds`) stops being `run_in_flight`
    but stays in the hourly counts."""
    counted = [
        d
        for d in history.decisions
        if d.repo == repo and d.task_key == task_key and not _voided(d, resets)
    ]
    if sum(1 for d in counted if d.decision == "REJECT") >= config.task_rejects:
        return "reject_limit"
    window = timedelta(days=config.task_window_days)
    if sum(1 for d in counted if now - d.at < window) >= config.task_decisions:
        return "task_ask_limit"
    timeout = timedelta(seconds=config.run_timeout_seconds)
    hour = timedelta(hours=1)
    repo_runs = pr_runs = 0
    for run in history.runs:
        if run.repo != repo:
            continue
        age = now - run.at
        if run.pr == pr and run.open and age < timeout:
            return "run_in_flight"
        if age < hour:
            repo_runs += 1
            if run.pr == pr:
                pr_runs += 1
    if pr_runs >= config.pr_runs_per_hour or repo_runs >= config.repo_runs_per_hour:
        return "rate_limited"
    return None


def check_rebind(
    history: History, *, repo: str, pr: int, now: datetime, config: LimitConfig
) -> str | None:
    """None when a rebind may be answered for (repo, PR), else `rate_limited` (SV-8 d)."""
    hour = timedelta(hours=1)
    asked = sum(1 for a in history.rebinds if a.repo == repo and a.pr == pr and now - a.at < hour)
    return "rate_limited" if asked >= config.rebinds_per_pr_per_hour else None


class LedgerLimits:
    """`core.vm_signer.Limits` over the ledger: `entries` returns the org stream in sequence
    order (an adapter over the store), `resets` the ledgered `merge.review_reset` records, which
    the adapter must build with `resets_from_ledger` (only admin-signed resets count)."""

    def __init__(
        self,
        *,
        org_id: str,
        entries: Callable[[], Iterable[LedgerEntry]],
        resets: Callable[[], Sequence[ResetRecord]],
        clock: Callable[[], datetime],
        config: LimitConfig | None = None,
    ) -> None:
        self._org_id = org_id
        self._entries = entries
        self._resets = resets
        self._clock = clock
        self._config = config or LimitConfig()

    def check_run(self, request: ReviewRequest, task_key: str) -> str | None:
        history = history_from_ledger(self._entries(), self._org_id)
        return check_run(
            history,
            repo=request.repo,
            pr=request.pr,
            task_key=task_key,
            now=self._clock(),
            resets=self._resets(),
            config=self._config,
        )

    def check_rebind(self, request: ReviewRequest) -> str | None:
        history = history_from_ledger(self._entries(), self._org_id)
        return check_rebind(
            history, repo=request.repo, pr=request.pr, now=self._clock(), config=self._config
        )
