"""The earned-autonomy overlay (E13): one policy check.

`effective` starts from the ordinary verdict. A `deny` or an `allow` is final. An
`ask` becomes `allow` only if **every** `ask` rule that matches the action (by the wide,
refusal-side matching) has an **active** grant covering the acting agent, is at the grant's rule
key **with the grant's rule hash**, and also matches the action under the strict `allow`
semantics. A `spend` action is never overlaid. Nothing here reads a ledger: the caller derives
the grant states (`autonomy.grant_states`) and passes them in; an unreadable ledger is the
caller's `unavailable` answer, so the overlay can never raise an error that reaches a caller
as `allow`.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from nuramesh.core.autonomy import GrantState, rule_hash
from nuramesh.core.policy import (
    Action,
    ActionKind,
    Decision,
    Policy,
    PolicyDecision,
    PolicyRule,
    command_matches,
    evaluate,
    glob_match,
)

REASON_PREFIX = "earned autonomy"
UNAVAILABLE = "earned autonomy unavailable"


@dataclass(frozen=True)
class Overlaid:
    """The final decision of a check and the grants that changed it (empty: unchanged)."""

    decision: PolicyDecision
    grants: tuple[str, ...] = ()


def unavailable(policy: Policy | None, action: Action) -> Overlaid:
    """The fail-closed answer when the grants cannot be read: an `ask` stays `ask` and says why;
    a `deny` or an `allow` is the plain verdict (a grant never changes either)."""
    plain = evaluate(policy, action)
    if plain.decision is Decision.ASK:
        return Overlaid(
            PolicyDecision(
                decision=Decision.ASK,
                rule=plain.rule,
                reason=UNAVAILABLE,
                reason_code=plain.reason_code,
            )
        )
    return Overlaid(plain)


def _strict_allow(rule: PolicyRule, action: Action) -> bool:
    """Whether the rule, read as an `allow` rule, also matches the action (spec 6.1 (b))."""
    if action.kind is ActionKind.RUN_COMMAND:
        assert action.target is not None
        return command_matches(rule.target, action.target, Decision.ALLOW)
    if action.kind is ActionKind.NETWORK:
        # an ask rule on a host covers every port, an allow rule does not widen: plain glob
        return glob_match(rule.target, action.target or "", ActionKind.NETWORK)
    return rule.matches(action)  # for the other kinds the allow matching is the ask matching


def effective(
    policy: Policy | None,
    action: Action,
    *,
    agent: str,
    role: str,
    states: Mapping[str, GrantState],
) -> Overlaid:
    """The decision of one check under the grants in `states` (all grants, any state)."""
    plain = evaluate(policy, action)
    if plain.decision is not Decision.ASK or policy is None or action.kind is ActionKind.SPEND:
        return Overlaid(plain)
    used: set[str] = set()
    for index, rule in enumerate(policy.rules):
        if rule.decision is not Decision.ASK or not rule.matches(action):
            continue
        covering = [
            s.grant.id
            for s in states.values()
            if s.active
            and s.grant.role == role
            and s.grant.rule_key == index
            and s.grant.rule_hash == rule_hash(rule)
            and (s.grant.subject_id == agent if s.grant.agent_level else s.grant.subject_id == role)
        ]
        if not covering or not _strict_allow(rule, action):
            return Overlaid(plain)
        used.update(covering)
    if not used:
        return Overlaid(plain)
    grants = tuple(sorted(used))
    return Overlaid(
        PolicyDecision(
            decision=Decision.ALLOW,
            rule=plain.rule,
            reason=f"{REASON_PREFIX}: {', '.join(grants)}",
        ),
        grants,
    )
