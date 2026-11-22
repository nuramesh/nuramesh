"""Verified Merge carry-forward rule BR (spec `spec/verified-merge` GT-15, EB-3, section 4).

A signed record (verdict or approval) bound to head H1 also counts for the live head H2 iff
(a) H1 is an ancestor of H2 (or equal); (b) every commit in `rev-list H2 ^H1 ^<base tip>` is a
merge commit with exactly two parents whose other parent is already in the protected branch's
history **and is a clean merge**: its tree, read from the repository with `read_commit`, equals the
pinned `git merge-tree --write-tree p1 p2` with no conflicts, recomputed by the caller's git (the
bundle's `tree`, `parents` and `merge_tree` fields record what the collector saw and are never
trusted: git's parents must equal the bundle's, and git's tree is the one compared); (c) the
live change hash equals the record's; (d) the record's `policy_sha256` equals the live one. A
merge the author edited or hand-resolved (an "evil merge", T-31) breaks carry-forward even
when the change hash is equal, because the normalised change hash ignores where lines sit.

This module is pure graph code over the bundle's commit chain (EB-3: `merge-base..head` with
parents and trees). Git runs stay in the engine (the `mt-1` pinned runner) and are supplied as
callables: `read_commit(sha)` (git's `(parents, tree)` of a commit, or None),
`in_base_history(sha)` (is the commit in the protected branch's history) and
`merge_tree(p1, p2)` (the tree of the pinned clean merge, or None on conflict). Anything the
chain cannot explain fails closed (`unknown_commit`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

Reason = str
"""`ok`, `change_hash`, `policy`, `not_ancestor`, `unknown_commit`, `non_merge_commit`,
`merge_not_of_base`, `not_clean_merge`."""


@dataclass(frozen=True)
class Commit:
    """One commit of the bundle's chain (EB-3)."""

    sha: str
    parents: tuple[str, ...]
    tree: str
    """What the collector saw; informational, never used here (GT-15 b)."""
    merge_tree: str | None = None
    """What the collector computed; informational, never used here (GT-15 b)."""


@dataclass(frozen=True)
class BrResult:
    ok: bool
    reason: Reason
    commit: str | None
    """The first commit (oldest first) that broke the rule, when one did."""


def carry_forward(
    chain: Sequence[Commit],
    *,
    record_head: str,
    live_head: str,
    read_commit: Callable[[str], tuple[Sequence[str], str] | None],
    in_base_history: Callable[[str], bool],
    merge_tree: Callable[[str, str], str | None],
    record_change_hash: str,
    live_change_hash: str,
    record_policy_sha256: str,
    live_policy_sha256: str,
) -> BrResult:
    """BR (a) to (d) for a record bound to `record_head` against `live_head`."""
    if record_change_hash != live_change_hash:
        return BrResult(False, "change_hash", None)
    if record_policy_sha256 != live_policy_sha256:
        return BrResult(False, "policy", None)
    if record_head == live_head:
        return BrResult(True, "ok", None)
    by_sha = {commit.sha: commit for commit in chain}
    if live_head not in by_sha:
        return BrResult(False, "unknown_commit", live_head)
    # Walk from the live head towards the record head; the set S collects the commits that are
    # in the live head and neither in the record head nor in the base's history.
    between: dict[str, Commit] = {}
    found_record_head = False
    stack = [live_head]
    while stack:
        sha = stack.pop()
        if sha in between:
            continue
        commit = by_sha[sha]
        between[sha] = commit
        for parent in commit.parents:
            if parent == record_head:
                found_record_head = True
            elif parent in between or in_base_history(parent):
                continue
            elif parent in by_sha:
                stack.append(parent)
            else:
                return BrResult(False, "unknown_commit", sha)
    if not found_record_head:
        return BrResult(False, "not_ancestor", None)
    for commit in _oldest_first(between, record_head):
        if len(commit.parents) != 2:
            return BrResult(False, "non_merge_commit", commit.sha)
        p1, p2 = commit.parents
        on_path = (p1 == record_head or p1 in between, p2 == record_head or p2 in between)
        in_base = (in_base_history(p1), in_base_history(p2))
        if not ((on_path[0] and in_base[1]) or (on_path[1] and in_base[0])):
            return BrResult(False, "merge_not_of_base", commit.sha)
        git = read_commit(commit.sha)  # GT-15: git's word, never the bundle's `tree`
        if git is None or tuple(git[0]) != commit.parents:
            return BrResult(False, "unknown_commit", commit.sha)
        if merge_tree(p1, p2) != git[1]:
            return BrResult(False, "not_clean_merge", commit.sha)
    return BrResult(True, "ok", None)


def _oldest_first(between: dict[str, Commit], record_head: str) -> list[Commit]:
    """The commits of S in a topological order (parents before children), so the first failure
    reported is the oldest one after the record head."""
    pending = {sha: sum(p in between for p in c.parents) for sha, c in between.items()}
    children: dict[str, list[str]] = {sha: [] for sha in between}
    for sha, commit in between.items():
        for parent in commit.parents:
            if parent in between:
                children[parent].append(sha)
    ready = sorted(sha for sha, count in pending.items() if count == 0)  # a fixed order
    order: list[Commit] = []
    while ready:
        sha = ready.pop(0)
        order.append(between[sha])
        for child in sorted(children[sha]):
            pending[child] -= 1
            if pending[child] == 0:
                ready.append(child)
    return order


def mechanical_rebase(
    h1_tree: str,
    mb0: str,
    n: str,
    h0: str,
    merge_tree_with_base: Callable[[str, str, str], str | None],
) -> tuple[bool, Reason]:
    """Section 4 (SV-8 d): H1 is a mechanical rebase of H0 iff H1's tree equals the pinned
    `git merge-tree --write-tree --merge-base=<mb0> <N> <H0>` with no conflict, where `mb0` is
    the merge base recorded in H0's bundle and `N` = `merge-base(H1, base tip)`. The signer
    runs the git itself; this is the comparison. Reasons: `ok`, `conflict`, `tree_mismatch`."""
    result = merge_tree_with_base(mb0, n, h0)
    if result is None:
        return False, "conflict"
    if result != h1_tree:
        return False, "tree_mismatch"
    return True, "ok"
