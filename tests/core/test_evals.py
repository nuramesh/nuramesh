"""Eval suites, scoring and eligibility (R1.10, T27)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from content_helpers import pure_hasher
from pydantic import ValidationError

from nuramesh.core.config import ConfigValidationError, parse_config
from nuramesh.core.evals import (
    CaseOutcome,
    EvalConfig,
    EvalRecord,
    EvalSuite,
    SubjectKind,
    check_eligibility,
    score_suite,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def case(cid: str, needle: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "prompt": f"Say {needle}",
        "rules": [{"criterion": "says-it", "check": {"type": "contains", "value": needle}}],
        **extra,
    }


def suite(*cases: dict[str, Any], **extra: Any) -> EvalSuite:
    return EvalSuite.model_validate(
        {"id": "basics", "role": "dev", "cases": list(cases) or [case("a", "x")], **extra}
    )


def record(
    s: EvalSuite,
    *,
    seq: int = 1,
    passed: bool = True,
    kind: SubjectKind = SubjectKind.AGENT,
    subject: str = "ada",
    model: str | None = "m/1",
    at: datetime = NOW - timedelta(days=1),
    hash_: str | None = None,
) -> EvalRecord:
    return EvalRecord(
        subject_kind=kind,
        subject_id=subject,
        model=model,
        suite_id=s.id,
        suite_version=s.version,
        suite_hash=hash_ or s.suite_hash,
        score=Decimal("1"),
        passed=passed,
        outcomes=(),
        at=at,
        seq=seq,
    )


def eligibility(s: EvalSuite, records: list[EvalRecord], model: str | None = "m/1") -> Any:
    config = EvalConfig.model_validate(
        {"suites": [s.model_dump(mode="json")], "requirements": [{"role": "dev", "suites": [s.id]}]}
    )
    return check_eligibility(config, role="dev", agent="ada", model=model, records=records, now=NOW)


def test_all_rules_of_a_case_must_be_met_and_the_score_rounds_down() -> None:
    s = suite(
        {
            "id": "two-rules",
            "prompt": "p",
            "rules": [
                {"criterion": "a", "check": {"type": "contains", "value": "yes"}},
                {"criterion": "b", "check": {"type": "not_contains", "value": "no"}},
            ],
        },
        case("b", "q"),
        case("c", "z"),
        pass_threshold="0.6666",
    )
    scored = score_suite(s, {"two-rules": "yes", "b": "q", "c": "nothing"}, pure_hasher())
    assert scored.score == Decimal("0.6666") and scored.passed
    failing = score_suite(s, {"two-rules": "yes no", "b": "q", "c": "z"}, pure_hasher())
    assert failing.score == Decimal("0.6666")
    assert [o.passed for o in failing.outcomes] == [False, True, True]


def test_the_threshold_is_exact_not_rounded() -> None:
    s = suite(case("a", "x"), case("b", "y"), case("c", "z"), pass_threshold="0.6667")
    scored = score_suite(s, {"a": "x", "b": "y", "c": "-"}, pure_hasher())
    assert scored.score == Decimal("0.6666") and not scored.passed


def test_a_critical_failure_fails_the_suite_and_missing_responses_fail_cases() -> None:
    s = suite(case("a", "x", critical=True), case("b", "y"), pass_threshold="0.5")
    result = score_suite(s, {"b": "y"}, pure_hasher())
    assert result.score == Decimal("0.5000") and not result.passed
    assert result.outcomes[0].details == ("no response",)
    with pytest.raises(ValueError, match="unknown cases"):
        score_suite(s, {"a": "x", "nope": "1"}, pure_hasher())


def test_weights_count() -> None:
    s = suite(case("a", "x", weight=3), case("b", "y"), pass_threshold="0.75")
    assert score_suite(s, {"a": "x"}, pure_hasher()).passed
    assert not score_suite(s, {"b": "y"}, pure_hasher()).passed


def test_outcome_details_never_quote_the_response() -> None:
    s = suite(case("a", "x"))
    secret = "hunter2-" + "x"
    outcome = score_suite(s, {"a": secret}, pure_hasher()).outcomes[0]
    assert secret not in " ".join(outcome.details) and outcome.response_hash is not None


def test_suite_models_reject_bad_input() -> None:
    with pytest.raises(ValidationError):
        suite(case("a", "x"), case("a", "y"))
    for threshold in ("0", "1.1", "0.12345"):
        with pytest.raises(ValidationError):
            suite(pass_threshold=threshold)
    with pytest.raises(ValidationError):
        suite(valid_days=0)
    with pytest.raises(ValidationError):
        EvalSuite.model_validate({"id": "s", "role": "r", "cases": []})


def test_editing_a_case_changes_the_suite_hash() -> None:
    assert suite(case("a", "x")).suite_hash != suite(case("a", "y")).suite_hash
    assert suite(case("a", "x")).suite_hash == suite(case("a", "x")).suite_hash


def test_requirements_must_be_consistent() -> None:
    s = suite().model_dump(mode="json")
    for bad in (
        {"suites": [s], "requirements": [{"role": "dev", "suites": ["nope"]}]},
        {"suites": [s], "requirements": [{"role": "other", "suites": ["basics"]}]},
        {"suites": [s, s], "requirements": []},
        {
            "suites": [s],
            "requirements": [
                {"role": "dev", "suites": ["basics"]},
                {"role": "dev", "suites": ["basics"]},
            ],
        },
    ):
        with pytest.raises(ValidationError):
            EvalConfig.model_validate(bad)


def test_a_role_without_requirements_needs_no_evals() -> None:
    config = EvalConfig()
    assert check_eligibility(
        config, role="dev", agent="ada", model=None, records=[], now=NOW
    ).eligible


def test_no_results_means_not_eligible_never_a_pass() -> None:
    s = suite()
    result = eligibility(s, [])
    assert not result.eligible and "not evaluated" in result.reasons[0]


def test_pass_makes_eligible_and_expiry_is_exact() -> None:
    s = suite(valid_days=10)
    edge = NOW - timedelta(days=10)
    assert not eligibility(s, [record(s, at=edge)]).eligible
    assert eligibility(s, [record(s, at=edge + timedelta(seconds=1))]).eligible
    assert "expired" in eligibility(s, [record(s, at=edge)]).reasons[0]


def test_stale_hash_and_other_model_never_count() -> None:
    s = suite()
    assert not eligibility(s, [record(s, hash_="f" * 64)]).eligible
    assert not eligibility(s, [record(s, model="m/2")]).eligible
    assert eligibility(s, [record(s, model=None)], model=None).eligible
    assert not eligibility(s, [record(s, model=None)]).eligible


def test_latest_wins_per_level_and_role_failures_block() -> None:
    s = suite()
    assert not eligibility(s, [record(s, seq=1), record(s, seq=2, passed=False)]).eligible
    assert eligibility(s, [record(s, seq=1, passed=False), record(s, seq=2)]).eligible
    role_fail = record(s, seq=3, passed=False, kind=SubjectKind.ROLE, subject="dev")
    assert not eligibility(s, [record(s, seq=1), role_fail]).eligible
    other_agent = record(s, seq=4, passed=False, subject="bob")
    assert eligibility(s, [record(s, seq=1), other_agent]).eligible


def test_an_expired_failure_no_longer_blocks_but_an_expired_pass_does_not_satisfy() -> None:
    s = suite(valid_days=5)
    old = NOW - timedelta(days=9)
    fresh_pass = record(s, seq=2)
    assert eligibility(s, [record(s, seq=1, passed=False, at=old), fresh_pass]).eligible
    assert not eligibility(s, [record(s, seq=1, at=old)]).eligible


def test_every_required_suite_must_be_satisfied() -> None:
    a = suite()
    b = EvalSuite.model_validate({**suite().model_dump(mode="json"), "id": "extra"})
    config = EvalConfig.model_validate(
        {
            "suites": [a.model_dump(mode="json"), b.model_dump(mode="json")],
            "requirements": [{"role": "dev", "suites": ["basics", "extra"]}],
        }
    )
    result = check_eligibility(
        config, role="dev", agent="ada", model="m/1", records=[record(a)], now=NOW
    )
    assert not result.eligible and len(result.reasons) == 1 and "extra" in result.reasons[0]


def test_now_must_be_timezone_aware() -> None:
    s = suite()
    config = EvalConfig.model_validate({"suites": [s.model_dump(mode="json")]})
    with pytest.raises(ValueError, match="timezone"):
        check_eligibility(
            config, role="dev", agent="ada", model=None, records=[], now=datetime(2026, 1, 1)
        )


def test_case_outcome_round_trips_through_json() -> None:
    outcome = CaseOutcome(
        case_id="a", passed=True, weight=1, critical=False, details=("x",), response_hash="a" * 64
    )
    assert CaseOutcome.model_validate(outcome.model_dump(mode="json")) == outcome


ORG: dict[str, Any] = {
    "id": "acme",
    "name": "Acme",
    "roles": [
        {"id": "cos", "name": "Boss", "kind": "chief_of_staff"},
        {"id": "dev", "name": "Developer"},
    ],
    "agents": [
        {"id": "boss", "name": "Boss", "role": "cos"},
        {"id": "ada", "name": "Ada", "role": "dev", "reports_to": "boss"},
    ],
}


def test_the_configuration_checks_eval_roles_against_the_org() -> None:
    good = {
        "org": ORG,
        "evals": {
            "suites": [suite().model_dump(mode="json")],
            "requirements": [{"role": "dev", "suites": ["basics"]}],
        },
    }
    assert parse_config(good).evals is not None
    bad_suite = suite().model_dump(mode="json") | {"role": "ghost"}
    with pytest.raises(ConfigValidationError) as caught:
        parse_config({"org": ORG, "evals": {"suites": [bad_suite]}})
    assert "$.evals.suites[0].role" in str(caught.value)


def test_the_threshold_is_normalised_so_reformatting_keeps_results_valid() -> None:
    assert suite(pass_threshold="0.8").suite_hash == suite(pass_threshold="0.80").suite_hash
    assert suite().suite_hash == suite(pass_threshold="0.8").suite_hash  # the default too


def test_building_a_config_directly_checks_eval_roles() -> None:
    from nuramesh.core.config import NuraMeshConfig

    bad = EvalConfig.model_validate(
        {"suites": [suite().model_dump(mode="json") | {"role": "ghost"}]}
    )
    with pytest.raises(ValueError, match="unknown role"):
        NuraMeshConfig.model_validate({"org": ORG, "evals": bad})
