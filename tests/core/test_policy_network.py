"""H-08 (R1.30, BASELINE P1b): network actions default to `ask`, host shown in the approval."""

from datetime import UTC, datetime

import pytest
from content_helpers import pure_hasher
from pydantic import ValidationError

from nuramesh.core.autonomy import GRANTABLE_KINDS, NEVER_GRANTABLE_KINDS
from nuramesh.core.overlay import unavailable
from nuramesh.core.policy import (
    Action,
    ActionKind,
    Approval,
    Decision,
    Policy,
    PolicyDecision,
    PolicyRule,
    evaluate,
)

AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def net(host: str) -> Action:
    return Action(kind=ActionKind.NETWORK, target=host)


def test_a_role_with_no_network_rule_gets_ask() -> None:
    policy = Policy(
        role="dev", rules=(PolicyRule(action=ActionKind.USE_TOOL, decision=Decision.ALLOW),)
    )
    result = evaluate(policy, net("api.example.com"))
    assert result.decision is Decision.ASK
    assert "api.example.com" in result.reason


def test_an_empty_policy_also_asks_for_the_network() -> None:
    assert evaluate(Policy(role="dev", rules=()), net("example.org")).decision is Decision.ASK


def test_a_role_without_any_policy_is_still_denied() -> None:
    assert evaluate(None, net("example.org")).decision is Decision.DENY


def test_an_explicit_rule_still_decides() -> None:
    policy = Policy(
        role="dev",
        rules=(
            PolicyRule(action=ActionKind.NETWORK, target="*.pypi.org", decision=Decision.ALLOW),
            PolicyRule(
                action=ActionKind.NETWORK, target="evil.example.com", decision=Decision.DENY
            ),
        ),
    )
    assert evaluate(policy, net("files.pypi.org")).decision is Decision.ALLOW
    assert evaluate(policy, net("evil.example.com")).decision is Decision.DENY
    assert evaluate(policy, net("other.example.com")).decision is Decision.ASK


def test_deny_wins_over_the_ask_default_and_over_allow() -> None:
    policy = Policy(
        role="dev",
        rules=(
            PolicyRule(action=ActionKind.NETWORK, target="*", decision=Decision.ALLOW),
            PolicyRule(action=ActionKind.NETWORK, target="bad.test", decision=Decision.DENY),
        ),
    )
    assert evaluate(policy, net("bad.test")).decision is Decision.DENY


def test_the_approval_payload_contains_the_host() -> None:
    action = net("api.example.com:8443")
    approval = Approval(
        id="ap1",
        run_id="r1",
        agent_id="a1",
        action=action,
        requested_at=AT,
        asked_because="network",
    )
    assert approval.action.target == "api.example.com:8443"
    assert approval.action.key == "network:api.example.com:8443"
    assert approval.answers("a1", None, net("api.example.com:8443"))
    assert not approval.answers("a1", None, net("api.example.com"))  # one host, one approval
    assert action.ledger_fields(pure_hasher()) == {
        "action": "network",
        "target": "api.example.com:8443",
    }


@pytest.mark.parametrize(
    "bad",
    [
        "https://api.example.com/x",  # a scheme and path are not a destination
        "user:pass@example.com",  # credentials never travel in a target
        "example.com/path",
        "example.com?q=1",
        "exa mple.com",
        "example.com\n",
        "",
        "-bad.example.com",
        "example.com:99999",
        "example.com:0",
        "example.com:",
        "Example.COM",  # lower case only: rules and approvals compare text
    ],
)
def test_a_network_target_is_a_bare_host_with_an_optional_port(bad: str) -> None:
    with pytest.raises(ValidationError):
        Action(kind=ActionKind.NETWORK, target=bad)


@pytest.mark.parametrize(
    "good", ["example.com", "localhost", "10.0.0.5:8080", "[2001:db8::1]:443", "a.b-c.io"]
)
def test_a_bare_host_is_accepted(good: str) -> None:
    assert net(good).target == good


def test_network_is_never_grantable_by_autonomy_history() -> None:
    assert "network" in NEVER_GRANTABLE_KINDS
    assert "network" not in GRANTABLE_KINDS
    assert GRANTABLE_KINDS | NEVER_GRANTABLE_KINDS == {k.value for k in ActionKind}


def test_an_over_long_host_is_rejected() -> None:
    label = "a" * 60
    just_over = ".".join([label] * 4 + ["a" * 11])  # 4 * 61 + 11 = 255 > 253
    way_over = ".".join([label] * 5)
    assert 253 < len(just_over) <= 260 and len(way_over) > 260
    for bad in (just_over, way_over, f"{just_over}:443"):
        with pytest.raises(ValidationError):
            Action(kind=ActionKind.NETWORK, target=bad)
    exactly = ".".join([label] * 4 + ["a" * 9])  # 4 * 61 + 9 = 253, the DNS maximum
    assert len(exactly) == 253
    assert net(exactly).target == exactly
    with pytest.raises(ValidationError):
        Action(kind=ActionKind.NETWORK, target="a" * 64 + ".com")  # a label over 63


@pytest.mark.parametrize("dotted", ["example.com.", "example.com.:443", "."])
def test_a_trailing_dot_is_rejected_not_normalised(dotted: str) -> None:
    # `example.com.` and `example.com` are one host to DNS but two texts to a rule and an
    # approval: rejecting keeps one spelling per destination (the caller strips the dot).
    with pytest.raises(ValidationError):
        Action(kind=ActionKind.NETWORK, target=dotted)


def test_a_rejection_never_echoes_the_secret_in_the_target() -> None:
    # Short, obviously fake: pydantic's input echo would show a short secret in full or as a
    # suffix, so any 6-character window of it in the message is a leak.
    secret = "xxxxxxxx"
    for bad in (
        f"https://user:{secret}@example.com/x",
        f"user:{secret}@example.com",
        f"example.com/{secret}",
        f"example.com?token={secret}",
    ):
        with pytest.raises(ValidationError) as caught:
            Action(kind=ActionKind.NETWORK, target=bad)
        text = str(caught.value)
        assert not any(secret[i : i + 6] in text for i in range(len(secret) - 5))
        assert secret not in repr(caught.value.errors(include_input=False))


def test_no_validation_error_leaks_the_input_in_errors_json_repr_or_nested() -> None:
    from nuramesh.core.policy import Approval

    secret = "xxxxxxxx"
    window = secret[:6]
    with pytest.raises(ValidationError) as caught:
        Action(kind=ActionKind.NETWORK, target=f"user:{secret}@example.com")
    err = caught.value
    for text in (
        str(err),
        repr(err),
        err.json(include_input=False),
        repr(err.errors(include_input=False)),
    ):
        assert window not in text
    with pytest.raises(ValidationError) as nested:
        Approval.model_validate(
            {"action": {"kind": "network", "target": f"user:{secret}@example.com"}}
        )
    for text in (str(nested.value), repr(nested.value), nested.value.json(include_input=False)):
        assert window not in text


# --- #853 m3: host spellings ---------------------------------------------------------------


def _decide(rule_target: str, decision: Decision, host: str) -> Decision:
    policy = Policy(
        role="dev",
        rules=(PolicyRule(action=ActionKind.NETWORK, target=rule_target, decision=decision),),
    )
    return evaluate(policy, net(host)).decision


@pytest.mark.parametrize("decision", [Decision.DENY, Decision.ASK])
@pytest.mark.parametrize("host", ["bad.test", "bad.test:443", "bad.test:1"])
def test_a_host_only_deny_or_ask_rule_covers_every_port(decision: Decision, host: str) -> None:
    assert _decide("bad.test", decision, host) is decision


def test_a_host_only_rule_for_an_ipv6_literal_covers_its_ports() -> None:
    assert _decide("[2001:db8::1]", Decision.DENY, "[2001:db8::1]:443") is Decision.DENY
    assert _decide("[2001:db8::1]", Decision.DENY, "[2001:db8::2]:443") is Decision.ASK


def test_a_rule_with_a_port_stays_exact() -> None:
    assert _decide("bad.test:443", Decision.DENY, "bad.test:80") is Decision.ASK
    assert _decide("bad.test:443", Decision.DENY, "bad.test") is Decision.DENY  # fail closed
    assert _decide("bad.test:443", Decision.DENY, "bad.test:443") is Decision.DENY


def test_an_allow_rule_never_widens_to_other_ports() -> None:
    assert _decide("api.test", Decision.ALLOW, "api.test") is Decision.ALLOW
    assert _decide("api.test", Decision.ALLOW, "api.test:443") is Decision.ASK
    assert _decide("api.test:443", Decision.ALLOW, "api.test:443") is Decision.ALLOW


@pytest.mark.parametrize(
    "bad",
    [
        "0x0a000005",
        "167772165",
        "010.0.0.5",
        "10.0.0.05",
        "0x7f.1",
        "1.2.3",
        "1.2.3.4.5",
        "256.1.1.1",
        "10.0.0.5.example",
        "08",
        "10.0.0.5:0x50",
    ],
)
def test_numeric_host_spellings_other_than_dotted_decimal_ipv4_are_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        Action(kind=ActionKind.NETWORK, target=bad)


@pytest.mark.parametrize(
    "good",
    [
        "10.0.0.5",
        "255.255.255.255",
        "0.0.0.0",
        "10.0.0.5:80",
        "a1.example",
        "x0x1.example",
        "1a.example",
    ],
)
def test_dotted_decimal_ipv4_and_ordinary_names_stay_accepted(good: str) -> None:
    assert net(good).target == good


@pytest.mark.parametrize("target", ["Evil.Example", "evil.EXAMPLE", "*.PyPI.org", "[2001:DB8::1]"])
@pytest.mark.parametrize("decision", list(Decision))
def test_an_upper_case_network_rule_target_is_refused_at_load(
    target: str, decision: Decision
) -> None:
    with pytest.raises(ValidationError):
        PolicyRule(action=ActionKind.NETWORK, target=target, decision=decision)


def test_upper_case_is_still_fine_for_other_kinds() -> None:
    PolicyRule(action=ActionKind.USE_TOOL, target="Search*", decision=Decision.ALLOW)


@pytest.mark.parametrize(
    "rule",
    [
        "*",
        "bad.test",
        "*.test",
        "bad.test:443",
        "bad.*",
        "b?d.test",
        "10.0.0.*",
        "10.0.0.5",
        "*:443",
    ],
)
@pytest.mark.parametrize(
    "host",
    [
        "bad.test",
        "bad.test:443",
        "bad.test:80",
        "good.test",
        "10.0.0.5",
        "10.0.0.5:443",
        "x.bad.test",
        "[::1]:80",
        "localhost",
    ],
)
def test_property_allow_never_matches_what_it_did_not_before(rule: str, host: str) -> None:
    """An allow rule matches exactly what the whole-target glob matched: the host-part match
    is for deny and ask only."""
    from nuramesh.core.policy import glob_match

    allow = PolicyRule(action=ActionKind.NETWORK, target=rule, decision=Decision.ALLOW)
    assert allow.matches(net(host)) == glob_match(rule, host, ActionKind.NETWORK)
    # and deny/ask never match less than allow does
    for decision in (Decision.DENY, Decision.ASK):
        wider = PolicyRule(action=ActionKind.NETWORK, target=rule, decision=decision)
        assert wider.matches(net(host)) or not allow.matches(net(host))


# H-08 m3 round 2: one spelling for bracketed IPv6 hosts and ports (Opus final, major)
OTHER_SPELLINGS = [
    "[0::1]",
    "[0:0:0:0:0:0:0:1]",
    "[::0001]",
    "[::ffff:127.0.0.1]",  # IPv4-mapped: use the IPv4 spelling
    "[::ffff:7f00:1]",
    "[::ffff:a9fe:a9fe]",
    "[2001:0db8::1]",
    "[2001:db8:0:0:0:0:0:1]",
    "[2001:DB8::1]",
    "[::1%eth0]",
    "[::1%25eth0]",
    "[::1].",
    "[::1]:",
    "[:::1]",
    "[:::::]",
    "[....]",
    "[::1]::80",
    " [::1]",
    "[::1] ",
    "[::1]\n",
    "[::1]:080",
    "[::1]:00080",
    "[::1]:0",
    "[::1]:65536",
    "10.0.0.5:080",
    "10.0.0.5:00080",
    "10.0.0.5:0",
    "10.0.0.5:99999",
    "10.0.0.5.",
    "10.0.0.5 ",
    "10.0.0.5:80:80",
    "[::1.2.3.4]",  # IPv4-compatible
    "[::102:304]",
    "[64:ff9b::7f00:1]",  # NAT64
    "[64:ff9b::127.0.0.1]",
    "[2002:7f00:1::]",  # 6to4
    "[2001:0:4136:e378:8000:63bf:3fff:fdd2]",  # Teredo
    "[::ffff:0:7f00:1]",  # IPv4-translated (SIIT)
    "[::ffff:0:127.0.0.1]",
]


@pytest.mark.parametrize("bad", OTHER_SPELLINGS)
def test_other_spellings_of_an_address_or_port_are_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        Action(kind=ActionKind.NETWORK, target=bad)


@pytest.mark.parametrize(
    "good",
    ["[::1]", "[::1]:80", "[::]", "[2001:db8::1]", "[2001:db8::1]:443", "[fe80::1]"],
)
def test_canonical_ipv6_hosts_stay_accepted(good: str) -> None:
    assert net(good).target == good


@pytest.mark.parametrize(
    ("deny", "spellings"),
    [
        ("[::1]", ["[0::1]", "[::0001]", "[0:0:0:0:0:0:0:1]"]),
        ("127.0.0.1", ["[::ffff:127.0.0.1]", "[::ffff:7f00:1]"]),
        ("169.254.169.254", ["[::ffff:a9fe:a9fe]", "[::ffff:169.254.169.254]"]),
        ("[2001:db8::1]", ["[2001:0db8::1]", "[2001:db8:0:0:0:0:0:1]"]),
        ("10.0.0.5:80", ["10.0.0.5:080", "10.0.0.5:00080"]),
    ],
)
def test_a_deny_rule_plus_allow_all_never_allows_another_spelling(
    deny: str, spellings: list[str]
) -> None:
    policy = Policy(
        role="dev",
        rules=(
            PolicyRule(action=ActionKind.NETWORK, target=deny, decision=Decision.DENY),
            PolicyRule(action=ActionKind.NETWORK, target="*", decision=Decision.ALLOW),
        ),
    )
    assert evaluate(policy, net(deny)).decision is Decision.DENY
    for spelling in spellings:
        with pytest.raises(ValidationError):
            net(spelling)


@pytest.mark.parametrize("bad", ["167772165", "0x0a000005", "010.0.0.5", "[0::1]", "10.0.0.5:080"])
@pytest.mark.parametrize("decision", list(Decision))
def test_a_wildcard_free_network_rule_that_is_not_canonical_is_refused_at_load(
    bad: str, decision: Decision
) -> None:
    with pytest.raises(ValidationError):
        PolicyRule(action=ActionKind.NETWORK, target=bad, decision=decision)


def test_hostile_inputs_are_decided_fast() -> None:
    import time

    start = time.perf_counter()
    for text in ("[" + ":" * 262144, "[" + "0:" * 131072, "1." * 131072, "a" * 262144):
        assert not __import__("nuramesh.core.policy", fromlist=["x"]).is_network_destination(text)
    assert time.perf_counter() - start < 1.0


# --- #1153 PR 1: NG-10 (c) wildcard allows never cover a non-public address; NG-18 redaction ---

WILDCARD_REDACTED = (
    "a wildcard allow rule does not cover a redacted non-public address: a human decides"
)
DEFAULT_REDACTED = "no rule for network access to a redacted target: a human decides"
RULE_REDACTED = "network access to a redacted target is decided by a rule"


def allow(target: str) -> PolicyRule:
    return PolicyRule(action=ActionKind.NETWORK, target=target, decision=Decision.ALLOW)


def policy_of(*rules: PolicyRule) -> Policy:
    return Policy(role="dev", rules=rules)


NON_PUBLIC = ["127.0.0.1:443", "10.1.2.3:443", "100.64.0.9:443", "[fd12:3456::1]:443", "[::1]:443"]


WILDCARDS = {
    "127.0.0.1:443": ["*", "*:443", "127.*", "127.0.0.?:443", "*:*"],
    "10.1.2.3:443": ["*", "*:443", "10.*", "10.1.*:443", "10.1.2.?:443"],
    "100.64.0.9:443": ["*", "*:443", "100.64.*", "100.*"],
    "[fd12:3456::1]:443": ["*", "*:443", "[fd12:*", "[fd12:3456::?]:443"],
    "[::1]:443": ["*", "*:443", "[::?]:443", "[::*"],
}


@pytest.mark.parametrize(
    ("target", "wildcard"), [(t, w) for t, ws in WILDCARDS.items() for w in ws]
)
def test_a_wildcard_allow_does_not_cover_a_non_public_address(target: str, wildcard: str) -> None:
    rule = allow(wildcard)
    assert rule.matches(net(target))  # the premise: the plain allow matches
    result = evaluate(policy_of(rule), net(target))
    assert result.decision is Decision.ASK
    assert result.reason_code == "wildcard_private_address"
    assert result.rule == 0


def test_a_wildcard_allow_on_a_public_address_still_allows() -> None:
    result = evaluate(policy_of(allow("*:443")), net("8.8.8.8:443"))
    assert result.decision is Decision.ALLOW
    assert result.reason_code is None


def test_a_wildcard_allow_on_a_name_still_allows() -> None:
    assert evaluate(policy_of(allow("*:443")), net("intra.example.test:443")).decision is (
        Decision.ALLOW
    )


@pytest.mark.parametrize("target", NON_PUBLIC)
def test_an_exact_host_allow_stands(target: str) -> None:
    result = evaluate(policy_of(allow(target)), net(target))
    assert result.decision is Decision.ALLOW
    assert result.reason_code is None


def test_a_port_wildcard_on_an_exact_host_stands() -> None:
    assert evaluate(policy_of(allow("10.0.0.5:*")), net("10.0.0.5:443")).decision is Decision.ALLOW


def test_a_wildcard_allow_does_not_mask_a_later_exact_allow() -> None:
    policy = policy_of(allow("*:443"), allow("10.0.0.9:80"), allow("10.0.0.5:443"))
    result = evaluate(policy, net("10.0.0.5:443"))
    assert result.decision is Decision.ALLOW
    assert result.rule == 2  # the rule that holds the exact host


def test_a_wildcard_after_an_exact_allow_changes_nothing() -> None:
    policy = policy_of(allow("10.0.0.5:443"), allow("*:443"))
    assert evaluate(policy, net("10.0.0.5:443")).decision is Decision.ALLOW


def test_a_host_only_allow_without_a_port_is_not_exact() -> None:
    result = evaluate(policy_of(allow("10.0.0.5")), net("10.0.0.5"))
    assert result.decision is Decision.ASK
    assert result.reason_code == "wildcard_private_address"


def test_a_deny_still_wins_over_an_exact_allow_on_a_non_public_address() -> None:
    deny = PolicyRule(action=ActionKind.NETWORK, target="10.*", decision=Decision.DENY)
    result = evaluate(policy_of(allow("10.0.0.5:443"), deny), net("10.0.0.5:443"))
    assert result.decision is Decision.DENY
    assert result.reason_code is None


def test_an_ask_rule_keeps_its_own_reason_code_none() -> None:
    ask = PolicyRule(action=ActionKind.NETWORK, target="10.*", decision=Decision.ASK)
    result = evaluate(policy_of(allow("*"), ask), net("10.0.0.5:443"))
    assert result.decision is Decision.ASK
    assert result.reason_code is None


def test_the_autonomy_overlay_inherits_the_rule() -> None:
    """`unavailable` re-runs `evaluate`: the reason code is passed through, never lost."""
    got = unavailable(policy_of(allow("*:443")), net("10.0.0.5:443"))
    assert got.decision.decision is Decision.ASK
    assert got.decision.reason_code == "wildcard_private_address"
    plain = unavailable(policy_of(allow("*:443")), net("8.8.8.8:443"))
    assert plain.decision.decision is Decision.ALLOW
    assert plain.decision.reason_code is None


def test_policy_decision_reason_code_defaults_to_none() -> None:
    assert PolicyDecision(decision=Decision.ALLOW, reason="x").reason_code is None


def redacted(host: str) -> Action:
    return Action(kind=ActionKind.NETWORK, target=host, redact_target=True)


def test_redacted_reasons_are_the_exact_fixed_texts() -> None:
    target = "10.0.0.5:443"
    assert evaluate(policy_of(), redacted(target)).reason == DEFAULT_REDACTED
    wild = evaluate(policy_of(allow("*")), redacted(target))
    assert wild.reason == WILDCARD_REDACTED
    assert wild.reason_code == "wildcard_private_address"
    deny = PolicyRule(
        action=ActionKind.NETWORK, target="10.*", decision=Decision.DENY, reason="no to 10.x"
    )
    assert evaluate(policy_of(deny), redacted(target)).reason == RULE_REDACTED
    ask = PolicyRule(action=ActionKind.NETWORK, target="10.*", decision=Decision.ASK)
    assert evaluate(policy_of(ask), redacted(target)).reason == RULE_REDACTED


@pytest.mark.parametrize("target", ["10.0.0.5:443", "169.254.169.254:80", "b.example.test:443"])
def test_no_redacted_reason_contains_the_target(target: str) -> None:
    for policy in (policy_of(), policy_of(allow("*")), policy_of(allow("*")).model_copy()):
        result = evaluate(policy, redacted(target))
        assert target.split(":")[0] not in result.reason


def test_unredacted_reasons_are_unchanged() -> None:
    result = evaluate(policy_of(), net("api.example.com:443"))
    assert result.reason == "no rule for network access to api.example.com:443: a human decides"


def test_redact_target_is_not_part_of_the_key_or_the_approval_match() -> None:
    plain = net("10.0.0.5:443")
    flagged = redacted("10.0.0.5:443")
    assert plain.key == flagged.key
    approval = Approval(
        id="appr-1",
        run_id="run-1",
        agent_id="dev",
        action=plain,
        requested_at=AT,
    )
    assert approval.answers("dev", None, flagged)
    assert Approval.model_validate_json(approval.model_dump_json()).action.redact_target is False
    stored = approval.model_copy(update={"action": flagged})
    assert Approval.model_validate_json(stored.model_dump_json()).action.redact_target is True


def test_ledger_fields_hash_a_redacted_network_target_only() -> None:
    hasher = pure_hasher()
    clear = net("a.example.test:443").ledger_fields(hasher)
    assert clear == {"action": "network", "target": "a.example.test:443"}
    hashed = redacted("10.0.0.5:443").ledger_fields(hasher)
    assert set(hashed) == {"action", "target_hash"}
    assert "10.0.0.5" not in str(hashed)
    again = redacted("10.0.0.5:443").ledger_fields(hasher)
    assert again == hashed  # the same target correlates


def test_translated_ipv4_spelling_is_refused_and_the_plain_spelling_stays_denied() -> None:
    policy = Policy(
        role="dev",
        rules=(
            PolicyRule(action=ActionKind.NETWORK, target="127.0.0.1", decision=Decision.DENY),
            PolicyRule(action=ActionKind.NETWORK, target="*", decision=Decision.ALLOW),
        ),
    )
    # the refused Action never reaches evaluate: the refusal is the guard
    with pytest.raises(ValidationError):
        net("[::ffff:0:7f00:1]")
    assert evaluate(policy, net("127.0.0.1")).decision is Decision.DENY


@pytest.mark.parametrize("decision", [Decision.DENY, Decision.ASK])
@pytest.mark.parametrize(
    ("rule", "host"),
    [
        ("bad.test:*", "bad.test"),
        ("bad.test:443", "bad.test"),
        ("*:443", "x.test"),
        ("[::1]:443", "[::1]"),
    ],
)
def test_a_port_naming_deny_or_ask_rule_also_covers_a_target_with_no_port(
    decision: Decision, rule: str, host: str
) -> None:
    policy = Policy(
        role="dev",
        rules=(
            PolicyRule(action=ActionKind.NETWORK, target=rule, decision=decision),
            PolicyRule(action=ActionKind.NETWORK, target="*", decision=Decision.ALLOW),
        ),
    )
    assert evaluate(policy, net(host)).decision is decision


def test_a_port_naming_allow_rule_does_not_cover_a_target_with_no_port() -> None:
    policy = Policy(
        role="dev",
        rules=(
            PolicyRule(action=ActionKind.NETWORK, target="ok.test:443", decision=Decision.ALLOW),
        ),
    )
    assert evaluate(policy, net("ok.test")).decision is Decision.ASK
    assert evaluate(policy, net("ok.test:80")).decision is Decision.ASK


@pytest.mark.parametrize("bad", ["[::ffff:0:7f00:1]:443", "[::ffff:0:127.0.0.1]:80"])
def test_a_translated_ipv4_spelling_with_a_port_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        net(bad)


@pytest.mark.parametrize("good", ["[::ffff:1:0:0]", "[::fffe:0:7f00:1]", "[2001:db8::1]"])
def test_neighbours_of_the_translated_range_stay_valid(good: str) -> None:
    assert net(good).target == good


def test_a_star_port_rule_does_not_match_a_ported_target_on_another_port() -> None:
    assert _decide("*:443", Decision.DENY, "x.test:80") is Decision.ASK
    assert _decide("*:443", Decision.DENY, "x.test:443") is Decision.DENY


@pytest.mark.parametrize("port", ["1", "65535"])
def test_a_port_naming_deny_rule_at_the_port_boundaries_covers_a_target_with_no_port(
    port: str,
) -> None:
    assert _decide(f"bad.test:{port}", Decision.DENY, "bad.test") is Decision.DENY
    assert _decide(f"bad.test:{port}", Decision.DENY, "other.test") is Decision.ASK


@pytest.mark.parametrize("port", ["0", "65536"])
def test_out_of_range_ports_are_refused_in_rules_and_targets(port: str) -> None:
    with pytest.raises(ValidationError):
        PolicyRule(action=ActionKind.NETWORK, target=f"bad.test:{port}", decision=Decision.DENY)
    with pytest.raises(ValidationError):
        net(f"bad.test:{port}")
