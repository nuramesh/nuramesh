"""Test doubles for the Merge Gate signer core (VM-2): a keyless fake backend whose
"signatures" are hashes only its own `verify` accepts (never Ed25519, never trusted by a real
verifier), an in-memory ledger that can be told to fail, an in-memory decision store and a limiter
that refuses nothing. Keys here are generated when the test runs or are obviously fake."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from nuramesh.core.vm_formats import public_key_id
from nuramesh.core.vm_signer import LedgerRef, ReviewRequest, StoredDecision

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeSignerBackend:
    """A fake `SignerBackend`: the private half is a seed held in a closure; the public half is
    its SHA-256; a signature is SHA-512 over seed, domain and message (64 bytes)."""

    def __init__(self, seed: bytes = b"\x01" * 32) -> None:
        public = hashlib.sha256(b"fake-public:" + seed).hexdigest()
        self._public = public
        self._key_id = public_key_id(public)
        self.signed: list[tuple[bytes, bytes]] = []

        def _sign(domain: bytes, message: bytes) -> bytes:
            return hashlib.sha512(seed + domain + message).digest()

        self._sign = _sign
        self.fail = False

    @property
    def key_id(self) -> str:
        return self._key_id

    def public_key_hex(self) -> str:
        return self._public

    def sign(self, domain: bytes, message: bytes) -> bytes:
        if self.fail:
            raise OSError("fake backend failure")
        self.signed.append((domain, message))
        return self._sign(domain, message)

    def verify(self, public_key_hex: str, message: bytes, sig_hex: str) -> bool:
        """A `Verify` callable for `vm_verdicts.judge_verdict` that accepts this fake's output.
        `message` already carries the domain, as `signed_bytes` builds it."""
        if public_key_hex != self._public:
            return False
        return sig_hex == self._sign(b"", message).hex()


@dataclass
class FakeLedger:
    """Records appends as (type, actor, payload) and hands out increasing sequence numbers; the
    entry time is the clock's (the ledger's own time, which F-16 compares with `issued_at`)."""

    clock: Callable[[], datetime]
    entries: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False
    start_seq: int = 100
    stamps: list[tuple[int, datetime]] = field(default_factory=list)

    def append(self, event_type: str, actor: str, payload: Mapping[str, Any]) -> LedgerRef:
        if self.fail:
            raise ConnectionError("ledger unreachable")
        self.entries.append((event_type, actor, dict(payload)))
        ref = LedgerRef(seq=self.start_seq + len(self.entries), at=self.clock())
        self.stamps.append((ref.seq, ref.at))
        return ref

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [payload for kind, _, payload in self.entries if kind == event_type]


class MemoryDecisionStore:
    """`DecisionStore` in memory: current decision per (org, repo, pr, change_hash, reviewer)
    and every record by id (chain origins stay reachable)."""

    def __init__(self) -> None:
        self.current: dict[tuple[str, str, int, str, str], StoredDecision] = {}
        self.records: dict[str, StoredDecision] = {}

    def get(
        self, org_id: str, repo: str, pr: int, change_hash: str, reviewer_id: str
    ) -> StoredDecision | None:
        return self.current.get((org_id, repo, pr, change_hash, reviewer_id))

    def by_id(self, verdict_id: str) -> StoredDecision | None:
        return self.records.get(verdict_id)

    def for_change(self, org_id: str, repo: str, change_hash: str) -> list[StoredDecision]:
        return [
            d
            for d in self.records.values()
            if d.org_id == org_id and d.repo == repo and d.change_hash == change_hash
        ]

    def put(self, decision: StoredDecision) -> None:
        self.records[decision.id] = decision
        key = (
            decision.org_id,
            decision.repo,
            decision.pr,
            decision.change_hash,
            decision.payload.reviewer_id,
        )
        self.current[key] = decision


class NoLimits:
    """A `Limits` that refuses nothing (the limiter is a port; VM-2's Sonnet part fills it)."""

    def __init__(self) -> None:
        self.runs: list[tuple[ReviewRequest, str]] = []
        self.rebinds: list[ReviewRequest] = []
        self.answer: str | None = None

    def check_run(self, request: ReviewRequest, task_key: str) -> str | None:
        self.runs.append((request, task_key))
        return self.answer

    def check_rebind(self, request: ReviewRequest) -> str | None:
        self.rebinds.append(request)
        return self.answer


class Clock:
    def __init__(self, now: datetime = T0 + timedelta(hours=1)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now = self.now + timedelta(**delta)


def ids() -> Any:
    """A deterministic `new_id` callable: 32-hex record ids counting up."""
    counter = {"n": 0}

    def next_id() -> str:
        counter["n"] += 1
        return f"{counter['n']:032x}"

    return next_id
