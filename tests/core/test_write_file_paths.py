"""write_file targets have one canonical spelling, and the team config, policy, canon, key and
budget files are never writable by an agent tool, whatever a policy says (R1.30 review 3)."""

import pytest

from nuramesh.core.policy import (
    Action,
    ActionKind,
    Decision,
    Policy,
    PolicyRule,
    evaluate,
    is_canonical_write_target,
    is_protected_path,
)


def write(target: str) -> Action:
    return Action(kind=ActionKind.WRITE_FILE, target=target)


@pytest.mark.parametrize(
    "spelling",
    [
        "./nuramesh.yaml",
        "/abs/team/nuramesh.yaml",
        ".//nuramesh.yaml",
        "src/./x",
        "config\\x",
        "a//b",
        "x/.",
        "./",
    ],
)
def test_non_canonical_spellings_are_not_canonical(spelling: str) -> None:
    assert not is_canonical_write_target(spelling)


def test_canonical_spellings() -> None:
    assert is_canonical_write_target("src/calc.py") and is_canonical_write_target("a")


@pytest.mark.parametrize(
    "spelling",
    [
        "./nuramesh.yaml",
        "/abs/team/nuramesh.yaml",
        "config\\policy.yaml",
        ".//.git/config",
    ],
)
def test_protection_sees_through_spellings(spelling: str) -> None:
    assert is_protected_path(spelling)


def test_other_kinds_keep_their_targets() -> None:
    assert Action(kind=ActionKind.RUN_COMMAND, target="./run.sh").target == "./run.sh"


ALLOW_ALL = Policy(
    role="dev",
    rules=(PolicyRule(action=ActionKind.WRITE_FILE, decision=Decision.ALLOW, target="**"),),
)


@pytest.mark.parametrize(
    "target",
    [
        "nuramesh.yaml",
        "team/nuramesh.yml",
        "NuraMesh.YAML",
        "nuramesh.json",
        "policy.yaml",
        "policies/dev.yaml",
        "canon.yaml",
        "canon/sources.yml",
        "budgets.yaml",
        "budget.json",
        "keys/anchor.key",
        "anchor.pub",
        ".nuramesh/state.db",
        ".git/config",
        "repo/.git/hooks/pre-commit",
    ],
)
def test_protected_files_are_denied_even_when_a_policy_allows_everything(target: str) -> None:
    decision = evaluate(ALLOW_ALL, write(target))
    assert decision.decision is Decision.DENY and "protected" in decision.reason


def test_ordinary_files_still_follow_the_policy() -> None:
    assert evaluate(ALLOW_ALL, write("src/calc.py")).decision is Decision.ALLOW
    assert evaluate(ALLOW_ALL, write("notes/policy-notes.md")).decision is Decision.ALLOW


@pytest.mark.parametrize(
    "target",
    [
        "src/policy/handlers.py",
        "app/budget/views.py",
        "app/canon/x.md",
        "src/keys/store.py",
        "lib/policies/a.py",
    ],
)
def test_ordinary_directories_with_those_names_stay_writable_when_a_policy_allows(
    target: str,
) -> None:
    assert not is_protected_path(target)
    assert evaluate(ALLOW_ALL, write(target)).decision is Decision.ALLOW


@pytest.mark.parametrize("target", ["policy/x.py", "canon/a.md", "budget/b", "keys/k", "Keys/k"])
def test_the_config_directories_at_the_workspace_root_are_protected(target: str) -> None:
    assert is_protected_path(target)


@pytest.mark.parametrize(
    "spelling",
    ["..", "../x", "a/..", "a/../x", "a\x00b", "a\nb", "a\x01b", "a\x7fb", "a\u2028b", "x" * 5000],
)
def test_what_action_would_reject_is_not_canonical(spelling: str) -> None:
    assert not is_canonical_write_target(spelling)


@pytest.mark.parametrize(
    "target",
    ["real-azure-verifier.yaml", "real-anthropic.yml", "other.json", "./Staging.YAML", "x.yaml"],
)
def test_every_config_file_at_the_workspace_root_is_protected(target: str) -> None:
    assert is_protected_path(target)
    assert evaluate(ALLOW_ALL, write(target.removeprefix("./"))).decision is Decision.DENY


@pytest.mark.parametrize("target", ["assets/openapi.yaml", "src/data.json", "notes.md", "app.py"])
def test_config_like_files_below_the_root_or_of_other_kinds_follow_the_policy(target: str) -> None:
    assert not is_protected_path(target)
