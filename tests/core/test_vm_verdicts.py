"""Merge Gate binding evaluator and GT-10 verdict check (spec SV-5, SV-7, SV-8 b-d, SV-9,
SV-12, GT-10, GT-15, 7.1; Fable fixes F-7, F-16, F-18). Pure functions over records and a gate
view; signature verification, BR and the signer's rebind are callables supplied by the caller.
Keys and signatures here are a fake scheme (SHA-512 of key and message), generated in the test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nuramesh.core.vm_formats import (
    VERDICT_DOMAIN,
    SignedVerdict,
    TrustList,
    VerdictPayload,
    payload_hash,
    public_key_id,
    signed_bytes,
)
from nuramesh.core.vm_verdicts import (
    EVALUATION_ORDER,
    STEP_IDS,
    GateView,
    ResetRecord,
    StepResult,
    UnparsedRecord,
    VerdictOutcome,
    VerdictRecord,
    evaluate_verdicts,
    judge_verdict,
    resolve_order,
)

H0, H1, H2 = "0" * 40, "1" * 40, "2" * 40
X = "c" * 64
P = "e" * 64
BUNDLE = "d" * 64
K1_PUB, K2_PUB = "7" * 64, "8" * 64
K1, K2 = public_key_id(K1_PUB), public_key_id(K2_PUB)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)


def ts(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fake_sign(public_key: str, message: bytes) -> str:
    return hashlib.sha512(public_key.encode() + message).hexdigest()


def verify(public_key: str, message: bytes, sig: str) -> bool:
    return sig == fake_sign(public_key, message)


def trust_key(public: str, holder: str = "service:reviewer-a", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key_id": public_key_id(public),
        "role": "verdict-signer",
        "holder": holder,
        "roles": [],
        "public_key": public,
        "custody": "service",
        "valid_from": ts(T0 - timedelta(days=30)),
        "valid_to": None,
        "revoked_at": None,
        "revoked_seq": None,
        "compromised": False,
    }
    base.update(over)
    return base


def trust(*keys: dict[str, Any], serial: int = 5) -> TrustList:
    return TrustList.model_validate(
        {"version": "vm-trust-1", "org_id": "org-example", "serial": serial, "keys": list(keys)}
    )


TRUST = trust(trust_key(K1_PUB))


def payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": "vm-verdict-1",
        "org_id": "org-example",
        "repo": "github.com/acme/widgets",
        "repo_id": "123456",
        "pr": 7,
        "head_sha": H1,
        "base_ref": "main",
        "base_sha": "9" * 40,
        "change_hash": X,
        "bundle_hash": BUNDLE,
        "tier": 3,
        "policy_sha256": P,
        "decision": "ACCEPT",
        "reviewer_id": "reviewer-a",
        "model_id": "example-model",
        "model_family": "family-x",
        "verdict_text_sha256": "f" * 64,
        "verdict_id": "a" * 32,
        "rebound_from": None,
        "key_id": K1,
        "issued_at": ts(T0),
        "expires_at": ts(T0 + timedelta(days=7)),
    }
    base.update(over)
    return base


def record(
    *,
    public: str = K1_PUB,
    seq: int | None = 100,
    ledger_at: datetime | None = T0,
    sig: str | None = None,
    ledgered_hash: str | None = None,
    **over: Any,
) -> VerdictRecord:
    body = VerdictPayload.model_validate(payload(**over))
    signature = sig if sig is not None else fake_sign(public, signed_bytes(VERDICT_DOMAIN, body))
    signed = SignedVerdict(payload=body, sig=signature)
    return VerdictRecord(
        verdict=signed,
        ledger_seq=seq,
        ledger_at=ledger_at,
        ledger_payload_hash=ledgered_hash if ledgered_hash else payload_hash(body),
    )


def view(**over: Any) -> GateView:
    base: dict[str, Any] = {
        "org_id": "org-example",
        "repo": "github.com/acme/widgets",
        "repo_id": "123456",
        "pr": 7,
        "base_ref": "main",
        "head_sha": H1,
        "change_hash": X,
        "policy_sha256": P,
        "tier": 3,
        "now": NOW,
        "ledgered_bundle_hashes": frozenset({BUNDLE}),
    }
    base.update(over)
    return GateView(**base)


def _stamp(records: Sequence[Any]) -> list[Any]:
    """Give each unstamped record a source time in argument order (oldest first, GC-15 b)."""
    return [
        replace(r, source_at=T0 + timedelta(seconds=i)) if r.source_at is None else r
        for i, r in enumerate(records)
    ]


def run(
    *records: VerdictRecord,
    gate: GateView | None = None,
    trust_list: TrustList = TRUST,
    resets: tuple[ResetRecord, ...] = (),
    ai_required: bool = True,
    br: bool = False,
    rebind: Any = None,
    verifier: Any = None,
    ai_quorum: int = 1,
    **extra: Any,
) -> VerdictOutcome:
    return evaluate_verdicts(
        _stamp(records),
        gate or view(),
        trust_list,
        verifier or verify,
        resets,
        ai_required=ai_required,
        carry_forward=lambda _head: "ok" if br else "not_ancestor",
        rebind=rebind,
        ai_quorum=ai_quorum,
        **extra,
    )


# --- GT-10 cases (section 10.1) ---------------------------------------------------------------


def test_g01_a_counting_accept_is_success() -> None:
    out = run(record())
    assert (out.state, out.reason) == ("success", "ok")
    assert out.counted == ("a" * 32,) and out.ignored == ()


def test_g19_no_verdict_is_pending_verdict_missing() -> None:
    assert run().state_reason == ("pending", "verdict_missing")


def test_g20_only_a_valid_reject_fails_verdict_rejected() -> None:
    assert run(record(decision="REJECT")).state_reason == ("failure", "verdict_rejected")


def test_g21_a_flipped_signature_bit_is_bad_signature() -> None:
    good = record()
    sig = good.verdict.sig
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert run(record(sig=flipped)).state_reason == ("failure", "verdict_bad_signature")


def test_g22_an_unknown_key_is_untrusted() -> None:
    assert run(record(public=K2_PUB, key_id=K2)).state_reason == (
        "failure",
        "verdict_key_untrusted",
    )


def test_wrong_role_key_is_untrusted() -> None:
    approver = trust_key(K2_PUB, holder="human:owner-1", role="approver", roles=["approver"])
    out = run(record(public=K2_PUB, key_id=K2), trust_list=trust(trust_key(K1_PUB), approver))
    assert out.state_reason == ("failure", "verdict_key_untrusted")


def test_key_window_is_checked_at_the_ledger_time_not_now() -> None:
    """GC-10, GC-9 (4): `t` is the ledger time of the record; a `valid_to` in the past never
    voids a verdict ledgered inside the window (GC-33), one ledgered after it does (GC-34)."""
    ended = trust(trust_key(K1_PUB, valid_to=ts(NOW)))  # now >= valid_to, ledger T0 < valid_to
    assert run(record(), trust_list=ended).state == "success"
    after = record(ledger_at=NOW, issued_at=ts(NOW))  # ledgered at valid_to: outside the window
    assert run(after, trust_list=ended).state_reason == ("failure", "verdict_key_untrusted")
    # valid_from is judged at the ledger time too: now is inside the window, the ledger time not
    late_start = trust(trust_key(K1_PUB, valid_from=ts(T0 + timedelta(minutes=1))))
    assert run(record(), trust_list=late_start).state_reason == (
        "failure",
        "verdict_key_untrusted",
    )
    # the verdict's own expiry is still tested at now (SV-9)
    expired = record(expires_at=ts(NOW))
    assert run(expired).state_reason == ("failure", "verdict_expired")


def _chain(length: int) -> list[VerdictRecord]:
    """A rebound chain of `length` verdicts: the origin plus `length - 1` rebounds (GC-59)."""
    out = [record(head_sha=H0, seq=100)]
    for i in range(1, length):
        out.append(
            record(
                verdict_id=f"{i:032x}",
                rebound_from=out[-1].id,
                issued_at=ts(NOW),
                seq=100 + i,
                ledger_at=NOW,
            )
        )
    return out


def test_gc59_chain_of_64_counts_and_65_is_refused() -> None:
    for length, expected in ((64, "ok"), (65, "verdict_wrong_binding")):
        chain = _chain(length)
        by_id = {r.id: r for r in chain}
        assert judge_verdict(chain[-1], view(), TRUST, verify, by_id, ()) == expected, length


@pytest.mark.parametrize(
    "change",
    [{"pr": 8}, {"repo": "github.com/acme/gadgets"}],
    ids=["G-23 other pr", "G-24 other repo"],
)
def test_g23_g24_records_of_another_pr_or_repo_are_wrong_binding(change: dict[str, Any]) -> None:
    out = run(record(**change))
    assert out.state_reason == ("failure", "verdict_wrong_binding")
    assert out.ignored == (("a" * 32, "verdict_wrong_binding"),)


def test_g25_a_different_change_hash_is_not_a_candidate() -> None:
    out = run(record(head_sha=H0, change_hash="b" * 64))
    assert out.state_reason == ("pending", "verdict_missing")
    assert out.ignored == (("a" * 32, "change_hash"),)


def test_g26_bundle_hash_must_name_a_ledgered_bundle_for_this_pr_and_change() -> None:
    assert run(record(bundle_hash="b" * 64)).state_reason == ("failure", "verdict_wrong_binding")


def test_g27_l04_a_record_without_its_ledger_entry_is_ignored_not_in_ledger() -> None:
    out = run(record(seq=None, ledger_at=None))
    assert out.state_reason == ("pending", "verdict_missing")
    assert out.ignored == (("a" * 32, "not_in_ledger"),)
    tampered = run(record(ledgered_hash="0" * 64))
    assert tampered.ignored == (("a" * 32, "not_in_ledger"),)


def test_g28_expiry_boundary() -> None:
    expires = T0 + timedelta(days=7)
    assert run(record(), gate=view(now=expires)).state_reason == ("failure", "verdict_expired")
    assert run(record(), gate=view(now=expires - timedelta(seconds=1))).state == "success"


def test_g29_revocation_by_ledger_order_and_compromise() -> None:
    later = trust(trust_key(K1_PUB, revoked_at=ts(T0 + timedelta(minutes=30)), revoked_seq=150))
    assert run(record(seq=100), trust_list=later).state == "success"  # G-29a
    compromised = trust(trust_key(K1_PUB, compromised=True))
    assert run(record(), trust_list=compromised).state_reason == ("failure", "verdict_revoked")
    earlier = trust(trust_key(K1_PUB, revoked_at=ts(T0 - timedelta(days=1)), revoked_seq=90))
    assert run(record(seq=100), trust_list=earlier).state_reason == ("failure", "verdict_revoked")


def test_g53_a_backdated_issued_at_does_not_beat_the_ledger_order() -> None:
    revoked = trust(trust_key(K1_PUB, revoked_at=ts(T0 + timedelta(minutes=30)), revoked_seq=150))
    out = run(record(seq=200, ledger_at=T0 + timedelta(minutes=40)), trust_list=revoked)
    assert out.state_reason == ("failure", "verdict_revoked")


def test_g68_revoked_at_without_seq_is_an_untrusted_key() -> None:
    bad = trust(trust_key(K1_PUB, revoked_at=ts(T0), revoked_seq=None))
    assert run(record(), trust_list=bad).state_reason == ("failure", "verdict_key_untrusted")


def test_g30_a_valid_accept_and_a_valid_reject_conflict() -> None:
    accept = record()
    reject = record(decision="REJECT", verdict_id="b" * 32, seq=101)
    assert run(accept, reject).state_reason == ("failure", "verdict_conflict")


def test_g31_g32_not_yet_valid_and_the_300_s_skew() -> None:
    ten = record(issued_at=ts(NOW + timedelta(minutes=10)), ledger_at=NOW + timedelta(minutes=10))
    assert run(ten).state_reason == ("failure", "verdict_not_yet_valid")
    four = record(issued_at=ts(NOW + timedelta(minutes=4)), ledger_at=NOW + timedelta(minutes=4))
    assert run(four).state == "success"


def test_g52b_g52c_a_reset_voids_only_earlier_records_by_ledger_order() -> None:
    reject = record(decision="REJECT", verdict_id="b" * 32, seq=100)
    accept = record(seq=130)
    reset_after = ResetRecord(seq=120, kind="change_hash", value=X)
    out = run(reject, accept, resets=(reset_after,))
    assert out.state_reason == ("success", "ok")
    assert out.ignored == (("b" * 32, "reset"),)
    reset_before = ResetRecord(seq=90, kind="change_hash", value=X)
    assert run(reject, accept, resets=(reset_before,)).state_reason == (
        "failure",
        "verdict_conflict",
    )
    other_scope = ResetRecord(seq=120, kind="change_hash", value="b" * 64)
    assert run(reject, accept, resets=(other_scope,)).state_reason == (
        "failure",
        "verdict_conflict",
    )


def test_reset_scopes_task_and_mode_flip() -> None:
    reject = record(decision="REJECT", verdict_id="b" * 32, seq=100)
    task = ResetRecord(seq=120, kind="task", value="issue-5")
    assert run(reject, gate=view(task_key="issue-5"), resets=(task,)).reason == "verdict_missing"
    assert run(reject, gate=view(task_key="issue-6"), resets=(task,)).reason == "verdict_rejected"
    assert run(reject, resets=(task,)).reason == "verdict_rejected"  # no task key: not in scope
    flip = ResetRecord(seq=120, kind="mode_flip", value=None)
    assert run(reject, resets=(flip,)).reason == "verdict_missing"


def test_g46a_with_ai_zero_verdicts_are_ignored_and_the_check_passes() -> None:
    out = run(record(decision="REJECT"), ai_required=False)
    assert out.state_reason == ("success", "ok") and out.counted == ()


# --- Fable fixes F-7 and F-16 --------------------------------------------------------------------


def test_f7_holder_must_be_service_plus_reviewer_id() -> None:
    other_holder = trust(trust_key(K1_PUB, holder="service:reviewer-b"))
    assert run(record(), trust_list=other_holder).state_reason == (
        "failure",
        "verdict_wrong_binding",
    )
    assert run(record(reviewer_id="reviewer-b"), trust_list=other_holder).state == "success"


def test_f16_ttl_above_the_hard_maximum_is_wrong_binding() -> None:
    long = record(expires_at=ts(T0 + timedelta(days=30, seconds=1)))
    assert run(long, gate=view(now=T0 + timedelta(days=1))).state_reason == (
        "failure",
        "verdict_wrong_binding",
    )
    exact = record(expires_at=ts(T0 + timedelta(days=30)))
    assert run(exact, gate=view(now=T0 + timedelta(days=1))).state == "success"


def test_f16_issued_at_must_sit_within_skew_of_the_ledger_time() -> None:
    drifted = record(ledger_at=T0 + timedelta(seconds=301))
    assert run(drifted).state_reason == ("failure", "verdict_wrong_binding")
    assert run(record(ledger_at=T0 + timedelta(seconds=300))).state == "success"
    assert run(record(ledger_at=T0 - timedelta(seconds=300))).state == "success"


def test_expires_before_issued_is_wrong_binding() -> None:
    assert run(record(expires_at=ts(T0 - timedelta(seconds=1)))).state_reason == (
        "failure",
        "verdict_wrong_binding",
    )


# --- heads: BR, rebinding, carry_forward off ---------------------------------------------------


def test_c01_a_record_for_an_older_head_counts_when_br_holds() -> None:
    out = run(record(head_sha=H1), gate=view(head_sha=H2), br=True)
    assert out.state_reason == ("success", "ok")


def test_c02_with_carry_forward_off_only_live_head_records_are_candidates() -> None:
    calls: list[VerdictRecord] = []

    def rebind(rec: VerdictRecord) -> VerdictRecord | None:
        calls.append(rec)
        return None

    out = run(
        record(head_sha=H1), gate=view(head_sha=H2, carry_forward=False), br=True, rebind=rebind
    )
    assert out.state_reason == ("pending", "verdict_missing")
    assert out.ignored == (("a" * 32, "wrong_head"),)
    assert calls == []


def test_g25b_when_br_fails_the_gate_asks_the_signer_once_and_uses_the_rebound_verdict() -> None:
    origin = record(head_sha=H0, seq=100)
    rebound = record(
        head_sha=H1,
        verdict_id="b" * 32,
        rebound_from="a" * 32,
        issued_at=ts(NOW),
        seq=140,
        ledger_at=NOW,
    )
    asked: list[str] = []

    def rebind(rec: VerdictRecord) -> VerdictRecord | None:
        asked.append(rec.verdict.payload.verdict_id)
        return rebound

    out = run(origin, br=False, rebind=rebind)
    assert out.state_reason == ("success", "ok")
    assert out.counted == ("b" * 32,)
    assert asked == ["a" * 32]


def test_g25c_a_refused_rebind_is_wrong_binding() -> None:
    out = run(record(head_sha=H0), br=False, rebind=lambda _rec: None)
    assert out.state_reason == ("failure", "verdict_wrong_binding")
    assert out.ignored == (("a" * 32, "verdict_wrong_binding"),)


def test_without_a_signer_a_failed_br_is_wrong_binding_and_nothing_is_asked() -> None:
    assert run(record(head_sha=H0), br=False, rebind=None).state_reason == (
        "failure",
        "verdict_wrong_binding",
    )


def test_g25d_a_reject_blocks_whatever_its_head_and_is_never_rebound() -> None:
    asked: list[str] = []

    def rebind(rec: VerdictRecord) -> VerdictRecord | None:
        asked.append(rec.verdict.payload.verdict_id)
        return None

    out = run(record(head_sha=H0, decision="REJECT"), br=False, rebind=rebind)
    assert out.state_reason == ("failure", "verdict_rejected")
    assert asked == []


def test_a_rebound_verdict_presented_directly_is_judged_through_its_chain() -> None:
    origin = record(head_sha=H0, seq=100)
    rebound = record(
        verdict_id="b" * 32, rebound_from="a" * 32, issued_at=ts(NOW), seq=140, ledger_at=NOW
    )
    assert run(origin, rebound, br=False, rebind=None).state_reason == ("success", "ok")


def test_g69_a_rebound_verdict_dies_with_its_origin_key() -> None:
    origin = record(head_sha=H0, seq=100)  # signed by K1
    rebound = record(
        public=K2_PUB,
        key_id=K2,
        verdict_id="b" * 32,
        rebound_from="a" * 32,
        issued_at=ts(NOW),
        seq=140,
        ledger_at=NOW,
    )
    k1_compromised = trust(
        trust_key(K1_PUB, compromised=True, valid_to=ts(NOW)), trust_key(K2_PUB), serial=8
    )
    out = run(origin, rebound, trust_list=k1_compromised, br=False)
    assert out.state_reason == ("failure", "verdict_revoked")
    rotated = trust(trust_key(K1_PUB, revoked_at=ts(NOW), revoked_seq=130), trust_key(K2_PUB))
    assert run(origin, rebound, trust_list=rotated, br=False).state_reason == ("success", "ok")


def test_a_rebound_verdict_whose_origin_is_voided_is_ignored_reset() -> None:
    origin = record(head_sha=H0, seq=100)
    rebound = record(
        verdict_id="b" * 32, rebound_from="a" * 32, issued_at=ts(NOW), seq=140, ledger_at=NOW
    )
    reset = ResetRecord(seq=120, kind="change_hash", value=X)
    out = run(origin, rebound, resets=(reset,), br=False)
    assert out.state_reason == ("pending", "verdict_missing")
    assert set(out.ignored) == {("a" * 32, "reset"), ("b" * 32, "reset")}


def test_a_rebound_verdict_never_extends_validity_and_needs_its_origin() -> None:
    origin = record(head_sha=H0, seq=100)
    longer = record(
        verdict_id="b" * 32,
        rebound_from="a" * 32,
        issued_at=ts(NOW),
        expires_at=ts(T0 + timedelta(days=7, seconds=1)),
        seq=140,
        ledger_at=NOW,
    )
    assert run(origin, longer, br=False).state_reason == ("failure", "verdict_wrong_binding")
    orphan = record(
        verdict_id="b" * 32, rebound_from="a" * 32, issued_at=ts(NOW), seq=140, ledger_at=NOW
    )
    assert run(orphan, br=False).state_reason == ("failure", "verdict_wrong_binding")
    from_reject = record(head_sha=H0, decision="REJECT", seq=100)
    assert run(from_reject, orphan, br=False).state_reason == ("failure", "verdict_rejected")


def test_a_rebound_chain_cycle_fails_closed() -> None:
    a = record(verdict_id="a" * 32, rebound_from="b" * 32, seq=100)
    b = record(verdict_id="b" * 32, rebound_from="a" * 32, seq=101)
    assert run(a, b).state_reason == ("failure", "verdict_wrong_binding")


def test_a_rebound_chain_whose_reviewer_changes_is_wrong_binding_gc93() -> None:
    origin = record(head_sha=H0, seq=100)  # reviewer-a, key K1
    rebound = record(
        public=K2_PUB,
        key_id=K2,
        reviewer_id="reviewer-b",
        verdict_id="b" * 32,
        rebound_from="a" * 32,
        issued_at=ts(NOW),
        seq=140,
        ledger_at=NOW,
    )
    both = trust(trust_key(K1_PUB), trust_key(K2_PUB, holder="service:reviewer-b"))
    out = run(origin, rebound, trust_list=both, br=False)
    assert out.state_reason == ("failure", "verdict_wrong_binding")


# --- limits and ordering --------------------------------------------------------------------------


def test_at_most_100_ledgered_candidates_then_fail_closed() -> None:
    many = [record(verdict_id=f"{i:032x}", seq=1000 + i, sig=f"{i:0128x}") for i in range(100)]
    assert run(*many).state_reason == ("failure", "verdict_bad_signature")
    one_more = record(verdict_id=f"{100:032x}", seq=2000)
    out = run(*many, one_more)
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert out.counted == () and out.candidates_checked == 0
    assert out.detail == "over_cap"  # #946: 0 checked alone would hide that the cap was hit
    assert run(*many).detail == ""  # exactly 100 is not over the cap
    assert run(*many[:99], one_more).state == "success"  # 100 exactly, newest first


def test_detail_is_empty_on_ordinary_outcomes_only_the_cap_sets_over_cap() -> None:
    """#1106 m1: `over_cap` must not leak onto other outcomes (pending, an ordinary failure,
    a success); a failed candidate has no detail (GC-15 c)."""
    pending = run()
    assert pending.state_reason == ("pending", "verdict_missing")
    assert pending.detail == ""
    failed = run(record(sig="0" * 128))
    assert failed.state_reason == ("failure", "verdict_bad_signature")
    assert failed.detail == ""  # same code as the cap, told apart by the detail
    rejected = run(record(decision="REJECT"))
    assert rejected.state_reason == ("failure", "verdict_rejected")
    assert rejected.detail == ""
    assert run(record()).state == "success" and run(record()).detail == ""


def test_distinct_signatures_count_once_toward_the_limit() -> None:
    dup = record()
    assert run(*([dup] * 150)).state_reason == ("success", "ok")


def test_g49_fakes_without_ledger_entries_do_not_count_toward_the_limit() -> None:
    fakes = [
        record(verdict_id=f"{i:032x}", seq=None, ledger_at=None, sig=f"{i:0128x}")
        for i in range(101)
    ]
    out = run(record(), *fakes)
    assert out.state_reason == ("success", "ok")
    assert len(out.ignored) == 101 and {r for _, r in out.ignored} == {"not_in_ledger"}


def test_the_newest_failing_candidate_by_ledger_order_gives_the_code() -> None:
    old = record(verdict_id="a" * 32, seq=100, public=K2_PUB, key_id=K2)  # untrusted
    new = record(verdict_id="b" * 32, seq=200, pr=8)  # wrong binding
    assert run(old, new).reason == "verdict_wrong_binding"
    assert run(new, old).reason == "verdict_wrong_binding"
    older_new = record(verdict_id="b" * 32, seq=50, pr=8)
    assert run(old, older_new).reason == "verdict_key_untrusted"


# --- mutation test: every exactly-bound field --------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["org_id", "repo", "repo_id", "pr", "base_ref", "policy_sha256", "tier"],
)
def test_flipping_any_exactly_bound_field_of_the_gate_view_is_wrong_binding(field: str) -> None:
    current = getattr(view(), field)
    flipped: Any
    if isinstance(current, int):
        flipped = current + 1
    elif field == "repo":
        flipped = current + "-other"
    elif field == "repo_id":
        flipped = current + "9"
    else:
        flipped = current[:-1] + ("0" if current[-1] != "0" else "1")
    out = run(record(), gate=view(**{field: flipped}))
    assert out.state_reason == ("failure", "verdict_wrong_binding"), field
    assert judge_verdict(record(), view(**{field: flipped}), TRUST, verify, {}, ()) == (
        "verdict_wrong_binding"
    )


def test_informational_fields_are_never_compared_for_equality() -> None:
    out = run(
        record(
            base_sha="8" * 40,
            model_id="other-model",
            model_family="other",
            verdict_text_sha256="1" * 64,
        )
    )
    assert out.state == "success"


def test_judge_verdict_reports_ok_for_a_counting_record() -> None:
    assert judge_verdict(record(), view(), TRUST, verify, {}, ()) == "ok"


# --- the evaluation order table (7.1 with F-18) ---------------------------------------------------


def test_order_table_has_trust_roots_at_step_3b_after_the_bundle() -> None:
    assert STEP_IDS == ("1", "2", "3", "3b", "4", "5", "6", "7", "9", "10")
    by_id = {step.id: step for step in EVALUATION_ORDER}
    assert by_id["3b"].name == "trust_roots"
    assert by_id["3b"].failure == ("trust_root_needs_admin",) and by_id["3b"].pending == ()
    assert by_id["6"].pending == ("verdict_missing",)
    assert set(by_id["6"].failure) == {
        "verdict_rejected",
        "verdict_conflict",
        "verdict_bad_signature",
        "verdict_key_untrusted",
        "verdict_wrong_binding",
        "verdict_expired",
        "verdict_not_yet_valid",
        "verdict_revoked",
    }
    assert by_id["7"].pending == ("quorum_unmet", "codeowner_uncovered")
    assert by_id["9"].pending == ("behind_main",)
    assert by_id["10"].failure == ("ledger_unavailable",)
    assert by_id["1"].pending == ("not_enforced",)


def test_resolve_order_first_non_ok_decides_and_pending_stops_like_a_failure() -> None:
    assert resolve_order([]) == ("success", "ok")
    outcomes = [
        StepResult("6", "failure", "verdict_rejected"),
        StepResult("4", "pending", "ci_pending"),
    ]
    assert resolve_order(outcomes) == ("pending", "ci_pending")
    f18 = [
        StepResult("7", "pending", "quorum_unmet"),
        StepResult("3b", "failure", "trust_root_needs_admin"),
    ]
    assert resolve_order(f18) == ("failure", "trust_root_needs_admin")  # G-18a with F-18
    assert resolve_order([StepResult("6", "success", "ok")]) == ("success", "ok")


def test_resolve_order_refuses_a_code_that_does_not_belong_to_its_step_or_state() -> None:
    with pytest.raises(ValueError):
        resolve_order([StepResult("4", "failure", "verdict_rejected")])
    with pytest.raises(ValueError):
        resolve_order([StepResult("6", "pending", "verdict_rejected")])
    with pytest.raises(ValueError):
        resolve_order([StepResult("8", "failure", "trust_root_needs_admin")])
    with pytest.raises(ValueError):
        resolve_order([StepResult("6", "skipped", "ok")])  # type: ignore[arg-type]


def test_gate_error_wins_over_everything() -> None:
    outcomes = [StepResult("2", "pending", "pr_draft"), StepResult("any", "failure", "gate_error")]
    assert resolve_order(outcomes) == ("failure", "gate_error")


# --- Opus round 1: dedupe by verdict_id, REJECT on any head, one decision per change hash ------


def test_vm1_r1_a_comment_copy_with_a_copied_signature_cannot_hide_a_reject() -> None:
    accept = record(seq=20)
    reject = record(decision="REJECT", verdict_id="b" * 32, seq=10)
    assert run(accept, reject).state_reason == ("failure", "verdict_conflict")
    # a commenter pairs the ledgered ACCEPT payload with the REJECT's signature
    forged = VerdictRecord(
        verdict=SignedVerdict(payload=accept.payload, sig=reject.verdict.sig),
        ledger_seq=accept.ledger_seq,
        ledger_at=accept.ledger_at,
        ledger_payload_hash=accept.ledger_payload_hash,
    )
    out = run(accept, reject, forged)
    assert out.state_reason == ("failure", "verdict_conflict")
    out = run(forged, accept, reject)  # the bad copy may come first or last
    assert out.state_reason == ("failure", "verdict_conflict")
    assert ("a" * 32, "verdict_bad_signature") in out.ignored  # never skipped silently


def test_vm1_r1_copies_of_one_ledgered_payload_do_not_trip_the_cap() -> None:
    good = record()
    copies = [
        VerdictRecord(
            good.verdict.__class__(payload=good.payload, sig=f"{i:0128x}"),
            100,
            good.ledger_at,
            good.ledger_payload_hash,
        )
        for i in range(101)
    ]
    out = run(good, *copies)
    assert out.state_reason == ("success", "ok")
    assert set(out.ignored) == {("a" * 32, "duplicate_id"), ("a" * 32, "verdict_bad_signature")}


def test_vm1_r1_reject_on_an_older_head_blocks_with_carry_forward_off() -> None:
    gate = view(head_sha=H2, carry_forward=False)
    accept = record(head_sha=H2, seq=20)
    reject = record(head_sha=H1, decision="REJECT", verdict_id="b" * 32, seq=10)
    out = run(accept, reject, gate=gate)
    assert out.state_reason == ("failure", "verdict_conflict")
    assert run(reject, gate=gate).state_reason == ("failure", "verdict_rejected")


def test_vm1_r1_two_decisions_for_one_change_hash_conflict_in_both_modes() -> None:
    for carry in (True, False):
        gate = view(head_sha=H2, carry_forward=carry)
        old = record(head_sha=H1, decision="REJECT", verdict_id="b" * 32, seq=10)
        new = record(head_sha=H2, verdict_id="c" * 32, seq=20)
        out = run(old, new, gate=gate, br=carry)
        assert out.state_reason == ("failure", "verdict_conflict"), carry
        assert out.detail
    # an ACCEPT at the old head plus one at the live head: two decisions, both modes
    for carry in (True, False):
        gate = view(head_sha=H2, carry_forward=carry)
        a1 = record(head_sha=H1, seq=10)
        a2 = record(head_sha=H2, verdict_id="c" * 32, seq=20)
        out = run(a1, a2, gate=gate, br=carry)
        assert out.state_reason == ("failure", "verdict_conflict"), carry
        assert out.detail == "decision_duplicate"
    # a later reset voids the older decision: the newer one stands
    gate = view(head_sha=H2)
    reset = ResetRecord(seq=15, kind="change_hash", value=X)
    old = record(head_sha=H1, decision="REJECT", verdict_id="b" * 32, seq=10)
    new = record(head_sha=H2, verdict_id="c" * 32, seq=20)
    assert run(old, new, gate=gate, resets=(reset,)).state_reason == ("success", "ok")
    # a different change hash is independent
    other = record(
        head_sha=H1, change_hash="b" * 64, decision="REJECT", verdict_id="d" * 32, seq=30
    )
    assert run(other, new, gate=gate).state_reason == ("success", "ok")


# --- Fable final on #919: per reviewer_id, trust list org, verdict_reuse ---------------------

TWO_REVIEWERS = trust(
    trust_key(K1_PUB, holder="service:reviewer-a"), trust_key(K2_PUB, holder="service:reviewer-b")
)


def _second(**over: Any) -> VerdictRecord:
    base: dict[str, Any] = {
        "public": K2_PUB,
        "reviewer_id": "reviewer-b",
        "key_id": K2,
        "verdict_id": "b" * 32,
        "seq": 101,
    }
    base.update(over)
    return record(**base)


def test_g39a_two_accepts_from_two_reviewers_are_one_decision_each_not_a_duplicate() -> None:
    out = run(record(), _second(), trust_list=TWO_REVIEWERS)
    assert out.state_reason == ("success", "ok")
    assert len(out.counted) == 2
    assert out.detail == ""


def test_g39a_one_reviewer_with_two_decisions_is_still_decision_duplicate() -> None:
    out = run(record(), record(verdict_id="c" * 32, seq=102), trust_list=TWO_REVIEWERS)
    assert out.state_reason == ("failure", "verdict_conflict")
    assert out.detail == "decision_duplicate"


def test_g39a_any_reject_still_blocks_beside_an_accept_of_another_reviewer() -> None:
    out = run(record(), _second(decision="REJECT"), trust_list=TWO_REVIEWERS)
    assert out.state_reason == ("failure", "verdict_conflict")
    assert out.detail == "accept_and_reject"
    rejects = run(_second(decision="REJECT"), trust_list=TWO_REVIEWERS)
    assert rejects.state_reason == ("failure", "verdict_rejected")


def test_tn_a_trust_list_of_another_org_does_not_count() -> None:
    other = TrustList.model_validate(
        {
            "version": "vm-trust-1",
            "org_id": "org-other",
            "serial": 5,
            "keys": [trust_key(K1_PUB)],
        }
    )
    assert judge_verdict(record(), view(), other, verify, {}, ()) == "verdict_key_untrusted"
    assert run(record(), trust_list=other).state_reason == ("failure", "verdict_key_untrusted")


def test_verdict_reuse_a_newer_accept_never_hides_an_older_reject_of_one_verdict_id() -> None:
    old = record(decision="REJECT", seq=10)
    new = record(decision="ACCEPT", seq=20)  # same verdict_id, a different ledgered payload
    for order in ((old, new), (new, old)):
        out = run(*order)
        assert out.state_reason == ("failure", "verdict_conflict")
        assert out.detail == "verdict_reuse"


# --- Opus final on #919: GC-30, GC-23/24 detail, GC-32 ----------------------------------------


def test_gc30_old_head_accept_with_failed_br_still_counts_as_a_decision() -> None:
    a1 = record(head_sha=H1, seq=10)
    a2 = record(head_sha=H2, verdict_id="c" * 32, seq=20)
    gate = view(head_sha=H2, carry_forward=True)
    for rebind in (None, lambda r: None):
        out = run(a1, a2, gate=gate, br=False, rebind=rebind)
        assert out.state_reason == ("failure", "verdict_conflict")
        assert out.detail == "decision_duplicate"


def test_gc23_gc24_one_reviewer_reject_and_accept_is_decision_duplicate() -> None:
    for carry in (True, False):
        gate = view(head_sha=H2, carry_forward=carry)
        old = record(head_sha=H1, decision="REJECT", verdict_id="b" * 32, seq=10)
        new = record(head_sha=H2, verdict_id="c" * 32, seq=20)
        out = run(old, new, gate=gate, br=carry)
        assert out.state_reason == ("failure", "verdict_conflict"), carry
        assert out.detail == "decision_duplicate", carry


def test_gc32_bad_signature_old_head_accept_is_not_rebound() -> None:
    bad = record(head_sha=H1, seq=10, sig="0" * 128)
    live = record(head_sha=H2, verdict_id="c" * 32, seq=20)
    out = run(
        bad,
        live,
        gate=view(head_sha=H2, carry_forward=True),
        br=False,
        rebind=lambda r: None,
    )
    assert out.state_reason == ("success", "ok")
    assert out.rebind_asked == ()


# --- GC-15 b, GC-84..GC-88 (#919 Opus delta): failed REJECTs, the 8-copy cap, pointers ---------

K3_PUB, K4_PUB = "5" * 64, "6" * 64
K3, K4 = public_key_id(K3_PUB), public_key_id(K4_PUB)
FOUR = trust(
    trust_key(K1_PUB, holder="service:reviewer-a"),
    trust_key(K2_PUB, holder="service:reviewer-b"),
    trust_key(K3_PUB, holder="service:reviewer-c"),
    trust_key(K4_PUB, holder="service:reviewer-d"),
)
_WHO = {
    "a": (K1_PUB, K1, "a" * 32, 100),
    "b": (K2_PUB, K2, "b" * 32, 101),
    "c": (K3_PUB, K3, "c" * 32, 102),
    "d": (K4_PUB, K4, "d" * 32, 103),
}


def rec(
    who: str, decision: str = "ACCEPT", *, vid: str | None = None, **over: Any
) -> VerdictRecord:
    pub, key_id, verdict_id, seq = _WHO[who]
    base: dict[str, Any] = {
        "public": pub,
        "key_id": key_id,
        "reviewer_id": f"reviewer-{who}",
        "verdict_id": vid or verdict_id,
        "seq": seq,
        "decision": decision,
    }
    base.update(over)
    return record(**base)


def bad(who: str, decision: str = "ACCEPT", **over: Any) -> VerdictRecord:
    return rec(who, decision, sig="e" * 128, **over)


def copies(base: VerdictRecord, count: int) -> list[VerdictRecord]:
    """`count` forge-only copies of one ledgered payload with distinct garbage signatures."""
    return [
        VerdictRecord(
            SignedVerdict(payload=base.payload, sig=f"{i + 1:0128x}"),
            base.ledger_seq,
            base.ledger_at,
            base.ledger_payload_hash,
        )
        for i in range(count)
    ]


def run4(*items: Any, **kw: Any) -> VerdictOutcome:
    return run(*items, trust_list=FOUR, **kw)


def test_gc84_an_expired_or_revoked_reject_fails_even_next_to_a_counting_accept() -> None:
    expired = rec("b", "REJECT", expires_at=ts(T0 + timedelta(minutes=30)))
    out = run4(rec("a"), expired)
    assert out.state_reason == ("failure", "verdict_expired")
    revoked_trust = trust(
        trust_key(K1_PUB, holder="service:reviewer-a"),
        trust_key(K2_PUB, holder="service:reviewer-b", revoked_at=ts(T0), revoked_seq=50),
    )
    out = run(rec("a"), rec("b", "REJECT"), trust_list=revoked_trust)
    assert out.state_reason == ("failure", "verdict_revoked")


def test_gc85_a_failed_reject_next_to_valid_decisions() -> None:
    assert run4(rec("a"), bad("b", "REJECT")).state_reason == ("failure", "verdict_bad_signature")
    out = run4(bad("b", "REJECT"), rec("c", "REJECT"))
    assert out.state_reason == ("failure", "verdict_rejected")
    out = run4(bad("b", "REJECT"), rec("a"), rec("c", "REJECT"))
    assert (out.state_reason, out.detail) == (
        ("failure", "verdict_conflict"),
        "accept_and_reject",
    )
    both = [rec("d", vid="1" * 32, seq=110), rec("d", "REJECT", vid="2" * 32, seq=111)]
    out = run4(rec("a"), bad("b", "REJECT"), *both)
    assert (out.state_reason, out.detail) == (
        ("failure", "verdict_conflict"),
        "decision_duplicate",
    )


def test_gc85_the_code_of_the_newest_failed_reject_decides() -> None:
    old = bad("b", "REJECT", seq=10)
    new = rec("c", "REJECT", seq=20, expires_at=ts(T0 + timedelta(minutes=30)))
    assert run4(rec("a"), old, new).reason == "verdict_expired"


def test_a_failed_non_reject_next_to_a_met_quorum_changes_nothing() -> None:
    assert run4(rec("a"), bad("b")).state_reason == ("success", "ok")


def test_gc86_an_unmet_ai_quorum_with_a_failed_candidate_is_that_failure() -> None:
    out = run4(rec("a"), bad("b"), ai_quorum=2)
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert run4(rec("a"), rec("b"), ai_quorum=2).state_reason == ("success", "ok")
    # no failed candidate: step 6 passes with its counted verdicts and step 7 says quorum_unmet
    out = run4(rec("a"), ai_quorum=2)
    assert (out.state_reason, out.counted) == (("success", "ok"), ("a" * 32,))


def test_gc71_at_most_8_copies_of_one_id_are_verified() -> None:
    seen: list[str] = []

    def counting(public_key: str, message: bytes, sig: str) -> bool:
        seen.append(sig)
        return False

    base = rec("a")
    out = run4(*copies(base, 101), verifier=counting)
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert len(seen) <= 8
    assert out.ignored.count(("a" * 32, "verdict_bad_signature")) == 1
    assert out.ignored.count(("a" * 32, "duplicate_id")) == 1
    assert len(out.ignored) == 2
    # GC-72: a valid ACCEPT of another reviewer meets the quorum
    out = run4(*copies(base, 101), rec("b"))
    assert out.state_reason == ("success", "ok")
    assert len(out.ignored) == 2


def test_gc80_the_genuine_copy_must_be_among_the_first_8() -> None:
    base = rec("a")
    garbage = copies(base, 20)
    out = run4(base, *garbage)  # (i) the genuine copy is the oldest comment
    assert out.state_reason == ("success", "ok")
    assert out.ignored.count(("a" * 32, "verdict_bad_signature")) == 1
    assert out.ignored.count(("a" * 32, "duplicate_id")) == 1
    out = run4(*garbage, base)  # (ii) the genuine copy is the newest of 21
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert ("a" * 32, "duplicate_id") in out.ignored


def test_gc87_a_pointer_is_ignored_never_a_copy_and_listed_once_per_id() -> None:
    pointers = [
        UnparsedRecord("pointer", "a" * 32),
        UnparsedRecord("pointer", "a" * 32),
        UnparsedRecord("pointer", "f" * 32),
    ]
    out = run4(pointers[0])
    assert out.state_reason == ("pending", "verdict_missing")
    assert out.ignored == (("a" * 32, "pointer_unresolved"),)
    out = run4(*pointers)
    assert out.state_reason == ("pending", "verdict_missing")
    assert sorted(out.ignored) == [
        ("a" * 32, "pointer_unresolved"),
        ("f" * 32, "pointer_unresolved"),
    ]


def test_gc88_malformed_copies_use_slots_only_when_the_ledger_holds_them() -> None:
    genuine = rec("a")
    held = [UnparsedRecord("malformed", "a" * 32, copy=True) for _ in range(8)]
    out = run4(*held, genuine)
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert ("a" * 32, "malformed") in out.ignored
    assert ("a" * 32, "duplicate_id") in out.ignored
    unheld = [UnparsedRecord("malformed", "a" * 32) for _ in range(8)]
    out = run4(*unheld, genuine)
    assert out.state_reason == ("success", "ok")
    assert out.ignored == (("a" * 32, "malformed"),)


def test_gc81_a_malformed_ledgered_reject_blocks_next_to_a_counting_accept() -> None:
    malformed = UnparsedRecord(
        "malformed", "b" * 32, ledger_seq=101, change_hash=X, ledgered_decision="REJECT", copy=True
    )
    out = run4(rec("a"), malformed)
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert ("b" * 32, "malformed") in out.ignored
    # a malformed ACCEPT is only ignored; one for another change hash is not a candidate
    accept = UnparsedRecord("malformed", "b" * 32, 101, X, "ACCEPT", copy=True)
    assert run4(rec("a"), accept).state_reason == ("success", "ok")
    other = UnparsedRecord("malformed", "b" * 32, 101, "9" * 64, "REJECT", copy=True)
    assert run4(rec("a"), other).state_reason == ("success", "ok")
    voided = ResetRecord(200, "change_hash", X)
    out = run4(rec("a"), malformed, resets=(voided,))
    assert out.state_reason == ("pending", "verdict_missing")


def test_gc89_a_record_the_ledger_does_not_hold_cannot_block() -> None:
    foreign_accept = rec("a", ledgered_hash="9" * 64)
    foreign_reject = rec("b", "REJECT", ledgered_hash="9" * 64)
    for item in (foreign_accept, foreign_reject):
        out = run4(item)
        assert out.state_reason == ("pending", "verdict_missing")
        assert out.ignored == ((item.id, "not_in_ledger"),)


@pytest.mark.parametrize("flaw", ["sig", "expired"])
def test_gc75_an_invalid_wrong_head_accept_is_its_code_not_wrong_head(flaw: str) -> None:
    gate = view(head_sha=H2, carry_forward=False)
    if flaw == "expired":
        stale = rec("a", head_sha=H1, expires_at=ts(T0 + timedelta(minutes=30)))
        code = "verdict_expired"
    else:
        stale = bad("a", head_sha=H1)
        code = "verdict_bad_signature"
    out = run4(stale, gate=gate)
    assert out.state_reason == ("failure", code)
    assert (stale.id, "wrong_head") not in out.ignored
    live = rec("b", head_sha=H2)
    out = run4(stale, live, gate=gate)
    assert out.state_reason == ("success", "ok")
    assert (stale.id, code) in out.ignored
    assert (stale.id, "wrong_head") not in out.ignored


# --- GC-15 b: the result never depends on the input order -----------------------------------------


def _shuffles(items: list[Any], n: int = 50) -> list[list[Any]]:
    import random

    rng = random.Random(919)
    out = []
    for _ in range(n):
        copy = list(items)
        rng.shuffle(copy)
        out.append(copy)
    return out


def _by_age(items: Sequence[Any]) -> list[Any]:
    return [replace(r, source_at=T0 + timedelta(seconds=i)) for i, r in enumerate(items)]


def _order_cases() -> dict[str, list[Any]]:
    base = rec("a")
    garbage = copies(base, 20)
    held = [UnparsedRecord("malformed", "a" * 32, copy=True) for _ in range(8)]
    # records of several ids that only produce `ignored` entries: their order is the sort order
    noise = [UnparsedRecord("pointer", f"{c}" * 32) for c in "123456"]
    stray = [replace(rec("c", vid=f"{c}" * 32), ledger_payload_hash="9" * 64) for c in "789"]
    everything: list[Any] = [base, *garbage, *stray, *noise]
    tied = [replace(r, source_at=T0) for r in everything]
    return {
        "gc80_i": _by_age([base, *garbage]),
        "gc80_ii": _by_age([*garbage, base]),
        "gc88_i": _by_age([*held, base]),
        "gc88_ii": _by_age([*[UnparsedRecord("malformed", f"{c}" * 32) for c in "12345678"], base]),
        "gc85_iii": _by_age([*noise[:3], rec("a"), *stray, bad("b", "REJECT"), *noise[3:]]),
        "duplicate_id": _by_age([*copies(base, 9), base, *copies(base, 3)]),
        "pointers": _by_age([*noise[:2], base, *noise[2:]]),
        "equal_source_at_and_comment_id": tied,
    }


@pytest.mark.parametrize("name", sorted(_order_cases()))
def test_shuffled_input_gives_the_same_result(name: str) -> None:
    items = _order_cases()[name]

    def result(stream: list[Any]) -> VerdictOutcome:
        return evaluate_verdicts(
            stream,
            view(),
            FOUR,
            verify,
            (),
            ai_required=True,
            carry_forward=lambda _h: "not_ancestor",
            rebind=None,
            ai_quorum=1,
        )

    expected = result(items)
    for shuffled in _shuffles(items):
        assert result(shuffled) == expected


def test_the_oldest_8_copies_are_judged_by_source_time_not_input_order() -> None:
    base = rec("a")
    stream = _by_age([base, *copies(base, 20)])
    out = run4(*reversed(stream))  # the genuine copy is the oldest comment, listed last
    assert out.state_reason == ("success", "ok")


def test_a_held_malformed_copy_with_unknown_decision_fails_closed() -> None:
    """GC-15 b, f: a ledgered copy without its change hash or decision could be a REJECT."""
    no_hash = UnparsedRecord("malformed", "b" * 32, 101, None, "ACCEPT", copy=True)
    no_decision = UnparsedRecord("malformed", "b" * 32, 101, X, None, copy=True)
    for item in (no_hash, no_decision):
        out = run4(rec("a"), item)
        assert out.state_reason == ("failure", "verdict_bad_signature")
    # a known ACCEPT is still only ignored
    known = UnparsedRecord("malformed", "b" * 32, 101, X, "ACCEPT", copy=True)
    assert run4(rec("a"), known).state_reason == ("success", "ok")


def test_equal_source_times_are_ordered_by_comment_id() -> None:
    base = rec("a")
    garbage = copies(base, 20)
    # one source time for all 21 copies; the genuine copy has the lowest comment id
    stamped = [replace(r, source_at=T0, comment_id=i + 1) for i, r in enumerate([base, *garbage])]
    for shuffled in _shuffles(stamped):
        out = evaluate_verdicts(
            shuffled,
            view(),
            FOUR,
            verify,
            (),
            ai_required=True,
            carry_forward=lambda _h: "not_ancestor",
            rebind=None,
            ai_quorum=1,
        )
        assert out.state_reason == ("success", "ok")


def test_a_naive_source_time_is_refused() -> None:
    """A naive `source_at` next to an aware one would raise in the sort key: refuse it."""
    naive = datetime(2026, 11, 14, 12, 0, 0)
    with pytest.raises(ValueError, match="source_at"):
        replace(rec("a"), source_at=naive)
    with pytest.raises(ValueError, match="source_at"):
        UnparsedRecord("pointer", "a" * 32, source_at=naive)


# --- #1122: the 100-candidate cap comes before any signature verification (GT-10, GC-15 c) ------

Y = "9" * 64  # another change hash


class CountingVerify:
    """The fake verifier, recording every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, public_key: str, message: bytes, sig: str) -> bool:
        self.calls.append(sig)
        return verify(public_key, message, sig)


def _vid(i: int) -> str:
    return f"{i:032x}"


def _distinct(count: int, **over: Any) -> list[VerdictRecord]:
    """`count` ledgered records with distinct ids and valid signatures; index 0 is the oldest."""
    return [record(verdict_id=_vid(i), seq=1000 + i, **over) for i in range(count)]


def _resign(base: VerdictRecord, sig: str) -> VerdictRecord:
    """The same ledgered payload with other signature bytes."""
    return replace(base, verdict=SignedVerdict(payload=base.payload, sig=sig))


def _cap_gate(carry: bool) -> GateView:
    # carry_forward on: the records are bound to the live head; off: they are wrong-head ACCEPTs
    return view() if carry else view(head_sha=H2, carry_forward=False)


@pytest.mark.parametrize("carry", [True, False], ids=["carry_forward_on", "carry_forward_off"])
def test_1122_over_the_cap_at_most_100_signatures_are_verified(carry: bool) -> None:
    counting = CountingVerify()
    out = run(*_distinct(150), gate=_cap_gate(carry), verifier=counting)
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert len(counting.calls) <= 100


@pytest.mark.parametrize("carry", [True, False], ids=["carry_forward_on", "carry_forward_off"])
@pytest.mark.parametrize("count", [101, 150])
def test_1122_over_the_cap_result_lists_the_candidates_beyond_the_newest_100(
    carry: bool, count: int
) -> None:
    out = run(*_distinct(count), gate=_cap_gate(carry))
    assert out.state_reason == ("failure", "verdict_bad_signature")
    assert out.counted == () and out.candidates_checked == 0
    assert out.rebind_asked == () and out.detail == "over_cap"
    # newest first: the ids beyond the 100 newest are the ones the gate did not verify
    beyond = tuple((_vid(i), "candidate_cap") for i in range(count - 101, -1, -1))
    assert tuple(e for e in out.ignored if e[1] == "candidate_cap") == beyond
    assert out.ignored == beyond  # nothing was verified, so no other reason is listed


@pytest.mark.parametrize("carry", [True, False], ids=["carry_forward_on", "carry_forward_off"])
def test_1122_exactly_100_candidates_are_each_verified_once(carry: bool) -> None:
    counting = CountingVerify()
    garbage = [_resign(r, f"{i + 1:0128x}") for i, r in enumerate(_distinct(99))]
    genuine = record(verdict_id=_vid(99), seq=2000)
    out = run(*garbage, genuine, gate=_cap_gate(carry), verifier=counting)
    assert len(counting.calls) == 100  # one call per candidate, none twice
    assert all(reason != "candidate_cap" for _, reason in out.ignored)
    if carry:
        assert out.state_reason == ("success", "ok")
        assert out.counted == (_vid(99),) and out.candidates_checked == 100
        assert len(out.ignored) == 99
    else:
        # the valid old-head ACCEPT is set aside, the 99 invalid ones are failures (GC-7 a)
        assert out.state_reason == ("failure", "verdict_bad_signature")
        assert out.ignored[0] == (_vid(99), "wrong_head") and len(out.ignored) == 100
    assert {reason for _, reason in out.ignored} <= {"verdict_bad_signature", "wrong_head"}


def test_1122_one_candidate_is_verified_once() -> None:
    counting = CountingVerify()
    assert run(record(), verifier=counting).state_reason == ("success", "ok")
    assert len(counting.calls) == 1


def test_1122_records_that_are_not_candidates_are_never_verified() -> None:
    counting = CountingVerify()
    other_change = _distinct(150, change_hash=Y)
    voided = [record(verdict_id=_vid(500 + i), seq=10 + i) for i in range(150)]
    genuine = record(verdict_id=_vid(999), seq=5000)
    out = run(
        *other_change,
        *voided,
        genuine,
        resets=(ResetRecord(seq=900, kind="change_hash", value=X),),
        verifier=counting,
    )
    assert out.state_reason == ("success", "ok")
    assert out.counted == (_vid(999),) and out.candidates_checked == 1
    assert len(counting.calls) == 1
    reasons = [reason for _, reason in out.ignored]
    assert reasons.count("change_hash") == 150 and reasons.count("reset") == 150


def test_1122_the_cap_counts_only_candidates_not_voided_or_other_change_records() -> None:
    # 100 candidates next to 150 records of another change hash: the cap is not tripped
    counting = CountingVerify()
    others = [record(verdict_id=_vid(500 + i), seq=10 + i, change_hash=Y) for i in range(150)]
    out = run(*others, *_distinct(100), verifier=counting)
    assert all(reason != "candidate_cap" for _, reason in out.ignored)
    assert out.candidates_checked == 100
    assert len(counting.calls) <= 100


def test_1122_an_origin_that_is_not_a_candidate_is_still_judged_by_its_verifying_copy() -> None:
    # the origin is for another change hash (not a candidate); its oldest copy is garbage
    origin = record(verdict_id="b" * 32, change_hash=Y, head_sha=H0, seq=90)
    garbage = _resign(origin, "e" * 128)
    rebound = record(rebound_from="b" * 32, issued_at=ts(NOW), seq=140, ledger_at=NOW)
    out = run(garbage, origin, rebound)
    # the chain is judged on the copy that verifies: the origin fails its binding, not its signature
    assert out.state_reason == ("failure", "verdict_wrong_binding")
    assert out.ignored == (
        ("b" * 32, "verdict_bad_signature"),
        ("b" * 32, "change_hash"),
        ("a" * 32, "verdict_wrong_binding"),
    )


def test_1122_a_failing_copy_of_a_record_that_is_not_a_candidate_is_not_listed() -> None:
    # forge-only: another change hash, one genuine and one garbage copy; neither is verified
    counting = CountingVerify()
    other = record(verdict_id="b" * 32, change_hash=Y, seq=90)
    out = run(_resign(other, "e" * 128), other, record(), verifier=counting)
    assert out.state_reason == ("success", "ok")
    assert out.ignored == (("b" * 32, "change_hash"),)
    assert len(counting.calls) == 1


# --- #1163: one ledger_seq per id, unique seqs across ids, the 99-candidate case --------------


def test_1163_two_copies_of_one_id_with_different_seqs_are_a_verdict_reuse() -> None:
    first = record(verdict_id=_vid(1), seq=5)
    second = record(verdict_id=_vid(1), seq=9)
    out = run(first, second)
    assert out.state_reason == ("failure", "verdict_conflict")
    assert out.detail == "verdict_reuse"


def test_1163_two_ids_sharing_one_ledger_seq_are_refused() -> None:
    out = run(record(verdict_id=_vid(1), seq=7), record(verdict_id=_vid(2), seq=7))
    assert out.state_reason == ("failure", "verdict_conflict")
    assert out.detail == "verdict_reuse"


def test_1163_two_reviewers_sharing_one_ledger_seq_are_refused() -> None:
    # the base accepted this (two reviewers, two decisions); a shared seq now fails closed
    out = run(record(seq=7), _second(seq=7), trust_list=TWO_REVIEWERS)
    assert out.state_reason == ("failure", "verdict_conflict")
    assert out.detail == "verdict_reuse"


def test_1163_control_two_ids_with_distinct_seqs_are_not_a_verdict_reuse() -> None:
    out = run(record(verdict_id=_vid(1), seq=7), record(verdict_id=_vid(2), seq=8))
    assert out.detail != "verdict_reuse"
    assert out.detail == "decision_duplicate"  # one reviewer, two decisions: the old, other reason
    out2 = run(record(seq=7), _second(seq=8), trust_list=TWO_REVIEWERS)
    assert out2.state_reason == ("success", "ok")


def test_1163_copies_of_one_id_with_the_same_seq_are_still_accepted() -> None:
    base = record(verdict_id=_vid(1), seq=5)
    out = run(base, _resign(base, f"{1:0128x}"))
    assert out.state_reason == ("success", "ok")


@pytest.mark.parametrize("carry", [True, False], ids=["carry_forward_on", "carry_forward_off"])
def test_1163_99_candidates_are_all_verified_without_a_cap_entry(carry: bool) -> None:
    counting = CountingVerify()
    out = run(*_distinct(99), gate=_cap_gate(carry), verifier=counting)
    assert len(counting.calls) == 99
    assert all(reason != "candidate_cap" for _, reason in out.ignored)
