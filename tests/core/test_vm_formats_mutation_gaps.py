"""Mutation pins for `nuramesh.core.vm_formats` (#1333): behaviour of the small helpers, the
default values and the numeric bounds that the wider format tests leave unchecked.
Fake data only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from nuramesh.core import vm_formats
from nuramesh.core.vm_formats import (
    VERDICT_TTL_MAX_SECONDS,
    EvidenceBundle,
    FormatError,
    KeyIdCollision,
    Policy,
    Quorum,
    ResetScope,
    TrustKey,
    TrustList,
    VerdictPayload,
    VmModel,
    format_time,
    parse_canonical,
    parse_time,
    public_key_id,
    read_hardened_json,
    refuse_key_id_collisions,
)

from .test_vm_formats import bundle_dict, full_policy, trust_dict  # noqa: E402

T0 = "2026-01-01T00:00:00.000000Z"
PUB = "7" * 64


def verdict_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": "vm-verdict-1",
        "org_id": "org-example",
        "repo": "github.com/acme/widgets",
        "repo_id": "123456",
        "pr": 7,
        "head_sha": "a" * 40,
        "base_ref": "main",
        "base_sha": "b" * 40,
        "change_hash": "c" * 64,
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
        "expires_at": "2026-01-08T00:00:00.000000Z",
    }
    base.update(over)
    return base


def key_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key_id": public_key_id(PUB),
        "role": "verdict-signer",
        "holder": "service:reviewer-a",
        "roles": [],
        "public_key": PUB,
        "custody": "service",
        "valid_from": T0,
        "valid_to": None,
        "revoked_at": None,
        "revoked_seq": None,
        "compromised": False,
    }
    base.update(over)
    return base


# --- TrustKey.problem: every branch returns its own code ---------------------------------------


def test_problem_is_none_for_a_well_formed_key() -> None:
    assert TrustKey.model_validate(key_dict()).problem() is None
    approver = key_dict(role="approver", holder="human:owner-1", custody="software")
    assert TrustKey.model_validate(approver).problem() is None


def test_problem_names_a_revocation_without_a_sequence() -> None:
    key = TrustKey.model_validate(key_dict(revoked_at=T0))
    assert key.problem() == "revoked_without_seq"
    assert TrustKey.model_validate(key_dict(revoked_at=T0, revoked_seq=3)).problem() is None


def test_problem_names_a_key_id_that_is_not_the_formula_value() -> None:
    assert TrustKey.model_validate(key_dict(key_id="0" * 16)).problem() == "key_id"


def test_problem_names_an_approver_with_a_service_holder() -> None:
    key = TrustKey.model_validate(key_dict(role="approver", holder="service:reviewer-a"))
    assert key.problem() == "holder_kind"


@pytest.mark.parametrize("role", ["verdict-signer", "attestor"])
def test_problem_names_a_service_role_with_a_human_holder(role: str) -> None:
    key = TrustKey.model_validate(key_dict(role=role, holder="human:owner-1"))
    assert key.problem() == "holder_kind"


def test_problem_reports_the_first_problem_in_order() -> None:
    both = key_dict(key_id="0" * 16, revoked_at=T0)
    assert TrustKey.model_validate(both).problem() == "revoked_without_seq"
    bad = key_dict(key_id="0" * 16, holder="human:owner-1")
    assert TrustKey.model_validate(bad).problem() == "key_id"


# --- Quorum defaults ----------------------------------------------------------------------------


def test_quorum_defaults_are_the_lenient_values() -> None:
    quorum = Quorum.model_validate({"ai": 0, "human": 0})
    assert quorum.codeowner is False
    assert quorum.min_model_families == 1
    assert quorum.author_family_differs is False


def test_quorum_min_model_families_is_at_least_one() -> None:
    assert Quorum.model_validate({"ai": 0, "human": 0, "min_model_families": 1})
    with pytest.raises(ValidationError):
        Quorum.model_validate({"ai": 0, "human": 0, "min_model_families": 0})


# --- tier bounds, verdict ttl, strict strings ---------------------------------------------------


@pytest.mark.parametrize("tier", [1, 2, 3])
def test_tier_accepts_exactly_one_to_three(tier: int) -> None:
    assert VerdictPayload.model_validate(verdict_dict(tier=tier)).tier == tier


@pytest.mark.parametrize("tier", [0, 4, -1])
def test_tier_outside_one_to_three_is_refused(tier: int) -> None:
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(tier=tier))


def test_verdict_ttl_maximum_is_thirty_days() -> None:
    assert VERDICT_TTL_MAX_SECONDS == 30 * 86400


def test_strict_strings_refuse_bytes() -> None:
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(head_sha=b"a" * 40))


# --- time helpers -------------------------------------------------------------------------------


def test_parse_time_returns_an_aware_utc_datetime() -> None:
    parsed = parse_time("2026-03-04T05:06:07.123456Z")
    assert parsed == datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC)
    assert parsed.utcoffset() == timedelta(0)


def test_format_time_converts_to_utc_and_keeps_microseconds() -> None:
    zone = timezone(timedelta(hours=2))
    assert format_time(datetime(2026, 3, 4, 7, 6, 7, 5, tzinfo=zone)) == (
        "2026-03-04T05:06:07.000005Z"
    )
    assert parse_time(format_time(parse_time(T0))) == parse_time(T0)


# --- integer bounds and strictness --------------------------------------------------------------


def test_pr_number_starts_at_one() -> None:
    assert VerdictPayload.model_validate(verdict_dict(pr=1)).pr == 1
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(pr=0))


def test_trust_list_serial_starts_at_one() -> None:
    body = {"version": "vm-trust-1", "org_id": "org-example", "serial": 1, "keys": []}
    assert TrustList.model_validate(body).serial == 1
    with pytest.raises(ValidationError):
        TrustList.model_validate({**body, "serial": 0})


def test_quorum_counts_allow_zero_and_refuse_negative_and_text() -> None:
    assert Quorum.model_validate({"ai": 0, "human": 0}).ai == 0
    with pytest.raises(ValidationError):
        Quorum.model_validate({"ai": -1, "human": 0})
    with pytest.raises(ValidationError):
        Quorum.model_validate({"ai": "1", "human": 0})  # strict: no coercion from text


def test_quorum_min_model_families_is_strict() -> None:
    with pytest.raises(ValidationError):
        Quorum.model_validate({"ai": 0, "human": 0, "min_model_families": "1"})


def test_a_huge_integer_literal_is_unsafe_not_unparseable() -> None:
    for text in (b"1" + b"0" * 16, b"-" + b"1" + b"0" * 16, b"1" * 5000):
        with pytest.raises(FormatError) as caught:
            read_hardened_json(text)
        assert caught.value.code == "unsafe_integer"


# --- wire version constants and the CI conclusion set (survivors of the first sweep) ---


@pytest.mark.parametrize(
    ("constant", "text", "model"),
    [
        ("EVIDENCE_VERSION", "vm-evidence-1", "EvidenceBundle"),
        ("VERDICT_VERSION", "vm-verdict-1", "VerdictPayload"),
        ("APPROVAL_VERSION", "vm-approval-1", "ApprovalPayload"),
        ("DECISION_VERSION", "vm-decision-1", "GateDecision"),
        ("POLICY_VERSION", "vm-policy-1", "Policy"),
        ("TRUST_VERSION", "vm-trust-1", "TrustList"),
        ("RESET_VERSION", "vm-reset-1", "ResetPayload"),
        ("WITHDRAWAL_VERSION", "vm-withdrawal-1", "WithdrawalPayload"),
        ("OVERRIDE_VERSION", "vm-override-1", "OverridePayload"),
    ],
)
def test_wire_version_constants_are_the_spec_strings(constant: str, text: str, model: str) -> None:
    from nuramesh.core import vm_formats

    assert getattr(vm_formats, constant) == text
    assert getattr(vm_formats, model).WIRE_VERSION == text


CONCLUSIONS = [
    "success",
    "failure",
    "neutral",
    "cancelled",
    "timed_out",
    "action_required",
    "skipped",
    "stale",
    "pending",
    "in_progress",
    "queued",
    "error",
]
LITERALS: dict[tuple[str, str], list[str]] = {
    ("ApprovalPayload", "role"): ["code_owner", "trust_root_admin", "reviewer"],
    ("Author", "kind"): ["agent", "human", "unknown"],
    ("Author", "source"): ["ledger_run", "human_evidence", "unknown"],
    ("CheckEntry", "kind"): ["check_run", "status"],
    ("CheckEntry", "conclusion"): CONCLUSIONS,
    ("FullTierEntry", "kind"): ["check_run", "status"],
    ("FullTierEntry", "conclusion"): CONCLUSIONS,
    ("GateDecision", "mode"): ["shadow", "enforce"],
    ("GateDecision", "state"): ["success", "failure", "pending"],
    ("PathEntry", "status"): ["A", "D", "M"],
    ("Pins", "merge_tree"): ["mt-1"],
    ("Pins", "diff"): ["diff-1", "diff-2"],
    ("Policy", "approver_custody_min"): ["software", "hardware"],
    ("Producer", "kind"): ["check_run", "status"],
    ("ResetScope", "kind"): ["change_hash", "task", "mode_flip"],
    ("ReviewEntry", "verdict"): ["ACCEPT", "REJECT"],
    ("TrustKey", "role"): ["verdict-signer", "attestor", "approver"],
    ("TrustKey", "roles"): ["approver", "trust_root_admin"],
    ("TrustKey", "custody"): ["hardware", "software", "service"],
    ("VerdictPayload", "decision"): ["ACCEPT", "REJECT"],
    ("EvidenceBundle", "version"): ["vm-evidence-1"],
    ("VerdictPayload", "version"): ["vm-verdict-1"],
    ("ApprovalPayload", "version"): ["vm-approval-1"],
    ("GateDecision", "version"): ["vm-decision-1"],
    ("Policy", "version"): ["vm-policy-1"],
    ("TrustList", "version"): ["vm-trust-1"],
    ("ResetPayload", "version"): ["vm-reset-1"],
    ("WithdrawalPayload", "version"): ["vm-withdrawal-1"],
    ("OverridePayload", "version"): ["vm-override-1"],
}


def _arms(annotation: Any) -> list[Any]:
    import typing

    if typing.get_origin(annotation) is typing.Literal:
        return [annotation]
    return [a for a in typing.get_args(annotation) if typing.get_origin(a) is typing.Literal]


@pytest.mark.parametrize(("where", "expected"), list(LITERALS.items()))
def test_every_literal_field_has_exactly_the_spec_values(
    where: tuple[str, str], expected: list[str]
) -> None:
    import typing

    model = getattr(vm_formats, where[0])
    (arm,) = _arms(model.model_fields[where[1]].annotation)
    assert list(typing.get_args(arm)) == expected
    adapter = TypeAdapter(arm)
    for value in expected:
        assert adapter.validate_python(value, strict=True) == value
        with pytest.raises(ValidationError):
            adapter.validate_python(value + "X", strict=True)


def test_no_literal_field_is_missing_from_the_table() -> None:
    found = set()
    for name in dir(vm_formats):
        cls = getattr(vm_formats, name)
        if isinstance(cls, type) and issubclass(cls, VmModel) and cls is not VmModel:
            for field, info in cls.model_fields.items():
                if _arms(info.annotation):
                    found.add((name, field))
    assert found == set(LITERALS)


def _message(model: Any, data: dict[str, Any]) -> list[str]:
    """The validator messages (after pydantic's `Value error, ` prefix) of a refused value."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data)
    return [str(e["msg"]).removeprefix("Value error, ") for e in caught.value.errors()]


def test_validator_messages_name_the_rule() -> None:
    assert "not a real calendar time" in _message(
        VerdictPayload, verdict_dict(issued_at="2026-02-30T00:00:00.000000Z")
    )
    assert "tier_reasons must be sorted and unique" in _message(
        EvidenceBundle, bundle_dict(tier_reasons=["b", "a"])
    )
    assert "tier_reasons must be sorted and unique" in _message(
        EvidenceBundle, bundle_dict(tier_reasons=["a", "a"])
    )
    keys = trust_dict()["keys"]
    assert "trust list has a duplicate key_id" in _message(
        TrustList, trust_dict(keys=[keys[0], keys[0]])
    )
    quorum = full_policy()["quorum"]
    assert "quorum must have exactly the keys 1, 2 and 3" in _message(
        Policy, full_policy(quorum={"1": quorum["1"]})
    )
    assert "required_checks must have exactly the keys 1, 2 and 3" in _message(
        Policy, full_policy(required_checks={"1": []})
    )


def test_reset_scope_rules() -> None:
    assert ResetScope.model_validate({"kind": "mode_flip", "value": None}).value is None
    assert ResetScope.model_validate({"kind": "task", "value": "t-1"}).kind == "task"
    ok = ResetScope.model_validate({"kind": "change_hash", "value": "a" * 64})
    assert ok.value == "a" * 64
    assert "a reset scope value is required unless the kind is mode_flip" in _message(
        ResetScope, {"kind": "task", "value": None}
    )
    assert "a reset scope value is required unless the kind is mode_flip" in _message(
        ResetScope, {"kind": "mode_flip", "value": "x"}
    )
    for bad in ("a" * 63, "a" * 65):
        assert "a change_hash scope value is a 64-hex hash" in _message(
            ResetScope, {"kind": "change_hash", "value": bad}
        )


def test_public_key_id_messages() -> None:
    for bad, text in (("zz", "public key is not hex"), ("ab" * 31, "public key is not 32 bytes")):
        with pytest.raises(ValueError) as caught:
            public_key_id(bad)
        assert str(caught.value) == text
    with pytest.raises(ValueError) as caught:
        public_key_id("ab" * 33)
    assert str(caught.value) == "public key is not 32 bytes"


def test_key_id_collision_message_and_format_error_message() -> None:
    trust = TrustList.model_validate(trust_dict())
    kid = trust.keys[0].key_id
    with pytest.raises(KeyIdCollision) as caught:
        refuse_key_id_collisions(trust, {kid: "anchor"})
    assert str(caught.value) == "trust-list key id collides with a anchor key id"
    err = FormatError("schema")
    assert str(err) == "format: schema" and err.public_message() == "format: schema"


def test_integer_bounds_of_the_shared_types() -> None:
    from nuramesh.core.canonical import MAX_SAFE_INTEGER

    assert Quorum.model_validate({"ai": MAX_SAFE_INTEGER, "human": 0}).ai == MAX_SAFE_INTEGER
    for bad in (True, "1", MAX_SAFE_INTEGER + 1, -1):
        with pytest.raises(ValidationError):
            Quorum.model_validate({"ai": bad, "human": 0})
    for seconds in (1, 604800):
        assert Policy.model_validate(full_policy(approval_ttl_seconds=seconds))
    with pytest.raises(ValidationError):
        Policy.model_validate(full_policy(approval_ttl_seconds=0))
    with pytest.raises(ValidationError):
        Policy.model_validate(full_policy(approval_ttl_seconds=True))
    for ttl in (1, VERDICT_TTL_MAX_SECONDS):
        assert Policy.model_validate(full_policy(verdict_ttl_seconds=ttl))
    for ttl in (0, VERDICT_TTL_MAX_SECONDS + 1, True):
        with pytest.raises(ValidationError):
            Policy.model_validate(full_policy(verdict_ttl_seconds=ttl))


def test_repo_length_limit_is_512() -> None:
    host = "github.com/acme/"
    ok = host + "a" * (512 - len(host))
    assert VerdictPayload.model_validate(verdict_dict(repo=ok)).repo == ok
    with pytest.raises(ValidationError):
        VerdictPayload.model_validate(verdict_dict(repo=ok + "a"))


def test_parse_canonical_error_codes() -> None:
    from nuramesh.core.canonical import canonical_json

    good = canonical_json(verdict_dict())
    assert parse_canonical(good, VerdictPayload).pr == 7
    with pytest.raises(FormatError) as c1:
        parse_canonical(b"\xff", VerdictPayload)
    assert c1.value.code == "not_json"
    with pytest.raises(FormatError) as c2:
        parse_canonical(b"{bad", VerdictPayload)
    assert c2.value.code == "not_json"
    with pytest.raises(FormatError) as c3:
        parse_canonical(b"[1]", VerdictPayload)
    assert c3.value.code == "not_object"
    with pytest.raises(FormatError) as c4:
        parse_canonical(
            canonical_json({**verdict_dict(), "version": "vm-verdict-9"}), VerdictPayload
        )
    assert c4.value.code == "unsupported_version"
    with pytest.raises(FormatError) as c5:
        parse_canonical(good.replace(b'"pr":7', b'"pr": 7'), VerdictPayload)
    assert c5.value.code == "not_canonical"
    with pytest.raises(FormatError) as c6:
        parse_canonical(canonical_json(verdict_dict(pr="7")), VerdictPayload)
    assert c6.value.code == "schema"
    with pytest.raises(FormatError) as c7:
        parse_canonical(b'{"a":1.5}', VerdictPayload)
    assert c7.value.code == "not_canonical_number"
    with pytest.raises(FormatError) as c8:
        parse_canonical(b'{"a":1,"a":2}', VerdictPayload)
    assert c8.value.code == "duplicate_key"


def test_serial_and_counts_are_strict_integers() -> None:
    for bad in ("5", True, 0):
        with pytest.raises(ValidationError):
            TrustList.model_validate(trust_dict(serial=bad))
    assert TrustList.model_validate(trust_dict(serial=1)).serial == 1


def test_a_policy_without_the_optional_key_is_not_canonical_after_validation() -> None:
    from nuramesh.core.canonical import canonical_json

    raw = canonical_json(full_policy())
    with pytest.raises(FormatError) as caught:
        parse_canonical(raw, Policy)
    assert caught.value.code == "not_canonical"


def test_tier_and_pr_are_strict_integers() -> None:
    for bad in ("3", True, 3.0):
        with pytest.raises(ValidationError):
            VerdictPayload.model_validate(verdict_dict(tier=bad))
    for bad in ("7", True, 7.0):
        with pytest.raises(ValidationError):
            VerdictPayload.model_validate(verdict_dict(pr=bad))
