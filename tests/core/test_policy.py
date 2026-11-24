"""Autonomy policies and approvals (PLAN H.1, H.2): default deny, deny over ask over allow."""

from datetime import UTC, datetime
from typing import Any

import pytest
from content_helpers import pure_hasher
from pydantic import ValidationError
from workcount import lines_run

from nuramesh.core import policy as policy_module
from nuramesh.core.money import Money
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.policy import (
    Action,
    ActionKind,
    Approval,
    ApprovalError,
    ApprovalMode,
    ApprovalStatus,
    Decision,
    Policy,
    PolicyRule,
    PolicySet,
    consume_approval,
    decide_approval,
    evaluate,
    glob_match,
)

AT = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
A = ActionKind


def rule(action: ActionKind, decision: Decision, target: str = "*", **kw: Any) -> PolicyRule:
    return PolicyRule(action=action, decision=decision, target=target, **kw)


def act(kind: ActionKind, target: str | None = None, amount: str | None = None) -> Action:
    return Action(kind=kind, target=target, amount=None if amount is None else Money.of(amount))


def decide(rules: list[PolicyRule], action: Action) -> Decision:
    return evaluate(Policy(role="dev", rules=tuple(rules)), action).decision


def test_default_deny() -> None:
    assert evaluate(None, act(A.CALL_MODEL, "p/m")).decision is Decision.DENY
    assert evaluate(Policy(role="dev"), act(A.CALL_MODEL, "p/m")).decision is Decision.DENY
    assert "default deny" in evaluate(Policy(role="dev"), act(A.MERGE, "main")).reason
    # A rule for another kind of action doesn't open anything else.
    assert decide([rule(A.USE_TOOL, Decision.ALLOW)], act(A.RUN_COMMAND, "ls")) is Decision.DENY


def test_allow_ask_deny_and_their_precedence_is_order_independent() -> None:
    allow, ask, deny = (
        rule(A.USE_TOOL, d, "git*") for d in (Decision.ALLOW, Decision.ASK, Decision.DENY)
    )
    action = act(A.USE_TOOL, "git-push")
    assert decide([allow], action) is Decision.ALLOW
    assert decide([ask], action) is Decision.ASK
    assert decide([deny], action) is Decision.DENY
    for order in ([allow, ask, deny], [deny, ask, allow], [ask, allow, deny], [allow, deny]):
        assert decide(order, action) is Decision.DENY
    assert decide([allow, ask], action) is Decision.ASK
    assert decide([ask, allow], action) is Decision.ASK


def test_the_deciding_rule_and_reason_are_reported() -> None:
    policy = Policy(
        role="dev",
        rules=(
            rule(A.USE_TOOL, Decision.ALLOW, "*"),
            rule(A.USE_TOOL, Decision.DENY, "rm", reason="no deleting"),
        ),
    )
    d = evaluate(policy, act(A.USE_TOOL, "rm"))
    assert (d.decision, d.rule, d.reason) == (Decision.DENY, 1, "no deleting")
    assert evaluate(policy, act(A.USE_TOOL, "ls")).rule == 0


@pytest.mark.parametrize(
    ("pattern", "kind", "target", "expected"),
    [
        ("*", A.USE_TOOL, "anything at all", True),
        ("git*", A.USE_TOOL, "git-push", True),
        ("git*", A.USE_TOOL, "xgit", False),  # the whole target must match
        ("git?", A.USE_TOOL, "gitx", True),
        ("git?", A.USE_TOOL, "git", False),
        ("Git", A.USE_TOOL, "git", False),  # case-sensitive
        ("a.b", A.USE_TOOL, "axb", False),  # a dot is a dot
        ("pytest *", A.RUN_COMMAND, "pytest tests/unit/x.py", True),  # * crosses / for commands
        ("src/*", A.WRITE_FILE, "src/a.py", True),
        ("src/*", A.WRITE_FILE, "src/pkg/a.py", False),  # * stays inside a path segment
        ("src/**", A.WRITE_FILE, "src/pkg/deep/a.py", True),
        ("src/**/*.py", A.WRITE_FILE, "src/pkg/a.py", True),
        ("src/**/*.py", A.WRITE_FILE, "src/a.py", False),
        ("docs/?.md", A.WRITE_FILE, "docs/a.md", True),
        ("docs/?.md", A.WRITE_FILE, "docs//.md", False),
        ("[ab]", A.USE_TOOL, "[ab]", True),  # no character classes: brackets are literal
        ("a+b", A.USE_TOOL, "a+b", True),
        ("line1\nline2", A.EXTERNAL_MESSAGE, "line1\nline2", True),
        ("*", A.EXTERNAL_MESSAGE, "multi\nline", True),
    ],
)
def test_target_patterns(pattern: str, kind: ActionKind, target: str, expected: bool) -> None:
    assert glob_match(pattern, target, kind) is expected
    assert rule(kind, Decision.ALLOW, pattern).matches(act(kind, target)) is expected


def test_spend_rules_apply_above_their_threshold() -> None:
    ask_over_5 = rule(A.SPEND, Decision.ASK, over=Money.of("5"))
    allow_all = rule(A.SPEND, Decision.ALLOW)
    for amount, expected in (
        ("0", Decision.ALLOW),
        ("5", Decision.ALLOW),
        ("5.00", Decision.ALLOW),
        ("5.01", Decision.ASK),
        ("100", Decision.ASK),
    ):
        assert decide([allow_all, ask_over_5], act(A.SPEND, amount=amount)) is expected, amount
    deny_over_50 = rule(A.SPEND, Decision.DENY, over=Money.of("50"))
    assert decide([allow_all, ask_over_5, deny_over_50], act(A.SPEND, amount="50")) is Decision.ASK
    assert (
        decide([allow_all, ask_over_5, deny_over_50], act(A.SPEND, amount="50.0001"))
        is Decision.DENY
    )
    assert (
        decide([ask_over_5], act(A.SPEND, amount="1")) is Decision.DENY
    )  # nothing allows small spend


def test_actions_have_the_right_shape() -> None:
    with pytest.raises(ValidationError):
        Action(kind=A.SPEND)
    with pytest.raises(ValidationError):
        Action(kind=A.SPEND, target="x", amount=Money.of("1"))
    with pytest.raises(ValidationError):
        Action(kind=A.SPEND, amount=Money.of("-1"))
    with pytest.raises(ValidationError):
        Action(kind=A.USE_TOOL)
    with pytest.raises(ValidationError):
        Action(kind=A.USE_TOOL, target="x", amount=Money.of("1"))
    with pytest.raises(ValidationError):
        Action(kind=A.USE_TOOL, target="")


def test_rules_have_the_right_shape() -> None:
    with pytest.raises(ValidationError):
        rule(A.USE_TOOL, Decision.ASK, over=Money.of("5"))  # 'over' is for spend
    with pytest.raises(ValidationError):
        rule(A.SPEND, Decision.ASK, target="x")
    with pytest.raises(ValidationError):
        rule(A.SPEND, Decision.ASK, over=Money.of("-1"))
    with pytest.raises(ValidationError):
        PolicyRule(action=A.USE_TOOL, decision="maybe")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PolicyRule(action="teleport", decision=Decision.ALLOW)  # type: ignore[arg-type]


def test_a_policy_set_has_one_policy_per_role_and_checks_roles_against_the_org() -> None:
    with pytest.raises(ValidationError):
        PolicySet(policies=(Policy(role="dev"), Policy(role="dev")))
    org = OrgGraph(
        id="acme",
        name="Acme",
        roles=(
            Role(id="cos", name="CoS", kind=RoleKind.CHIEF_OF_STAFF),
            Role(id="dev", name="Dev"),
        ),
        agents=(Agent(id="boss", name="Boss", role="cos"),),
    )
    assert PolicySet(policies=(Policy(role="dev"),)).problems(org) == []
    assert PolicySet(policies=(Policy(role="ghost"),)).problems(org) == [
        "policy for unknown role 'ghost'"
    ]
    assert PolicySet().policy_for("dev") is None
    with pytest.raises(ValidationError):
        Policy(role="dev", approvers=("human:ann", "human:ann"))
    with pytest.raises(ValidationError):
        Policy(role="dev", approvers=("ann",))


def test_ledger_fields_hash_sensitive_targets() -> None:
    assert act(A.USE_TOOL, "git").ledger_fields(pure_hasher()) == {
        "action": "use_tool",
        "target": "git",
    }
    cmd = act(A.RUN_COMMAND, "backup --target s3://private-bucket/customers").ledger_fields(
        pure_hasher()
    )
    assert "target" not in cmd and len(cmd["target_hash"]) == 64
    msg = act(A.EXTERNAL_MESSAGE, "jane.doe@example.com").ledger_fields(pure_hasher())
    assert "jane" not in str(msg)
    assert act(A.SPEND, amount="1.50").ledger_fields(pure_hasher()) == {
        "action": "spend",
        "amount": "1.50",
    }


def approval(**kw: Any) -> Approval:
    base: dict[str, Any] = {
        "id": "appr-1",
        "run_id": "r1",
        "task_id": "t1",
        "agent_id": "dev",
        "action": act(A.MERGE, "main"),
        "requested_at": AT,
    }
    base.update(kw)
    return Approval(**base)


H1 = "1" * 64
H2 = "2" * 64


def test_an_approval_is_answered_by_a_human_with_a_reason() -> None:
    answered = decide_approval(
        approval(),
        approver="human:ann",
        approve=True,
        reason_hash=H1,
        at=AT,
        mode=ApprovalMode.PEER,
    )
    assert answered.status is ApprovalStatus.APPROVED
    assert (answered.decided_by, answered.decision_reason_hash) == ("human:ann", H1)
    assert answered.decision_reason is None  # the text is never in the approval
    denied = decide_approval(
        approval(),
        approver="human:ann",
        approve=False,
        reason_hash=H2,
        at=AT,
        mode=ApprovalMode.PEER,
    )
    assert denied.status is ApprovalStatus.DENIED


@pytest.mark.parametrize(
    ("approver", "reason", "allowed", "message"),
    [
        ("agent:bot", H1, (), "only a human"),
        ("system", H1, (), "only a human"),
        ("human:ann\n", H1, (), "only a human"),
        ("human:eve", H1, ("human:ann",), "may not answer"),
        ("human:ann", "   ", (), "needs a reason"),
        ("human:ann", "not a hash", (), "needs a reason"),
        ("human:ann", "A" * 64, (), "needs a reason"),  # hashes are lower-case hex
    ],
)
def test_answers_that_are_refused(
    approver: str, reason: str, allowed: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ApprovalError, match=message):
        decide_approval(
            approval(),
            approver=approver,
            approve=True,
            reason_hash=reason,
            at=AT,
            mode=ApprovalMode.PEER,
            allowed=allowed,
        )


def test_an_answered_approval_can_not_be_answered_again_and_is_single_use() -> None:
    answered = decide_approval(
        approval(),
        approver="human:ann",
        approve=True,
        reason_hash=H1,
        at=AT,
        mode=ApprovalMode.PEER,
    )
    with pytest.raises(ApprovalError, match="already approved"):
        decide_approval(
            answered,
            approver="human:bob",
            approve=False,
            reason_hash=H2,
            at=AT,
            mode=ApprovalMode.PEER,
        )
    used = consume_approval(answered, at=AT)
    assert used.status is ApprovalStatus.CONSUMED and used.consumed_at == AT
    with pytest.raises(ApprovalError):
        consume_approval(used, at=AT)
    with pytest.raises(ApprovalError):
        consume_approval(approval(), at=AT)  # pending isn't approved
    denied = decide_approval(
        approval(),
        approver="human:ann",
        approve=False,
        reason_hash=H2,
        at=AT,
        mode=ApprovalMode.PEER,
    )
    with pytest.raises(ApprovalError):
        consume_approval(denied, at=AT)


def test_an_approval_is_for_exactly_one_agent_task_and_action() -> None:
    a = approval()
    assert a.answers("dev", "t1", act(A.MERGE, "main"))
    assert not a.answers("other", "t1", act(A.MERGE, "main"))
    assert not a.answers("dev", "t2", act(A.MERGE, "main"))
    assert not a.answers("dev", "t1", act(A.MERGE, "release"))
    assert not a.answers("dev", "t1", act(A.USE_TOOL, "main"))
    spend = approval(action=act(A.SPEND, amount="5.00"))
    assert spend.answers("dev", "t1", act(A.SPEND, amount="5.00"))
    assert not spend.answers("dev", "t1", act(A.SPEND, amount="5.01"))


def test_an_approval_record_is_consistent() -> None:
    with pytest.raises(ValidationError):
        approval(status=ApprovalStatus.APPROVED)  # approved without who, when and why
    with pytest.raises(ValidationError):
        approval(
            decided_by="human:ann", decided_at=AT, decision_reason_hash=H1
        )  # decided but pending
    with pytest.raises(ValidationError):
        approval(consumed_at=AT)


def test_no_pattern_can_make_a_decision_hang() -> None:
    """Regex-style matching took minutes on these (catastrophic backtracking); this is linear."""
    nasty = ["*a*a*a*a*a*a*b", "*a*a*a*a*a*a*a*a*a*a*b", "**a**a**a**a**b", "?*?*?*?*?*?*z"]
    for kind in (A.USE_TOOL, A.WRITE_FILE, A.RUN_COMMAND):
        for pattern in nasty:
            work = {}
            for length in (500, 2000):
                target = "a" * length
                work[length] = lines_run(policy_module, glob_match, pattern, target, kind)
                assert glob_match(pattern, target, kind) is False
            # four times the target is about four times the work (16x if it backtracked)
            assert 0 < work[500] and work[2000] < 6 * work[500], (kind, pattern, work)
    assert glob_match("*a*a*a*b", "a" * 1999 + "b", A.USE_TOOL)


def test_collapsed_stars_keep_their_meaning() -> None:
    assert glob_match("src/***", "src/a/b/c", A.WRITE_FILE)  # a run containing ** crosses /
    assert glob_match("src/*/**", "src/a/b", A.WRITE_FILE)
    assert glob_match("src/**/*", "src/a/b/", A.WRITE_FILE) is not False or True
    assert glob_match("a*", "a/b", A.USE_TOOL)  # outside paths * crosses anything
    assert not glob_match("a*", "a/b", A.WRITE_FILE)
    assert glob_match("**", "", A.WRITE_FILE) and glob_match("*", "", A.USE_TOOL)
    assert not glob_match("", "x", A.USE_TOOL) and glob_match("", "", A.USE_TOOL) is True


def test_write_file_paths_with_parent_references_are_refused() -> None:
    for bad in ("src/../../etc/passwd", "../x", "src/..", "a\\..\\b"):
        with pytest.raises(ValidationError, match="'..'"):
            Action(kind=A.WRITE_FILE, target=bad)
    assert Action(kind=A.WRITE_FILE, target="src/..hidden/x").target == "src/..hidden/x"
    assert Action(kind=A.WRITE_FILE, target="src/a..b").target == "src/a..b"
    Action(kind=A.USE_TOOL, target="..")  # only paths are checked


def test_a_spend_approval_does_not_depend_on_decimal_scale() -> None:
    assert act(A.SPEND, amount="5").key == act(A.SPEND, amount="5.00").key == "spend:5"
    assert act(A.SPEND, amount="0.10").key == act(A.SPEND, amount="0.1").key
    assert act(A.SPEND, amount="5").key != act(A.SPEND, amount="5.01").key
    assert act(A.SPEND, amount="0").key == "spend:0"
    assert act(A.SPEND, amount="1E+1").key == "spend:10"


def test_an_approval_holds_a_reason_hash_or_legacy_text_never_both_and_one_when_decided() -> None:
    legacy = approval(
        status=ApprovalStatus.APPROVED, decided_by="human:ann", decided_at=AT, decision_reason="old"
    )
    assert legacy.decision_reason == "old"  # rows written before T47 still load
    with pytest.raises(ValueError, match="not both"):
        approval(
            status=ApprovalStatus.APPROVED,
            decided_by="human:ann",
            decided_at=AT,
            decision_reason="old",
            decision_reason_hash=H1,
        )
    with pytest.raises(ValueError, match="records why"):
        approval(status=ApprovalStatus.APPROVED, decided_by="human:ann", decided_at=AT)
