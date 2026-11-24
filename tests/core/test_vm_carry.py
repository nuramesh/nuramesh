"""Merge Gate carry-forward rule BR (spec GT-15 a-d, EB-3 commit chain, section 4 "clean
merge" and "mechanical rebase"). Pure graph code over the bundle's commit chain; git merge-tree
runs are supplied by the caller as callables (the `mt-1` runner lives in the engine).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Mapping, Sequence

from nuramesh.core.vm_carry import BrResult, Commit, carry_forward, mechanical_rebase

X = "c" * 64
P = "e" * 64


def sha(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()  # noqa: S324 (a fake commit id)


def tree(name: str) -> str:
    return hashlib.sha1(("tree:" + name).encode()).hexdigest()  # noqa: S324


def clean_tree(p1: str, p2: str) -> str:
    """The tree a clean merge of two commits would have (a fixed, symmetric function)."""
    return hashlib.sha1(("merge:" + "".join(sorted((p1, p2)))).encode()).hexdigest()  # noqa: S324


M1, M2, M3 = sha("M1"), sha("M2"), sha("M3")
BASE_HISTORY = {M1, M2, M3}


def in_base(commit: str) -> bool:
    return commit in BASE_HISTORY


def merge_tree(p1: str, p2: str) -> str | None:
    return clean_tree(p1, p2)


def br(
    chain: list[Commit],
    record_head: str,
    live_head: str,
    *,
    live_change: str = X,
    record_change: str = X,
    live_policy: str = P,
    record_policy: str = P,
    mt: Callable[[str, str], str | None] = merge_tree,
    git: Mapping[str, tuple[Sequence[str], str]] | None = None,
) -> BrResult:
    """`git` is what the repository says per commit (parents, tree); by default it agrees with
    the bundle's chain."""
    truth = git if git is not None else {c.sha: (c.parents, c.tree) for c in chain}
    return carry_forward(
        chain,
        read_commit=lambda s: truth.get(s),
        record_head=record_head,
        live_head=live_head,
        in_base_history=in_base,
        merge_tree=mt,
        record_change_hash=record_change,
        live_change_hash=live_change,
        record_policy_sha256=record_policy,
        live_policy_sha256=live_policy,
    )


H1 = sha("H1")
C1 = Commit(H1, (M1,), tree("H1"))


def test_same_head_carries() -> None:
    assert br([C1], H1, H1) == BrResult(True, "ok", None)


def test_c01_clean_merge_of_main_carries() -> None:
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, M2), clean_tree(H1, M2))]
    assert br(chain, H1, h2).ok


def test_c03_a_new_non_merge_commit_breaks_br() -> None:
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1,), tree("H2"))]
    assert br(chain, H1, h2) == BrResult(False, "non_merge_commit", h2)


def test_c09_evil_merge_tree_differs_from_the_recomputed_clean_merge() -> None:
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, M2), tree("hand-made"))]
    assert br(chain, H1, h2) == BrResult(False, "not_clean_merge", h2)


def test_c11_conflicting_merge_resolved_by_hand_is_not_clean() -> None:
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, M2), clean_tree(H1, M2))]

    def conflict(p1: str, p2: str) -> str | None:
        return None

    assert br(chain, H1, h2, mt=conflict) == BrResult(False, "not_clean_merge", h2)


def test_c07_policy_change_and_c04_change_hash_fail_before_the_graph_walk() -> None:
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, M2), clean_tree(H1, M2))]
    assert br(chain, H1, h2, live_policy="f" * 64) == BrResult(False, "policy", None)
    assert br(chain, H1, h2, live_change="f" * 64) == BrResult(False, "change_hash", None)


def test_record_head_must_be_an_ancestor_of_the_live_head() -> None:
    """G-25b: a rebase puts the branch on new commits; H0 is no ancestor of H1 (rebinding, not
    BR, is the path then)."""
    h0, h1 = sha("H0"), sha("H1r")
    chain = [Commit(h1, (M2,), tree("H1r"))]
    assert br(chain, h0, h1) == BrResult(False, "not_ancestor", None)


def test_merge_of_a_non_base_commit_does_not_carry() -> None:
    f = sha("feature")
    h2 = sha("H2")
    chain = [C1, Commit(f, (M1,), tree("f")), Commit(h2, (H1, f), clean_tree(H1, f))]
    result = br(chain, H1, h2)
    assert not result.ok and result.reason in {"merge_not_of_base", "non_merge_commit"}


def test_a_parent_missing_from_the_chain_fails_closed() -> None:
    ghost = sha("ghost")
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, ghost), clean_tree(H1, ghost))]
    assert br(chain, H1, h2) == BrResult(False, "unknown_commit", h2)


def test_octopus_and_three_parent_merges_do_not_carry() -> None:
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, M2, M3), clean_tree(H1, M2))]
    assert br(chain, H1, h2) == BrResult(False, "non_merge_commit", h2)


def test_two_clean_merges_in_a_row_carry_and_c06_an_old_head_two_back_does_not() -> None:
    h2, h3 = sha("H2"), sha("H3")
    chain = [
        C1,
        Commit(h2, (H1, M2), clean_tree(H1, M2)),
        Commit(h3, (h2, M3), clean_tree(h2, M3)),
    ]
    assert br(chain, H1, h3).ok
    h0 = sha("H0")
    chain_c06 = [Commit(h0, (M1,), tree("H0")), Commit(H1, (h0,), tree("H1")), *chain[1:]]
    assert br(chain_c06, h0, h3) == BrResult(False, "non_merge_commit", H1)


def test_the_bundles_recorded_merge_tree_is_never_trusted() -> None:
    """GT-15 (b): `tree` and `merge_tree` in the bundle record what the collector saw; the
    evaluator asks the supplied merge-tree callable, never the record."""
    h2 = sha("H2")
    calls: list[tuple[str, str]] = []

    def spy(p1: str, p2: str) -> str | None:
        calls.append((p1, p2))
        return clean_tree(p1, p2)

    chain = [C1, Commit(h2, (H1, M2), clean_tree(H1, M2), merge_tree=tree("claimed"))]
    assert br(chain, H1, h2, mt=spy).ok
    assert calls == [(H1, M2)]


def test_the_bundles_tree_is_never_trusted_a_lying_tree_is_not_clean() -> None:
    """GT-15 (b): the bundle claims the clean-merge tree for an evil merge; git's tree wins."""
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, M2), clean_tree(H1, M2))]  # the bundle lies
    git = {H1: ((M1,), tree("H1")), h2: ((H1, M2), tree("hand-made"))}
    assert br(chain, H1, h2, git=git) == BrResult(False, "not_clean_merge", h2)


def test_a_commit_git_does_not_know_or_with_other_parents_fails_closed() -> None:
    h2 = sha("H2")
    chain = [C1, Commit(h2, (H1, M2), clean_tree(H1, M2))]
    assert br(chain, H1, h2, git={}) == BrResult(False, "unknown_commit", h2)
    other = {h2: ((H1, M3), clean_tree(H1, M2))}
    assert br(chain, H1, h2, git=other) == BrResult(False, "unknown_commit", h2)


def test_mechanical_rebase_predicate() -> None:
    mb0, n, h0 = sha("mb0"), sha("N"), sha("H0")
    expected = hashlib.sha1(b"rebased").hexdigest()  # noqa: S324

    def mt(base: str, a: str, b: str) -> str | None:
        return expected if (base, a, b) == (mb0, n, h0) else None

    assert mechanical_rebase(expected, mb0, n, h0, mt) == (True, "ok")
    assert mechanical_rebase(tree("other"), mb0, n, h0, mt) == (False, "tree_mismatch")
    assert mechanical_rebase(expected, mb0, n, sha("Z"), mt) == (False, "conflict")


def test_property_random_commit_graphs() -> None:
    """Random chains: PR commits, clean merges of main, and sometimes one evil merge or one
    non-merge commit after the record head. BR holds iff nothing but clean merges of base
    commits sits between the record head and the live head."""
    rng = random.Random(1110)
    for case in range(300):
        base = [sha(f"m{case}-{i}") for i in range(rng.randint(1, 5))]
        BASE_HISTORY.update(base)
        chain: list[Commit] = []
        prev = base[0]
        for i in range(rng.randint(1, 4)):
            commit = sha(f"pr{case}-{i}")
            chain.append(Commit(commit, (prev,), tree(commit)))
            prev = commit
        record_head = prev
        expect_ok = True
        breaker: str | None = None
        for i in range(rng.randint(0, 4)):
            kind = rng.choice(["clean", "clean", "clean", "evil", "commit", "conflict"])
            commit = sha(f"after{case}-{i}")
            main_commit = rng.choice(base)
            if kind == "clean":
                chain.append(Commit(commit, (prev, main_commit), clean_tree(prev, main_commit)))
            elif kind == "evil":
                chain.append(Commit(commit, (prev, main_commit), tree(commit)))
            elif kind == "conflict":
                chain.append(Commit(commit, (prev, main_commit), clean_tree(prev, main_commit)))
            else:
                chain.append(Commit(commit, (prev,), tree(commit)))
            if kind != "clean" and expect_ok:
                expect_ok, breaker = False, commit
            prev = commit
        live_head = prev
        conflicting = {c.sha for c in chain if c.sha == breaker and len(c.parents) == 2}

        def mt(
            p1: str, p2: str, _bad: set[str] = conflicting, _chain: list[Commit] = chain
        ) -> str | None:
            for c in _chain:
                if c.parents == (p1, p2) and c.sha in _bad and c.tree == clean_tree(p1, p2):
                    return None  # the "conflict" kind: git reports a conflict
            return clean_tree(p1, p2)

        rng.shuffle(chain)  # order in the bundle must not matter
        result = br(chain, record_head, live_head, mt=mt)
        assert result.ok == expect_ok, (case, result, expect_ok, breaker)
        if not expect_ok:
            assert result.commit == breaker
