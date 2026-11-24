"""Key rotation, the pure rules (R1.30 PR 5, H-14; spec/r130/anchor-remote-rotation.md R10 to R13).

No Git here: records are built with keys generated when the test runs, the chain walk and the
key-per-anchor rules take their inputs as data."""

import hashlib
import json

import pytest

pytest.importorskip("cryptography")

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nuramesh.core.anchors import (  # noqa: E402
    KEY_ROTATED_KEYS,
    Anchor,
    ChainKey,
    KeyChange,
    Retirement,
    anchor_message,
    check_key_rules,
    key_change_message,
    key_rotated_payload,
    validate_key_rotated_payload,
    walk_key_chain,
)

ROOT_COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
T0 = "2026-11-05T12:00:00.000000Z"
T1 = "2026-11-05T13:00:00.000000Z"
T2 = "2026-11-05T14:00:00.000000Z"


def raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def pub(key: Ed25519PrivateKey) -> str:
    return raw(key).hex()


def kid(key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(raw(key)).hexdigest()[:16]


def chain_key(key: Ed25519PrivateKey) -> ChainKey:
    return ChainKey(kid(key), pub(key))


def verify(public_hex: str, signature_hex: str, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            bytes.fromhex(signature_hex), message
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def derive(public_hex: str) -> str | None:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
    except ValueError:
        return None
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]


def record(
    old: Ed25519PrivateKey,
    new: Ed25519PrivateKey,
    *,
    n: int = 1,
    at: str = T1,
    since: str | None = None,
    repo_root: str = ROOT_COMMIT,
    sign_old: Ed25519PrivateKey | None = None,
    sign_new: Ed25519PrivateKey | None = None,
    new_key_id: str | None = None,
    prev_key_id: str | None = None,
) -> KeyChange:
    fields = {
        "n": n,
        "repo_root": repo_root,
        "prev_key_id": prev_key_id or kid(old),
        "new_key_id": new_key_id or kid(new),
        "new_public_key": pub(new),
        "at": at,
        "compromised_since": since,
    }
    message = key_change_message(**fields)  # type: ignore[arg-type]
    return KeyChange(
        **fields,  # type: ignore[arg-type]
        sig_old=(sign_old or old).sign(message).hex(),
        sig_new=(sign_new or new).sign(message).hex(),
    )


def walk(root: Ed25519PrivateKey, *records: KeyChange, repo_roots: set[str] | None = None):  # type: ignore[no-untyped-def]
    text = "".join(r.line() + "\n" for r in records)
    return walk_key_chain(
        text,
        root=chain_key(root),
        repo_roots=repo_roots or {ROOT_COMMIT},
        verify=verify,
        derive_id=derive,
    )


def reason(chain) -> str | None:  # type: ignore[no-untyped-def]
    return None if chain.problem is None else chain.problem.detail


def anchor(key: Ed25519PrivateKey, seq: int, at: str) -> Anchor:
    message = anchor_message("run:r1", seq, "c" * 64, at, kid(key))
    return Anchor(
        stream="run:r1", seq=seq, head="c" * 64, at=at, key_id=kid(key), sig=key.sign(message).hex()
    )


def test_a_valid_record_extends_the_chain() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = walk(a, record(a, b))
    assert chain.problem is None and [k.key_id for k in chain.keys] == [kid(a), kid(b)]


def test_kr_03_forged_record_with_only_a_valid_new_signature_is_rejected() -> None:
    a, b, attacker = (Ed25519PrivateKey.generate() for _ in range(3))
    chain = walk(a, record(a, b, sign_old=attacker))
    assert "bad_old_signature" in (reason(chain) or "")
    assert [k.key_id for k in chain.keys] == [kid(a)]


def test_kr_04_attacker_key_named_as_previous_is_wrong_prev() -> None:
    a, b, attacker = (Ed25519PrivateKey.generate() for _ in range(3))
    chain = walk(a, record(attacker, b))
    assert "wrong_prev" in (reason(chain) or "") and len(chain.keys) == 1


def test_kr_05_new_key_id_must_match_the_public_key() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = walk(a, record(a, b, new_key_id="0" * 16))
    assert "id_mismatch" in (reason(chain) or "")


def test_kr_06_new_key_must_sign_too() -> None:
    a, b, third = (Ed25519PrivateKey.generate() for _ in range(3))
    chain = walk(a, record(a, b, sign_new=third))
    assert "bad_new_signature" in (reason(chain) or "")


def test_kr_07_a_forged_second_record_stops_the_chain_at_the_first() -> None:
    a, b, c, attacker = (Ed25519PrivateKey.generate() for _ in range(4))
    chain = walk(a, record(a, b), record(b, c, n=2, at=T2, sign_old=attacker))
    assert [k.key_id for k in chain.keys] == [kid(a), kid(b)]
    assert "record 2" in (reason(chain) or "") and "bad_old_signature" in (reason(chain) or "")
    # an anchor by the forged key is not in the chain: the engine reports it as wrong_key
    assert kid(c) not in {k.key_id for k in chain.keys}


def test_records_after_the_first_invalid_one_are_not_trusted() -> None:
    a, b, c, d = (Ed25519PrivateKey.generate() for _ in range(4))
    chain = walk(a, record(a, b, n=2), record(b, c, n=2), record(c, d, n=3))
    assert len(chain.keys) == 1 and "record 1" in (reason(chain) or "")


def test_kr_08_rotating_back_to_a_retired_key_is_refused() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = walk(a, record(a, b), record(b, a, n=2, at=T2))
    assert "key_reused" in (reason(chain) or "") and len(chain.keys) == 2


def test_a_record_may_not_be_dated_before_the_previous_one() -> None:
    a, b, c = (Ed25519PrivateKey.generate() for _ in range(3))
    chain = walk(a, record(a, b, at=T2), record(b, c, n=2, at=T1))
    assert "out_of_order" in (reason(chain) or "")


def test_kr_13_a_trusted_root_that_is_not_the_first_key_trusts_no_record() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = walk(b, record(a, b))
    assert "wrong_prev" in (reason(chain) or "")
    assert [k.key_id for k in chain.keys] == [kid(b)]


def test_kr_13b_root_first_then_extra_key_has_no_repository_problem() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    assert walk(a, record(a, b)).problem is None


def test_kr_19_signatures_do_not_cross_between_anchors_and_key_changes() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    good = record(a, b)
    # the key change's bytes carry a domain prefix; an anchor signature over any anchor message
    # (or plain canonical JSON without the prefix) is not a key-change signature
    plain = key_change_message(
        n=1,
        repo_root=ROOT_COMMIT,
        prev_key_id=kid(a),
        new_key_id=kid(b),
        new_public_key=pub(b),
        at=T1,
        compromised_since=None,
    )
    assert plain.startswith(b"nuramesh-anchor-key-change/1\n")
    without_prefix = plain.removeprefix(b"nuramesh-anchor-key-change/1\n")
    replayed = good.model_copy(
        update={"sig_old": a.sign(without_prefix).hex(), "sig_new": b.sign(without_prefix).hex()}
    )
    assert "bad_old_signature" in (reason(walk(a, replayed)) or "")
    # and a key-change signature is no anchor signature
    anchor_bytes = anchor_message("run:r1", 1, "c" * 64, T1, kid(a))
    assert not verify(pub(a), good.sig_old, anchor_bytes)


def test_kr_30_a_record_for_another_repository_is_wrong_repository() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    chain = walk(a, record(a, b, repo_root=OTHER_COMMIT))
    assert "wrong_repository" in (reason(chain) or "")


def test_kr_31_compromised_since_after_the_record_is_invalid() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    assert "bad_compromised_since" in (reason(walk(a, record(a, b, since=T2))) or "")
    assert walk(a, record(a, b, since=T0)).problem is None  # earlier than the record: allowed
    assert walk(a, record(a, b, since=T1)).problem is None  # equal to it: allowed


def test_unreadable_lines_are_reported_as_unreadable() -> None:
    a = Ed25519PrivateKey.generate()
    for text in ("\n", "not json\n", json.dumps({"v": 1}) + "\n", "x" * 5000 + "\n"):
        chain = walk_key_chain(
            text, root=chain_key(a), repo_roots={ROOT_COMMIT}, verify=verify, derive_id=derive
        )
        assert "unreadable" in (reason(chain) or "")


def test_an_empty_key_file_is_a_chain_of_one() -> None:
    a = Ed25519PrivateKey.generate()
    chain = walk_key_chain(
        "", root=chain_key(a), repo_roots={ROOT_COMMIT}, verify=verify, derive_id=derive
    )
    assert chain.problem is None and len(chain.keys) == 1


def test_a_record_with_an_extra_field_is_unreadable() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    data = json.loads(record(a, b).line())
    data["extra"] = 1
    chain = walk_key_chain(
        json.dumps(data) + "\n",
        root=chain_key(a),
        repo_roots={ROOT_COMMIT},
        verify=verify,
        derive_id=derive,
    )
    assert "unreadable" in (reason(chain) or "")


# -- which key may sign which anchor (R13) -------------------------------------------------------


def test_kr_02_anchors_before_and_after_a_rotation_both_verify() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    anchors = [
        anchor(a, 1, "2026-11-05T10:00:00.000000Z"),
        anchor(a, 2, "2026-11-05T10:30:00.000000Z"),
        anchor(b, 3, "2026-11-05T13:30:00.000000Z"),
        anchor(b, 4, "2026-11-05T13:45:00.000000Z"),
    ]
    problems, rejected = check_key_rules(
        anchors, {kid(a): 0, kid(b): 1}, {kid(a): Retirement(lines=2, at=T1)}
    )
    assert problems == [] and rejected == set()


def test_kr_10_an_old_key_anchor_dated_after_the_change_is_refused() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    anchors = [anchor(a, 1, "2026-11-05T13:30:00.000000Z")]
    problems, rejected = check_key_rules(
        anchors, {kid(a): 0, kid(b): 1}, {kid(a): Retirement(lines=1, at=T1)}
    )
    assert [p.code for p in problems] == ["anchor_after_key_change"] and rejected == {0}


def test_kr_11_compromised_since_rejects_later_anchors_only() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    since = "2026-11-05T10:15:00.000000Z"
    anchors = [
        anchor(a, 1, "2026-11-05T10:00:00.000000Z"),
        anchor(a, 2, "2026-11-05T10:20:00.000000Z"),
    ]
    problems, rejected = check_key_rules(
        anchors,
        {kid(a): 0, kid(b): 1},
        {kid(a): Retirement(lines=2, at=T1, compromised_since=since)},
    )
    assert [p.code for p in problems] == ["anchor_from_compromised_key"] and rejected == {1}


def test_kr_12_an_old_key_anchor_after_a_new_key_anchor_is_regressed() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    anchors = [
        anchor(a, 1, "2026-11-05T10:00:00.000000Z"),
        anchor(b, 2, "2026-11-05T13:30:00.000000Z"),
        anchor(a, 3, "2026-11-05T10:30:00.000000Z"),
    ]
    problems, rejected = check_key_rules(
        anchors, {kid(a): 0, kid(b): 1}, {kid(a): Retirement(lines=3, at=T1)}
    )
    assert "anchor_key_regressed" in [p.code for p in problems] and 2 in rejected


def test_a_retired_key_line_beyond_the_allowed_prefix_is_refused_whatever_its_time() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    anchors = [
        anchor(a, 1, "2026-11-05T10:00:00.000000Z"),
        anchor(a, 2, "2026-11-05T10:10:00.000000Z"),
    ]
    problems, rejected = check_key_rules(
        anchors, {kid(a): 0, kid(b): 1}, {kid(a): Retirement(lines=1, at=T1)}
    )
    assert [p.code for p in problems] == ["anchor_after_key_change"] and rejected == {1}


def test_a_bounded_tail_has_no_time_rules_only_the_prefix() -> None:
    a, c = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    anchors = [
        anchor(a, 1, "2099-01-01T00:00:00.000000Z"),
        anchor(a, 2, "2099-01-02T00:00:00.000000Z"),
    ]
    problems, rejected = check_key_rules(
        anchors, {kid(a): 0, kid(c): 1}, {kid(a): Retirement(lines=1)}
    )
    assert rejected == {1} and [p.code for p in problems] == ["anchor_after_key_change"]


def test_unknown_and_skipped_anchors_are_left_to_the_signature_checks() -> None:
    a, stranger = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    anchors = [anchor(stranger, 1, T0), anchor(a, 2, T0)]
    assert check_key_rules(anchors, {kid(a): 0}, {}) == ([], set())
    assert check_key_rules(anchors, {kid(a): 0, kid(stranger): 1}, {}, skip={0}) == ([], set())


# -- the ledger event payload (KR-18, closed key set) --------------------------------------------


def test_kr_18_the_rotation_event_payload_is_the_closed_set_of_facts() -> None:
    chained = key_rotated_payload(
        prev_key_id="a" * 16,
        new_key_id="b" * 16,
        chained=True,
        n=1,
        record_sha256="c" * 64,
        compromised_since=None,
        commit="d" * 40,
    )
    assert set(chained) == KEY_ROTATED_KEYS and len(KEY_ROTATED_KEYS) == 7
    lost = key_rotated_payload(
        prev_key_id="a" * 16,
        new_key_id="b" * 16,
        chained=False,
        n=None,
        record_sha256=None,
        compromised_since=T0,
        commit="d" * 40,
    )
    assert lost["n"] is None and lost["record_sha256"] is None and lost["compromised_since"] == T0


@pytest.mark.parametrize(
    "change",
    [
        {"path": "/home/x"},  # an extra key
        {"chained": "yes"},
        {"n": None},  # chained needs n
        {"commit": "not-an-id"},
        {"prev_key_id": "short"},
        {"compromised_since": "yesterday"},
    ],
)
def test_kr_18_a_payload_outside_the_closed_set_or_types_is_refused(
    change: dict[str, object],
) -> None:
    good = key_rotated_payload(
        prev_key_id="a" * 16,
        new_key_id="b" * 16,
        chained=True,
        n=1,
        record_sha256="c" * 64,
        compromised_since=None,
        commit="d" * 40,
    )
    with pytest.raises(ValueError):
        validate_key_rotated_payload({**good, **change})


def test_a_not_chained_payload_cannot_carry_a_record_hash() -> None:
    with pytest.raises(ValueError):
        key_rotated_payload(
            prev_key_id="a" * 16,
            new_key_id="b" * 16,
            chained=False,
            n=None,
            record_sha256="c" * 64,
            compromised_since=None,
            commit="d" * 40,
        )


def test_a_rejected_regressed_anchor_does_not_lower_the_rank_floor() -> None:
    a, c = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    anchors = [
        anchor(c, 1, "2026-11-05T10:00:00.000000Z"),
        anchor(a, 2, "2026-11-05T10:01:00.000000Z"),
        anchor(a, 3, "2026-11-05T10:02:00.000000Z"),
    ]
    problems, rejected = check_key_rules(anchors, {kid(a): 0, kid(c): 1}, {})
    assert [p.code for p in problems] == ["anchor_key_regressed"] * 2
    assert rejected == {1, 2}
