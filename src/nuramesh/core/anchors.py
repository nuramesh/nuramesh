"""Ledger head anchors: the data and the rules (R1.04a, ND22). No signing and no Git here.

A hash chain proves a stream wasn't edited *against a head kept outside the writer's database*.
An **anchor** is that head, signed and written somewhere else: `(stream, seq, head hash, time)`
plus the id of the key that signed it. Anchors for one stream form a **file of lines** (JSON, one
anchor per line, append-only); the anchor repository keeps one file per stream in Git, so Git's
own history is a second tamper-evident chain.

`anchor_message` is the exact bytes that get signed. `parse_anchor_lines` reads a file strictly.
`check_anchors` compares anchors with a stream's events and reports every problem; it knows
nothing about keys (the engine verifies signatures first and passes the verified anchors in).
"""

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from nuramesh.core.canonical import MAX_SAFE_INTEGER, canonical_json
from nuramesh.core.ledger import Hash, LedgerEvent, StreamId, format_time

MAX_LINE = 4_096
_KEY_ID = re.compile(r"^[0-9a-f]{16}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")  # Ed25519: 64 bytes in hex


class Anchor(BaseModel):
    """One signed head of one stream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = Field(default=1, ge=1, le=1)
    stream: StreamId
    seq: int = Field(ge=1, le=MAX_SAFE_INTEGER)  # the signed message can't carry a larger one
    head: Hash
    at: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")]
    key_id: Annotated[str, StringConstraints(pattern=_KEY_ID.pattern)]
    sig: Annotated[str, StringConstraints(pattern=_SIGNATURE.pattern)]

    @field_validator("at")
    @classmethod
    def _real_time(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        return value

    def message(self) -> bytes:
        """The bytes the signature covers."""
        return anchor_message(self.stream, self.seq, self.head, self.at, self.key_id)

    def line(self) -> str:
        """The anchor as one JSON line (stable key order)."""
        return canonical_json(self.model_dump(mode="json")).decode("utf-8")


def anchor_message(stream: str, seq: int, head: str, at: str, key_id: str) -> bytes:
    """The signed bytes: a versioned, unambiguous encoding of the anchor's fields."""
    return canonical_json(
        {"v": 1, "stream": stream, "seq": seq, "head": head, "at": at, "key_id": key_id}
    )


def anchor_time(at: datetime) -> str:
    """The canonical anchor timestamp (UTC, microseconds)."""
    return format_time(at)


class AnchorFileError(ValueError):
    """An anchor file can't be read; the message says which line and why."""


def parse_anchor_lines(text: str, *, stream: str) -> list[Anchor]:
    """Read an anchor file strictly: one JSON object per line, all for `stream`."""
    anchors: list[Anchor] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise AnchorFileError(f"line {number}: blank lines aren't allowed")
        if len(line) > MAX_LINE:
            raise AnchorFileError(f"line {number}: too long")
        try:
            anchor = Anchor.model_validate(json.loads(line))
        except (ValueError, TypeError) as exc:
            raise AnchorFileError(
                f"line {number}: not a valid anchor ({type(exc).__name__})"
            ) from None
        if anchor.stream != stream:
            raise AnchorFileError(f"line {number}: belongs to stream '{anchor.stream}'")
        anchors.append(anchor)
    return anchors


@dataclass(frozen=True)
class AnchorProblem:
    """One thing wrong between anchors and a stream."""

    code: str
    detail: str


def check_anchors(
    stream: str, anchors: Sequence[Anchor], events: Sequence[LedgerEvent]
) -> list[AnchorProblem]:
    """Compare **already signature-verified** anchors with the stream's events.

    Every anchor must name an event that exists with exactly that hash (so nothing at or before
    it was changed or removed), anchors must never go backwards in `seq` or `at`, and an anchor
    may not be ahead of the stream (events removed from the end).
    """
    problems: list[AnchorProblem] = []
    by_seq: Mapping[int, LedgerEvent] = {e.seq: e for e in events}
    last = events[-1].seq if events else 0
    previous: Anchor | None = None
    for anchor in anchors:
        if anchor.seq > last:
            problems.append(
                AnchorProblem(
                    "stream_shorter",
                    f"anchored at seq {anchor.seq} but the stream ends at {last}: "
                    "events were removed",
                )
            )
        else:
            event = by_seq.get(anchor.seq)
            if event is None or event.hash != anchor.head:
                problems.append(
                    AnchorProblem(
                        "head_mismatch", f"the event at seq {anchor.seq} isn't the anchored one"
                    )
                )
        if previous is not None and (anchor.seq < previous.seq or anchor.at < previous.at):
            problems.append(
                AnchorProblem("anchors_out_of_order", f"anchor at seq {anchor.seq} goes backwards")
            )
        previous = anchor
    return problems


def unanchored_events(anchors: Sequence[Anchor], events: Sequence[LedgerEvent]) -> int:
    """How many of the stream's events are newer than its last anchor."""
    if not events:
        return 0
    return events[-1].seq - (anchors[-1].seq if anchors else 0)


def anchor_filename(stream: str) -> str:
    """The file for a stream in the anchor repository (`run:r1` becomes `run__r1.jsonl`)."""
    return stream.replace(":", "__") + ".jsonl"


# -- key rotation (R1.30 PR 5, H-14) -----------------------------------------------------------

KEY_FILE = "keys.jsonl"
KEY_CHANGE_DOMAIN = b"nuramesh-anchor-key-change/1\n"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
_TIME = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$"
Timestamp = Annotated[str, StringConstraints(pattern=_TIME)]

REPOSITORY_PROBLEMS = frozenset(
    {
        "anchor_history_rewritten",
        "anchor_history_not_linear",
        "key_change_invalid",
        "key_file_rewritten",
        "key_file_removed",
    }
)
KEY_ROTATED_KEYS = frozenset(
    {"prev_key_id", "new_key_id", "chained", "n", "record_sha256", "compromised_since", "commit"}
)
"""The closed payload key set of the `anchor.key_rotated` ledger event (ND60: minimal facts)."""


def _check_time(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    return value


class KeyChange(BaseModel):
    """One signed key-change record: one line of `keys.jsonl` (rule R10)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = Field(default=1, ge=1, le=1)
    n: int = Field(ge=1)
    repo_root: Annotated[str, StringConstraints(pattern=_OBJECT_ID.pattern)]
    prev_key_id: Annotated[str, StringConstraints(pattern=_KEY_ID.pattern)]
    new_key_id: Annotated[str, StringConstraints(pattern=_KEY_ID.pattern)]
    new_public_key: Annotated[str, StringConstraints(pattern=_HEX64.pattern)]
    at: Timestamp
    compromised_since: Timestamp | None = None
    sig_old: Annotated[str, StringConstraints(pattern=_SIGNATURE.pattern)]
    sig_new: Annotated[str, StringConstraints(pattern=_SIGNATURE.pattern)]

    @field_validator("at", "compromised_since")
    @classmethod
    def _real_time(cls, value: str | None) -> str | None:
        return None if value is None else _check_time(value)

    def message(self) -> bytes:
        """The bytes both signatures cover."""
        return key_change_message(
            n=self.n,
            repo_root=self.repo_root,
            prev_key_id=self.prev_key_id,
            new_key_id=self.new_key_id,
            new_public_key=self.new_public_key,
            at=self.at,
            compromised_since=self.compromised_since,
        )

    def line(self) -> str:
        """The record as one JSON line (stable key order)."""
        return canonical_json(self.model_dump(mode="json")).decode("utf-8")


def key_change_message(
    *,
    n: int,
    repo_root: str,
    prev_key_id: str,
    new_key_id: str,
    new_public_key: str,
    at: str,
    compromised_since: str | None,
) -> bytes:
    """The signed bytes of a key change: a domain prefix (so it can never be replayed as an anchor
    signature, nor the reverse) and the canonical JSON of the fields."""
    return KEY_CHANGE_DOMAIN + canonical_json(
        {
            "v": 1,
            "n": n,
            "repo_root": repo_root,
            "prev_key_id": prev_key_id,
            "new_key_id": new_key_id,
            "new_public_key": new_public_key,
            "at": at,
            "compromised_since": compromised_since,
        }
    )


def key_rotated_payload(
    *,
    prev_key_id: str,
    new_key_id: str,
    chained: bool,
    n: int | None,
    record_sha256: str | None,
    compromised_since: str | None,
    commit: str,
) -> dict[str, object]:
    """The `anchor.key_rotated` payload, validated: exactly the closed key set, typed values."""
    return validate_key_rotated_payload(
        {
            "prev_key_id": prev_key_id,
            "new_key_id": new_key_id,
            "chained": chained,
            "n": n,
            "record_sha256": record_sha256,
            "compromised_since": compromised_since,
            "commit": commit,
        }
    )


def validate_key_rotated_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Refuse any key outside `KEY_ROTATED_KEYS` and any value of the wrong type."""
    if set(payload) != KEY_ROTATED_KEYS:
        raise ValueError("anchor.key_rotated payload keys must be exactly the closed set")
    for name in ("prev_key_id", "new_key_id"):
        value = payload[name]
        if not isinstance(value, str) or not _KEY_ID.match(value):
            raise ValueError(f"{name} must be a key id")
    chained, n, digest = payload["chained"], payload["n"], payload["record_sha256"]
    if not isinstance(chained, bool):
        raise ValueError("chained must be a boolean")
    if chained:
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("n must be a positive integer when chained")
        if not isinstance(digest, str) or not _HEX64.match(digest):
            raise ValueError("record_sha256 must be a SHA-256 when chained")
    elif n is not None or digest is not None:
        raise ValueError("n and record_sha256 must be null when not chained")
    since = payload["compromised_since"]
    if since is not None:
        if not isinstance(since, str) or not re.match(_TIME, since):
            raise ValueError("compromised_since must be a timestamp or null")
        _check_time(since)
    commit = payload["commit"]
    if not isinstance(commit, str) or not _OBJECT_ID.match(commit):
        raise ValueError("commit must be a git object id")
    return dict(payload)


@dataclass(frozen=True)
class ChainKey:
    """A key in the chain: its id and raw public key (64 hex)."""

    key_id: str
    public_key: str


@dataclass
class KeyChain:
    """The result of walking `keys.jsonl`: the valid prefix, and the first problem if any."""

    keys: list[ChainKey]
    records: list[KeyChange] = field(default_factory=list)
    problem: AnchorProblem | None = None


def _invalid(n: int, reason: str, detail: str = "") -> AnchorProblem:
    text = f"record {n}: {reason}" + (f" ({detail})" if detail else "")
    return AnchorProblem("key_change_invalid", text)


def walk_key_chain(
    text: str,
    *,
    root: ChainKey | None,
    repo_roots: Collection[str],
    verify: Callable[[str, str, bytes], bool],
    derive_id: Callable[[str], str | None],
) -> KeyChain:
    """Walk the key file from the trusted root (rule R11) and return the valid prefix.

    `verify(public_key_hex, signature_hex, message)` checks one Ed25519 signature and
    `derive_id(public_key_hex)` gives the key id of a raw public key (None if it is not one); both
    come from the engine, so this module keeps to the standard library and pydantic. With
    `root=None` (the `rotate` command, which is not told the root) the first record's
    `prev_key_id` is taken as given and its `sig_old` is not checked; verifiers always pass the
    root. The first invalid record stops the walk.
    """
    chain = KeyChain(keys=[] if root is None else [root])
    previous_at: str | None = None
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip() or len(line.encode("utf-8")) > MAX_LINE:
            chain.problem = _invalid(number, "unreadable", "blank or too long line")
            return chain
        try:
            record = KeyChange.model_validate(json.loads(line))
        except (ValueError, TypeError):
            chain.problem = _invalid(number, "unreadable", "not a valid key-change record")
            return chain
        reason = _record_reason(
            record, number, chain, previous_at, root is None, repo_roots, verify, derive_id
        )
        if reason is not None:
            chain.problem = _invalid(number, reason)
            return chain
        if not chain.keys:  # unrooted walk: the first record names its own start
            chain.keys.append(ChainKey(record.prev_key_id, ""))
        chain.keys.append(ChainKey(record.new_key_id, record.new_public_key))
        chain.records.append(record)
        previous_at = record.at
    return chain


def _record_reason(
    record: KeyChange,
    number: int,
    chain: KeyChain,
    previous_at: str | None,
    unrooted: bool,
    repo_roots: Collection[str],
    verify: Callable[[str, str, bytes], bool],
    derive_id: Callable[[str], str | None],
) -> str | None:
    """Why a record isn't valid at its place in the chain, or None. The order of the checks fixes
    which reason code a forged record gets."""
    first_unrooted = unrooted and not chain.keys
    if record.n != number or (previous_at is not None and record.at < previous_at):
        return "out_of_order"
    if not first_unrooted and record.prev_key_id != chain.keys[-1].key_id:
        return "wrong_prev"
    if derive_id(record.new_public_key) != record.new_key_id:
        return "id_mismatch"
    if record.repo_root not in repo_roots:
        return "wrong_repository"
    if record.compromised_since is not None and record.compromised_since > record.at:
        return "bad_compromised_since"
    if any(k.key_id == record.new_key_id for k in chain.keys) or (
        first_unrooted and record.new_key_id == record.prev_key_id
    ):
        return "key_reused"
    message = record.message()
    if not first_unrooted and not verify(chain.keys[-1].public_key, record.sig_old, message):
        return "bad_old_signature"
    if not verify(record.new_public_key, record.sig_new, message):
        return "bad_new_signature"
    return None


@dataclass(frozen=True)
class Retirement:
    """How a retired (or bounded tail) key may still appear in a stream file.

    `lines` is the allowed prefix: how many lines the file had at the retiring commit. `at` and
    `compromised_since` come from the retiring record (None for a bounded tail, which has no
    record, so rules (b) and (c) don't apply)."""

    lines: int
    at: str | None = None
    compromised_since: str | None = None


def check_key_rules(
    anchors: Sequence[Anchor],
    ranks: Mapping[str, int],
    retired: Mapping[str, Retirement],
    skip: Collection[int] = (),
) -> tuple[list[AnchorProblem], set[int]]:
    """Which key may sign which anchor (rule R13), on **signature-verified** anchors of one file.

    `ranks` maps every known key id to its rank (unknown ids are skipped: the engine reports them
    as `wrong_key`). `retired` maps the ids of retired keys to their `Retirement`. `anchors` is the
    whole file in order (so an index is the line's place in the file); `skip` holds the indexes
    that didn't pass their signature check. Returns the problems and the 0-based indexes of the
    anchors that must not count as anchoring anything.
    """
    problems: list[AnchorProblem] = []
    rejected: set[int] = set()
    last_rank: int | None = None
    for index, anchor in enumerate(anchors):
        rank = ranks.get(anchor.key_id)
        if rank is None or index in skip:
            continue
        found = retired.get(anchor.key_id)
        if found is not None:
            if index >= found.lines or (found.at is not None and anchor.at >= found.at):
                problems.append(
                    AnchorProblem(
                        "anchor_after_key_change",
                        f"anchor at seq {anchor.seq} was signed by a key retired before it",
                    )
                )
                rejected.add(index)
            elif found.compromised_since is not None and anchor.at >= found.compromised_since:
                problems.append(
                    AnchorProblem(
                        "anchor_from_compromised_key",
                        f"anchor at seq {anchor.seq} is from a key claimed compromised by then",
                    )
                )
                rejected.add(index)
        if last_rank is not None and rank < last_rank:
            problems.append(
                AnchorProblem(
                    "anchor_key_regressed",
                    f"anchor at seq {anchor.seq} was signed by an older key than the anchor "
                    "before it",
                )
            )
            rejected.add(index)
        last_rank = rank if last_rank is None else max(last_rank, rank)
    return problems, rejected
