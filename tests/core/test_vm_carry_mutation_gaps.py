"""Mutation pins for `nuramesh.core.vm_carry` (issue #1334): exact results and the parent-order
branches of the carry-forward walk. Fake commit ids only, derived from names at test time."""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Sequence

import pytest

from nuramesh.core.vm_carry import BrResult, Commit, carry_forward

X = "c" * 64
P = "e" * 64


def sha(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()  # noqa: S324 (a fake commit id)


def clean(p1: str, p2: str) -> str:
    return hashlib.sha1(("merge:" + "".join(sorted((p1, p2)))).encode()).hexdigest()  # noqa: S324


M1, M2, M3 = sha("M1"), sha("M2"), sha("M3")
H1, H2, H3 = sha("H1"), sha("H2"), sha("H3")
BASE = {M1, M2, M3}


def run(chain: Sequence[Commit], record_head: str, live_head: str) -> BrResult:
    truth = {c.sha: (c.parents, c.tree) for c in chain}
    return carry_forward(
        chain,
        record_head=record_head,
        live_head=live_head,
        read_commit=lambda s: truth.get(s),
        in_base_history=lambda s: s in BASE,
        merge_tree=clean,
        record_change_hash=X,
        live_change_hash=X,
        record_policy_sha256=P,
        live_policy_sha256=P,
    )


def test_live_head_missing_from_the_chain_is_unknown_commit_naming_it() -> None:
    chain = [Commit(H1, (M1,), "t1")]
    assert run(chain, H1, H2) == BrResult(False, "unknown_commit", H2)


def test_a_carried_merge_returns_ok_with_no_breaking_commit() -> None:
    chain = [Commit(H1, (M1,), "t1"), Commit(H2, (H1, M2), clean(H1, M2))]
    assert run(chain, H1, H2) == BrResult(True, "ok", None)
    chain.append(Commit(H3, (H2, M3), clean(H2, M3)))
    assert run(chain, H1, H3) == BrResult(True, "ok", None)


def test_the_record_head_may_be_the_second_parent_of_the_merge() -> None:
    chain = [Commit(H1, (M1,), "t1"), Commit(H2, (M2, H1), clean(M2, H1))]
    assert run(chain, H1, H2) == BrResult(True, "ok", None)


def test_a_merge_of_two_base_commits_that_is_not_in_base_does_not_carry() -> None:
    feature_merge = sha("feature-merge")
    chain = [
        Commit(H1, (M1,), "t1"),
        Commit(feature_merge, (M1, M2), clean(M1, M2)),
        Commit(H2, (H1, feature_merge), clean(H1, feature_merge)),
    ]
    assert run(chain, H1, H2) == BrResult(False, "merge_not_of_base", feature_merge)


def test_a_merge_of_two_chain_commits_with_no_base_side_does_not_carry() -> None:
    """Both parents are on the path (record head and a chain commit), neither is in base."""
    side = sha("side")
    chain = [
        Commit(H1, (M1,), "t1"),
        Commit(side, (H1, M2), clean(H1, M2)),
        Commit(H2, (side, H1), clean(side, H1)),
    ]
    assert run(chain, H1, H2) == BrResult(False, "merge_not_of_base", H2)


def test_the_result_and_the_commit_records_are_immutable() -> None:
    result = BrResult(True, "ok", None)
    commit = Commit(H1, (M1,), "t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        commit.tree = "other"  # type: ignore[misc]
    assert commit.merge_tree is None
