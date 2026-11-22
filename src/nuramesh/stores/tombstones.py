"""Pure helpers for key tombstones (R1.37) shared by every store: the backfill of migration 12 and
the coalesced security-log count."""

import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from nuramesh.core.auth import MAX_COUNT
from nuramesh.core.content import KeyTombstone, normalise_tombstone_actor
from nuramesh.stores.base import BadTimeError

_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def shred_entries(
    rows: Iterable[tuple[str, datetime | BadTimeError, str, str]],
) -> list[tuple[str, datetime, str]]:
    """From `(stream, at, actor, payload_json)` of `decision.recorded` entries: for every `org:<id>`
    stream with a `content_shred` entry, `(org_id, earliest time, that entry's actor)`.

    A stored `at` that is not a real time arrives as the `BadTimeError` the caller built for it.
    The payload kind is read first (ND277, T256): a readable decision that is not a content shred
    never needs its time, so it is skipped and cannot fail the call for every organization. A
    content shred, or a payload that cannot be read (it could have been the shred), with a bad
    time raises that error."""
    found: dict[str, tuple[datetime, str]] = {}
    for stream, at, actor, payload in rows:
        if not stream.startswith("org:") or not _SLUG.fullmatch(stream[4:]):
            continue
        try:
            kind = json.loads(payload).get("kind")
        except (ValueError, AttributeError):
            if isinstance(at, BadTimeError):
                raise at from None
            continue
        if kind != "content_shred":
            continue
        if isinstance(at, BadTimeError):
            raise at
        org = stream[4:]
        if org not in found or at < found[org][0]:
            found[org] = (at, actor)
    return [(org, at, actor) for org, (at, actor) in sorted(found.items())]


def backfill_shreds(
    rows: Iterable[tuple[str, datetime | BadTimeError, str, str]],
    bad_time_fallback: Callable[[], datetime],
) -> list[tuple[str, datetime, str, bool]]:
    """Migration 12's view of `shred_entries` (T258, ND233, ND277): the store must open and no
    shred is dropped, so nothing here raises. `(org_id, time, actor, approximate)` for every
    `org:<id>` stream with a confirmed `content_shred` or a decision that cannot be read (it could
    have been the shred: the id is retired, fail closed). A confirmed shred with a readable time
    keeps that time; one with a bad time, and an unreadable decision, are approximate (the
    unreadable one is dated by its own time if that is readable, else by `bad_time_fallback`,
    read only when needed so a clean store never ticks its clock). A confirmed exact shred wins."""
    found: dict[str, tuple[bool, datetime, str]] = {}
    for stream, at, actor, payload in rows:
        if not stream.startswith("org:") or not _SLUG.fullmatch(stream[4:]):
            continue
        try:
            kind = json.loads(payload).get("kind")
            unreadable = False
        except (ValueError, AttributeError):
            kind, unreadable = None, True
        if kind != "content_shred" and not unreadable:
            continue
        approximate = unreadable or isinstance(at, BadTimeError)
        if isinstance(at, BadTimeError):
            at = bad_time_fallback()
        org = stream[4:]
        if org not in found or (approximate, at) < found[org][:2]:
            found[org] = (approximate, at, actor)
    return [(org, at, actor, approx) for org, (approx, at, actor) in sorted(found.items())]


def backfill_records(shreds: Iterable[tuple[str, datetime, str, bool]]) -> list[KeyTombstone]:
    """The tombstones migration 12 writes: reason `backfilled` (`backfilled_approximate` when the
    date is not the ledger's), key id unknown, a legacy actor that is not an opaque id stored as
    `system` (the migration never fails on an actor)."""
    return [
        KeyTombstone(
            org_id=org,
            destroyed_at=at.astimezone(UTC).replace(microsecond=0),
            actor=normalise_tombstone_actor(actor),
            reason="backfilled_approximate" if approximate else "backfilled",
            key_id=None,
            backfilled=True,
        )
        for org, at, actor, approximate in shreds
    ]


def bumped(stored: int | None) -> int:
    """The count after one more detection: 1 for a new row, else +1 saturating at `MAX_COUNT`."""
    return 1 if stored is None else min(stored + 1, MAX_COUNT)
