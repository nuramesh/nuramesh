"""Pins the policy helpers that mutation testing showed no other test notices: command matching
(allow versus refusal), pattern validation, glob compilation, protected paths and the approval
consistency rules. Expected values come from the module docstrings and the RUN_COMMAND spec."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core import policy as pm
from nuramesh.core.policy import (
    CONTROL_CHARACTERS,
    MAX_PATTERN_WORDS,
    MAX_POLICY_PATTERN_WORDS,
    MAX_TARGET,
    Action,
    ActionKind,
    Approval,
    ApprovalStatus,
    Decision,
    Policy,
    PolicyRule,
    PolicySet,
    command_matches,
    command_pattern_problem,
    command_text,
    command_words,
    evaluate,
    glob_match,
    has_control_character,
    is_canonical_write_target,
    is_protected_path,
    is_simple_command,
)

ALLOW, ASK, DENY = Decision.ALLOW, Decision.ASK, Decision.DENY
RUN, WRITE = ActionKind.RUN_COMMAND, ActionKind.WRITE_FILE


# --- constants -------------------------------------------------------------------------------


def test_limits_and_control_characters() -> None:
    assert (MAX_TARGET, MAX_PATTERN_WORDS, MAX_POLICY_PATTERN_WORDS) == (2_000, 64, 1_000)
    # every control character but the tab is compound; the tab is a word separator
    for code in [*range(0x00, 0x09), *range(0x0A, 0x20), 0x7F]:
        assert chr(code) in CONTROL_CHARACTERS
        assert has_control_character(f"a{chr(code)}b")
    assert chr(0x09) not in CONTROL_CHARACTERS
    assert chr(0x20) not in CONTROL_CHARACTERS
    assert not has_control_character("a\tb")
    assert CONTROL_CHARACTERS >= set(";|&`()<>")


def test_rule_and_policy_size_bounds() -> None:
    PolicyRule(action=ActionKind.USE_TOOL, decision=ALLOW, reason="r" * 500)
    with pytest.raises(ValidationError):
        PolicyRule(action=ActionKind.USE_TOOL, decision=ALLOW, reason="r" * 501)
    rule = PolicyRule(action=ActionKind.USE_TOOL, decision=ALLOW)
    Policy(role="dev", rules=(rule,) * 500)
    with pytest.raises(ValidationError):
        Policy(role="dev", rules=(rule,) * 501)
    with pytest.raises(ValidationError):
        Approval(**_approval(decision_reason="x" * 1_001, status=ApprovalStatus.APPROVED))


def test_policy_pattern_word_budget() -> None:
    def rule(n: int) -> PolicyRule:
        return PolicyRule(action=RUN, decision=DENY, target=" ".join(["w"] * n))

    # 1000 words in all is allowed, 1001 is not
    Policy(role="dev", rules=tuple(rule(50) for _ in range(20)))
    with pytest.raises(ValidationError, match="1001 words"):
        Policy(role="dev", rules=(*(rule(50) for _ in range(20)), rule(1)))
    # non-command rules are not counted
    other = PolicyRule(action=ActionKind.USE_TOOL, decision=ALLOW)
    Policy(role="dev", rules=(*(rule(50) for _ in range(20)), other))


def test_unparsable_shape_pattern_counts_as_one_word() -> None:
    # `';` has a control character and no parse: it counts as one word, never zero
    assert command_words("';") is None
    lone = PolicyRule(action=RUN, decision=DENY, target="';")
    full = PolicyRule(action=RUN, decision=DENY, target=" ".join(["w"] * 50))
    Policy(role="dev", rules=(*(full,) * 19, *(lone,) * 50))  # 950 + 50 = 1000
    with pytest.raises(ValidationError, match="1001 words"):
        Policy(role="dev", rules=(*(full,) * 19, *(lone,) * 51))


# --- command_words / text --------------------------------------------------------------------


def test_command_words_and_text() -> None:
    assert command_words("a 'b c' d") == ("a", "b c", "d")
    assert command_words("a 'b") is None
    assert command_words("a\\") is None
    assert command_words("   ") is None
    assert command_words("") is None
    argv = ["echo", "hello world", "it's"]
    text = command_text(argv)
    assert command_words(text) == tuple(argv)
    assert command_text(("ls", "-l")) == "ls -l"


def test_is_simple_command() -> None:
    assert is_simple_command("ls -l")
    assert is_simple_command("ls\t-l")
    assert not is_simple_command("ls; rm x")  # control character, well formed
    assert not is_simple_command("ls 'x")  # no control character, malformed
    assert not is_simple_command("")  # no words
    assert not is_simple_command("a\nb")


# --- command_pattern_problem -----------------------------------------------------------------


def test_command_pattern_problem() -> None:
    assert command_pattern_problem("git status", ALLOW) is None
    assert command_pattern_problem("git status", DENY) is None
    # an allow shape pattern with a wildcard could never match
    for wildcard in ("a;*", "a;?", "a;b*c", "a|b?"):
        message = command_pattern_problem(wildcard, ALLOW)
        assert message is not None and "exact command" in message
        assert command_pattern_problem(wildcard, DENY) is None
        assert command_pattern_problem(wildcard, ASK) is None
    # an allow shape pattern with no wildcard is an exact text
    assert command_pattern_problem("a;b", ALLOW) is None
    # a wildcard without a control character is fine for an allow
    assert command_pattern_problem("git *", ALLOW) is None
    assert command_pattern_problem("git ?", ALLOW) is None
    for bad in ("echo 'x", "echo x\\", "   "):
        message = command_pattern_problem(bad, ALLOW)
        assert message is not None and "unclosed quote" in message
        assert command_pattern_problem(bad, DENY) == message
    assert command_pattern_problem(" ".join(["w"] * 64), DENY) is None
    too_long = command_pattern_problem(" ".join(["w"] * 65), DENY)
    assert too_long == "the pattern has more than 64 words"


# --- command_matches: allow ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("git status", "git status", True),
        ("git status", "git  status", True),  # extra blanks are not words
        ("git status", "git status -s", False),
        ("git status", "git", False),
        ("git *", "git status", True),
        ("git *", "git", False),  # a `*` word needs a word
        ("git *", "git a b", False),  # `*` is one word
        ("git **", "git", True),  # zero or more further words
        ("git **", "git a b c", True),
        ("git **", "svn a", False),
        ("git **", "gi", False),
        ("a b **", "a b", True),
        ("a b **", "a", False),
        ("a b **", "a c d", False),
        ("a b **", "a b c", True),
        ("**", "anything at all", True),  # a lone `**` has an empty head
        ("*", "anything at all", True),
        ("*", "x; y", False),  # `*` alone does not allow a compound command, see below
        ("ls", "'ls'", True),
        ("ls", "ls x", False),
        ("rm?", "rmx", True),
        ("rm?", "rm", False),
    ],
)
def test_allow_matching(pattern: str, text: str, expected: bool) -> None:
    assert command_matches(pattern, text, ALLOW) is expected


def test_allow_of_a_compound_command_is_by_exact_text_only() -> None:
    assert command_matches("a; b", "a; b", ALLOW)
    assert not command_matches("a; b", "a;  b", ALLOW)
    assert not command_matches("a;*", "a;*", ALLOW)  # a wildcard never matches here
    assert not command_matches("a;?", "a;?", ALLOW)
    assert not command_matches("a; b", "a; c", ALLOW)
    assert not command_matches("*", "a; b", ALLOW)
    assert not command_matches("a", "a; b", ALLOW)
    assert not command_matches("a b", "a 'b", ALLOW)  # malformed text is compound too
    assert command_matches("a 'b", "a 'b", ALLOW)  # only by exact text


def test_allow_pattern_that_is_not_a_command_never_matches_a_simple_one() -> None:
    assert not command_matches("a;b", "a", ALLOW)
    assert not command_matches("a 'b", "a b", ALLOW)
    assert not command_matches("   ", "a", ALLOW)
    assert not command_matches("", "a", ALLOW)


# --- command_matches: refusals ---------------------------------------------------------------


@pytest.mark.parametrize("decision", [DENY, ASK])
@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("*", "anything; at all", True),
        ("*", "", True),
        ("rm -rf", "rm -rf /", True),  # any run of consecutive words
        ("rm -rf", "sudo rm -rf /", True),
        ("rm -rf", "rm -r -f", False),
        ("rm -rf", "rm", False),
        ("rm -rf", "echo hi; rm -rf /", True),  # any segment
        ("rm -rf", "echo hi && rm -rf /", True),
        ("rm -rf", "sh -c 'rm -rf /'", True),  # inside a quoted word
        ("rm -rf", 'sh -c "echo; rm -rf /"', True),
        ("rm -rf", "echo 'rm -rf'", True),
        ("rm -rf", "echo rm; -rf", False),  # the run is cut at the control character
        ("rm **", "rm", True),  # a trailing `**` is already covered
        ("rm **", "x rm y", True),
        ("rm **", "x", False),
        ("**", "x", True),  # a lone `**` stays one word
        ("**", "", False),  # ... which needs a word to match
        ("a b c", "a b", False),
        ("a b", "x a b y", True),
        ("a ?", "a b", True),
        ("a ?", "a", False),
        ("a;b", "x a;b y", False),  # a shape pattern is a character glob over the whole text
        ("a;b", "a;b", True),
        ("a;*", "a;bcd", True),
        ("a;*", "x a;b", False),  # the whole text must match
        ("a;?", "a;b", True),
        ("a;?", "a;bc", False),
        ("echo 'x", "echo x", False),  # malformed pattern: no match
        ("   ", "echo x", False),
    ],
)
def test_refusal_matching(decision: Decision, pattern: str, text: str, expected: bool) -> None:
    assert command_matches(pattern, text, decision) is expected


def test_refusal_sees_inside_malformed_and_nested_text() -> None:
    assert command_matches("rm -rf", "echo 'unclosed; rm -rf /", DENY)
    assert command_matches("rm -rf", "sh -c 'sh -c \"rm -rf /\"'", DENY)
    assert command_matches("rm", "sh -c 'a\\ b rm'", DENY)
    # a word with an escaped space is its own text and does not loop
    assert command_matches("a\\ b", "x a\\ b", DENY)
    assert command_matches("b", "x a\\ b", DENY)  # the expanded form is searched too
    # a tab inside a quoted word is split as well
    assert command_matches("c d", "sh -c 'c\td'", DENY)
    # a quote left open inside a word with a space is split at blanks only
    assert command_matches("-rf", 'sh -c "x \'rm -rf"', DENY)
    assert not command_matches("rm -rf", 'sh -c "x \'rm -rf"', DENY)  # the quote stays in the word


def test_internal_segments_and_expansion() -> None:
    assert pm._blank_split("a  b\tc") == ("a", "b", "c")
    assert pm._blank_split("  ") == ()
    assert pm._expand(("sh", "-c", "rm -rf /")) == ("sh", "-c", "rm", "-rf", "/")
    assert pm._expand(("a", "b")) == ("a", "b")
    assert pm._expand(("x y\tz",)) == ("x", "y", "z")
    assert pm._expand(("it's a",)) == ("it's", "a")  # malformed inner text: blank split
    assert pm._expand(("'x y'",)) == ("x", "y")  # inner words are expanded again
    assert pm._segments("a b; c") == [("a", "b"), ("c",)]
    assert pm._segments("sh -c 'a b'") == [("sh", "-c", "a b"), ("sh", "-c", "a", "b")]
    assert pm._segments(";;") == []
    assert pm._segments("a 'b") == [("a", "'b")]  # malformed piece: blank split


def test_has_run_and_masks() -> None:
    assert pm._match_mask("a", ("a", "b", "a")) == 0b101
    assert pm._match_mask("*", ("a", "b")) == 0b11
    assert pm._match_mask("z", ("a",)) == 0
    words = ("x", "a", "b", "y")
    assert pm._has_run(("a", "b"), words)
    assert pm._has_run(("x",), words)
    assert pm._has_run(("y",), words)
    assert pm._has_run(("x", "a", "b", "y"), words)
    assert not pm._has_run(("a", "y"), words)
    assert not pm._has_run(("b", "a"), words)
    assert not pm._has_run(("x", "a", "b", "y", "z"), words)  # longer than the words
    assert not pm._has_run(("a", "b", "y", "z"), words)
    assert pm._has_run(("x", "a"), ("x", "a"))  # exactly one start
    assert not pm._has_run(("q",), ())
    assert pm._words_match(("a", "?"), ("a", "b"))
    assert not pm._words_match(("a",), ("a", "b"))
    assert not pm._words_match(("a", "b"), ("a",))
    assert pm._words_match((), ())


# --- glob compilation ------------------------------------------------------------------------


def test_compile_glob_masks() -> None:
    literals, q, s1, s2, accept = pm._compile_glob("a?*b", True)
    assert literals == {"a": 0b0001, "b": 0b1000}
    assert (q, s1, s2, accept) == (0b0010, 0b0100, 0, 0b10000)
    # consecutive stars collapse; `**` anywhere in the run (path only) crosses segments
    assert pm._compile_glob("a**", True)[2:4] == (0, 0b10)
    assert pm._compile_glob("a***", True)[2:4] == (0, 0b10)
    assert pm._compile_glob("a*", True)[2:4] == (0b10, 0)
    assert pm._compile_glob("a*", False)[2:4] == (0, 0b10)  # not a path: a star crosses anything
    assert pm._compile_glob("**", True)[3] == 0b1
    # a repeated letter has its bits ORed
    assert pm._compile_glob("aa", False)[0] == {"a": 0b11}
    assert pm._compile_glob("", False) == ({}, 0, 0, 0, 1)
    # accepting bit sits one past the last token, stars collapsed
    assert pm._compile_glob("a***b", False)[4] == 1 << 3


@pytest.mark.parametrize(
    ("pattern", "target", "kind", "expected"),
    [
        ("src/*", "src/a.py", WRITE, True),
        ("src/*", "src/a/b.py", WRITE, False),  # a single star stays in a segment
        ("src/**", "src/a/b.py", WRITE, True),
        ("src/***", "src/a/b.py", WRITE, True),
        ("src/**", "src/", WRITE, True),
        ("src/**", "src", WRITE, False),
        ("*", "a/b", WRITE, False),
        ("**", "a/b", WRITE, True),
        ("a*b*c", "aXbYc", WRITE, True),
        ("a*b*c", "a/b/c", WRITE, False),
        ("a**/c", "a/b/c", WRITE, True),
        ("a?c", "a/c", WRITE, False),  # `?` never matches a slash in a path
        ("a?c", "abc", WRITE, True),
        ("a*", "a/b", ActionKind.USE_TOOL, True),  # not a path: a star crosses everything
        ("a?c", "a/c", ActionKind.USE_TOOL, True),
        ("", "", WRITE, True),
        ("", "a", WRITE, False),
        ("a*", "a", WRITE, True),  # a star may match nothing
        ("*a", "", WRITE, False),
        ("a**b", "a/x/b", WRITE, True),
        ("a*b", "a/b", WRITE, False),
        ("abc", "ab", WRITE, False),
        ("ab", "abc", WRITE, False),
    ],
)
def test_glob_match(pattern: str, target: str, kind: ActionKind, expected: bool) -> None:
    assert glob_match(pattern, target, kind) is expected


# --- write targets ---------------------------------------------------------------------------


def test_canonical_write_target() -> None:
    assert is_canonical_write_target("src/app.py")
    assert is_canonical_write_target("a" * 2_000)
    assert not is_canonical_write_target("a" * 2_001)
    assert not is_canonical_write_target("")
    assert not is_canonical_write_target("/etc/passwd")
    assert not is_canonical_write_target("a\\b")
    for bad in ("a//b", "./a", "a/./b", "a/..", "../a", "a/", "a/b/"):
        assert not is_canonical_write_target(bad)
    for bad in ("a\nb", "a\x00b", "a\x7fb", "a\x85b", "a b", "a b"):
        assert not is_canonical_write_target(bad)
    assert is_canonical_write_target("a.b/..c/d..")
    assert is_canonical_write_target("a b/é")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("", False),
        (".", False),
        ("./", False),
        ("src/app.py", False),
        ("src/policy/x.py", False),  # only the root directory is protected
        ("src/keys/x.py", False),
        ("README.md", False),
        ("a/b/c.txt", False),
        (".git/config", True),
        ("a/.git/hooks/x", True),
        (".nuramesh/x", True),
        ("a/.NURAMESH/x", True),
        ("a/.nuramesh", True),
        ("Policy/x.py", True),
        ("policies/x", True),
        ("canon/x", True),
        ("budget/x", True),
        ("budgets/x", True),
        ("keys/x", True),
        ("./keys/x", True),
        ("keys", True),
        ("package.json", True),
        ("x.YAML", True),
        ("x.yml", True),
        ("a/package.json", False),  # a root config file only at the root
        ("a/nuramesh.yaml", True),  # a protected name at any depth
        ("a/nuramesh-team.yml", True),
        ("a/policy_x.json", True),
        ("a/budgets.toml", True),
        ("a/canon.yaml", True),
        ("a/policies.json", True),
        ("a/nuramesh.txt", False),
        ("a/nuramesh/x.yaml.bak", False),
        ("a/policyx.yaml", False),  # needs a separator after the stem
        ("a/server.key", True),
        ("a/server.pub", True),
        ("a/x.keys", False),
        ("a\\.git\\x", True),  # backslash is read as a slash
        ("a/./.git/x", True),
        ("a//keys", False),
    ],
)
def test_is_protected_path(target: str, expected: bool) -> None:
    assert is_protected_path(target) is expected


def test_protected_paths_are_denied_even_with_an_allow_rule() -> None:
    policy = Policy(role="dev", rules=(PolicyRule(action=WRITE, decision=ALLOW, target="**"),))
    protected = evaluate(policy, Action(kind=WRITE, target=".git/config"))
    assert protected.decision is DENY and protected.rule is None
    assert "protected" in protected.reason
    assert evaluate(policy, Action(kind=WRITE, target="src/a.py")).decision is ALLOW
    # protection is only for writes
    tool = Policy(role="dev", rules=(PolicyRule(action=ActionKind.USE_TOOL, decision=ALLOW),))
    assert evaluate(tool, Action(kind=ActionKind.USE_TOOL, target=".git/x")).decision is ALLOW


def test_action_rejects_dotdot_and_control_characters_in_paths() -> None:
    with pytest.raises(ValidationError):
        Action(kind=WRITE, target="a/../b")
    with pytest.raises(ValidationError):
        Action(kind=WRITE, target="a\\..\\b")
    with pytest.raises(ValidationError):
        Action(kind=WRITE, target="a\x85b")
    Action(kind=WRITE, target="a/..b")
    Action(kind=ActionKind.USE_TOOL, target="a/../b")
    Action(kind=RUN, target="a" * 2_000)
    with pytest.raises(ValidationError):
        Action(kind=RUN, target="a" * 2_001)


# --- evaluate for run_command ----------------------------------------------------------------


def test_evaluate_run_command_reasons_and_defaults() -> None:
    policy = Policy(role="dev", rules=(PolicyRule(action=RUN, decision=ALLOW, target="ls"),))
    compound = evaluate(policy, Action(kind=RUN, target="ls; id"))
    assert compound.decision is DENY and compound.rule is None
    assert "control character or is malformed" in compound.reason
    plain = evaluate(policy, Action(kind=RUN, target="id"))
    assert plain.decision is DENY and plain.reason == "default deny: no rule matches"
    assert evaluate(None, Action(kind=RUN, target="ls")).reason == (
        "default deny: the role has no policy"
    )
    hit = evaluate(policy, Action(kind=RUN, target="ls"))
    assert (hit.decision, hit.rule, hit.reason) == (ALLOW, 0, "rule 0 says allow")


def test_evaluate_order_deny_then_ask_then_allow() -> None:
    rules = (
        PolicyRule(action=RUN, decision=ALLOW, target="git **"),
        PolicyRule(action=RUN, decision=ASK, target="git push", reason="push asks"),
        PolicyRule(action=RUN, decision=DENY, target="git push --force"),
    )
    policy = Policy(role="dev", rules=rules)
    forced = evaluate(policy, Action(kind=RUN, target="git push --force origin"))
    assert (forced.decision, forced.rule) == (DENY, 2)
    pushed = evaluate(policy, Action(kind=RUN, target="git push origin"))
    assert (pushed.decision, pushed.rule, pushed.reason) == (ASK, 1, "push asks")
    assert evaluate(policy, Action(kind=RUN, target="git log")).rule == 0
    # a refusal inside a compound command still wins; the compound itself is never allowed
    inner = evaluate(policy, Action(kind=RUN, target="git log; git push --force"))
    assert (inner.decision, inner.rule) == (DENY, 2)


def test_rule_matches_other_kinds_and_spend() -> None:
    rule = PolicyRule(action=ActionKind.USE_TOOL, decision=ALLOW, target="a*")
    assert rule.matches(Action(kind=ActionKind.USE_TOOL, target="abc"))
    assert not rule.matches(Action(kind=ActionKind.USE_TOOL, target="b"))
    assert not rule.matches(Action(kind=ActionKind.MERGE, target="abc"))
    run_rule = PolicyRule(action=RUN, decision=ALLOW, target="ls")
    assert not run_rule.matches(Action(kind=ActionKind.USE_TOOL, target="ls"))


def test_policy_set_lookup() -> None:
    first = Policy(role="dev")
    second = Policy(role="qa")
    pset = PolicySet(policies=(first, second))
    assert pset.policy_for("qa") is second
    assert pset.policy_for("dev") is first
    assert pset.policy_for("ops") is None
    with pytest.raises(ValidationError, match="more than one policy"):
        PolicySet(policies=(first, first))
    with pytest.raises(ValidationError, match="twice"):
        Policy(role="dev", approvers=("human:a", "human:a"))
    assert Policy(role="dev", approvers=("human:a", "human:b")).approvers == (
        "human:a",
        "human:b",
    )


# --- approval consistency --------------------------------------------------------------------

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _approval(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ap-1",
        "run_id": "run-1",
        "agent_id": "dev-1",
        "action": Action(kind=ActionKind.USE_TOOL, target="t"),
        "requested_at": T0,
    }
    base.update(over)
    return base


def test_approval_consistency() -> None:
    Approval(**_approval())
    decided = {
        "status": ApprovalStatus.APPROVED,
        "decided_by": "human:a",
        "decided_at": T0,
        "decision_reason": "fine",
    }
    Approval(**_approval(**decided))
    # decided needs both who and when
    with pytest.raises(ValidationError, match="who and when"):
        Approval(**_approval(**{**decided, "decided_by": None}))
    with pytest.raises(ValidationError, match="who and when"):
        Approval(**_approval(**{**decided, "decided_at": None}))
    # a pending one has neither
    with pytest.raises(ValidationError, match="who and when"):
        Approval(**_approval(decided_by="human:a", decided_at=T0))
    # (half a record on a pending approval is tolerated: only both together count as decided)
    assert Approval(**_approval(decided_by="human:a")).status is ApprovalStatus.PENDING
    assert Approval(**_approval(decided_at=T0)).status is ApprovalStatus.PENDING
    # a reason is required, blank text is not a reason, a hash is
    with pytest.raises(ValidationError, match="why"):
        Approval(**_approval(**{**decided, "decision_reason": "  "}))
    with pytest.raises(ValidationError, match="why"):
        Approval(**_approval(**{**decided, "decision_reason": None}))
    Approval(**_approval(**{**decided, "decision_reason": None, "decision_reason_hash": "a" * 64}))
    # a hash and legacy text are exclusive
    with pytest.raises(ValidationError, match="not both"):
        Approval(**_approval(**{**decided, "decision_reason_hash": "a" * 64}))
    # consumed time only with the consumed status
    with pytest.raises(ValidationError, match="consumption"):
        Approval(**_approval(**decided, consumed_at=T0))
    with pytest.raises(ValidationError, match="consumption"):
        Approval(**_approval(**{**decided, "status": ApprovalStatus.CONSUMED}))
    Approval(**_approval(**{**decided, "status": ApprovalStatus.CONSUMED}, consumed_at=T0))
    # a pending approval has no reason requirement
    assert Approval(**_approval(asked_because="b" * 500)).asked_because == "b" * 500
    with pytest.raises(ValidationError):
        Approval(**_approval(asked_because="b" * 501))


# --- action shape, key and ledger fields, approval flow -----------------------------------------


def _usd(text: str) -> Any:
    from decimal import Decimal

    from nuramesh.core.money import Money

    return Money(amount=Decimal(text))


def test_action_shape_rules() -> None:
    Action(kind=ActionKind.SPEND, amount=_usd("0"))
    with pytest.raises(ValidationError, match="amount and no target"):
        Action(kind=ActionKind.SPEND)
    with pytest.raises(ValidationError, match="amount and no target"):
        Action(kind=ActionKind.SPEND, amount=_usd("1"), target="x")
    with pytest.raises(ValidationError, match="amount and no target"):
        Action(kind=ActionKind.SPEND, target="x")
    with pytest.raises(ValidationError, match="non-negative USD"):
        Action(kind=ActionKind.SPEND, amount=_usd("-1"))
    with pytest.raises(ValidationError, match="target and no amount"):
        Action(kind=ActionKind.USE_TOOL)
    with pytest.raises(ValidationError, match="target and no amount"):
        Action(kind=ActionKind.USE_TOOL, target="t", amount=_usd("1"))
    with pytest.raises(ValidationError, match="target and no amount"):
        Action(kind=ActionKind.MERGE, amount=_usd("1"))


def test_action_key() -> None:
    assert Action(kind=ActionKind.SPEND, amount=_usd("5.00")).key == "spend:5"
    assert Action(kind=ActionKind.SPEND, amount=_usd("5")).key == "spend:5"
    assert Action(kind=ActionKind.SPEND, amount=_usd("5.5")).key == "spend:5.5"
    assert Action(kind=ActionKind.SPEND, amount=_usd("50")).key == "spend:50"
    assert Action(kind=ActionKind.SPEND, amount=_usd("0.25")).key == "spend:0.25"
    assert Action(kind=ActionKind.USE_TOOL, target="web").key == "use_tool:web"
    assert Action(kind=ActionKind.RUN_COMMAND, target="ls -l").key == "run_command:ls -l"


def test_action_ledger_fields() -> None:
    calls: list[tuple[Any, bytes]] = []

    def hasher(kind: Any, data: bytes) -> str:
        calls.append((kind, data))
        return "h" * 64

    from nuramesh.core.content import LedgerHashKind

    spend = Action(kind=ActionKind.SPEND, amount=_usd("5.00"))
    assert spend.ledger_fields(hasher) == {"action": "spend", "amount": "5.00"}
    assert calls == []
    tool = Action(kind=ActionKind.USE_TOOL, target="web")
    assert tool.ledger_fields(hasher) == {"action": "use_tool", "target": "web"}
    assert calls == []
    for kind in (ActionKind.RUN_COMMAND, ActionKind.EXTERNAL_MESSAGE):
        calls.clear()
        out = Action(kind=kind, target="secret text").ledger_fields(hasher)
        assert out == {"action": kind.value, "target_hash": "h" * 64}
        assert calls == [(LedgerHashKind.ACTION_TARGET, b"secret text")]
    for kind in (ActionKind.WRITE_FILE, ActionKind.MERGE):
        out = Action(kind=kind, target="a/b").ledger_fields(hasher)
        assert out == {"action": kind.value, "target": "a/b"}
    assert pm.HASHED_TARGETS == {ActionKind.RUN_COMMAND, ActionKind.EXTERNAL_MESSAGE}


def test_cache_sizes() -> None:
    assert pm.command_words.cache_info().maxsize == 8192
    assert pm._match_mask.cache_info().maxsize == 16384
    assert pm._compile_glob.cache_info().maxsize == 4096


def _decided(status: ApprovalStatus = ApprovalStatus.APPROVED, **over: Any) -> Approval:
    base = {
        "status": status,
        "decided_by": "human:a",
        "decided_at": T0,
        "decision_reason_hash": "a" * 64,
    }
    return Approval(**_approval(**{**base, **over}))


def test_approval_answers_needs_agent_task_and_action() -> None:
    act = Action(kind=ActionKind.USE_TOOL, target="t")
    ap = Approval(**_approval(task_id="task-1"))
    assert ap.answers("dev-1", "task-1", act)
    assert not ap.answers("dev-2", "task-1", act)
    assert not ap.answers("dev-1", "task-2", act)
    assert not ap.answers("dev-1", None, act)
    assert not ap.answers("dev-1", "task-1", Action(kind=ActionKind.USE_TOOL, target="u"))
    assert not ap.answers("dev-1", "task-1", Action(kind=ActionKind.MERGE, target="t"))
    assert Approval(**_approval()).answers("dev-1", None, act)


def test_check_decidable_order_and_messages() -> None:
    pending = Approval(**_approval())
    pm.check_decidable(pending, approver="human:a")
    pm.check_decidable(pending, approver="human:a", allowed=("human:a",), reason="ok")
    with pytest.raises(pm.ApprovalError, match="already approved"):
        pm.check_decidable(_decided(), approver="human:a")
    with pytest.raises(pm.ApprovalError, match="already denied"):
        pm.check_decidable(_decided(ApprovalStatus.DENIED), approver="human:a")
    with pytest.raises(pm.ApprovalError, match="only a human"):
        pm.check_decidable(pending, approver="agent:x")
    with pytest.raises(pm.ApprovalError, match="may not answer"):
        pm.check_decidable(pending, approver="human:b", allowed=("human:a",))
    pm.check_decidable(pending, approver="human:b", allowed=("human:c", "human:b"))
    with pytest.raises(pm.ApprovalError, match="needs a reason"):
        pm.check_decidable(pending, approver="human:a", reason="   ")
    with pytest.raises(pm.ApprovalError, match="needs a reason"):
        pm.check_decidable(pending, approver="human:a", reason="")
    # order: status, then human, then allowed, then reason
    with pytest.raises(pm.ApprovalError, match="already"):
        pm.check_decidable(_decided(), approver="agent:x", allowed=("human:a",), reason=" ")
    with pytest.raises(pm.ApprovalError, match="only a human"):
        pm.check_decidable(pending, approver="agent:x", allowed=("human:a",), reason=" ")
    with pytest.raises(pm.ApprovalError, match="may not answer"):
        pm.check_decidable(pending, approver="human:b", allowed=("human:a",), reason=" ")


def test_decide_and_consume_approval() -> None:
    pending = Approval(**_approval())
    yes = pm.decide_approval(
        pending,
        approver="human:a",
        approve=True,
        reason_hash="b" * 64,
        at=T0,
        mode=pm.ApprovalMode.PEER,
    )
    assert yes.status is ApprovalStatus.APPROVED
    assert (yes.decided_by, yes.decided_at, yes.decision_reason_hash) == ("human:a", T0, "b" * 64)
    assert pending.status is ApprovalStatus.PENDING
    no = pm.decide_approval(
        pending,
        approver="human:a",
        approve=False,
        reason_hash="b" * 64,
        at=T0,
        mode=pm.ApprovalMode.PEER,
    )
    assert no.status is ApprovalStatus.DENIED
    for bad in ("", "B" * 64, "b" * 63, "b" * 65, "b" * 63 + "\n", "x" * 64):
        with pytest.raises(pm.ApprovalError, match="needs a reason"):
            pm.decide_approval(
                pending,
                approver="human:a",
                approve=True,
                reason_hash=bad,
                at=T0,
                mode=pm.ApprovalMode.PEER,
            )
    with pytest.raises(pm.ApprovalError, match="already approved"):
        pm.decide_approval(
            yes,
            approver="human:a",
            approve=True,
            reason_hash="b" * 64,
            at=T0,
            mode=pm.ApprovalMode.PEER,
        )
    with pytest.raises(pm.ApprovalError, match="may not answer"):
        pm.decide_approval(
            pending,
            approver="human:b",
            approve=True,
            reason_hash="b" * 64,
            at=T0,
            mode=pm.ApprovalMode.PEER,
            allowed=("human:a",),
        )
    done = pm.consume_approval(yes, at=T0)
    assert done.status is ApprovalStatus.CONSUMED and done.consumed_at == T0
    assert yes.status is ApprovalStatus.APPROVED
    for st in (pending, no, done):
        with pytest.raises(pm.ApprovalError, match="not approved"):
            pm.consume_approval(st, at=T0)
