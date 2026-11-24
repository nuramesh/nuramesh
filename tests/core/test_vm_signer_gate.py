"""The VM-2 signer's verdicts read by main's VM-1 gate step 6 (`vm_verdicts.evaluate_verdicts`).

The signer core is only useful if what it issues and rebinds is what the gate counts. These
tests run the signer's own output through main's `evaluate_verdicts` with its current signature
(the required `ai_quorum=` and the `UnparsedRecord` stream items of GC-15 b, f) and pin the GC-15 a
duplicate rules around rebinding: a rebound verdict and its origin are one decision, never
`decision_duplicate`; the signer's one-decision-per-change rule (SV-8 c) means it never produces a
duplicate itself; a second decision from the same reviewer that bypasses that rule is
`decision_duplicate`, and an ACCEPT and a REJECT from different reviewers are `accept_and_reject`.
Spec: README SV-8 c, d, GT-10; gate-clarifications GC-7, GC-9, GC-10, GC-15 a, b, e.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest

from nuramesh.core.vm_formats import payload_hash
from nuramesh.core.vm_signer import Issued, Signer
from nuramesh.core.vm_verdicts import (
    UnparsedRecord,
    VerdictOutcome,
    VerdictRecord,
    evaluate_verdicts,
)

from .test_vm_signer import (
    H0,
    H1,
    H2,
    ORG,
    REVIEWER,
    Rig,
    X,
    facts,
    proposal,
    request,
    trust,
    trust_key,
)
from .vm_fakes import FakeSignerBackend, MemoryDecisionStore, NoLimits

POINTER_ID = "a" * 32
MALFORMED_ID = "b" * 32


def record_of(issued: Issued) -> VerdictRecord:
    d = issued.decision
    return VerdictRecord(d.verdict, d.ledger_seq, d.ledger_at, payload_hash(d.verdict.payload))


def noise(rig: Rig) -> list[UnparsedRecord]:
    """A pointer comment and a malformed record that is not a copy (GC-15 b, case GC-87/GC-88):
    neither is a decision, both are listed ignored."""
    at = rig.clock.now - timedelta(minutes=5)
    return [
        UnparsedRecord(kind="pointer", verdict_id=POINTER_ID, source_at=at, comment_id=1),
        UnparsedRecord(kind="malformed", verdict_id=MALFORMED_ID, source_at=at, comment_id=2),
    ]


def evaluate(
    rig: Rig,
    records: Sequence[VerdictRecord | UnparsedRecord],
    *,
    head: str,
    rebind: Any = None,
    br: str = "base_moved",
    ai_quorum: int = 1,
    verify: Any = None,
) -> VerdictOutcome:
    return evaluate_verdicts(
        records,
        rig.view(head_sha=head),
        rig.signer.trust,
        verify or rig.backend.verify,
        (),
        ai_required=True,
        carry_forward=lambda _head: br,
        rebind=rebind,
        ai_quorum=ai_quorum,
    )


def test_main_evaluate_verdicts_signature_is_the_one_these_tests_drive() -> None:
    """Rebase guard: main's step 6 takes the `UnparsedRecord` stream and a required, keyword-only
    `ai_quorum` (the pre-merge VM-1 copy on the signer branch had neither)."""
    params = inspect.signature(evaluate_verdicts).parameters
    assert params["ai_quorum"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["ai_quorum"].default is inspect.Parameter.empty
    assert "UnparsedRecord" in str(params["records"].annotation)


def test_a_fresh_signer_accept_counts_with_ai_quorum_and_noise_is_ignored() -> None:
    rig = Rig()
    first = rig.review(request(head_sha=H1), facts(head_sha=H1), proposal=proposal(head=H1))
    assert isinstance(first, Issued)
    out = evaluate(rig, [record_of(first), *noise(rig)], head=H1)
    assert (out.state, out.reason) == ("success", "ok")
    assert out.counted == (first.decision.id,)
    assert (POINTER_ID, "pointer_unresolved") in out.ignored
    assert (MALFORMED_ID, "malformed") in out.ignored
    # ai 2 with the one ACCEPT and no failed candidate: not met, `verdict_missing` is not the
    # answer either (step 7 counts; GC-15 b "when the ai count is not met")
    two = evaluate(rig, [record_of(first)], head=H1, ai_quorum=2)
    assert two.state == "success" and two.counted == (first.decision.id,)


def test_the_gate_asks_the_signer_to_rebind_and_counts_the_fresh_verdict_sv8d() -> None:
    """GT-10, SV-8 d: the live head H1 fails BR for the H0 ACCEPT; the gate's `rebind` callback is
    the signer's own `rebind`, and the rebound verdict counts alone (no duplicate)."""
    rig = Rig()
    origin = rig.review(request(head_sha=H0), facts(head_sha=H0), proposal=proposal(head=H0))
    assert isinstance(origin, Issued)
    rig.clock.advance(hours=1)
    fresh: list[Issued] = []

    def ask(stale: VerdictRecord) -> VerdictRecord | None:
        assert stale.id == origin.decision.id
        out = rig.signer.rebind(request(head_sha=H1), facts(head_sha=H1), (), lambda s, f: True)
        if not isinstance(out, Issued):
            return None
        fresh.append(out)
        return record_of(out)

    out = evaluate(rig, [record_of(origin), *noise(rig)], head=H1, rebind=ask)
    assert (out.state, out.reason, out.detail) == ("success", "ok", "")
    assert len(fresh) == 1 and out.counted == (fresh[0].decision.id,)
    assert out.rebind_asked == (origin.decision.id,)
    assert fresh[0].decision.payload.rebound_from == origin.decision.id


def test_a_stored_rebound_verdict_and_its_origin_are_one_decision_gc15a() -> None:
    """GC-15 a counts decisions per reviewer; the origin of a counted rebound verdict is ignored
    `rebound`, so the pair is never `decision_duplicate`."""
    rig = Rig()
    origin = rig.review(request(head_sha=H0), facts(head_sha=H0), proposal=proposal(head=H0))
    assert isinstance(origin, Issued)
    rebound = rig.signer.rebind(request(head_sha=H1), facts(head_sha=H1), (), lambda s, f: True)
    assert isinstance(rebound, Issued)
    records: list[VerdictRecord | UnparsedRecord] = [
        record_of(origin),
        record_of(rebound),
        *noise(rig),
    ]
    for order in (records, list(reversed(records))):
        out = evaluate(rig, order, head=H1)
        assert (out.state, out.reason, out.detail) == ("success", "ok", "")
        assert out.counted == (rebound.decision.id,)
        assert (origin.decision.id, "rebound") in out.ignored


def test_a_two_step_chain_is_one_decision_at_the_newest_head_gc15a() -> None:
    """H0 -> H1 -> H2: the gate at H2 judges the newest rebound verdict through its whole chain
    (every record of the chain is in the stream, as the signer's API serves it); the H1 and H0
    verdicts are origins (`rebound`), no rebind is asked and nothing new is signed. Without the
    H0 origin the chain cannot be judged and the newest verdict fails closed."""
    rig = Rig()
    origin = rig.review(request(head_sha=H0), facts(head_sha=H0), proposal=proposal(head=H0))
    assert isinstance(origin, Issued)
    r1 = rig.signer.rebind(request(head_sha=H1), facts(head_sha=H1), (), lambda s, f: True)
    assert isinstance(r1, Issued)
    r2 = rig.signer.rebind(request(head_sha=H2), facts(head_sha=H2), (), lambda s, f: True)
    assert isinstance(r2, Issued) and r2.decision.payload.rebound_from == r1.decision.id
    signed = len(rig.backend.signed)
    asked: list[str] = []

    def ask(stale: VerdictRecord) -> VerdictRecord | None:
        asked.append(stale.id)
        out = rig.signer.rebind(request(head_sha=H2), facts(head_sha=H2), (), lambda s, f: True)
        return record_of(out) if isinstance(out, Issued) else None

    chain = [record_of(r2), record_of(r1), record_of(origin)]
    for order in (chain, list(reversed(chain))):
        out = evaluate(rig, order, head=H2, rebind=ask)
        assert (out.state, out.reason, out.detail) == ("success", "ok", "")
        assert out.counted == (r2.decision.id,)
        assert (r1.decision.id, "rebound") in out.ignored
        assert (origin.decision.id, "rebound") in out.ignored
    assert asked == [] and len(rig.backend.signed) == signed  # nothing asked, nothing signed
    broken = evaluate(rig, [record_of(r2), record_of(r1)], head=H2)
    assert (broken.state, broken.reason) == ("failure", "verdict_wrong_binding")


def test_the_signer_never_issues_a_second_decision_for_one_change_sv8c_gc15a() -> None:
    """SV-8 c: a second request for the same (repo, PR, change hash) returns the stored decision;
    the gate therefore sees one decision, never `decision_duplicate`."""
    rig = Rig()
    first = rig.review(request(head_sha=H1), facts(head_sha=H1), proposal=proposal(head=H1))
    again = rig.review(
        request(head_sha=H1), facts(head_sha=H1), proposal=proposal("REJECT", head=H1)
    )
    assert isinstance(first, Issued) and isinstance(again, Issued)
    assert again.new is False and again.decision is first.decision
    out = evaluate(rig, [record_of(first), record_of(again)], head=H1)
    assert (out.state, out.reason, out.detail) == ("success", "ok", "")


def _other_ids() -> Any:
    """Record ids of the second signer: 32 hex, disjoint from `vm_fakes.ids`."""
    counter = iter(range(1, 1000))
    return lambda: f"5{next(counter):031x}"


def _second_signer(rig: Rig, reviewer: str, seed: bytes) -> tuple[Signer, FakeSignerBackend]:
    """A signer with its own store (it bypasses SV-8 c on purpose) on the same ledger."""
    backend = FakeSignerBackend(seed)
    signer = Signer(
        backend=backend,
        reviewer_id=reviewer,
        org_id=ORG,
        trust=rig.signer.trust,
        ledger=rig.ledger,
        store=MemoryDecisionStore(),
        limits=NoLimits(),
        clock=rig.clock,
        new_id=_other_ids(),
    )
    return signer, backend


def _issue(signer: Signer, decision: str) -> Issued:
    req, fac = request(head_sha=H1), facts(head_sha=H1)
    assert signer.admit(req, fac, resets=()) is None
    out = signer.issue(
        req,
        fac,
        proposal_text=proposal(decision, head=H1),
        verdict_text_sha256="f" * 64,
        live_head=H1,
        resets=(),
    )
    assert isinstance(out, Issued)
    return out


@pytest.mark.parametrize(
    ("other_reviewer", "other_decision", "detail"),
    [
        (REVIEWER, "ACCEPT", "decision_duplicate"),  # same reviewer, two ACCEPTs
        (REVIEWER, "REJECT", "decision_duplicate"),  # same reviewer wins over accept_and_reject
        ("reviewer-b", "REJECT", "accept_and_reject"),  # different reviewers
    ],
)
def test_two_valid_decisions_for_one_change_are_a_conflict_gc15a(
    other_reviewer: str, other_decision: str, detail: str
) -> None:
    rig = Rig()
    first = rig.review(request(head_sha=H1), facts(head_sha=H1), proposal=proposal(head=H1))
    assert isinstance(first, Issued)
    seed = b"\x0b" * 32
    other_pub = FakeSignerBackend(seed).public_key_hex()
    rig.signer.load_trust(
        trust(
            trust_key(rig.backend.public_key_hex()),
            trust_key(other_pub, holder="service:" + other_reviewer),
            serial=6,
        )
    )
    signer, backend = _second_signer(rig, other_reviewer, seed)
    second = _issue(signer, other_decision)
    keys = {rig.backend.public_key_hex(): rig.backend, other_pub: backend}

    def verify(public: str, message: bytes, sig: str) -> bool:
        return keys[public].verify(public, message, sig)

    out = evaluate(rig, [record_of(first), record_of(second)], head=H1, verify=verify)
    assert (out.state, out.reason, out.detail) == ("failure", "verdict_conflict", detail)
    assert out.counted == ()
    assert second.decision.payload.change_hash == X


def test_unknown_commit_asks_the_signer_for_no_rebind_gc15e() -> None:
    """GC-15 e: a BR (b) failure with `unknown_commit` is final: the gate never asks the signer
    to rebind (so the signer never judges such a chain) and nothing is signed."""
    rig = Rig()
    origin = rig.review(request(head_sha=H0), facts(head_sha=H0), proposal=proposal(head=H0))
    assert isinstance(origin, Issued)
    signed = len(rig.backend.signed)
    asked: list[str] = []

    def ask(stale: VerdictRecord) -> VerdictRecord | None:
        asked.append(stale.id)
        return None

    out = evaluate(rig, [record_of(origin)], head=H1, rebind=ask, br="unknown_commit")
    assert (out.state, out.reason, out.detail) == (
        "failure",
        "verdict_wrong_binding",
        "unknown_commit",
    )
    assert asked == [] and out.rebind_asked == ()
    assert len(rig.backend.signed) == signed
