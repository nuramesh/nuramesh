"""Merge Gate signer limits (spec SV-8 d, SV-10, SV-11; cases S-05, S-06, S-14, S-17, S-18,
S-21, S-23): a pure evaluator over what the ledger already holds (no table of its own, plan D-6).
All data here is obviously fake (repeated characters, small numbers)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nuramesh.core import vm_limits
from nuramesh.core.vm_events import GATE_ACTOR, SIGNER_ACTOR
from nuramesh.core.vm_formats import ResetPayload
from nuramesh.core.vm_limits import (
    Decision,
    History,
    LedgerEntry,
    LedgerLimits,
    LimitConfig,
    RebindAsk,
    Run,
    check_rebind,
    check_run,
    history_from_ledger,
)
from nuramesh.core.vm_signer import LIMIT_CODES, ReviewRequest
from nuramesh.core.vm_verdicts import ResetRecord

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
ORG = "org-example"
REPO = "github.com/acme/widgets"
OTHER_REPO = "github.com/acme/gadgets"
TASK = "issue:5"
X, Y, Z = "c" * 64, "d" * 64, "e" * 64


def ago(**delta: int) -> datetime:
    return NOW - timedelta(**delta)


def run(seq: int, pr: int = 7, repo: str = REPO, open_: bool = False, **delta: int) -> Run:
    return Run(seq=seq, at=ago(**(delta or {"minutes": 10})), repo=repo, pr=pr, open=open_)


def decision(
    seq: int,
    verdict: str = "ACCEPT",
    pr: int = 7,
    change: str = X,
    task: str = TASK,
    repo: str = REPO,
    **delta: int,
) -> Decision:
    return Decision(
        seq=seq,
        at=ago(**(delta or {"days": 1})),
        repo=repo,
        pr=pr,
        change_hash=change,
        task_key=task,
        decision=verdict,
    )


def ask(
    history: History,
    pr: int = 7,
    task: str = TASK,
    resets: tuple[ResetRecord, ...] = (),
    config: LimitConfig | None = None,
) -> str | None:
    return check_run(
        history,
        repo=REPO,
        pr=pr,
        task_key=task,
        now=NOW,
        resets=resets,
        config=config or LimitConfig(),
    )


def test_defaults_are_the_spec_numbers() -> None:
    c = LimitConfig()
    assert (c.repo_runs_per_hour, c.pr_runs_per_hour) == (6, 3)
    assert (c.task_rejects, c.task_decisions, c.task_window_days) == (2, 4, 30)
    assert c.rebinds_per_pr_per_hour == 6


def test_nothing_recorded_means_proceed() -> None:
    assert ask(History()) is None
    assert check_rebind(History(), repo=REPO, pr=7, now=NOW, config=LimitConfig()) is None


def test_repo_rate_s05_six_runs_an_hour_then_refused() -> None:
    runs = tuple(run(i, pr=10 + i) for i in range(6))
    assert ask(History(runs=runs[:5])) is None
    assert ask(History(runs=runs)) == "rate_limited"
    # another repository's runs do not count; neither does a run exactly one hour old
    other = tuple(run(i, pr=10 + i, repo=OTHER_REPO) for i in range(6))
    assert ask(History(runs=other)) is None
    aged = (*runs[:5], run(9, pr=30, minutes=60))
    assert ask(History(runs=aged)) is None
    assert ask(History(runs=(*runs[:5], run(9, pr=30, minutes=59)))) == "rate_limited"


def test_pr_rate_s21_three_runs_an_hour_per_pr_while_the_repository_has_room() -> None:
    runs = (run(1), run(2), run(3), run(4, pr=8))  # the repository total is 4 of 6
    assert ask(History(runs=runs)) == "rate_limited"
    assert ask(History(runs=runs), pr=8) is None
    assert ask(History(runs=runs[:2])) is None


def test_run_in_flight_s14_and_a_stale_run_stops_blocking() -> None:
    assert ask(History(runs=(run(1, open_=True, minutes=5),))) == "run_in_flight"
    assert ask(History(runs=(run(1, open_=True, minutes=5),)), pr=8) is None
    config = LimitConfig(run_timeout_seconds=600)
    stale = History(runs=(run(1, open_=True, minutes=11),))
    assert ask(stale, config=config) is None
    assert ask(History(runs=(run(1, open_=True, minutes=9),)), config=config) == "run_in_flight"


def test_reject_limit_s06_s17_two_rejects_for_the_task_across_prs_and_change_hashes() -> None:
    rejects = (decision(1, "REJECT", pr=7, change=X), decision(2, "REJECT", pr=8, change=Y))
    assert ask(History(decisions=rejects), pr=9) == "reject_limit"
    assert ask(History(decisions=rejects[:1]), pr=9) is None
    # another task, or the same task key in another repository, has its own count
    assert ask(History(decisions=rejects), pr=9, task="issue:6") is None
    elsewhere = tuple(decision(i, "REJECT", repo=OTHER_REPO) for i in (1, 2))
    assert ask(History(decisions=elsewhere), pr=9) is None
    # the reject count has no time window: only a human reset clears it
    old = tuple(decision(i, "REJECT", change=c, days=200) for i, c in ((1, X), (2, Y)))
    assert ask(History(decisions=old), pr=9) == "reject_limit"


def test_task_ask_limit_s18_the_fifth_well_formed_decision_in_thirty_days() -> None:
    four = (
        decision(1, "ACCEPT", pr=1, change=X),
        decision(2, "REJECT", pr=2, change=Y),
        decision(3, "ACCEPT", pr=3, change=Z),
        decision(4, "ACCEPT", pr=4, change="f" * 64),
    )
    assert ask(History(decisions=four), pr=9) == "task_ask_limit"
    assert ask(History(decisions=four[:3]), pr=9) is None
    aged = (decision(1, "ACCEPT", pr=1, days=30), *four[1:])
    assert ask(History(decisions=aged), pr=9) is None  # exactly 30 days old is outside


def test_a_human_reset_voids_the_counters_by_ledger_order() -> None:
    rejects = (decision(10, "REJECT", pr=7, change=X), decision(20, "REJECT", pr=8, change=Y))
    history = History(decisions=rejects)
    assert ask(history, pr=9, resets=(ResetRecord(21, "task", TASK),)) is None
    assert ask(history, pr=9, resets=(ResetRecord(15, "task", TASK),)) is None  # one REJECT left
    assert ask(history, pr=9, resets=(ResetRecord(5, "task", TASK),)) == "reject_limit"
    assert ask(history, pr=9, resets=(ResetRecord(21, "task", "issue:6"),)) == "reject_limit"
    # a change-hash reset voids that change's record only; a mode flip voids everything before it
    assert ask(history, pr=9, resets=(ResetRecord(21, "change_hash", X),)) is None
    assert ask(history, pr=9, resets=(ResetRecord(21, "change_hash", Z),)) == "reject_limit"
    assert ask(history, pr=9, resets=(ResetRecord(21, "mode_flip", None),)) is None


def test_issue_less_requests_are_keyed_by_the_pr_author_never_shared_per_repository() -> None:
    """SV-10: with no linked issue the key is the PR author's id (the signer resolves it; the
    limits always get a key)."""
    a, b = "agent:worker-a", "agent:worker-b"
    rejects = (decision(1, "REJECT", task=a), decision(2, "REJECT", task=a, change=Y))
    assert ask(History(decisions=rejects), pr=9, task=a) == "reject_limit"
    assert ask(History(decisions=rejects), pr=9, task=b) is None
    assert ask(History(decisions=rejects), pr=9, task=TASK) is None


def test_a_decision_entry_without_a_task_key_is_malformed() -> None:
    for payload in ({}, {"task_key": None}, {"task_key": ""}):
        broken = entry(
            1,
            "merge.verdict_issued",
            change_hash=X,
            decision="REJECT",
            rebound_from=None,
            **payload,
        )
        with pytest.raises(ValueError, match="^limits: a merge entry is malformed$"):
            history_from_ledger([broken], ORG)


def test_a_timed_out_run_still_counts_toward_the_hourly_limits() -> None:
    """`run_timeout_seconds` (1800, not in the spec) only stops an abandoned run from being
    `run_in_flight` for ever; the run was admitted, so it stays in the per-PR and the
    per-repository hourly counts (SV-10) until it is an hour old."""
    assert LimitConfig().run_timeout_seconds == 1800
    stale = tuple(run(i, open_=True, minutes=31 + i) for i in (1, 2, 3))
    assert ask(History(runs=stale)) == "rate_limited"  # 3 per PR per hour, not `run_in_flight`
    assert ask(History(runs=stale[:2])) is None  # two timed-out runs: the third may start
    assert ask(History(runs=stale), pr=8) is None
    repo_wide = tuple(run(i, pr=10 + i, open_=True, minutes=31 + i) for i in range(6))
    assert ask(History(runs=repo_wide), pr=99) == "rate_limited"  # 6 per repository per hour
    assert ask(History(runs=repo_wide[:5]), pr=99) is None
    # exactly at the timeout the run no longer blocks, and it still counts until the hour is over
    edge = (run(1, open_=True, minutes=30), run(2, minutes=40), run(3, minutes=50))
    assert ask(History(runs=edge)) == "rate_limited"
    assert ask(History(runs=(run(1, open_=True, minutes=29),))) == "run_in_flight"
    aged = (run(1, open_=True, minutes=60), run(2, minutes=40), run(3, minutes=50))
    assert ask(History(runs=aged)) is None


def test_a_timed_out_run_read_from_the_ledger_counts_too() -> None:
    entries = [
        entry(i, "merge.review_requested", minutes=31 + i, task_key=TASK, change_hash=X)
        for i in (1, 2, 3)
    ]
    limits = LedgerLimits(org_id=ORG, entries=lambda: entries, resets=lambda: (), clock=lambda: NOW)
    req = ReviewRequest(ORG, REPO, "123456", 7, "1" * 40, GATE_ACTOR)
    assert [r.open for r in history_from_ledger(entries, ORG).runs] == [True, True, True]
    assert limits.check_run(req, TASK) == "rate_limited"
    assert limits.check_run(ReviewRequest(ORG, REPO, "123456", 8, "1" * 40, GATE_ACTOR), TASK) is (
        None
    )


def test_order_permanent_limits_first_then_in_flight_then_rate() -> None:
    rejects = (decision(1, "REJECT"), decision(2, "REJECT", change=Y))
    busy = (run(3, open_=True), run(4), run(5))
    assert ask(History(runs=busy, decisions=rejects)) == "reject_limit"
    assert ask(History(runs=busy)) == "run_in_flight"
    assert ask(History(runs=(run(3), run(4), run(5)))) == "rate_limited"


def test_rebind_rate_s23_six_an_hour_per_pr_and_never_in_the_run_or_task_counts() -> None:
    asks = tuple(RebindAsk(seq=i, at=ago(minutes=5 + i), repo=REPO, pr=7) for i in range(6))
    config = LimitConfig()
    assert check_rebind(History(rebinds=asks), repo=REPO, pr=7, now=NOW, config=config) == (
        "rate_limited"
    )
    assert check_rebind(History(rebinds=asks[:5]), repo=REPO, pr=7, now=NOW, config=config) is None
    assert check_rebind(History(rebinds=asks), repo=REPO, pr=8, now=NOW, config=config) is None
    assert ask(History(rebinds=asks)) is None  # six rebinds do not use up signer runs


# --- the history is read from the ledger entries the signer writes --------------------------------


def entry(seq: int, kind: str, minutes: int = 10, **payload: Any) -> LedgerEntry:
    base = {"org_id": ORG, "repo": REPO, "repo_id": "123456", "pr": 7, "head_sha": "1" * 40}
    base.update(payload)
    return LedgerEntry(seq=seq, at=ago(minutes=minutes), type=kind, payload=base)


def test_history_from_ledger_pairs_runs_with_their_end_and_classifies_entries() -> None:
    entries = [
        entry(1, "merge.review_requested", task_key=TASK, change_hash=X),
        entry(2, "merge.review_failed", reason="malformed_proposal", detail=None),
        entry(3, "merge.review_requested", task_key=TASK, change_hash=X),
        entry(
            4,
            "merge.verdict_issued",
            change_hash=X,
            decision="REJECT",
            rebound_from=None,
            task_key=TASK,
        ),
        entry(5, "merge.review_requested", pr=8, task_key=TASK, change_hash=Y),
        entry(6, "merge.review_failed", reason="rate_limited", detail=None),  # no run behind it
        entry(7, "merge.review_failed", reason="head_moved", detail="before_review", pr=8),
        entry(
            8,
            "merge.verdict_issued",
            change_hash=X,
            decision="ACCEPT",
            rebound_from="0" * 32,
            task_key=TASK,
        ),
        entry(9, "merge.review_failed", reason="rebind_refused", detail="tree_mismatch"),
        entry(10, "merge.gate_decided", state="success"),
        entry(11, "run.started", anything="else"),
    ]
    history = history_from_ledger(entries, ORG)
    runs = [(r.seq, r.pr, r.open) for r in history.runs]
    assert runs == [(1, 7, False), (3, 7, False), (5, 8, True)]
    assert [(d.seq, d.decision, d.task_key, d.change_hash) for d in history.decisions] == [
        (4, "REJECT", TASK, X)
    ]
    assert [a.seq for a in history.rebinds] == [8, 9]


@pytest.mark.parametrize(
    ("reason", "detail", "closes"),
    [
        ("malformed_proposal", None, True),
        ("model_failed", None, True),
        ("budget_exceeded", None, True),
        ("head_moved", "during_review", True),
        ("head_moved", "before_review", False),
        ("org_mismatch", None, False),
        ("requester_forbidden", None, False),
        ("diff_too_large", None, False),
        ("run_in_flight", None, False),
        ("reject_limit", None, False),
    ],
)
def test_only_a_refusal_after_the_request_ends_a_run(
    reason: str, detail: str | None, closes: bool
) -> None:
    entries = [
        entry(1, "merge.review_requested", task_key=TASK, change_hash=X),
        entry(2, "merge.review_failed", reason=reason, detail=detail),
    ]
    assert history_from_ledger(entries, ORG).runs[0].open is (not closes)


def test_history_ignores_other_orgs_and_fails_closed_on_a_malformed_merge_entry() -> None:
    foreign = entry(1, "merge.review_requested", org_id="org-other", task_key=TASK, change_hash=X)
    assert history_from_ledger([foreign], ORG) == History()
    for broken in (
        LedgerEntry(1, NOW, "merge.review_requested", {"org_id": ORG, "repo": REPO}),
        entry(1, "merge.review_requested", pr="7", task_key=TASK, change_hash=X),
        entry(1, "merge.verdict_issued", change_hash=X, decision="MAYBE", rebound_from=None),
        entry(1, "merge.review_failed", detail=None),
    ):
        with pytest.raises(ValueError, match="^limits: a merge entry is malformed$"):
            history_from_ledger([broken], ORG)
    out_of_order = [
        entry(2, "merge.review_requested", task_key=TASK, change_hash=X),
        entry(1, "merge.review_requested", task_key=TASK, change_hash=X),
    ]
    with pytest.raises(ValueError, match="^limits: ledger entries are not in sequence order$"):
        history_from_ledger(out_of_order, ORG)


def test_ledger_limits_is_the_signers_limits_port() -> None:
    entries: list[LedgerEntry] = [
        entry(i, "merge.review_requested", task_key=TASK, change_hash=X, pr=20 + i)
        for i in range(1, 7)
    ]
    resets: list[ResetRecord] = []
    limits = LedgerLimits(
        org_id=ORG, entries=lambda: entries, resets=lambda: resets, clock=lambda: NOW
    )
    req = ReviewRequest(ORG, REPO, "123456", 7, "1" * 40, "service:nuramesh-gate")
    assert limits.check_run(req, TASK) == "rate_limited"
    assert limits.check_run(req, TASK) in LIMIT_CODES
    assert limits.check_rebind(req) is None
    del entries[3:]
    assert limits.check_run(req, TASK) is None  # three runs left, none of them for PR 7
    assert limits.check_run(ReviewRequest(ORG, REPO, "123456", 21, "1" * 40, "x"), TASK) == (
        "run_in_flight"
    )
    entries.clear()
    assert limits.check_run(req, TASK) is None


# --- resets: only an admin-signed `merge.review_reset` becomes a ResetRecord ----------------------

ADMIN = "human:admin-1"


def reset_payload(
    kind: str = "task",
    value: str | None = TASK,
    approver: str = ADMIN,
    org: str = ORG,
    reset_id: str = "a" * 32,
) -> ResetPayload:
    return ResetPayload.model_validate(
        {
            "version": "vm-reset-1",
            "org_id": org,
            "repo": REPO,
            "repo_id": "123456",
            "scope": {"kind": kind, "value": value},
            "reason_sha256": "b" * 64,
            "approver_id": approver,
            "key_id": "1" * 16,
            "reset_id": reset_id,
            "issued_at": "2026-01-02T11:00:00.000000Z",
        }
    )


def reset_entry(seq: int, actor: str, signed: ResetPayload | None) -> LedgerEntry:
    """A `merge.review_reset` entry; `signed` stands for what the admin-signature check (VM-3's
    port) finds in it: the verified 6.8 payload, or None."""
    return LedgerEntry(
        seq=seq,
        at=ago(minutes=5),
        type="merge.review_reset",
        payload={"org_id": ORG, "signed": signed},
        actor=actor,
    )


def read_resets(*entries: LedgerEntry) -> tuple[ResetRecord, ...]:
    return vm_limits.resets_from_ledger(entries, ORG, admin_signed=lambda e: e.payload["signed"])


def test_an_admin_signed_reset_entry_becomes_a_reset_record() -> None:
    records = read_resets(
        reset_entry(21, ADMIN, reset_payload()),
        reset_entry(22, ADMIN, reset_payload("change_hash", X, reset_id="c" * 32)),
        reset_entry(23, ADMIN, reset_payload("mode_flip", None, reset_id="d" * 32)),
        entry(24, "merge.review_requested", task_key=TASK, change_hash=X),
    )
    assert records == (
        ResetRecord(21, "task", TASK),
        ResetRecord(22, "change_hash", X),
        ResetRecord(23, "mode_flip", None),
    )


@pytest.mark.parametrize(
    "actor",
    [
        "agent:worker-1",
        SIGNER_ACTOR,
        GATE_ACTOR,
        "service:anything",
        "unmapped:unavailable",
        "human:",
        "human:someone@example.com",
        "",
    ],
)
def test_a_reset_entry_from_a_non_human_actor_never_becomes_a_reset_record(actor: str) -> None:
    """F-8: `merge.review_reset` is a signed human act. Whatever the entry carries (here even a
    payload the signature port would accept), an entry appended by an agent or a service is not
    a reset."""
    for kind, value in (("task", TASK), ("change_hash", X), ("mode_flip", None)):
        assert read_resets(reset_entry(21, actor, reset_payload(kind, value))) == ()


def test_a_human_entry_without_an_admin_signature_never_becomes_a_reset_record() -> None:
    # the port found no valid admin signature (not an admin's key, a bad signature, no signature)
    assert read_resets(reset_entry(21, "human:author-1", None)) == ()
    assert read_resets(reset_entry(21, ADMIN, None)) == ()
    # an admin's signed reset re-appended by somebody else (a replay at a higher sequence)
    assert read_resets(reset_entry(21, "human:author-1", reset_payload("mode_flip", None))) == ()
    # an admin's own reset counts once: the same reset id again is a replay, not a new reset
    twice = read_resets(
        reset_entry(21, ADMIN, reset_payload("mode_flip", None)),
        reset_entry(40, ADMIN, reset_payload("mode_flip", None)),
    )
    assert twice == (ResetRecord(21, "mode_flip", None),)
    # a reset signed for another org is not this org's
    assert read_resets(reset_entry(21, ADMIN, reset_payload(org="org-other"))) == ()


def test_a_non_admin_mode_flip_voids_nothing() -> None:
    """A `mode_flip` reset voids every decision of the org, so it is the entry worth forging:
    two REJECTs stay counted unless the reset entry is an admin's signed one."""
    rejects = History(decisions=(decision(10, "REJECT"), decision(20, "REJECT", change=Y)))
    flip = reset_payload("mode_flip", None)
    for actor, signed in (
        ("agent:worker-1", flip),
        (SIGNER_ACTOR, flip),
        ("human:author-1", None),
        ("human:author-1", flip),
    ):
        forged = read_resets(reset_entry(21, actor, signed))
        assert forged == ()
        assert ask(rejects, pr=9, resets=forged) == "reject_limit"
    real = read_resets(reset_entry(21, ADMIN, flip))
    assert ask(rejects, pr=9, resets=real) is None


def test_resets_fail_closed_and_loud() -> None:
    def broken(_: LedgerEntry) -> ResetPayload | None:
        raise ConnectionError("enrollment store unreachable")

    with pytest.raises(ConnectionError):
        vm_limits.resets_from_ledger([reset_entry(21, ADMIN, None)], ORG, admin_signed=broken)
    with pytest.raises(ValueError, match="^limits: a merge entry is malformed$"):
        vm_limits.resets_from_ledger(
            [reset_entry(21, ADMIN, None)],
            ORG,
            admin_signed=lambda e: "yes",  # type: ignore[arg-type,return-value]
        )
    with pytest.raises(ValueError, match="^limits: ledger entries are not in sequence order$"):
        read_resets(reset_entry(22, ADMIN, None), reset_entry(21, ADMIN, None))
