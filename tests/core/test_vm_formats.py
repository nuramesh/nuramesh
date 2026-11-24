"""Merge Gate wire formats (spec section 6, SV-4, SV-14, QA-8; Fable fixes F-2, F-7, F-8,
F-11, F-15): pydantic models with `extra="forbid"`, the hardened canonical parser, domain strings,
the key-id formula and the key-id collision refusal. Golden vectors are frozen (never regenerated;
a format change is a `-2` version with a new domain string).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from nuramesh.core.canonical import canonical_json
from nuramesh.core.vm_formats import (
    APPROVAL_DOMAIN,
    OVERRIDE_DOMAIN,
    RESET_DOMAIN,
    VERDICT_DOMAIN,
    WITHDRAWAL_DOMAIN,
    ApprovalPayload,
    EvidenceBundle,
    FormatError,
    GateDecision,
    KeyIdCollision,
    OverridePayload,
    Policy,
    ResetPayload,
    SignedVerdict,
    Time,
    TrustList,
    VerdictPayload,
    WithdrawalPayload,
    parse_canonical,
    payload_hash,
    public_key_id,
    read_hardened_json,
    refuse_key_id_collisions,
    signed_bytes,
)

H = "a" * 40
H2 = "b" * 40
X = "c" * 64
T0 = "2026-01-01T00:00:00.000000Z"
T7 = "2026-01-08T00:00:00.000000Z"


def verdict_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": "vm-verdict-1",
        "org_id": "org-example",
        "repo": "github.com/acme/widgets",
        "repo_id": "123456",
        "pr": 7,
        "head_sha": H,
        "base_ref": "main",
        "base_sha": H2,
        "change_hash": X,
        "bundle_hash": "d" * 64,
        "tier": 3,
        "policy_sha256": "e" * 64,
        "decision": "ACCEPT",
        "reviewer_id": "reviewer-a",
        "model_id": "example-model",
        "model_family": "family-x",
        "verdict_text_sha256": "f" * 64,
        "verdict_id": "0" * 32,
        "rebound_from": None,
        "key_id": "1" * 16,
        "issued_at": T0,
        "expires_at": T7,
    }
    base.update(over)
    return base


def bundle_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": "vm-evidence-1",
        "org_id": "org-example",
        "repo": "github.com/acme/widgets",
        "repo_id": "123456",
        "pr": 7,
        "head_sha": H,
        "tree_sha": "2" * 40,
        "base_ref": "main",
        "base_sha": H2,
        "merge_base": H2,
        "change_hash": X,
        "diff_hash": "3" * 64,
        "git_version": "2.45.0",
        "pins": {"merge_tree": "mt-1", "diff": "diff-1"},
        "author": {
            "kind": "human",
            "id": "human:author-1",
            "model_family": None,
            "source": "human_evidence",
            "run_ref": None,
        },
        "paths": [{"path": "src/a.py", "status": "M"}],
        "commits": [
            {
                "sha": H,
                "parents": [H2],
                "tree": "2" * 40,
                "merge_tree": None,
                "author_id": "human:author-1",
                "pusher_id": "human:author-1",
            }
        ],
        "tier": 3,
        "tier_reasons": ["tier3_path"],
        "tier_list_sha256": "4" * 64,
        "policy_sha256": "e" * 64,
        "codeowners_sha256": "5" * 64,
        "canon_commit": None,
        "task_sha256": None,
        "checks": [
            {
                "name": "ci-ok",
                "kind": "check_run",
                "producer_id": "15368",
                "head_sha": H,
                "conclusion": "success",
                "run_id": "123",
                "completed_at": T0,
            }
        ],
        "full_tier": [
            {
                "name": "full-gate",
                "binding": {"pr": 7, "base_ref": "main", "head_sha": H},
                "kind": "check_run",
                "producer_id": "15368",
                "conclusion": "success",
                "run_id": "124",
                "started_at": T0,
                "completed_at": T0,
            }
        ],
        "reviews": [],
        "labels": ["tier-3"],
        "collected_at": T0,
        "collector": "service:merge-gate-collector",
        "bundle_hash": "6" * 64,
    }
    base.update(over)
    return base


def trust_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": "vm-trust-1",
        "org_id": "org-example",
        "serial": 5,
        "keys": [
            {
                "key_id": public_key_id("7" * 64),
                "role": "verdict-signer",
                "holder": "service:reviewer-a",
                "roles": [],
                "public_key": "7" * 64,
                "custody": "service",
                "valid_from": T0,
                "valid_to": None,
                "revoked_at": None,
                "revoked_seq": None,
                "compromised": False,
            }
        ],
    }
    base.update(over)
    return base


# --- strictness ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "good"),
    [
        (VerdictPayload, verdict_dict()),
        (EvidenceBundle, bundle_dict()),
        (TrustList, trust_dict()),
    ],
)
def test_unknown_fields_are_refused(model: type[Any], good: dict[str, Any]) -> None:
    model.model_validate(good)
    with pytest.raises(ValidationError):
        model.model_validate({**good, "extra": 1})


def test_types_are_strict_and_shapes_are_checked() -> None:
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(pr="7"))  # a string is not an int
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(head_sha=H.upper()))
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(decision="MAYBE"))
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(issued_at="2026-01-01T00:00:00Z"))  # µs needed
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(version="vm-verdict-2"))
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(repo_id="not-a-number"))
    assert VerdictPayload.model_validate(verdict_dict()).rebound_from is None
    assert VerdictPayload.model_validate(verdict_dict(rebound_from="9" * 32)).rebound_from


def test_models_are_frozen() -> None:
    verdict = VerdictPayload.model_validate(verdict_dict())
    with pytest.raises(ValidationError):
        verdict.pr = 8


def test_identity_fields_are_opaque_ids_never_logins() -> None:
    good = bundle_dict()
    for bad in ("alice", "alice@example.com", "human:", "unmapped:alice"):
        commits = [dict(good["commits"][0], pusher_id=bad)]
        with pytest.raises(ValidationError):
            EvidenceBundle.model_validate(bundle_dict(commits=commits))
    for ok in ("human:owner-1", "agent:builder", "unmapped:" + "0" * 64, "unmapped:unavailable"):
        commits = [dict(good["commits"][0], pusher_id=ok)]
        EvidenceBundle.model_validate(bundle_dict(commits=commits))


def test_944_pins_diff_accepts_diff2_and_the_frozen_diff1_only() -> None:
    """Writers record `diff-2` (#944); `diff-1` still parses so format 1 is never edited (the gate,
    not the parser, refuses a `diff-1` bundle); any other pin is not `vm-evidence-1`."""
    for pin in ("diff-1", "diff-2"):
        pins = {"merge_tree": "mt-1", "diff": pin}
        bundle = EvidenceBundle.model_validate(bundle_dict(pins=pins))
        assert bundle.pins.diff == pin
    for bad in ("diff-3", "DIFF-2", "", "diff-2 "):
        with pytest.raises(ValidationError):
            EvidenceBundle.model_validate(bundle_dict(pins={"merge_tree": "mt-1", "diff": bad}))


def test_bundle_hash_is_over_the_object_without_its_own_hash() -> None:
    bundle = EvidenceBundle.model_validate(bundle_dict())
    body = bundle.model_dump(mode="json")
    body.pop("bundle_hash")
    expected = hashlib.sha256(canonical_json(body)).hexdigest()
    assert bundle.body_hash() == expected
    assert not bundle.hash_ok()  # the fixture carries a fake hash
    sealed = EvidenceBundle.model_validate(bundle_dict(bundle_hash=expected))
    assert sealed.hash_ok()
    edited = EvidenceBundle.model_validate(bundle_dict(bundle_hash=expected, tier=2))
    assert not edited.hash_ok()  # G-08: a field edited after hashing


def full_policy(**over: Any) -> dict[str, Any]:
    """A complete policy file (GC-11 (c): every top-level key but `extra_trust_roots`)."""
    base: dict[str, Any] = {
        "version": "vm-policy-1",
        "enforce_tiers": [3],
        "required_checks": {"1": ["ci-ok"], "2": ["ci-ok"], "3": ["ci-ok"]},
        "full_tier": ["full-gate"],
        "full_tier_tiers": [3],
        "producers": {"ci-ok": {"kind": "check_run", "id": "15368"}},
        "quorum": {
            "1": {"ai": 0, "human": 0},
            "2": {"ai": 1, "human": 0},
            "3": {"ai": 1, "human": 1},
        },
        "hold_labels": ["hold"],
        "carry_forward": True,
        "verdict_ttl_seconds": 604800,
        "approval_ttl_seconds": 1209600,
        "bundle_ttl_seconds": 86400,
        "last_pusher_may_approve": False,
        "sole_codeowner_exception": False,
        "approval_binds_bundle": False,
        "approver_custody_min": "software",
        "review_budget_scope": "merge-review",
        "max_diff_bytes": 3_000_000,
        "reject_memory_days": 30,
        "task_reject_limit": 2,
        "task_ask_limit": 4,
        "gate_secret_names": [],
    }
    base.update(over)
    return base


def test_policy_values_match_section_6_6_and_the_tm21_tier3_quorum() -> None:
    policy = Policy.model_validate(full_policy())
    assert policy.enforce_tiers == [3]
    assert policy.quorum["3"].ai == 1 and policy.quorum["3"].human == 1
    assert policy.quorum["3"].codeowner is True  # GC-11 (a): the quorum fields keep defaults
    assert policy.quorum["3"].min_model_families == 1
    assert policy.quorum["3"].author_family_differs is True
    assert policy.quorum["2"].ai == 1 and policy.quorum["2"].human == 0
    assert policy.quorum["1"].ai == 0 and policy.quorum["1"].human == 0
    assert policy.carry_forward is True
    assert policy.producers["ci-ok"].kind == "check_run"
    assert policy.extra_trust_roots == []
    with pytest.raises(ValidationError):
        Policy.model_validate(full_policy(verdict_ttl_seconds=31 * 86400))


@pytest.mark.parametrize("key", sorted(k for k in full_policy() if k != "version"))
def test_gc11c_a_policy_missing_a_top_level_key_is_invalid(key: str) -> None:
    body = full_policy()
    del body[key]
    with pytest.raises(ValidationError):
        Policy.model_validate(body)


def test_gc11c_only_extra_trust_roots_may_be_absent_and_hashes_like_an_empty_array() -> None:
    absent = Policy.model_validate(full_policy())
    empty = Policy.model_validate(full_policy(extra_trust_roots=[]))
    assert payload_hash(absent) == payload_hash(empty)
    assert absent.model_dump(mode="json")["extra_trust_roots"] == []


@pytest.mark.parametrize(
    "value",
    ["ops/**", {"a": "b"}, None, 5, True, ["ops/**", 5], [None], [["a"]]],
    ids=["string", "object", "null", "number", "bool", "mixed", "null item", "nested"],
)
def test_tc149_an_invalid_extra_trust_roots_is_carried_as_written(value: Any) -> None:
    """GC-11 (b), TL-29: not `policy_invalid`; the populated object keeps the value as written
    so it is hashed as written (the tier decision then gives `trust_roots_invalid`)."""
    policy = Policy.model_validate(full_policy(extra_trust_roots=value))
    assert policy.model_dump(mode="json")["extra_trust_roots"] == value
    assert payload_hash(policy) != payload_hash(Policy.model_validate(full_policy()))


def test_gc11b_arrays_are_hashed_as_written() -> None:
    a = Policy.model_validate(full_policy(extra_trust_roots=["a/", "b/"]))
    b = Policy.model_validate(full_policy(extra_trust_roots=["b/", "a/"]))
    dup = Policy.model_validate(full_policy(extra_trust_roots=["a/", "a/", "b/"]))
    assert len({payload_hash(a), payload_hash(b), payload_hash(dup)}) == 3


# --- SV-14 parse hardening ----------------------------------------------------------------------


def test_duplicate_keys_are_refused() -> None:
    with pytest.raises(FormatError) as info:
        read_hardened_json(b'{"a":1,"a":2}')
    assert info.value.code == "duplicate_key"
    with pytest.raises(FormatError) as info:
        read_hardened_json(b'{"x":{"a":1,"a":2}}')
    assert info.value.code == "duplicate_key"


def test_integers_outside_the_i_json_range_and_floats_are_refused() -> None:
    assert read_hardened_json(b"9007199254740991") == 2**53 - 1
    for text in (b"9007199254740992", b"-9007199254740992", b"1" * 400):
        with pytest.raises(FormatError) as info:
            read_hardened_json(text)
        assert info.value.code == "unsafe_integer"
    for text in (b"1.5", b"1e3", b"NaN", b"Infinity"):
        with pytest.raises(FormatError) as info:
            read_hardened_json(text)
        assert info.value.code == "not_canonical_number"


def test_parse_canonical_requires_byte_for_byte_canonical_input() -> None:
    payload = verdict_dict()
    canonical = canonical_json(payload)
    assert parse_canonical(canonical, VerdictPayload).pr == 7
    spaced = json.dumps(payload).encode()
    with pytest.raises(FormatError) as info:
        parse_canonical(spaced, VerdictPayload)
    assert info.value.code == "not_canonical"
    with pytest.raises(FormatError) as info:
        parse_canonical(b"\xff", VerdictPayload)
    assert info.value.code == "not_json"
    with pytest.raises(FormatError) as info:
        parse_canonical(b"[]", VerdictPayload)
    assert info.value.code == "not_object"


def test_parse_canonical_stops_at_unsupported_version_before_anything_else() -> None:
    wrong = canonical_json(verdict_dict(version="vm-verdict-9", pr="7"))
    with pytest.raises(FormatError) as info:
        parse_canonical(wrong, VerdictPayload)
    assert info.value.code == "unsupported_version"
    other = canonical_json({"version": "vm-approval-1"})
    with pytest.raises(FormatError) as info:
        parse_canonical(other, VerdictPayload)
    assert info.value.code == "unsupported_version"
    bad_shape = canonical_json(verdict_dict(pr="7"))
    with pytest.raises(FormatError) as info:
        parse_canonical(bad_shape, VerdictPayload)
    assert info.value.code == "schema"
    assert "7" not in info.value.public_message()


def test_error_text_never_carries_the_input() -> None:
    with pytest.raises(FormatError) as info:
        read_hardened_json(b'{"secret-looking-key":1,"secret-looking-key":2}')
    assert "secret-looking" not in str(info.value)


# --- domains, signed bytes, hashes --------------------------------------------------------------


def test_domain_strings_are_distinct_and_versioned() -> None:
    domains = {VERDICT_DOMAIN, APPROVAL_DOMAIN, RESET_DOMAIN, WITHDRAWAL_DOMAIN, OVERRIDE_DOMAIN}
    assert len(domains) == 5
    assert VERDICT_DOMAIN == b"nuramesh-vm-verdict-v1\n"
    assert APPROVAL_DOMAIN == b"nuramesh-vm-approval-v1\n"
    assert RESET_DOMAIN == b"nuramesh-vm-reset-v1\n"
    assert WITHDRAWAL_DOMAIN == b"nuramesh-vm-withdrawal-v1\n"
    assert OVERRIDE_DOMAIN == b"nuramesh-vm-override-v1\n"
    assert all(d.endswith(b"\n") and b"nuramesh-vm-" in d for d in domains)


def test_signed_bytes_are_domain_plus_canonical_payload() -> None:
    verdict = VerdictPayload.model_validate(verdict_dict())
    assert signed_bytes(VERDICT_DOMAIN, verdict) == VERDICT_DOMAIN + canonical_json(verdict_dict())
    assert payload_hash(verdict) == hashlib.sha256(canonical_json(verdict_dict())).hexdigest()


def test_golden_vectors_never_regenerated() -> None:
    """Frozen (never-regenerate policy): a change here means a new format version."""
    verdict = VerdictPayload.model_validate(verdict_dict())
    assert (
        payload_hash(verdict) == "1bd90eb35910008aeb802cd608e8b219cdcfcf18a35039121b49e300bb8381d1"
    )
    assert (
        hashlib.sha256(signed_bytes(VERDICT_DOMAIN, verdict)).hexdigest()
        == "1f24d3131fb8269051a350f624bc116e56dbe8e79a4ced0ca7b72e88fcb379c6"
    )
    bundle = EvidenceBundle.model_validate(bundle_dict())
    assert bundle.body_hash() == "bb1d0ab11584c1010df7ab21aba7e62129deb06bf23ca941981bb6ed71c6a93d"
    trust = TrustList.model_validate(trust_dict())
    assert payload_hash(trust) == "51cb345f4c018cea8e0749570e91bf53ada9ef60166d934fb19158c23ffd5579"


def test_signed_object_carries_payload_and_hex_signature() -> None:
    stored = {"payload": verdict_dict(), "sig": "9" * 128}
    assert SignedVerdict.model_validate(stored).sig == "9" * 128
    with pytest.raises(ValidationError):
        SignedVerdict.model_validate({"payload": verdict_dict(), "sig": "9" * 127})
    with pytest.raises(ValidationError):
        SignedVerdict.model_validate({"payload": verdict_dict(), "sig": "9" * 128, "poster": "x"})


def test_reserved_human_signed_formats_6_8_to_6_10() -> None:
    reset = ResetPayload.model_validate(
        {
            "version": "vm-reset-1",
            "org_id": "org-example",
            "repo": "github.com/acme/widgets",
            "repo_id": "123456",
            "scope": {"kind": "change_hash", "value": X},
            "reason_sha256": "8" * 64,
            "approver_id": "human:admin-1",
            "key_id": "1" * 16,
            "reset_id": "0" * 32,
            "issued_at": T0,
        }
    )
    assert reset.scope.kind == "change_hash"
    with pytest.raises(ValidationError):
        ResetPayload.model_validate(
            {**reset.model_dump(), "scope": {"kind": "task", "value": None}}
        )
    flip = ResetPayload.model_validate(
        {**reset.model_dump(), "scope": {"kind": "mode_flip", "value": None}}
    )
    assert flip.scope.value is None
    withdrawal = WithdrawalPayload.model_validate(
        {
            "version": "vm-withdrawal-1",
            "org_id": "org-example",
            "repo": "github.com/acme/widgets",
            "repo_id": "123456",
            "pr": 7,
            "approval_id": "0" * 32,
            "approver_id": "human:owner-1",
            "key_id": "1" * 16,
            "withdrawal_id": "2" * 32,
            "issued_at": T0,
        }
    )
    assert withdrawal.approval_id == "0" * 32
    override = OverridePayload.model_validate(
        {
            "version": "vm-override-1",
            "org_id": "org-example",
            "repo": "github.com/acme/widgets",
            "repo_id": "123456",
            "pr": 7,
            "head_sha": H,
            "change_hash": X,
            "reason_sha256": "8" * 64,
            "approver_id": "human:admin-1",
            "key_id": "1" * 16,
            "override_id": "3" * 32,
            "issued_at": T0,
        }
    )
    assert override.head_sha == H  # F-9: the override binds the head it saw
    with pytest.raises(ValidationError):
        OverridePayload.model_validate({**override.model_dump(), "reason": "free text"})


def test_approval_and_decision_shapes() -> None:
    approval = ApprovalPayload.model_validate(
        {
            "version": "vm-approval-1",
            "org_id": "org-example",
            "repo": "github.com/acme/widgets",
            "repo_id": "123456",
            "pr": 7,
            "head_sha": H,
            "change_hash": X,
            "bundle_hash": None,
            "policy_sha256": "e" * 64,
            "approver_id": "human:owner-1",
            "role": "code_owner",
            "key_id": "1" * 16,
            "approval_id": "0" * 32,
            "issued_at": T0,
            "expires_at": T7,
        }
    )
    assert approval.role == "code_owner"
    decision = GateDecision.model_validate(
        {
            "version": "vm-decision-1",
            "org_id": "org-example",
            "repo": "github.com/acme/widgets",
            "repo_id": "123456",
            "pr": 7,
            "head_sha": H,
            "group_head_sha": None,
            "shared_head_prs": [7],
            "mode": "enforce",
            "state": "success",
            "reason": "ok",
            "counted": {"verdicts": ["0" * 32], "approvals": []},
            "ignored": [{"id": "1" * 32, "reason": "not_in_ledger"}],
            "inputs": {"bundle_hash": "6" * 64, "status_ids": ["55"]},
            "override": False,
            "decided_at": T0,
            "gate_version": "0.1.0",
            "decision_hash": "7" * 64,
        }
    )
    assert decision.state == "success"
    with pytest.raises(ValidationError):
        GateDecision.model_validate({**decision.model_dump(), "state": "skipped"})


# --- trust list ---------------------------------------------------------------------------------


def test_key_id_formula_matches_anchors() -> None:
    public = "7" * 64
    assert public_key_id(public) == hashlib.sha256(bytes.fromhex(public)).hexdigest()[:16]
    with pytest.raises(ValueError):
        public_key_id("77")  # not 32 bytes


def test_trust_list_loads_a_malformed_key_and_reports_it_per_key() -> None:
    """6.7: `revoked_at` set with `revoked_seq` null is an invalid entry the gate treats as
    untrusted; the list itself still loads so the rest of the keys work."""
    bad = dict(trust_dict()["keys"][0], revoked_at=T0, revoked_seq=None)
    trust = TrustList.model_validate(trust_dict(keys=[bad]))
    key = trust.keys[0]
    assert key.problem() == "revoked_without_seq"
    assert trust.key(key.key_id) is key
    wrong_id = dict(trust_dict()["keys"][0], key_id="f" * 16)
    assert TrustList.model_validate(trust_dict(keys=[wrong_id])).keys[0].problem() == "key_id"
    good = TrustList.model_validate(trust_dict())
    assert good.keys[0].problem() is None
    assert good.key("0" * 16) is None


def test_trust_list_refuses_duplicates_and_bad_holders() -> None:
    one = trust_dict()["keys"][0]
    with pytest.raises(ValidationError):
        TrustList.model_validate(trust_dict(keys=[one, one]))
    with pytest.raises(ValidationError):
        TrustList.model_validate(trust_dict(keys=[dict(one, holder="alice@example.com")]))
    with pytest.raises(ValidationError):
        TrustList.model_validate(trust_dict(serial=0))
    with pytest.raises(ValidationError):
        TrustList.model_validate(trust_dict(keys=[dict(one, roles=["root"])]))
    approver = dict(
        one,
        key_id=public_key_id("8" * 64),
        public_key="8" * 64,
        role="approver",
        holder="human:admin-1",
        roles=["approver", "trust_root_admin"],
        custody="hardware",
    )
    trust = TrustList.model_validate(trust_dict(keys=[one, approver]))
    admin = trust.key(approver["key_id"])
    assert admin is not None and admin.has_role("trust_root_admin")
    assert not trust.keys[0].has_role("trust_root_admin")


def test_key_id_collision_with_other_key_spaces_is_refused() -> None:
    trust = TrustList.model_validate(trust_dict())
    kid = trust.keys[0].key_id
    refuse_key_id_collisions(trust, {"0" * 16: "anchor"})
    with pytest.raises(KeyIdCollision) as info:
        refuse_key_id_collisions(trust, {kid: "anchor"})
    assert info.value.space == "anchor" and info.value.key_id == kid
    with pytest.raises(KeyIdCollision) as info:
        refuse_key_id_collisions(trust, {"0" * 16: "anchor", kid: "licence"})
    assert info.value.space == "licence"


class VerdictTimeProbe(BaseModel):
    issued_at: Time


def test_vm1_r1_time_must_be_a_real_calendar_time() -> None:
    from nuramesh.core.vm_formats import parse_time  # noqa: F401

    good = {"issued_at": "2026-01-01T00:00:00.000000Z"}
    VerdictTimeProbe.model_validate(good)
    for bad in ("2026-13-45T99:99:99.000000Z", "2026-02-30T00:00:00.000000Z"):
        with pytest.raises(ValidationError):
            VerdictTimeProbe.model_validate({"issued_at": bad})


@pytest.mark.parametrize(
    "reason", ["tier3_path:src/nuramesh/core/ledger.py", "free text", "Tier3", "", "a" * 65]
)
def test_tier_reasons_hold_codes_only_never_a_path_or_text(reason: str) -> None:
    """#947, TL-17: a path or free text never goes into the ledger through `tier_reasons`."""
    base = bundle_dict()

    def make(items: list[str]) -> EvidenceBundle:
        return EvidenceBundle.model_validate({**base, "tier_reasons": items})

    good = ["tier3_label", "tier3_path"]  # TL-17: sorted, unique, never mixed with tier2
    assert make(good).tier_reasons == good
    with pytest.raises(ValidationError):
        make([reason])


def _with_reasons(tier: int, items: list[str]) -> EvidenceBundle:
    return EvidenceBundle.model_validate({**bundle_dict(), "tier": tier, "tier_reasons": items})


@pytest.mark.parametrize(
    "items",
    [
        ["tier3_path", "tier3_label"],  # not sorted by code point
        ["tier3_path", "tier3_path"],  # duplicate
        ["tier3_label", "tier3_path", "tier3_label"],
    ],
)
def test_m2_tier_reasons_must_be_sorted_and_unique(items: list[str]) -> None:
    """#1106 m2, TL-17: the wire type rejects an unsorted or duplicated list."""
    with pytest.raises(ValidationError):
        EvidenceBundle.model_validate({**bundle_dict(), "tier_reasons": items})


@pytest.mark.parametrize(
    "items",
    [["tier3_label", "tier3_path", "tier3_sticky"], ["tier3_path"], ["tier2_default"], []],
)
def test_m2_sorted_unique_lists_still_validate(items: list[str]) -> None:
    """Fit to the tier is the gate's check (`tier_mismatch`, TL-20), so it is not a wire rule."""
    base = {**bundle_dict(), "tier_reasons": items}
    assert EvidenceBundle.model_validate(base).tier_reasons == items
