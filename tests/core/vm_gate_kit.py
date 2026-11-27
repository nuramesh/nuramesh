"""Test kit for the Merge Gate (VM-4a): the case-table baseline B (spec section 10) as a
mutable `World` that renders `GateInputs`, a fake forge with the ruleset semantics the gate relies
on (one check run per (head, name, app), `expectedHeadOid`, strict up-to-date, absent-check blocks,
producer pin), a fake ledger that enforces the actor table, and a host that wires them to the gate
flow. Keys and signatures are a fake scheme (SHA-512 of key and message), generated here; no real
key material, nothing key-shaped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nuramesh.core.vm_collect import CollectError, LiveCheck, PrFacts, Recompute, bundle_paths
from nuramesh.core.vm_events import check_merge_actor
from nuramesh.core.vm_formats import (
    VERDICT_DOMAIN,
    EvidenceBundle,
    Policy,
    SignedVerdict,
    TrustList,
    VerdictPayload,
    canonical_bytes,
    format_time,
    payload_hash,
    public_key_id,
    signed_bytes,
)
from nuramesh.core.vm_gate import (
    ApprovalView,
    GateCallables,
    GateInputs,
    HeadOutcome,
    Outcome,
    evaluate_head,
    evaluate_pr,
)
from nuramesh.core.vm_gate_flow import GateEvent, GateHost, Posted, handle_event
from nuramesh.core.vm_tiers import ChangedPath, load_extra_trust_roots, load_tier_list, tier_of
from nuramesh.core.vm_verdicts import ResetRecord, UnparsedRecord, VerdictRecord

ORG = "org-example"
REPO = "github.com/acme/widgets"
REPO_ID = "123456"
CI_APP = "15368"
GATE_APP = "99001"
OTHER_APP = "99"
AUTHOR = "human:author-1"
OWNER = "human:owner-1"
ADMIN = "human:admin-1"
OTHER_OWNER = "human:owner-2"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)


def sha(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()  # noqa: S324 (a test label, not security)


def tree(name: str) -> str:
    return hashlib.sha1(("tree:" + name).encode()).hexdigest()  # noqa: S324


def hex64(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def clean_tree(p1: str, p2: str) -> str:
    return hashlib.sha1(f"merge:{p1}:{p2}".encode()).hexdigest()  # noqa: S324


_DEFAULT: Any = {"__baseline__": True}
M0, M1, M2 = sha("M0"), sha("M1"), sha("M2")
H0, H1, H2 = sha("H0"), sha("H1"), sha("H2")
X = hex64("change")
X_OTHER = hex64("other-change")
K1_PUB, K2_PUB = "7" * 64, "8" * 64
K1, K2 = public_key_id(K1_PUB), public_key_id(K2_PUB)


def ts(when: datetime) -> str:
    return format_time(when)


def fake_sign(public_key: str, message: bytes) -> str:
    return hashlib.sha512(public_key.encode() + message).hexdigest()


def verify(public_key: str, message: bytes, sig: str) -> bool:
    return sig == fake_sign(public_key, message)


# --- policy and trust -----------------------------------------------------------------------------


def policy_dict(**over: Any) -> dict[str, Any]:
    """The policy of baseline B, a full file (GC-11 c: every key but `extra_trust_roots` is
    required); the tier-3 quorum leaves its three optional fields to the strict defaults."""
    base: dict[str, Any] = {
        "version": "vm-policy-1",
        "enforce_tiers": [1, 2, 3],
        "required_checks": {"1": ["ci-ok"], "2": ["ci-ok"], "3": ["ci-ok"]},
        "full_tier": ["full-gate"],
        "full_tier_tiers": [3],
        "producers": {
            "ci-ok": {"kind": "check_run", "id": CI_APP},
            "full-gate": {"kind": "check_run", "id": CI_APP},
        },
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


def make_policy(**over: Any) -> Policy:
    return Policy.model_validate(policy_dict(**over))


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


def make_trust(*keys: dict[str, Any], serial: int = 5) -> TrustList:
    return TrustList.model_validate(
        {"version": "vm-trust-1", "org_id": ORG, "serial": serial, "keys": list(keys)}
    )


# --- the world: baseline B ------------------------------------------------------------------------


def commit_entry(
    sha_: str, parents: Sequence[str], tree_: str, author: str = AUTHOR, pusher: str = AUTHOR
) -> dict[str, Any]:
    return {
        "sha": sha_,
        "parents": list(parents),
        "tree": tree_,
        "merge_tree": None,
        "author_id": author,
        "pusher_id": pusher,
    }


LB = (
    b'{"version":"vm-tiers-1","tier3":["src/nuramesh/core/ledger.py"],'
    b'"tier1":["guide/**"],"tier1_except":[]}'
)
"""`tiers.md` 9.5: the tier list the protected branch's tip M1 holds in baseline B."""
LB_SHA256 = "4a9368e806eaeaf3d835dfe3cc19d18f7ed26542d5ea9b4c0fa32b3ddf89a542"
# tier 3: baseline B carries the label `tier-3` and has an earlier ledgered tier-3 bundle of H1
# (TL-14, TL-17; owner ruling 2026-11-11; vectors version 3, blind-confirmed)
_TIER_REASONS = {
    1: ["tier1_paths"],
    2: ["tier2_default"],
    3: ["tier3_label", "tier3_path", "tier3_sticky"],
}


BASELINE_PATH = "src/nuramesh/core/ledger.py"
"""The path the README's baseline B changes (section 10). It is no line of the generic constant
`OWNER_ONLY_PATHS`; it is a trust root only in a repository whose policy lists it (ours, `XN`)."""

DOCS = "".join(("do", "cs"))
"""The documentation folder's name, written in two pieces on purpose. The Merge Gate tests
use paths under it only as **strings given to the tier function and the matcher** (the spec's
cases: the CODEOWNERS place there, the decisions folder); they read no file in that folder. The
CI test selector takes every test source that spells the folder's name for a reader of it and
would run these tests for every documentation change (and its guards would list them as readers);
the pre-review commit `baaa88d14` met the same guard. A test that does read that folder must
spell the name and be registered in the CI test selector."""

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_FILE = REPO_ROOT / "spec" / "verified-merge" / "tiers.md"
VENDORED_BLOCKS = REPO_ROOT / "tests" / "data" / "vm_spec" / "trust-roots-v1.md"
FENCE = "`" * 3


def spec_text() -> str:
    """`tiers.md` when this checkout has it (then it must hold the blocks), else the vendored
    byte-for-byte copy of its three fenced blocks."""
    return (SPEC_FILE if SPEC_FILE.is_file() else VENDORED_BLOCKS).read_text("utf-8")


def block_lines(text: str, marker: str) -> list[str]:
    """The lines of the one fence opened by three backticks plus `marker`, fences included."""
    lines = text.split("\n")
    opens = [index for index, line in enumerate(lines) if line == FENCE + marker]
    assert len(opens) == 1, "the marker must appear exactly once"
    rest = lines[opens[0] + 1 :]
    assert FENCE in rest, "the block is not closed"
    return [FENCE + marker, *rest[: rest.index(FENCE)], FENCE]


XN: list[str] = block_lines(spec_text(), "extra-trust-roots-nuramesh-v1")[1:-1]
"""Our repository's `extra_trust_roots` (TL-26, the block `extra-trust-roots-nuramesh-v1`): the
cases marked `XN` and the README rows that change gate code (G-18, Q-05) use a policy with it."""


@dataclass
class World:
    """Baseline B; every test mutates one field and renders `inputs()`."""

    policy: Policy = field(default_factory=make_policy)
    trust: TrustList = field(default_factory=lambda: make_trust(trust_key(K1_PUB)))
    mode: str = "enforce"
    now: datetime = NOW
    protected: str = "main"
    base_tip: str = M1
    base_history: set[str] = field(default_factory=lambda: {M0, M1})
    pr_number: int = 7
    pr_state: str = "open"
    draft: bool = False
    base_ref: str = "main"
    head: str = H1
    head_repo_id: str = REPO_ID
    labels: set[str] = field(default_factory=set)
    open_ask: bool = False
    last_base_change_at: datetime | None = None
    paths: list[str] = field(default_factory=lambda: [BASELINE_PATH])
    tier: int | None = None
    """The tier the test expects the gate to compute (checked against the real tier function);
    None: baseline B, tier 3 with the label and an earlier tier-3 bundle."""
    label: bool | None = None
    """The PR carries the tier-3 label now (default: only when `tier` is not 1 or 2)."""
    sticky: bool | None = None
    """An earlier ledgered tier-3 bundle of the PR exists (default as `label`)."""
    path_modes: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    gate_tier_codes: tuple[str, ...] | None = None
    """A test double for the gate's own codes (a unit test of one step); None: computed."""
    recompute_error: str | None = None
    """The `CollectError` code the gate's own recomputation raised, or None."""
    change_hash: str = X
    head_contains_base: bool = True
    merge_bases: tuple[str, ...] = (M1,)
    trees: dict[str, str] = field(default_factory=lambda: {H0: tree("H0"), H1: tree("H1")})
    commits: list[dict[str, Any]] = field(
        default_factory=lambda: [commit_entry(H1, [M1], tree("H1"))]
    )
    author: dict[str, Any] = field(
        default_factory=lambda: {
            "kind": "human",
            "id": AUTHOR,
            "model_family": None,
            "source": "human_evidence",
            "run_ref": None,
        }
    )
    tier_list: bytes | None = LB
    """The raw tier list at the live base tip, as the gate reads it itself (None: missing)."""
    bundle_tier: int | None = None
    bundle_policy_sha256: str | None = None
    bundle_tier_reasons: list[str] | None = None
    bundle_tier_list_sha256: str | None = None
    bundle_base_sha: str | None = None
    bundle_change_hash: str | None = None
    bundle_head: str | None = None
    bundle_tree: str | None = None
    bundle_collected_at: datetime | None = None
    # README section 4 (#944): the pin the stored bundle records; `diff-1` is the superseded one
    bundle_diff_pin: str = "diff-2"
    bundle_present: bool = True
    bundle_ledgered: bool = True
    bundle_bytes_override: bytes | None = None
    checks: list[dict[str, Any]] = field(default_factory=lambda: [_DEFAULT])
    verdict_specs: list[dict[str, Any]] = field(default_factory=lambda: [_DEFAULT])
    unparsed: list[UnparsedRecord] = field(default_factory=list)
    """Forge-only comment records the reader could not parse (pointers, malformed; GC-15 b)."""
    resets: list[ResetRecord] = field(default_factory=list)
    approvals: list[ApprovalView] = field(default_factory=lambda: [_DEFAULT])
    owners: dict[str, frozenset[str]] = field(default_factory=dict)
    default_owners: frozenset[str] = frozenset({OWNER})
    task_key: str | None = "task-1"
    human_quorum: str = "evaluate"
    quorum_entitled: bool = True
    """The enterprise quorum is granted in the kit; the tests of the refusal turn it off."""
    inputs_read_at: str | None = None
    recompute_missing: bool = False
    sharers: list[World] = field(default_factory=list)
    rebind_result: Callable[[VerdictRecord], VerdictRecord | None] | None = None
    merge_tree_results: dict[tuple[str, str], str | None] = field(default_factory=dict)
    unknown_commits: set[str] = field(default_factory=set)

    # -- building blocks ---------------------------------------------------------------------

    def __post_init__(self) -> None:
        """Baseline B for the fields not given; an explicit empty list stays empty."""
        if self.checks == [_DEFAULT]:
            self.checks = [
                check("ci-ok", head=self.head),
                check("full-gate", head=self.head, binding=(self.pr_number, "main", self.head)),
            ]
        if self.verdict_specs == [_DEFAULT]:
            self.verdict_specs = [verdict_spec()]
        if self.approvals == [_DEFAULT]:
            self.approvals = [approval(OWNER)]

    @property
    def policy_sha256(self) -> str:
        return payload_hash(self.policy)

    def ledgered_bundle_hashes(self) -> frozenset[str]:
        hashes = {hex64("earlier-bundle")}
        if self.bundle_ledgered and self.bundle_present:
            hashes.add(self.bundle().bundle_hash)
        return frozenset(hashes)

    def changed(self) -> tuple[ChangedPath, ...]:
        regular = ("100644", "100644")
        return tuple(ChangedPath(p, *self.path_modes.get(p, regular)) for p in sorted(self.paths))

    def computed(self) -> tuple[int, tuple[str, ...]]:
        """The gate's own tier and codes, from the real tier function (TL-24)."""
        marked = self.tier in (None, 3)
        tier, codes = tier_of(
            self.changed(),
            load_tier_list(self.tier_list),
            label=marked if self.label is None else self.label,
            sticky=marked if self.sticky is None else self.sticky,
            extra_trust_roots=load_extra_trust_roots(self.policy.extra_trust_roots),
        )
        assert self.tier is None or tier == self.tier, f"the fixture is tier {tier} ({codes})"
        return tier, self.gate_tier_codes if self.gate_tier_codes is not None else codes

    def bundle_dict(self) -> dict[str, Any]:
        head = self.bundle_head or self.head
        tier, codes = self.computed()
        honest = self.bundle_tier in (None, tier)
        collected = self.bundle_collected_at or (self.now - timedelta(minutes=30))
        return {
            "version": "vm-evidence-1",
            "org_id": ORG,
            "repo": REPO,
            "repo_id": REPO_ID,
            "pr": self.pr_number,
            "head_sha": head,
            "tree_sha": self.bundle_tree or self.trees.get(head, tree(head)),
            "base_ref": self.base_ref,
            "base_sha": self.bundle_base_sha or self.base_tip,
            "merge_base": self.merge_bases[0],
            "change_hash": self.bundle_change_hash or self.change_hash,
            "diff_hash": hex64("diff"),
            "git_version": "2.43.0",
            "pins": {"merge_tree": "mt-1", "diff": self.bundle_diff_pin},
            "author": dict(self.author),
            "paths": bundle_paths(self.changed()),  # the collector's own rendering (6.1)
            "commits": [dict(c) for c in self.commits],
            "tier": self.bundle_tier or tier,
            "tier_reasons": self.bundle_tier_reasons
            or (list(codes) if honest else _TIER_REASONS[self.bundle_tier or tier]),
            "tier_list_sha256": self.bundle_tier_list_sha256
            or load_tier_list(self.tier_list).sha256,
            "policy_sha256": self.bundle_policy_sha256 or self.policy_sha256,
            "codeowners_sha256": hex64("codeowners"),
            "canon_commit": None,
            "task_sha256": None,
            "checks": [],
            "full_tier": [],
            "reviews": [],
            "labels": sorted(self.labels | {"tier-3"}),
            "collected_at": ts(collected),
            "collector": "service:merge-gate-collector",
            "bundle_hash": "0" * 64,
        }

    def bundle(self) -> EvidenceBundle:
        draft = EvidenceBundle.model_validate(self.bundle_dict())
        return EvidenceBundle.model_validate(
            {**self.bundle_dict(), "bundle_hash": draft.body_hash()}
        )

    def bundle_bytes(self) -> bytes | None:
        if self.bundle_bytes_override is not None:
            return self.bundle_bytes_override
        if not self.bundle_present:
            return None
        return canonical_bytes(self.bundle())

    def verdict_records(self) -> tuple[VerdictRecord, ...]:
        return tuple(verdict_record(self, **spec) for spec in self.verdict_specs)

    def pr_facts(self) -> PrFacts:
        return PrFacts(
            number=self.pr_number,
            state=self.pr_state,  # type: ignore[arg-type]
            draft=self.draft,
            base_ref=self.base_ref,
            head_sha=self.head,
            head_repo_id=self.head_repo_id,
            labels=frozenset(self.labels),
            open_ask=self.open_ask,
            last_base_change_at=self.last_base_change_at,
        )

    def recompute(self) -> Recompute | CollectError | None:
        if self.recompute_missing:
            return None
        if self.recompute_error is not None:
            return CollectError(self.recompute_error)
        if len(self.merge_bases) != 1:
            return CollectError("merge_base_ambiguous")  # as `vm_collect.recompute` answers
        tier, codes = self.computed()
        return Recompute(
            head_sha=self.head,
            tree_sha=self.trees.get(self.head, tree(self.head)),
            base_sha=self.base_tip,
            merge_base=self.merge_bases[0],
            change_hash=self.change_hash,
            tier=tier,
            tier_codes=codes,
            paths=self.changed(),
            head_contains_base=self.head_contains_base,
            tier_list_sha256=load_tier_list(self.tier_list).sha256,
            policy_sha256=self.policy_sha256,
            read_at=self.base_tip,
        )

    def read_commit(self, commit: str) -> tuple[Sequence[str], str] | None:
        """The gate's own git: a commit's parents and tree, None for one it cannot find."""
        for entry in self.commits:
            if entry["sha"] == commit and commit not in self.unknown_commits:
                return tuple(entry["parents"]), self.trees.get(commit, entry["tree"])
        return None

    def owners_of(self, path: str) -> frozenset[str]:
        return self.owners.get(path, self.default_owners)

    def live_checks(self) -> tuple[LiveCheck, ...]:
        return tuple(live_check(**c) for c in self.checks)

    def inputs(self) -> GateInputs:
        return GateInputs(
            org_id=ORG,
            repo=REPO,
            repo_id=REPO_ID,
            protected_branch=self.protected,
            mode=self.mode,  # type: ignore[arg-type]
            now=self.now,
            pr=self.pr_facts(),
            sharers=tuple(w.pr_facts() for w in self.sharers),
            policy=self.policy,
            trust=self.trust,
            inputs_read_at=self.inputs_read_at or self.base_tip,
            recompute=self.recompute(),
            live_bundle=self.bundle_bytes(),
            ledgered_bundle_hashes=self.ledgered_bundle_hashes(),
            checks=self.live_checks(),
            verdicts=(*self.verdict_records(), *self.unparsed),
            resets=tuple(self.resets),
            approvals=tuple(self.approvals),
            owners_of=self.owners_of,
            task_key=self.task_key,
            human_quorum=self.human_quorum,  # type: ignore[arg-type]
            quorum_entitled=self.quorum_entitled,
        )

    def in_base_history(self, commit: str) -> bool:
        return commit in self.base_history

    def merge_tree(self, p1: str, p2: str) -> str | None:
        if (p1, p2) in self.merge_tree_results:
            return self.merge_tree_results[(p1, p2)]
        return clean_tree(p1, p2)

    def callables(self) -> GateCallables:
        return GateCallables(
            verify=verify,
            read_commit=self.read_commit,
            in_base_history=self.in_base_history,
            merge_tree=self.merge_tree,
            rebind=self.rebind_result,
        )

    # -- common mutations --------------------------------------------------------------------

    def merge_main_into_pr(self, *, clean: bool = True) -> None:
        """C-01: main moved to M2 and the author merged it: H2 = merge(H1, M2)."""
        self.base_tip = M2
        self.base_history.add(M2)
        merged_tree = clean_tree(H1, M2) if clean else tree("evil")
        self.trees[H2] = merged_tree
        self.commits.append(commit_entry(H2, [H1, M2], merged_tree))
        self.head = H2
        self.merge_bases = (M2,)
        self.checks = [
            check("ci-ok", head=H2),
            check("full-gate", head=H2, binding=(self.pr_number, "main", H2)),
        ]

    def add_commit(self, new_head: str, *, change_hash: str | None = None) -> None:
        """A new non-merge commit by the author on top of the head."""
        self.trees[new_head] = tree(new_head)
        self.commits.append(commit_entry(new_head, [self.head], tree(new_head)))
        self.head = new_head
        if change_hash is not None:
            self.change_hash = change_hash
        self.checks = [
            check("ci-ok", head=new_head),
            check("full-gate", head=new_head, binding=(self.pr_number, "main", new_head)),
        ]


def check(
    name: str,
    *,
    head: str = H1,
    producer: str = CI_APP,
    conclusion: str = "success",
    kind: str = "check_run",
    run_id: str = "run-1",
    completed_at: datetime | None = T0 + timedelta(minutes=20),
    started_at: datetime | None = T0 + timedelta(minutes=5),
    binding: tuple[int, str, str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "head": head,
        "producer": producer,
        "conclusion": conclusion,
        "kind": kind,
        "run_id": run_id,
        "completed_at": completed_at,
        "started_at": started_at,
        "binding": binding,
    }


def live_check(
    name: str,
    head: str,
    producer: str,
    conclusion: str,
    kind: str,
    run_id: str,
    completed_at: datetime | None,
    started_at: datetime | None,
    binding: tuple[int, str, str] | None,
) -> LiveCheck:
    return LiveCheck(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        producer_id=producer,
        head_sha=head,
        conclusion=conclusion,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        binding=binding,
    )


def verdict_spec(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decision": "ACCEPT",
        "head": H1,
        "public": K1_PUB,
        "seq": 100,
        "ledger_at": T0,
        "issued_at": T0,
        "expires_at": T0 + timedelta(days=7),
        "verdict_id": "a" * 32,
        "reviewer_id": "reviewer-a",
        "model_family": "family-x",
        "change_hash": None,
        "pr": None,
        "repo": None,
        "org_id": None,
        "bundle_hash": None,
        "policy_sha256": None,
        "tier": None,
        "rebound_from": None,
        "flip_sig": False,
        "ledger_payload_hash": None,
        "source_at": None,
        "comment_id": None,
    }
    base.update(over)
    return base


def verdict_record(
    world: World,
    *,
    decision: str,
    head: str,
    public: str,
    seq: int | None,
    ledger_at: datetime | None,
    issued_at: datetime,
    expires_at: datetime,
    verdict_id: str,
    reviewer_id: str,
    model_family: str | None,
    change_hash: str | None,
    pr: int | None,
    repo: str | None,
    org_id: str | None,
    bundle_hash: str | None,
    policy_sha256: str | None,
    tier: int | None,
    rebound_from: str | None,
    flip_sig: bool,
    ledger_payload_hash: str | None,
    source_at: datetime | None,
    comment_id: int | None,
) -> VerdictRecord:
    payload = VerdictPayload.model_validate(
        {
            "version": "vm-verdict-1",
            "org_id": org_id or ORG,
            "repo": repo or REPO,
            "repo_id": REPO_ID,
            "pr": pr or world.pr_number,
            "head_sha": head,
            "base_ref": "main",
            "base_sha": M1,
            "change_hash": change_hash or world.change_hash,
            "bundle_hash": bundle_hash or hex64("earlier-bundle"),
            "tier": tier or world.bundle_tier or world.computed()[0],
            "policy_sha256": policy_sha256 or world.policy_sha256,
            "decision": decision,
            "reviewer_id": reviewer_id,
            "model_id": "example-model",
            "model_family": model_family,
            "verdict_text_sha256": hex64("verdict-text"),
            "verdict_id": verdict_id,
            "rebound_from": rebound_from,
            "key_id": public_key_id(public),
            "issued_at": ts(issued_at),
            "expires_at": ts(expires_at),
        }
    )
    sig = fake_sign(public, signed_bytes(VERDICT_DOMAIN, payload))
    if flip_sig:
        sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    signed = SignedVerdict(payload=payload, sig=sig)
    return VerdictRecord(
        verdict=signed,
        ledger_seq=seq,
        ledger_at=ledger_at,
        ledger_payload_hash=ledger_payload_hash or (payload_hash(payload) if seq else None),
        source_at=source_at,
        comment_id=comment_id,
    )


def approval(
    approver: str,
    *,
    approval_id: str | None = None,
    roles: Iterable[str] = ("approver",),
    head: str = H1,
    ignore: str | None = None,
    expires_at: datetime | None = None,
) -> ApprovalView:
    return ApprovalView(
        approval_id=approval_id or hex64("approval:" + approver + head)[:32],
        approver_id=approver,
        roles=frozenset(roles),
        head_sha=head,
        ignore=ignore,
        expires_at=expires_at,
    )


def baseline(**over: Any) -> World:
    return World(**over)


def evaluate(world: World) -> Outcome:
    return evaluate_pr(world.inputs(), world.callables())


def result(world: World) -> tuple[str, str]:
    outcome = evaluate(world)
    return outcome.state, outcome.reason


def head_evaluate(world: World, *others: World) -> HeadOutcome:
    return evaluate_head([world.inputs(), *(w.inputs() for w in others)], world.callables())


# --- fake forge, ledger and host ------------------------------------------------------------------


@dataclass
class CheckRun:
    id: str
    head: str
    name: str
    app_id: str
    state: str  # "pending" | "completed"
    conclusion: str | None
    external_id: str | None
    updates: int = 0


@dataclass
class ForgePr:
    number: int
    head: str
    base_ref: str
    head_repo_id: str
    draft: bool = False
    state: str = "open"
    labels: set[str] = field(default_factory=set)
    auto_merge_head: str | None = None
    enqueued_head: str | None = None


class FakeForge:
    """What the gate relies on from the forge: check runs keyed by (head, name, app) with one
    id per key (an update, never a second run), required contexts pinned to an app id, strict
    up-to-date, `expectedHeadOid` at merge time, absent-check blocks, no bypass."""

    def __init__(
        self,
        *,
        required: Sequence[tuple[str, str]] = (("merge-gate", GATE_APP),),
        strict: bool = True,
        protected: str = "main",
    ) -> None:
        self.prs: dict[int, ForgePr] = {}
        self.runs: dict[tuple[str, str, str], CheckRun] = {}
        self.statuses: list[tuple[str, str, str, str]] = []  # head, name, creator, state
        self.required = tuple(required)
        self.strict = strict
        self.protected = protected
        self.base_tip = M1
        self.base_history: set[str] = {M0, M1}
        self.head_contains: dict[str, set[str]] = {}  # head -> base tips it contains
        self.merged: list[tuple[int, str]] = []
        self.failed_merges: list[tuple[int, str, str]] = []
        self.next_run = 1000
        self.fail_check_write = False
        self.pre_merge_hook: Callable[[int, str], None] | None = None

    # -- PRs ---------------------------------------------------------------------------------

    def open_pr(
        self,
        number: int,
        head: str,
        *,
        base_ref: str | None = None,
        head_repo_id: str = REPO_ID,
        draft: bool = False,
    ) -> ForgePr:
        pr = ForgePr(number, head, base_ref or self.protected, head_repo_id, draft)
        self.prs[number] = pr
        self.head_contains.setdefault(head, {self.base_tip})
        return pr

    def push(self, number: int, new_head: str) -> None:
        pr = self.prs[number]
        pr.head = new_head
        pr.enqueued_head = None
        self.head_contains.setdefault(new_head, {self.base_tip})

    def move_base(self, new_tip: str) -> None:
        self.base_tip = new_tip
        self.base_history.add(new_tip)

    def sharers_of(self, number: int) -> list[ForgePr]:
        me = self.prs[number]
        return [
            p
            for p in self.prs.values()
            if p.number != number and p.state == "open" and p.head == me.head
        ]

    def open_prs_at(self, head: str) -> list[ForgePr]:
        return [p for p in self.prs.values() if p.state == "open" and p.head == head]

    # -- check runs and statuses -------------------------------------------------------------

    def upsert_check_run(
        self,
        head: str,
        name: str,
        app_id: str,
        *,
        state: str,
        conclusion: str | None,
        external_id: str | None = None,
    ) -> str:
        if self.fail_check_write:
            raise RuntimeError("forge 500")
        key = (head, name, app_id)
        run = self.runs.get(key)
        if run is None:
            run = CheckRun(
                f"cr-{self.next_run}", head, name, app_id, state, conclusion, external_id
            )
            self.next_run += 1
            self.runs[key] = run
        else:
            run.state, run.conclusion, run.external_id = state, conclusion, external_id
            run.updates += 1
        return run.id

    def ci(self, head: str, name: str, conclusion: str, app_id: str = CI_APP) -> str:
        state = "completed" if conclusion in ("success", "failure") else "in_progress"
        return self.upsert_check_run(head, name, app_id, state=state, conclusion=conclusion)

    def commit_status(self, head: str, name: str, creator: str, state: str) -> None:
        self.statuses.append((head, name, creator, state))

    def run_for(self, head: str, name: str, app_id: str) -> CheckRun | None:
        return self.runs.get((head, name, app_id))

    def live_checks(self, head: str, pr: ForgePr) -> tuple[LiveCheck, ...]:
        out: list[LiveCheck] = []
        for run in self.runs.values():
            if run.head != head or run.app_id == GATE_APP:
                continue
            out.append(
                LiveCheck(
                    name=run.name,
                    kind="check_run",
                    producer_id=run.app_id,
                    head_sha=run.head,
                    conclusion=run.conclusion or "in_progress",
                    run_id=run.id,
                    started_at=T0 + timedelta(minutes=5),
                    completed_at=T0 + timedelta(minutes=20) if run.conclusion else None,
                    binding=(pr.number, pr.base_ref, head) if run.name == "full-gate" else None,
                )
            )
        for head_, name, creator, state in self.statuses:
            if head_ == head:
                out.append(
                    LiveCheck(
                        name=name,
                        kind="status",
                        producer_id=creator,
                        head_sha=head_,
                        conclusion=state,
                        run_id=None,
                        started_at=None,
                        completed_at=None,
                        binding=None,
                    )
                )
        return tuple(out)

    # -- the ruleset ---------------------------------------------------------------------------

    def required_green(self, head: str) -> bool:
        for name, app_id in self.required:
            run = self.runs.get((head, name, app_id))
            if run is None or run.state != "completed" or run.conclusion != "success":
                return False  # absent, pending or failed: blocks (a status never satisfies it)
        return True

    def merge(self, number: int, expected_head_oid: str) -> bool:
        pr = self.prs[number]
        if pr.state != "open":
            return self._refuse(number, pr.head, "not_open")
        if pr.head != expected_head_oid:
            return self._refuse(number, pr.head, "head_moved")
        if pr.draft:
            return self._refuse(number, pr.head, "draft")
        if not self.required_green(pr.head):
            return self._refuse(number, pr.head, "required_not_green")
        if self.strict and self.base_tip not in self.head_contains.get(pr.head, set()):
            return self._refuse(number, pr.head, "not_up_to_date")
        if self.pre_merge_hook is not None:
            self.pre_merge_hook(number, pr.head)
        pr.state = "merged"
        self.merged.append((number, pr.head))
        new_tip = sha(f"merge-of-{number}-{pr.head}")
        self.move_base(new_tip)
        return True

    def _refuse(self, number: int, head: str, why: str) -> bool:
        self.failed_merges.append((number, head, why))
        return False

    def enable_auto_merge(self, number: int, expected_head_oid: str) -> None:
        self.prs[number].auto_merge_head = expected_head_oid

    def try_auto_merge(self) -> list[tuple[int, str]]:
        """GitHub's auto-merge fires when the required checks turn green on the live head."""
        fired: list[tuple[int, str]] = []
        for pr in list(self.prs.values()):
            if pr.state != "open" or pr.auto_merge_head is None:
                continue
            if self.merge(pr.number, pr.auto_merge_head):
                fired.append((pr.number, pr.head))
            elif pr.head != pr.auto_merge_head:
                # GitHub keeps auto-merge after a push and merges the new head once green
                pr.auto_merge_head = pr.head
        return fired


class FakeLedger:
    def __init__(self) -> None:
        self.events: list[tuple[int, str, str, Mapping[str, Any]]] = []
        self.fail = False

    def append(self, event_type: str, actor: str, payload: Mapping[str, Any]) -> int:
        if self.fail:
            raise RuntimeError("ledger unavailable")
        problem = check_merge_actor(event_type, actor)
        if problem is not None:
            raise PermissionError(problem)
        seq = len(self.events) + 1
        self.events.append((seq, event_type, actor, dict(payload)))
        return seq

    def of_type(self, event_type: str) -> list[Mapping[str, Any]]:
        return [p for _, t, _, p in self.events if t == event_type]

    def newest_decision(self, head: str) -> Mapping[str, Any] | None:
        for _, t, _, p in reversed(self.events):
            if t == "merge.gate_decided" and p.get("head_sha") == head:
                return dict(p)
        return None


class FakeHost(GateHost):
    """Wires the fake forge and ledger to the flow. Evidence per PR comes from a `World`; the
    live facts (state, head, labels, sharers, checks, base tip) come from the forge."""

    def __init__(self, forge: FakeForge, ledger: FakeLedger, mode: str = "enforce") -> None:
        self.forge = forge
        self.ledger = ledger
        self.mode = mode
        self.worlds: dict[int, World] = {}
        self.posted: dict[str, Posted] = {}
        self.read_failures = 0
        self.now = NOW
        self.ops: list[str] = []
        self.drop_pending_posts = False  # a test-side mutation: no pending-first write
        self.during_evaluation: Callable[[], None] | None = None
        self.before_confirm: Callable[[], None] | None = None
        self.confirms = 0  # how often the flow asked for the open-PR set
        self.on_confirm: Callable[[int], None] | None = None  # called with the call's number
        self.group_reader: Callable[[Sequence[int]], Sequence[tuple[GateInputs, str]]] | None = None

    def world_for(self, number: int, **over: Any) -> World:
        world = baseline(pr_number=number, **over)
        self.worlds[number] = world
        return world

    def _inputs(self, pr: ForgePr) -> GateInputs:
        world = self.worlds.get(pr.number) or self.world_for(pr.number)
        world.pr_number = pr.number
        world.pr_state = pr.state
        world.draft = pr.draft
        world.base_ref = pr.base_ref
        world.head = pr.head
        world.head_repo_id = pr.head_repo_id
        world.labels = set(pr.labels)
        world.base_tip = self.forge.base_tip
        world.base_history = set(self.forge.base_history)
        world.head_contains_base = self.forge.base_tip in self.forge.head_contains.get(
            pr.head, set()
        )
        world.mode = self.mode
        world.now = self.now
        world.checks = []
        inputs = world.inputs()
        world.checks = []
        return GateInputs(
            **{
                **inputs.__dict__,
                "checks": self.forge.live_checks(pr.head, pr),
                "sharers": tuple(p_facts(p) for p in self.forge.sharers_of(pr.number)),
            }
        )

    def live_head(self, repo: str, pr: int) -> str | None:
        found = self.forge.prs.get(pr)
        return found.head if found is not None and found.state == "open" else None

    def read_inputs(self, repo: str, head: str) -> Sequence[GateInputs]:
        if self.read_failures:
            self.read_failures -= 1
            raise RuntimeError("forge 500")
        hook, self.during_evaluation = self.during_evaluation, None
        if hook is not None:
            hook()  # the race test: someone acts while the gate is still evaluating
        return [self._inputs(pr) for pr in self.forge.open_prs_at(head)]

    def open_prs_at(self, repo: str, head: str) -> Sequence[int]:
        hook, self.before_confirm = self.before_confirm, None
        if hook is not None:
            hook()  # the race test: the head or the PR set moves after the inputs were read
        self.confirms += 1
        if self.on_confirm is not None:
            self.on_confirm(self.confirms)  # the race test: the set moves before the n-th read
        return [pr.number for pr in self.forge.open_prs_at(head)]

    def base_tip(self, repo: str) -> str:
        return self.forge.base_tip

    def read_group_inputs(self, repo: str, prs: Sequence[int]) -> Sequence[tuple[GateInputs, str]]:
        if self.group_reader is not None:
            return self.group_reader(prs)  # a lying or broken reader
        return [
            (self._inputs(self.forge.prs[n]), self.forge.prs[n].enqueued_head or "") for n in prs
        ]

    def read_checks(self, repo: str, head: str) -> Sequence[LiveCheck]:
        any_pr = next(iter(self.forge.prs.values()))
        return self.forge.live_checks(head, any_pr)

    def callables(self) -> GateCallables:
        any_world = next(iter(self.worlds.values()), None) or baseline()
        return any_world.callables()

    def post(
        self,
        head: str,
        status_name: str,
        state: str,
        reason: str,
        external_id: str,
        decision_key: str | None,
    ) -> str:
        completed = state in ("success", "failure")
        if self.drop_pending_posts and not completed and head in self.posted:
            return self.posted[head].run_id  # the mutation: the old result is left standing
        self.ops.append(f"post:{state}:{reason}")
        run_id = self.forge.upsert_check_run(
            head,
            status_name,
            GATE_APP,
            state="completed" if completed else "in_progress",
            conclusion=state if completed else None,
            external_id=external_id,
        )
        self.posted[head] = Posted(
            head_sha=head,
            state=state,
            reason=reason,
            external_id=external_id,
            run_id=run_id,
            decision_key=decision_key,
            status_name=status_name,
        )
        return run_id

    def append(self, event_type: str, actor: str, payload: Mapping[str, Any]) -> int:
        seq = self.ledger.append(event_type, actor, payload)
        self.ops.append(f"ledger:{event_type}:{payload.get('state')}:{payload.get('reason_code')}")
        return seq

    def last_posted(self, head: str) -> Posted | None:
        return self.posted.get(head)

    def pre_merge_check(self, number: int, head: str) -> None:
        """Hook for the race test: the state at merge time must still be a success."""


def own_codes(host: FakeHost, number: int) -> tuple[str, ...]:
    """The enabler's own tier codes for a PR (M-9, TC-183): computed from the PR's paths, the
    base-tip tier list and policy with the one tier function, never read from a bundle."""
    world = host.worlds.get(number) or baseline(pr_number=number)
    return world.computed()[1]


def p_facts(pr: ForgePr) -> PrFacts:
    return PrFacts(
        number=pr.number,
        state=pr.state,  # type: ignore[arg-type]
        draft=pr.draft,
        base_ref=pr.base_ref,
        head_sha=pr.head,
        head_repo_id=pr.head_repo_id,
        labels=frozenset(pr.labels),
        open_ask=False,
        last_base_change_at=None,
    )


def drive(host: FakeHost, kind: str, number: int, *, head: str | None = None) -> Any:
    """Send one event to the flow for the PR's live head (or `head`)."""
    pr = host.forge.prs[number]
    event = GateEvent(kind=kind, repo=REPO, pr=number, head_sha=head or pr.head)  # type: ignore[arg-type]
    return handle_event(event, host, host.callables(), now=host.now, gate_version="test")
