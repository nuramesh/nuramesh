"""The record of a start refused by a halt in force (R1.37; bounded by T154).

One function, used by BOTH the engine (`RunEngine.run`, `resume`, `begin_run`) and the CLI
(`nuramesh try`, `nuramesh eval`), so a halted start leaves the same footprint whichever way it
was started, and nothing else (no run stream, seed, reconcile, budget or task).

**Bounded growth (T154, `spec/t154/README.md`):** the first refusal of a halt episode (the seq of
the governing `org.halted`) appends the one `model.blocked` record (`reason` `org_halted`, `stage`
`start`, the run id, the time, the `episode`). Later refusals are only counted in two mutable
security-log rows (ND60): a marker (`org_halted_episode`) and a pending counter
(`org_halted_pending`). The pending count enters the append-only ledger as ONE summary
(`model.blocked`, `reason` `org_halted_coalesced`, `count`, `since`, `at`) in the same transaction
that removes the pending row: on the first refusal after the window, at the 1,000,000 cap, at
resume, from the breaker ticker, and before the 90-day purge. Nothing is edited or deleted in the
ledger and no count is lost. Every refusal does ALL of its reads and writes in one transaction.

An ERASED organization gets no org-scoped write at all (its id is retired for the life of the
store): the refusal is counted in the coalesced security-log row `run.start_refused` instead
(reason `org_halted`; no person, no run id, no route). If erasure cannot be ruled out (a tombstone
that cannot be read), the organization is treated as erased: fail closed.

**The late path (R0):** a caller whose decision read saw a halt (`E`) but whose in-transaction
read finds none (a human resume won the race) gets ONE plain `model.blocked` record, no counters:
one such row per caller that raced a resume (also a caller that could not read the halt, when the
transaction finds none). If the outside read and the in-transaction
read keep disagreeing (a persistent divergence between the caller's store view and the
transaction's), every refusal is a late row and the bound does not hold; the two reads are of the
same store, so that is a fault, not a design case.

Best effort: the store may be what is down. A failed write is logged, never raised.
"""

import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from nuramesh.core.auth import (
    HALT_EPISODE_REASON,
    HALT_PENDING_REASON,
    HALT_REASON,
    MAX_COUNT,
    SecurityLogEntry,
    coalesced_row_id,
    halt_row_id,
)
from nuramesh.core.breakers import halt_state
from nuramesh.core.ledger import EventType, LedgerEvent, format_time
from nuramesh.engine.org_halt import PAGE, org_stream_id
from nuramesh.stores.base import EventDraft, Store, UnitOfWork

_LOG = logging.getLogger(__name__)

START_REFUSED = "run.start_refused"
"""The security-log type of a halted start under an erased organization (a coalesced row)."""
REFUSAL_ACTOR = "system"
"""The ledger actor of the refusal record: the engine's default `EngineSettings.actor`, the same
value the CLI writes (one source; the engine passes its own settings actor, default this)."""
COALESCED_REASON = "org_halted_coalesced"
"""The `reason` of the summary entry (T154)."""
UNKNOWN = "unknown"
"""The episode when the halt state cannot be read (T154 R9)."""
SUMMARY_ACTOR = "system"
"""The actor of every summary written by resume, the ticker or the 90-day fold (R1a)."""
PENDING_CAP = MAX_COUNT
"""A pending count flushes before it would pass this. A module value so a test can lower it."""
WINDOW_ENV = "NURAMESH_HALT_REFUSAL_WINDOW_SECONDS"
DEFAULT_WINDOW_SECONDS = 3600
MAX_WINDOW_SECONDS = 86400


class HaltWindowError(ValueError):
    """A bad `NURAMESH_HALT_REFUSAL_WINDOW_SECONDS` value (fail closed, never a silent default)."""


def halt_window_seconds(environ: Mapping[str, str] | None = None) -> int:
    """The summary window W in seconds (T154 R12): unset is `DEFAULT_WINDOW_SECONDS`; anything else
    must be a whole number from 1 to 86400 or `HaltWindowError` names the variable and the range."""
    env = os.environ if environ is None else environ
    if WINDOW_ENV not in env:
        return DEFAULT_WINDOW_SECONDS
    raw = env[WINDOW_ENV].strip()
    if not re.fullmatch(r"[0-9]{1,6}", raw) or not 1 <= int(raw) <= MAX_WINDOW_SECONDS:
        raise HaltWindowError(f"{WINDOW_ENV} must be a whole number from 1 to {MAX_WINDOW_SECONDS}")
    return int(raw)


_STORE_ID = re.compile(r"[0-9a-f]{32}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def ring_tombstone_absent(key_dir: Path, store_id: str, org_id: str) -> bool:
    """True only when the ring tombstone file `<key_dir>/<store_id>/<org_id>.tombstone` is clearly
    absent. Stdlib only (for when the `content` extra is missing): it never opens, creates or
    writes anything, only looks the path up. Present in any form, a malformed name or any other
    error is False (ambiguous: the caller treats the organization as erased)."""
    try:
        if not (_STORE_ID.fullmatch(store_id) and _SLUG.fullmatch(org_id)):
            return False
        os.lstat(key_dir / store_id / f"{org_id}.tombstone")
    except FileNotFoundError:
        return True
    except Exception:
        return False
    return False


def organization_erased(store: Store, org_id: str, content: Any | None = None) -> bool:
    """Whether this organization id was erased (a key tombstone in the store, or in the ring when
    a content service is given). Anything that cannot be read counts as erased (fail closed)."""
    try:
        if store.key_tombstone(org_id) is not None:
            return True
        return content is not None and content.retired(org_id) is not None
    except Exception:
        return True


def _erased_in(uow: UnitOfWork, org_id: str, content: Any | None = None) -> bool:
    """`organization_erased` read inside the transaction (T154 R0 step 1); fail closed."""
    try:
        if uow.key_tombstone(org_id) is not None:
            return True
        return content is not None and content.retired(org_id) is not None
    except Exception:
        return True


# -- the rows --------------------------------------------------------------------------------


def _marker_id(org_id: str, episode: str) -> str:
    return halt_row_id("halt.episode", org_id, episode)


def _pending_id(org_id: str, episode: str) -> str:
    return halt_row_id("halt.pending", org_id, episode)


def _episode_value(text: str) -> int | str:
    return int(text) if text.isdigit() else text


def _day_row(org_id: str, now: datetime, count: int) -> SecurityLogEntry:
    """The day-coalesced `run.start_refused` row of an erased organization (ND121)."""
    return SecurityLogEntry.new(
        org_id,
        now,
        START_REFUSED,
        actor="system",
        details={"reason": HALT_REASON, "count": count},
    ).model_copy(update={"id": coalesced_row_id(START_REFUSED, org_id, now, HALT_REASON)})


def _due(row: SecurityLogEntry, now: datetime, window: int) -> bool:
    return (now - row.at).total_seconds() >= window


def _count(row: SecurityLogEntry) -> int:
    return int(row.details.get("count", 1) or 1)


def _events(uow: UnitOfWork, stream: str) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    cursor = 0
    while True:
        batch = uow.events_after(stream, cursor, PAGE)
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        cursor = batch[-1].seq


def _flush(
    uow: UnitOfWork, org_id: str, row_id: str, now: datetime, actor: str, *, erased: bool | None
) -> int | None:
    """Take the pending row and account for its count in ONE transaction (T154 R5): a summary in
    the ledger, or, for an erased organization, an addition to the day-coalesced row. Returns the
    count flushed, or None when there was no row (then nothing is written)."""
    taken = uow.take_security_log_count(org_id, row_id)
    if taken is None:
        return None
    count = _count(taken)
    if erased is None:
        erased = _erased_in(uow, org_id)
    if erased:
        uow.add_security_log_count(
            org_id,
            coalesced_row_id(START_REFUSED, org_id, now, HALT_REASON),
            count,
            create=_day_row(org_id, now, count),
        )
        return count
    episode = str(taken.details.get("episode", UNKNOWN))
    uow.append(
        EventDraft(
            stream=org_stream_id(org_id),
            type=EventType.MODEL_BLOCKED,
            actor=actor,
            payload={
                "reason": COALESCED_REASON,
                "stage": "start",
                "episode": _episode_value(episode),
                "count": count,
                "since": format_time(taken.at),
                "at": format_time(now),
            },
        )
    )
    return count


def _end_unknown_stretch(
    uow: UnitOfWork, org_id: str, now: datetime, actor: str, *, erased: bool | None
) -> int:
    """R9: flush the `unknown` pending row (due or not) and take the `unknown` marker, so a later
    unreadable stretch starts with a new first record. Returns the ledger rows appended."""
    flushed = _flush(uow, org_id, _pending_id(org_id, UNKNOWN), now, actor, erased=erased)
    uow.take_security_log_count(org_id, _marker_id(org_id, UNKNOWN))
    return 0 if flushed is None or erased else 1


def flush_for_resume(uow: UnitOfWork, org_id: str, episode: int | None, now: datetime) -> None:
    """Called by `resume_org` inside its transaction, before `org.resumed` (R5c): flush the lifted
    episode's pending row and the `unknown` stretch. The summaries carry the actor `system`."""
    erased = _erased_in(uow, org_id)
    _end_unknown_stretch(uow, org_id, now, SUMMARY_ACTOR, erased=erased)
    if episode is not None:
        _flush(uow, org_id, _pending_id(org_id, str(episode)), now, SUMMARY_ACTOR, erased=erased)


def flush_due_pending(store: Store, org_id: str, tick: datetime, window: int) -> int:
    """The breaker ticker's flush (R5d): every DUE pending row of the organization becomes a
    summary (or a fold, when erased), each in its own transaction that re-reads the row and
    re-checks that it is due. A due `unknown` row also takes the `unknown` marker. Returns the
    number of rows flushed. `tick` is only a pre-filter hint (which rows to open a transaction
    for); the due check and the summary's time are the unit of work's one time (ND234)."""
    hints = [
        e
        for e in store.list_security_log(org_id)
        if e.details.get("reason") == HALT_PENDING_REASON and _due(e, tick, window)
    ]
    flushed = 0
    for hint in hints:
        with store.transaction() as uow:
            now = uow.now()
            current = uow.get_security_log(org_id, hint.id)
            if current is None or not _due(current, now, window):
                continue
            erased = _erased_in(uow, org_id)
            _flush(uow, org_id, hint.id, now, SUMMARY_ACTOR, erased=erased)
            if str(current.details.get("episode")) == UNKNOWN:
                uow.take_security_log_count(org_id, _marker_id(org_id, UNKNOWN))
            flushed += 1
    return flushed


def fold_expired_pending(uow: UnitOfWork) -> int:
    """Run in the transaction of EVERY `purge_security_log` call, before it (R7): each pending row
    at or past its 90-day expiry becomes a summary in the ledger (actor `system`, the episode read
    from the row), or, for an erased organization, an addition to the day-coalesced row. So no
    pending row outlives its 90 days once a purge has run. Returns the rows folded. The time is
    the unit of work's own (ND234): the caller passes none."""
    now = uow.now()
    done = 0
    for row in uow.list_expired_security_log(now, HALT_PENDING_REASON):
        if _flush(uow, row.org_id, row.id, now, SUMMARY_ACTOR, erased=None) is not None:
            done += 1
    return done


def record_halt_refusal(
    store: Store,
    org_id: str,
    run_id: str,
    *,
    episode: int | None,
    actor: str = REFUSAL_ACTOR,
    content: Any | None = None,
    assume_erased: bool = False,
    window_seconds: int | None = None,
) -> str:
    """Record ONE refused start (T154 R0 to R10), everything in one transaction. `episode` is the
    seq of the governing `org.halted` as the caller read it, or None when the caller could not
    read the halt state. `window_seconds` is W (default: the setting). Returns where it went:
    `ledger` (it appended at least one entry), `security_log` (only counted, or an erased
    organization), or `none` (the transaction failed and rolled back; logged)."""
    window = halt_window_seconds() if window_seconds is None else window_seconds
    caller = UNKNOWN if episode is None else str(episode)
    try:
        with store.transaction() as uow:
            now = uow.now()  # the unit's one time, read inside the write lock (ND234)
            if assume_erased or _erased_in(uow, org_id, content):
                uow.upsert_security_log_count(_day_row(org_id, now, 1))
                return "security_log"
            try:
                state = halt_state(_events(uow, org_stream_id(org_id)))
                readable = True
            except Exception:
                state, readable = None, False
            if state is not None and not (state.halted and caller in (UNKNOWN, str(state.seq))):
                # the late path (R0): a human resume won the race; one plain record, no counters
                uow.append(_record(org_id, run_id, now, actor, _episode_value(caller)))
                return "ledger"
            current = UNKNOWN if state is None else str(state.seq)
            appended = 0
            if readable:
                appended += _end_unknown_stretch(uow, org_id, now, actor, erased=False)
            pid = _pending_id(org_id, current)
            pending = uow.get_security_log(org_id, pid)
            if pending is not None and (
                _due(pending, now, window) or _count(pending) >= PENDING_CAP
            ):
                _flush(uow, org_id, pid, now, actor, erased=False)
                appended += 1
            marker = SecurityLogEntry.new(
                org_id,
                now,
                START_REFUSED,
                actor="system",
                details={"reason": HALT_EPISODE_REASON, "count": 1},
            ).model_copy(update={"id": _marker_id(org_id, current)})
            if uow.upsert_security_log_count(marker) == 1:
                uow.append(_record(org_id, run_id, now, actor, _episode_value(current)))
                appended += 1
            else:
                uow.upsert_security_log_count(
                    SecurityLogEntry.new(
                        org_id,
                        now,
                        START_REFUSED,
                        actor="system",
                        details={
                            "reason": HALT_PENDING_REASON,
                            "count": 1,
                            "episode": current,
                        },
                    ).model_copy(update={"id": pid})
                )
        return "ledger" if appended else "security_log"
    except Exception:
        _LOG.warning("could not record the start refused by a halt", exc_info=True)
        return "none"


def _record(org_id: str, run_id: str, now: datetime, actor: str, episode: int | str) -> EventDraft:
    """The `model.blocked` record of a refused start (the pre-T154 shape plus `episode`)."""
    return EventDraft(
        stream=org_stream_id(org_id),
        type=EventType.MODEL_BLOCKED,
        actor=actor,
        payload={
            "reason": HALT_REASON,
            "stage": "start",
            "run": run_id,
            "at": format_time(now),
            "episode": episode,
        },
    )
