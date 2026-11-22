"""Verified Merge collector (spec `spec/verified-merge` EB-1 to EB-9, 6.1, 6.4a, LA-1;
`tiers.md` TL-3, TL-13, TL-19, TL-20, TL-24, TL-29 to TL-32).

Pure functions from facts to one evidence bundle (`vm-evidence-1`) for one (repo, PR, head). The
service gathers the facts (the forge's PR, push events and check runs; the engine's git runs under
the pinned diff and `mt-1` runs; the engine-written run links in the ledger) and passes them in as
`CollectFacts`; nothing here talks to the forge, git or the ledger. The one read this module
drives is `read_base_inputs`: the policy and the tier list, by 40-hex commit sha at the base tip,
through a host port.

What the collector guarantees:

* **Facts only, no text** (EB-6): the diff is hashed (`change_hash`, `diff_hash`) and its paths
  are listed; no line of it, no verdict text, no prompt reaches the bundle. Tier reasons are codes.
* **Policy inputs from the base tip only** (EB-2, TL-3, TL-19): the policy, the tier list and the
  CODEOWNERS hash each carry the commit they were read at, and it must be the bundle's `base_sha`.
* **Opaque identities** (EB-3): a forge user is an enrolled `human:`/`agent:` id, or `unmapped:`
  plus the SHA-256 of the forge's numeric user id, or `unmapped:unavailable` when the push event
  is gone. Never a login or an email.
* **The author is human only on positive evidence** (EB-9): an agent needs an engine-written run
  link whose agent identity is the commit's pusher; a human needs no run link at all and one
  enrolled human pushing every commit that is not a clean merge of the protected branch. Anything
  else is `unknown`.
* **The tier comes from the tier list on the protected branch** (EB-4, TL-14): one tier function
  (`vm_tiers.tier_of`) for the collector and for the gate's own recomputation (`recompute`,
  TL-24). An invalid list or an invalid `extra_trust_roots` is tier 3 with its code and the
  bundle is still built (TL-15, TL-29).
* **A failure while computing is no bundle and no tier** (TL-32): a failed `diff-1` run, raw
  output that does not parse, a matcher that raises. A missing or invalid policy, a read error
  and a SHA-256 repository are no bundle either (TL-29, TL-9, TL-3).
* **One change-hash pin** (README section 4, #944): the hash is made with `vm_diff.DIFF_PIN`
  (`diff-2`) and the bundle's `pins.diff` records that same constant, here and in `recompute`;
  the gate refuses a bundle of any other pin (`bundle_invalid`, `diff_pin_superseded`).
* **Fail closed with a code**: every refusal is a `CollectError` whose message is the code and
  never input bytes. No bundle is better than a wrong one: the gate then shows `pending
  bundle_missing`.

`record` appends `merge.evidence_recorded` as the collector actor **before** the bundle is stored
(LA-1), so a stored bundle without a ledger entry cannot exist through this path.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from nuramesh.core.canonical import canonical_json
from nuramesh.core.ledger import EventType
from nuramesh.core.people import HUMAN_ID_BODY
from nuramesh.core.vm_diff import (
    DIFF_PIN,
    DiffFormatError,
    FileChange,
    change_document,
    line_text,
    parse_diff,
)
from nuramesh.core.vm_events import COLLECTOR_ACTOR, check_merge_actor
from nuramesh.core.vm_formats import (
    Author,
    CommitEntry,
    EvidenceBundle,
    FormatError,
    Policy,
    canonical_bytes,
    format_time,
    parse_time,
    payload_hash,
    read_hardened_json,
)
from nuramesh.core.vm_tiers import (
    MAX_TIER_LIST_BYTES,
    REGULAR_MODES,
    ChangedPath,
    RawPathsError,
    TierListAt,
    TierListReader,
    _too_deep,  # the TL-9 nesting limit, shared with the tier-list loader (TL-29 (ii))
    load_extra_trust_roots,
    merge_patch_paths,
    parse_raw_paths,
    path_unreadable,
    read_tier_list,
    tier_of,
)

POLICY_PATH = ".nuramesh/policy.json"
TIER3_LABEL = "tier-3"
UNAVAILABLE = "unmapped:unavailable"
OBJECT_FORMAT = "sha1"
REFRESH_NUMERATOR, REFRESH_DENOMINATOR = 3, 4
"""EB-8: the live bundle of an open PR is refreshed at 75% of `bundle_ttl`."""

_SHA = re.compile(r"^[0-9a-f]{40}$")
_USER_ID = re.compile(r"^[0-9]{1,20}$")
_ENROLLED = re.compile(rf"^(human:{HUMAN_ID_BODY}|agent:[A-Za-z0-9._@-]{{1,128}})$")
_CONCLUSIONS = frozenset(
    {
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
    }
)


class CollectError(ValueError):
    """The collector refuses to build or record a bundle. `code` names why; the message is the
    code only and never carries input (a path, a diff line, a forge or file-system message)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"collector: {code}")


# --- what the forge says ------------------------------------------------------------------------


@dataclass(frozen=True)
class PrFacts:
    """What the forge says about one PR, re-read at call time (GT-3, GT-5)."""

    number: int
    state: Literal["open", "closed", "merged"]
    draft: bool
    base_ref: str
    head_sha: str
    head_repo_id: str
    """The repository the head branch lives in; another id than the base's is a fork (GT-5)."""
    labels: frozenset[str]
    open_ask: bool
    """An open decision record against the PR in the ask store (GT-5: `hold_label`)."""
    last_base_change_at: datetime | None
    """When the PR was last retargeted (GT-9), or None when it never was."""


@dataclass(frozen=True)
class LiveCheck:
    """One check run or commit status as the forge lists it now (EB-5)."""

    name: str
    kind: Literal["check_run", "status"]
    producer_id: str
    head_sha: str
    conclusion: str
    run_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    binding: tuple[int, str, str] | None = None
    """The full-tier run's own payload binding (PR, base ref, head sha) as the workflow wrote it
    from its event payload (GT-9), or None when the run carries none."""


# --- the policy and the tier list at the base tip (TL-3, TL-4, TL-29) -----------------------------


def load_policy(raw: bytes | None) -> Policy:
    """TL-29: the policy from the raw blob at `.nuramesh/policy.json` (None: missing). TL-9
    steps 1 to 7 with the tier list's limits (missing, zero bytes, too large, not strict UTF-8,
    not one RFC 8259 value within the nesting limit, a duplicate key in any object, not an
    object), then the schema of README 6.6 (GC-11: every top-level key but `extra_trust_roots`
    is required; that one key is carried as written and judged by the tier function). Anything
    else is `CollectError("policy_invalid")`: there is no policy to take the quorum and the
    required checks from, so no bundle is built and nothing can lower a tier."""
    invalid = CollectError("policy_invalid")
    if raw is None or len(raw) == 0 or len(raw) > MAX_TIER_LIST_BYTES:
        raise invalid
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise invalid from None
    if text.startswith("﻿") or _too_deep(text):
        raise invalid
    try:
        value = read_hardened_json(raw)
        if not isinstance(value, dict):
            raise invalid
        policy = Policy.model_validate(value)
        payload_hash(policy)  # a value that has no canonical form (a lone surrogate) is refused
    except (FormatError, ValidationError, ValueError, TypeError, RecursionError):
        raise invalid from None
    return policy


@dataclass(frozen=True)
class PolicyAt:
    """A policy together with the commit it was read at (EB-2, TL-29): the collector and the
    gate refuse one read anywhere but the protected branch's tip."""

    commit_sha: str
    policy: Policy

    @property
    def sha256(self) -> str:
        """`policy_sha256` (GC-11 (b)): the hash of the fully populated policy object, never of
        the file bytes."""
        return payload_hash(self.policy)


class BaseReader(TierListReader, Protocol):
    """The host port for the base-tip reads (TL-3, TL-4): the git object store by commit sha,
    never a ref name, a working tree, the PR head or the forge's file view; no filter and no
    conversion; a symlink is reported as its own entry and never followed."""

    def policy_entry(self, commit_sha: str) -> tuple[str, bytes] | None:
        """The tree entry at `.nuramesh/policy.json` in that commit as `(mode, raw bytes)`, or
        None when the path does not exist."""


def read_base_inputs(
    reader: BaseReader, base_sha: str, *, object_format: str
) -> tuple[PolicyAt, TierListAt]:
    """The policy and the tier list as they are in the commit `base_sha`, the policy first
    (TL-29 (i)). A repository that is not SHA-1 is refused before anything is read (TL-3:
    `repo_object_format_unsupported`); a missing or invalid policy is `policy_invalid`; a reader
    that raises is `policy_read_error` or `tier_list_read_error` (TL-9: a read error is never a
    cause code and never a hash). An invalid tier list is not a refusal: it is tier 3 with its
    cause code (TL-15)."""
    if object_format != OBJECT_FORMAT:
        raise CollectError("repo_object_format_unsupported")
    if not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha):
        raise CollectError("inputs_not_from_base_tip")
    try:
        entry = reader.policy_entry(base_sha)
    except Exception:
        raise CollectError("policy_read_error") from None
    raw: bytes | None = None
    if entry is not None and entry[0] in REGULAR_MODES:
        raw = entry[1]
    policy = PolicyAt(base_sha, load_policy(raw))
    try:
        tier_list = read_tier_list(reader, base_sha)
    except Exception:
        raise CollectError("tier_list_read_error") from None
    return policy, tier_list


# --- the change: one computation for the collector and the gate (TL-13, TL-24, TL-30, TL-32) ------


@dataclass(frozen=True)
class _Change:
    change_hash: str
    paths: tuple[ChangedPath, ...]
    tier: int
    codes: tuple[str, ...]


def _same_change(files: Sequence[FileChange], changed: Sequence[ChangedPath]) -> None:
    """The raw path set and the hashed patch must describe one change: the same paths with the
    same modes (the tier and the change hash are facts about one diff). The patch form splits a
    type change into a delete and an add, merged here (TL-30)."""
    try:
        merged = merge_patch_paths([ChangedPath(f.path, f.old_mode, f.new_mode) for f in files])
    except RawPathsError:
        raise CollectError("paths_mismatch") from None
    from_patch = sorted((e.path, e.old_mode or "", e.new_mode or "") for e in merged)
    from_raw = sorted(
        (line_text(e.path.encode("utf-8", "surrogateescape")), e.old_mode or "", e.new_mode or "")
        for e in changed
    )
    if from_patch != from_raw:
        raise CollectError("paths_mismatch")


def _change(
    diff: bytes | None,
    raw_paths: bytes | None,
    tier_list: TierListAt,
    policy: Policy,
    *,
    labels: frozenset[str],
    earlier_tier3_bundle: bool,
) -> _Change:
    """The change hash (under the writer pin `DIFF_PIN`, `diff-2`: README section 4, #944), the
    path set and the tier of one pinned diff run (both forms; the git run is the one `tiers.md`
    TL-13 names `diff-1`, which `diff-2` keeps). None for either form means the run failed or
    timed out. TL-32: a failure here is never a tier."""
    if diff is None or raw_paths is None:
        raise CollectError("tier_compute_error")
    try:
        files = parse_diff(diff)
    except DiffFormatError:
        raise CollectError("diff_invalid") from None
    try:
        changed = parse_raw_paths(raw_paths)
    except RawPathsError:
        raise CollectError("tier_compute_error") from None
    _same_change(files, changed)
    try:
        tier, codes = tier_of(
            changed,
            tier_list.tier_list,
            label=TIER3_LABEL in labels,
            sticky=earlier_tier3_bundle,
            extra_trust_roots=load_extra_trust_roots(policy.extra_trust_roots),
        )
    except Exception:
        raise CollectError("tier_compute_error") from None
    # README section 4 (#944): the one writer pin; `collect` records the same constant in `pins`
    digest = hashlib.sha256(canonical_json(change_document(files, DIFF_PIN))).hexdigest()
    return _Change(digest, changed, tier, codes)


def _base_tip(base_sha: str, object_format: str, *read_at: str) -> None:
    if object_format != OBJECT_FORMAT:
        raise CollectError("repo_object_format_unsupported")
    if any(commit != base_sha for commit in read_at):
        raise CollectError("inputs_not_from_base_tip")


# --- identities (EB-3) ----------------------------------------------------------------------------


def map_identity(forge_user_id: str | None, enrollment: Mapping[str, str]) -> str:
    """The opaque id of a forge user: the org's enrolled `human:`/`agent:` id, else `unmapped:`
    plus the SHA-256 of the forge's numeric user id; `unmapped:unavailable` when the forge gave
    none (the push event is gone). A non-numeric id (a login) is refused."""
    if forge_user_id is None:
        return UNAVAILABLE
    if not _USER_ID.fullmatch(forge_user_id):
        raise CollectError("identity_invalid")
    mapped = enrollment.get(forge_user_id)
    if mapped is None:
        return "unmapped:" + hashlib.sha256(forge_user_id.encode("ascii")).hexdigest()
    if not _ENROLLED.fullmatch(mapped):
        raise CollectError("identity_invalid")
    return mapped


# --- the author (EB-9) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RunLink:
    """An engine-written ledger entry linking a run to commits (never a commit trailer, a commit
    message or PR text), with the run's agent identity and the model family of its recorded
    model calls (None when they disagree or are unknown)."""

    run_ref: str
    agent_id: str
    model_family: str | None
    commits: frozenset[str]


_UNKNOWN = Author(kind="unknown", id=None, model_family=None, source="unknown", run_ref=None)


def _exempt(entry: CommitEntry, in_base_history: Callable[[str], bool]) -> bool:
    """BR-exempt (GT-15 b): a two-parent merge with a parent in the protected branch's history
    whose tree is the clean merge the collector's own `mt-1` run printed. An edited merge
    carries its pusher's own lines, so it is not exempt."""
    return (
        len(entry.parents) == 2
        and any(in_base_history(parent) for parent in entry.parents)
        and entry.merge_tree is not None
        and entry.merge_tree == entry.tree
    )


def author_of(
    commits: Sequence[CommitEntry],
    links: Sequence[RunLink],
    in_base_history: Callable[[str], bool],
) -> Author:
    """EB-9 over the commits that are not BR-exempt. `agent`: every such commit is covered by a
    run link whose agent identity is that commit's pusher, and all of them name one agent.
    `human`: no run link names any of them and one enrolled human pushed them all. Otherwise
    `unknown` (no own commit included)."""
    own = [entry for entry in commits if not _exempt(entry, in_base_history)]
    if not own:
        return _UNKNOWN
    touching = [link for link in links if any(entry.sha in link.commits for entry in own)]
    if not touching:
        pushers = {entry.pusher_id for entry in own}
        if len(pushers) == 1:
            (pusher,) = pushers
            if pusher.startswith("human:"):
                return Author(
                    kind="human",
                    id=pusher,
                    model_family=None,
                    source="human_evidence",
                    run_ref=None,
                )
        return _UNKNOWN
    used: dict[str, RunLink] = {}
    for entry in own:
        counting = [
            link
            for link in touching
            if entry.sha in link.commits
            and link.agent_id == entry.pusher_id
            and link.agent_id.startswith("agent:")
        ]
        if not counting:
            return _UNKNOWN
        for link in counting:
            used[link.run_ref] = link
    agents = {link.agent_id for link in used.values()}
    if len(agents) != 1:
        return _UNKNOWN
    families = {link.model_family for link in used.values()}
    return Author(
        kind="agent",
        id=next(iter(agents)),
        model_family=next(iter(families)) if len(families) == 1 else None,
        source="ledger_run",
        run_ref=next(iter(used)) if len(used) == 1 else None,
    )


# --- the facts and the bundle ---------------------------------------------------------------------


@dataclass(frozen=True)
class CommitFact:
    """One commit of `merge-base..head` as the engine and the forge report it: `merge_tree` is
    what the collector's own `mt-1` run printed for a merge commit (informational: the gate
    recomputes it, GT-15); the user ids are the forge's numeric ids of the commit author (a
    claim) and of the actor of the push that introduced the commit (None: no push event)."""

    sha: str
    parents: tuple[str, ...]
    tree: str
    merge_tree: str | None
    author_user_id: str | None
    pusher_user_id: str | None


@dataclass(frozen=True)
class CollectFacts:
    """Everything one bundle is built from, read by the service at collection time."""

    org_id: str
    repo: str
    repo_id: str
    object_format: str
    """`git rev-parse --show-object-format`: only `sha1` is supported (TL-3)."""
    protected_branch: str
    pr: PrFacts
    """The PR as read before the facts were gathered."""
    head_after: str | None
    """The PR's head re-read after the facts were gathered (None: not open any more)."""
    tree_sha: str
    base_sha: str
    merge_bases: tuple[str, ...]
    diff: bytes | None
    """The patch form of the pinned TL-13 run over merge-base..head (the run TL-13 names `diff-1`
    and `diff-2` keeps; the hash made from it uses `DIFF_PIN`). None: the run failed or timed
    out."""
    raw_paths: bytes | None
    """The raw form of the same run (`vm_tiers.RAW_PATHS_COMMAND`, TL-13), or None likewise."""
    git_version: str
    commits: tuple[CommitFact, ...]
    """Topological: a parent that is in the chain comes before its child; the head is last."""
    enrollment: Mapping[str, str]
    """The org's enrollment: forge numeric user id to `human:<id>` or `agent:<id>`."""
    run_links: tuple[RunLink, ...]
    in_base_history: Callable[[str], bool]
    policy: PolicyAt
    tier_list: TierListAt
    """Both as `read_base_inputs` returned them: read at `base_sha` (EB-2, TL-19)."""
    codeowners_sha256: str
    codeowners_read_at: str
    """The commit CODEOWNERS was hashed at: it must be `base_sha` too (EB-2)."""
    checks: tuple[LiveCheck, ...]
    reviews: tuple[Mapping[str, Any], ...]
    """6.1 `reviews` entries from the signer (hashes and ids), possibly empty."""
    canon_commit: str | None
    task_sha256: str | None
    earlier_tier3_bundle: bool
    """TL-14: a ledgered bundle of this PR was tier 3 (the caller's ledger query by `repo_id`)."""
    now: datetime


@dataclass(frozen=True)
class Collected:
    bundle: EvidenceBundle
    data: bytes
    """The canonical bytes: what is stored and what the gate parses."""
    key: tuple[str, int, str]
    """The store key (repo id, PR, head sha): liveness is decided by it (section 4)."""
    ledger_payload: Mapping[str, Any]


def bundle_paths(changed: Sequence[ChangedPath]) -> list[dict[str, str]]:
    """6.1 `paths`: one entry per path of the raw path set, sorted by the written path. An
    unreadable path (TL-16: not UTF-8, a control or invisible character, a backslash, a leading
    `hex:`) is written as `hex:` plus the hex of its bytes, so the bundle holds no raw control
    character and an entry that begins with `hex:` is always this rendering. A type change is
    `M` (both modes known), an add `A`, a delete `D`."""
    out: list[dict[str, str]] = []
    for entry in changed:
        raw = entry.path.encode("utf-8", "surrogateescape")
        shown = "hex:" + raw.hex() if path_unreadable(entry.path) else entry.path
        status = "A" if entry.old_mode is None else "D" if entry.new_mode is None else "M"
        out.append({"path": shown, "status": status})
    return sorted(out, key=lambda item: item["path"])


def _chain(facts: CollectFacts, head: str) -> None:
    seen: set[str] = set()
    shas = {commit.sha for commit in facts.commits}
    if not facts.commits or len(shas) != len(facts.commits):
        raise CollectError("commit_chain_invalid")
    for commit in facts.commits:
        if any(parent in shas and parent not in seen for parent in commit.parents):
            raise CollectError("commit_chain_invalid")
        seen.add(commit.sha)
    last = facts.commits[-1]
    if last.sha != head or last.tree != facts.tree_sha:
        raise CollectError("commit_chain_invalid")


def _time(when: datetime | None) -> str | None:
    if when is None:
        return None
    if when.tzinfo is None:  # a naive time would be read as local time: never guessed
        raise CollectError("time_invalid")
    return format_time(when)


def collect(facts: CollectFacts) -> Collected:
    """Build the bundle or refuse with a code. Never returns a bundle for a head that moved
    while the facts were gathered, for policy inputs not read at the base tip, for a PR the
    gate would not judge, or when the tier could not be computed (TL-32)."""
    pr = facts.pr
    _base_tip(facts.base_sha, facts.object_format)
    if pr.state != "open" or pr.base_ref != facts.protected_branch:
        raise CollectError("pr_not_eligible")
    if pr.head_repo_id != facts.repo_id:
        raise CollectError("fork_head")
    if facts.head_after != pr.head_sha:
        raise CollectError("head_moved")
    if len(facts.merge_bases) != 1:
        raise CollectError("merge_base_ambiguous")
    _base_tip(
        facts.base_sha,
        facts.object_format,
        facts.policy.commit_sha,
        facts.tier_list.commit_sha,
        facts.codeowners_read_at,
    )
    collected_at = _time(facts.now)
    change = _change(
        facts.diff,
        facts.raw_paths,
        facts.tier_list,
        facts.policy.policy,
        labels=pr.labels,
        earlier_tier3_bundle=facts.earlier_tier3_bundle,
    )
    _chain(facts, pr.head_sha)
    assert facts.diff is not None  # `_change` refused None
    checks, full_tier = _checks(facts, pr.head_sha)
    try:
        commits = [
            CommitEntry(
                sha=commit.sha,
                parents=list(commit.parents),
                tree=commit.tree,
                merge_tree=commit.merge_tree,
                author_id=map_identity(commit.author_user_id, facts.enrollment),
                pusher_id=map_identity(commit.pusher_user_id, facts.enrollment),
            )
            for commit in facts.commits
        ]
        author = author_of(commits, facts.run_links, facts.in_base_history)
        draft: dict[str, Any] = {
            "version": "vm-evidence-1",
            "org_id": facts.org_id,
            "repo": facts.repo,
            "repo_id": facts.repo_id,
            "pr": pr.number,
            "head_sha": pr.head_sha,
            "tree_sha": facts.tree_sha,
            "base_ref": pr.base_ref,
            "base_sha": facts.base_sha,
            "merge_base": facts.merge_bases[0],
            "change_hash": change.change_hash,
            "diff_hash": hashlib.sha256(facts.diff).hexdigest(),
            "git_version": facts.git_version,
            "pins": {"merge_tree": "mt-1", "diff": DIFF_PIN},
            "author": author.model_dump(mode="json"),
            "paths": bundle_paths(change.paths),
            "commits": [entry.model_dump(mode="json") for entry in commits],
            "tier": change.tier,
            "tier_reasons": list(change.codes),
            "tier_list_sha256": facts.tier_list.tier_list.sha256,
            "policy_sha256": facts.policy.sha256,
            "codeowners_sha256": facts.codeowners_sha256,
            "canon_commit": facts.canon_commit,
            "task_sha256": facts.task_sha256,
            "checks": checks,
            "full_tier": full_tier,
            "reviews": [dict(review) for review in facts.reviews],
            "labels": sorted(pr.labels),
            "collected_at": collected_at,
            "collector": COLLECTOR_ACTOR,
            "bundle_hash": "0" * 64,
        }
        unsealed = EvidenceBundle.model_validate(draft)
        bundle = EvidenceBundle.model_validate({**draft, "bundle_hash": unsealed.body_hash()})
        data = canonical_bytes(bundle)
    except (ValidationError, ValueError, TypeError) as exc:
        if isinstance(exc, CollectError):
            raise
        raise CollectError("bundle_invalid") from None
    return Collected(
        bundle=bundle,
        data=data,
        key=(bundle.repo_id, bundle.pr, bundle.head_sha),
        ledger_payload=evidence_recorded_payload(bundle),
    )


def _checks(facts: CollectFacts, head: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """EB-5: by name, producer and sha, for the exact head only, ordered by (name, producer,
    run id). A run of a full-tier name that carries its own payload binding is full-tier
    evidence (GT-9); without the binding it is only a plain check. A conclusion the format does
    not know is written `error` (never success)."""
    full_names = frozenset(facts.policy.policy.full_tier)
    checks: list[dict[str, Any]] = []
    full_tier: list[dict[str, Any]] = []
    runs = [run for run in facts.checks if run.head_sha == head]
    for run in sorted(runs, key=lambda r: (r.name, r.producer_id, r.run_id or "")):
        conclusion = run.conclusion if run.conclusion in _CONCLUSIONS else "error"
        common = {
            "name": run.name,
            "kind": run.kind,
            "producer_id": run.producer_id,
            "conclusion": conclusion,
            "run_id": run.run_id,
            "completed_at": _time(run.completed_at),
        }
        if run.name in full_names and run.binding is not None:
            number, base_ref, bound_head = run.binding
            binding = {"pr": number, "base_ref": base_ref, "head_sha": bound_head}
            full_tier.append({**common, "binding": binding, "started_at": _time(run.started_at)})
        else:
            checks.append({**common, "head_sha": run.head_sha})
    return checks, full_tier


# --- the gate's own recomputation (GT-7, TL-20, TL-24) --------------------------------------------


@dataclass(frozen=True)
class Recompute:
    """What the gate recomputes itself from its own git runs and its own base-tip reads, to
    compare with the live bundle (GT-7, TL-20) and to decide step 3b (GT-12)."""

    head_sha: str
    tree_sha: str
    base_sha: str
    merge_base: str
    change_hash: str
    tier: int
    tier_codes: tuple[str, ...]
    """The gate's own reason codes (TL-20 rule 3a and step 3b read these, never the bundle's)."""
    paths: tuple[ChangedPath, ...]
    """One entry per path as git printed it (UTF-8 with `surrogateescape`, never `hex:`)."""
    head_contains_base: bool
    tier_list_sha256: str
    policy_sha256: str
    read_at: str
    """The commit the policy and the tier list were read at: the live base tip."""


def recompute(
    *,
    head_sha: str,
    tree_sha: str,
    base_sha: str,
    merge_bases: tuple[str, ...],
    diff: bytes | None,
    raw_paths: bytes | None,
    head_contains_base: bool,
    tier_list: TierListAt,
    policy: PolicyAt,
    labels: frozenset[str],
    earlier_tier3_bundle: bool,
    object_format: str,
) -> Recompute:
    """The collector's computation for the gate (TL-24: the same loader, path set and tier
    function). `tier_list` and `policy` are the gate's own reads at the live base tip
    `base_sha`. Raises `CollectError` with the collector's codes and never invents a tier
    (TL-32): `merge_base_ambiguous` (the gate answers `bundle_invalid`), `tier_compute_error`,
    `diff_invalid`, `paths_mismatch`, `inputs_not_from_base_tip`,
    `repo_object_format_unsupported` (the gate answers `gate_error`)."""
    _base_tip(base_sha, object_format, tier_list.commit_sha, policy.commit_sha)
    if len(merge_bases) != 1:
        raise CollectError("merge_base_ambiguous")
    change = _change(
        diff,
        raw_paths,
        tier_list,
        policy.policy,
        labels=labels,
        earlier_tier3_bundle=earlier_tier3_bundle,
    )
    return Recompute(
        head_sha=head_sha,
        tree_sha=tree_sha,
        base_sha=base_sha,
        merge_base=merge_bases[0],
        change_hash=change.change_hash,
        tier=change.tier,
        tier_codes=change.codes,
        paths=change.paths,
        head_contains_base=head_contains_base,
        tier_list_sha256=tier_list.tier_list.sha256,
        policy_sha256=policy.sha256,
        read_at=base_sha,
    )


# --- ledger before store (LA-1), refresh (EB-8) ---------------------------------------------------


def evidence_recorded_payload(bundle: EvidenceBundle) -> dict[str, Any]:
    """The `merge.evidence_recorded` payload (6.4a, 14 keys): ids, hashes, the tier, its codes
    and the hash of the tier list that decided it (TL-21), one time. No path, no label, no
    text; no key named `reason`, `note` or `message` (a ledger read would hide it)."""
    return {
        "actor": COLLECTOR_ACTOR,
        "org_id": bundle.org_id,
        "repo": bundle.repo,
        "repo_id": bundle.repo_id,
        "pr": bundle.pr,
        "head_sha": bundle.head_sha,
        "base_sha": bundle.base_sha,
        "change_hash": bundle.change_hash,
        "bundle_hash": bundle.bundle_hash,
        "tier": bundle.tier,
        "tier_codes": list(bundle.tier_reasons),
        "tier_list_sha256": bundle.tier_list_sha256,
        "policy_sha256": bundle.policy_sha256,
        "collected_at": bundle.collected_at,
    }


class CollectorHost(Protocol):
    """The I/O `record` delegates. Either method may raise."""

    def append(self, event_type: str, actor: str, payload: Mapping[str, Any]) -> int:
        """Append to the org stream; returns the sequence number."""

    def store(self, repo_id: str, pr: int, head_sha: str, data: bytes) -> None:
        """Store the bundle bytes under the key (repo id, PR, head)."""


def record(collected: Collected, host: CollectorHost) -> int:
    """Ledger first, then store (LA-1). What is ledgered and stored is re-derived from the
    bundle: a `Collected` whose bytes, key or payload are not the bundle's is `bundle_invalid`
    and nothing is written. A failed append stores nothing; a failed store leaves a ledgered
    hash without bytes, which the gate shows as `pending bundle_missing`. Returns the ledger
    sequence number."""
    bundle = collected.bundle
    payload = evidence_recorded_payload(bundle)
    if (
        not bundle.hash_ok()
        or collected.data != canonical_bytes(bundle)
        or collected.key != (bundle.repo_id, bundle.pr, bundle.head_sha)
        or dict(collected.ledger_payload) != payload
    ):
        raise CollectError("bundle_invalid")
    event_type = EventType.MERGE_EVIDENCE_RECORDED.value
    if check_merge_actor(event_type, COLLECTOR_ACTOR) is not None:  # pragma: no cover
        raise CollectError("actor_forbidden")
    try:
        seq = host.append(event_type, COLLECTOR_ACTOR, payload)
    except Exception:
        raise CollectError("ledger_unavailable") from None
    try:
        host.store(*collected.key, collected.data)
    except Exception:
        raise CollectError("store_unavailable") from None
    return seq


def refresh_due(bundle: EvidenceBundle, policy: Policy, now: datetime) -> bool:
    """EB-8: an open PR's live bundle is collected again (same facts, new `collected_at`, a new
    hash and ledger entry) once 75% of `bundle_ttl` has passed."""
    after = policy.bundle_ttl_seconds * REFRESH_NUMERATOR // REFRESH_DENOMINATOR
    return now >= parse_time(bundle.collected_at) + timedelta(seconds=after)
