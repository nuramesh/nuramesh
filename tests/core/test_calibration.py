"""R2.08 calibration, pure core: the spec's CAL cases (spec/r208/README.md section 6)."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from nuramesh.core import calibration as cal
from nuramesh.core import verifier as verifier_module
from nuramesh.core.calibration import (
    CalibrationRecord,
    CalibrationSuite,
    CalibrationSuiteError,
    Label,
    Observation,
    SampleResult,
    classify,
    parse_suite,
    prompt_hash,
    score_run,
    standing,
    verifier_input,
)
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.verifier import build_messages

R = SampleResult
LOOSE = {"min_catch_rate": "0.0001", "max_false_accept_rate": "1.0000", "min_accept_rate": "0.0001"}


def _suite(
    bad: int = 5,
    good: int = 5,
    *,
    thresholds: dict[str, Any] | None = None,
    critical_bad: tuple[int, ...] = (),
    **over: Any,
) -> CalibrationSuite:
    samples = [
        {
            "id": f"b{i}",
            "output": {"text": f"bad {i}"},
            "label": "bad",
            "defect_criteria": ["c1"],
            "critical": i in critical_bad,
        }
        for i in range(bad)
    ] + [{"id": f"g{i}", "output": {"text": f"good {i}"}, "label": "good"} for i in range(good)]
    data: dict[str, Any] = {
        "id": "s1",
        "task": {"title": "Write a greeting", "instructions": "Say hello."},
        "criteria": [{"id": "c1", "text": "Greets"}, {"id": "c2", "text": "Polite"}],
        "samples": samples,
        **over,
    }
    if thresholds is not None:
        data["thresholds"] = thresholds
    return parse_suite(data)


def _results(
    bad: int,
    good: int,
    *,
    c: int = 0,
    m: int = 0,
    w: int = 0,
    e: int = 0,
    a: int = 0,
    r: int = 0,
    x: int = 0,
) -> list[SampleResult]:
    assert c + m + w + e == bad and a + r + x == good
    return (
        [R.CAUGHT] * c
        + [R.MISSED] * m
        + [R.WRONG_REASON] * w
        + [R.BAD_ERROR] * e
        + [R.ACCEPTED] * a
        + [R.FALSE_REJECT] * r
        + [R.GOOD_ERROR] * x
    )


def _score(suite: CalibrationSuite, results: list[SampleResult]) -> tuple[str, bool]:
    s = score_run(suite, results)
    rates = s.rates
    text = (
        f"{rates.catch_rate} / {rates.false_accept_rate} / {rates.accept_rate} / {rates.agreement}"
    )
    return text, s.passed


# (id, B, G, kwargs, expected rates, passed, suite kwargs)
SCORING = [
    ("01", 20, 20, dict(c=20, a=20), "1.0000 / 0.0000 / 1.0000 / 1.0000", True, {}),
    ("02", 20, 20, dict(c=19, m=1, a=20), "0.9500 / 0.0500 / 1.0000 / 0.9750", True, {}),
    ("03", 20, 20, dict(c=18, m=2, a=20), "0.9000 / 0.1000 / 1.0000 / 0.9500", False, {}),
    ("04", 20, 20, dict(c=19, w=1, a=20), "0.9500 / 0.0000 / 1.0000 / 0.9750", True, {}),
    ("05", 20, 20, dict(c=18, w=1, e=1, a=20), "0.9000 / 0.0000 / 1.0000 / 0.9500", False, {}),
    ("06", 7, 7, dict(c=6, m=1, a=7), "0.8571 / 0.1429 / 1.0000 / 0.9285", False, {}),
    ("07", 6, 6, dict(c=6, a=5, r=1), "1.0000 / 0.0000 / 0.8333 / 0.9166", True, {}),
    ("08", 10, 10, dict(c=10, a=7, r=2, x=1), "1.0000 / 0.0000 / 0.7000 / 0.8500", False, {}),
    (
        "09",
        20,
        20,
        dict(c=19, m=1, a=20),
        "0.9500 / 0.0500 / 1.0000 / 0.9750",
        False,
        {"critical_bad": (19,)},
    ),
    (
        "10",
        20,
        20,
        dict(c=19, w=1, a=20),
        "0.9500 / 0.0000 / 1.0000 / 0.9750",
        False,
        {"critical_bad": (19,)},
    ),
    (
        "11",
        6,
        5,
        dict(c=4, m=2, a=5),
        "0.6666 / 0.3334 / 1.0000 / 0.8181",
        False,
        {
            "thresholds": {
                "min_catch_rate": "0.6667",
                "max_false_accept_rate": "1.0000",
                "min_accept_rate": "0.0001",
            }
        },
    ),
    (
        "12",
        6,
        5,
        dict(c=4, m=2, a=5),
        "0.6666 / 0.3334 / 1.0000 / 0.8181",
        True,
        {
            "thresholds": {
                "min_catch_rate": "0.6666",
                "max_false_accept_rate": "1.0000",
                "min_accept_rate": "0.0001",
            }
        },
    ),
    ("13", 5, 5, dict(m=5, a=5), "0.0000 / 1.0000 / 1.0000 / 0.5000", False, {}),
    ("14", 5, 5, dict(c=5, x=5), "1.0000 / 0.0000 / 0.0000 / 0.5000", False, {}),
    ("15", 9, 9, dict(c=8, m=1, a=9), "0.8888 / 0.1112 / 1.0000 / 0.9444", False, {}),
    ("83", 20, 20, dict(c=19, e=1, a=20), "0.9500 / 0.0000 / 1.0000 / 0.9750", False, {}),
    ("84", 20, 20, dict(c=20, a=19, x=1), "1.0000 / 0.0000 / 0.9500 / 0.9750", False, {}),
]


@pytest.mark.parametrize("case", SCORING, ids=[f"CAL-{c[0]}" for c in SCORING])
def test_scoring(case: Any) -> None:
    _, bad, good, kw, rates, passed, suite_kw = case
    suite = _suite(bad, good, **suite_kw)
    assert _score(suite, _results(bad, good, **kw)) == (rates, passed)


def test_cal16_too_few_samples_refused() -> None:
    for bad, good in ((4, 5), (5, 4)):
        with pytest.raises(CalibrationSuiteError, match="calibration_suite_invalid"):
            _suite(bad, good)


def test_cal17_bad_defect_criteria_refused() -> None:
    base = _suite()
    data = base.model_dump(mode="json")
    for index, defect in ((0, []), (5, ["c1"]), (0, ["nope"])):
        broken = {**data, "samples": [dict(s) for s in data["samples"]]}
        broken["samples"][index]["defect_criteria"] = defect
        with pytest.raises(CalibrationSuiteError, match="calibration_suite_invalid"):
            parse_suite(broken)


@pytest.mark.parametrize(
    ("case", "label", "defects", "not_met", "obs", "expected"),
    [
        ("20", Label.BAD, ["c1"], ["c1"], Observation.FAILED, R.CAUGHT),
        ("21", Label.BAD, ["c1"], ["c2"], Observation.FAILED, R.WRONG_REASON),
        ("22", Label.BAD, ["c1", "c2"], ["c2"], Observation.FAILED, R.CAUGHT),
        ("23", Label.BAD, ["c1"], [], Observation.PASSED, R.MISSED),
        ("24", Label.BAD, ["c1"], [], Observation.ERROR, R.BAD_ERROR),
        ("25", Label.GOOD, [], [], Observation.PASSED, R.ACCEPTED),
        ("26", Label.GOOD, [], ["c1"], Observation.FAILED, R.FALSE_REJECT),
        ("27", Label.GOOD, [], [], Observation.ERROR, R.GOOD_ERROR),
    ],
)
def test_classification(
    case: str,
    label: Label,
    defects: list[str],
    not_met: list[str],
    obs: Observation,
    expected: SampleResult,
) -> None:
    assert classify(label, obs, defects, not_met) is expected, f"CAL-{case}"


def test_cal28_stopped_run_is_scored_and_fails() -> None:
    suite = _suite(10, 10, thresholds=LOOSE)
    results = [R.CAUGHT, R.CAUGHT, R.ACCEPTED] + [
        R.BAD_ERROR if s.label is Label.BAD else R.GOOD_ERROR for s in suite.samples[3:]
    ]
    # the first three samples are b0, b1, b2; fix the order to the suite's labels
    results = [
        (R.CAUGHT if s.label is Label.BAD else R.ACCEPTED)
        if i < 3
        else (R.BAD_ERROR if s.label is Label.BAD else R.GOOD_ERROR)
        for i, s in enumerate(suite.samples)
    ]
    score = score_run(suite, results)
    assert score.counts.bad_error + score.counts.good_error == 17
    assert not score.passed


# -- standing (CAL-30..43), now = 2026-11-30T12:00:00Z, valid_days 30 ----------------------------

NOW = datetime(2026, 11, 30, 12, tzinfo=UTC)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _record(
    suite: CalibrationSuite,
    results: list[SampleResult],
    at: str,
    seq: int,
    *,
    verifier: str = "v1",
    model: str = "fake/a",
    **tamper: Any,
) -> CalibrationRecord:
    payload = CalibrationRecord.ledger_payload(
        verifier=verifier, model=model, suite=suite, run_id="cal-x", results=results,
        cost=Decimal("0"), currency="USD", cost_complete=True,
    )  # fmt: skip
    payload.update(tamper)
    event: Any = SimpleNamespace(payload=payload, at=_at(at), seq=seq)
    return CalibrationRecord.from_event(event)


def _good(suite: CalibrationSuite) -> list[SampleResult]:
    return [R.CAUGHT if s.label is Label.BAD else R.ACCEPTED for s in suite.samples]


def _bad(suite: CalibrationSuite) -> list[SampleResult]:
    return [R.MISSED if s.label is Label.BAD else R.ACCEPTED for s in suite.samples]


def _stand(suite: CalibrationSuite, records: list[CalibrationRecord], **kw: Any) -> Any:
    return standing(suite, records, verifier="v1", model=kw.get("model", "fake/a"), now=NOW)


NONE = "not calibrated for this suite version, prompt and model"


def test_cal30_none() -> None:
    s = _stand(_suite(), [])
    assert (s.calibrated, s.reason) == (False, NONE)


def test_cal31_pass() -> None:
    suite = _suite()
    assert _stand(suite, [_record(suite, _good(suite), "2026-11-20T00:00:00Z", 1)]).calibrated


def test_cal32_exactly_valid_days_is_expired() -> None:
    suite = _suite()
    s = _stand(suite, [_record(suite, _good(suite), "2026-10-31T12:00:00Z", 1)])
    assert (s.calibrated, s.reason) == (False, "expired")


def test_cal33_latest_failed() -> None:
    suite = _suite()
    recs = [
        _record(suite, _good(suite), "2026-11-10T00:00:00Z", 1),
        _record(suite, _bad(suite), "2026-11-20T00:00:00Z", 2),
    ]
    s = _stand(suite, recs)
    assert (s.calibrated, s.reason) == (False, "the latest result failed")


def test_cal34_failure_cleared_by_newer_pass() -> None:
    suite = _suite()
    recs = [
        _record(suite, _bad(suite), "2026-11-10T00:00:00Z", 1),
        _record(suite, _good(suite), "2026-11-20T00:00:00Z", 2),
    ]
    assert _stand(suite, recs).calibrated


def test_cal35_36_37_38_other_hash_prompt_model_verifier() -> None:
    suite = _suite()
    for tamper, kw in (
        ({"suite_hash": "e" * 64}, {}),
        ({"prompt_hash": "e" * 64}, {}),
        ({}, {"model": "fake/b"}),
        ({}, {"verifier": "v2"}),
    ):
        rec = _record(suite, _good(suite), "2026-11-20T00:00:00Z", 1, **kw, **tamper)
        s = _stand(suite, [rec])
        assert not s.calibrated and s.reason == NONE, (tamper, kw)


def test_cal39_expired_failure_only() -> None:
    suite = _suite()
    s = _stand(suite, [_record(suite, _bad(suite), "2026-10-01T00:00:00Z", 1)])
    assert (s.calibrated, s.reason) == (False, "expired")


def test_cal40_counts_that_do_not_reproduce_fail() -> None:
    suite = _suite()
    rec = _record(suite, _bad(suite), "2026-11-20T00:00:00Z", 1, passed=True, caught=5, missed=0)
    s = _stand(suite, [rec])
    assert (s.calibrated, s.reason) == (False, "the latest result does not reproduce")


def test_cal41_only_the_failing_suite_named() -> None:
    s1 = _suite(id="s1")
    s2 = _suite(id="s2")
    config = cal.CalibrationConfig(suites=(s1, s2))
    reasons = cal.gate_standing_reasons(
        config, ["s1", "s2"], notes="", max_output_tokens=1500, criteria=s1.criteria,
        records=[_record(s1, _good(s1), "2026-11-20T00:00:00Z", 1)], verifier="v1",
        model="fake/a", now=NOW,
    )  # fmt: skip
    assert reasons == [f"s2: {NONE}"]


def test_cal42_43_trend() -> None:
    suite = _suite(10, 10, thresholds={"min_catch_rate": "0.9000"})

    def run(caught: int) -> list[SampleResult]:
        out, seen = [], 0
        for s in suite.samples:
            if s.label is Label.BAD:
                out.append(R.CAUGHT if seen < caught else R.MISSED)
                seen += 1
            else:
                out.append(R.ACCEPTED)
        return out

    up = [
        _record(suite, run(9), "2026-11-01T00:00:00Z", 1),
        _record(suite, run(10), "2026-11-20T00:00:00Z", 2),
    ]
    assert _stand(suite, up).trend == Decimal("0.1000")
    down = [
        _record(suite, run(10), "2026-11-01T00:00:00Z", 1),
        _record(suite, run(9), "2026-11-20T00:00:00Z", 2),
    ]
    assert _stand(suite, down).trend == Decimal("-0.1000")
    assert _stand(suite, down[:1]).trend is None


def test_cal81_recent_results_newest_first_at_most_five() -> None:
    suite = _suite()
    recs = [_record(suite, _good(suite), f"2026-11-{10 + i}T00:00:00Z", i + 1) for i in range(7)]
    s = _stand(suite, recs)
    assert [v.seq for v in s.recent_results] == [7, 6, 5, 4, 3]


# -- prompt, independence, hashes ---------------------------------------------------------------


def test_cal64_prompts_of_good_and_bad_sample_are_byte_equal() -> None:
    suite = _suite(
        task={"title": "Write a greeting", "instructions": "Say hello."},
    )
    data = suite.model_dump(mode="json")
    samples = [dict(s) for s in data["samples"]]
    samples[0]["output"] = samples[5]["output"] = {"text": "same words"}
    samples[0]["id"], samples[5]["id"] = "zzbad", "zzgood"
    suite = parse_suite({**data, "samples": samples})
    bad, good = suite.samples[0], suite.samples[5]
    assert bad.label is Label.BAD and good.label is Label.GOOD
    one = build_messages(verifier_input(suite, bad))
    two = build_messages(verifier_input(suite, good))
    assert one == two
    text = "".join(m.content for m in one)
    for word in ("label", "defect", "critical", bad.id, good.id, "thresholds"):
        assert word not in text, word


def test_cal68_core_imports_only_core_stdlib_pydantic() -> None:
    import inspect
    import re
    import sys

    source = inspect.getsource(cal)
    for name in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M):
        root = name.split(".")[0]
        ok = (
            root in sys.stdlib_module_names
            or root == "pydantic"
            or name.startswith("nuramesh.core")
        )
        assert ok, name


def test_cal76_prompt_hash_changes_with_each_fixed_part(monkeypatch: pytest.MonkeyPatch) -> None:
    base = prompt_hash()
    assert len(base) == 64
    messages = build_messages(cal._PROBE)
    assert "Criterion ids to answer for" in "".join(m.content for m in messages)
    for name in ("_SYSTEM", "_REMINDER"):
        with monkeypatch.context() as m:
            m.setattr(verifier_module, name, getattr(verifier_module, name) + " changed")
            assert prompt_hash() != base, name
    with monkeypatch.context() as m:
        original = verifier_module._section
        m.setattr(verifier_module, "_section", lambda label, body: original(label, body) + "!")
        assert prompt_hash() != base


def test_cal77_dropped_critical_sample_does_not_reproduce() -> None:
    suite = _suite(critical_bad=(4,))
    results = _good(suite)
    rec = _record(suite, results, "2026-11-20T00:00:00Z", 1)
    dropped = [o.model_dump(mode="json") for o in rec.outcomes if o.sample_id != "b4"]
    tampered = _record(suite, results, "2026-11-20T00:00:00Z", 1, outcomes=dropped)
    assert cal.reproduces(rec, suite) and not cal.reproduces(tampered, suite)
    s = _stand(suite, [tampered])
    assert s.reason == "the latest result does not reproduce"


def test_cal78_invalid_suites_and_defaults() -> None:
    data = _suite().model_dump(mode="json")

    def bad(**over: Any) -> None:
        with pytest.raises(CalibrationSuiteError, match="calibration_suite_invalid"):
            parse_suite({**data, **over})

    bad(extra_field=1)
    bad(valid_days=0)
    bad(valid_days=366)
    bad(version=0)
    bad(max_output_tokens=16001)
    bad(thresholds={"min_catch_rate": "0"})
    bad(thresholds={"min_catch_rate": "1.0001"})
    bad(thresholds={"min_catch_rate": "0.95001"})
    samples = [dict(s) for s in data["samples"]]
    bad(samples=[*samples, dict(samples[0])])  # duplicate id
    bad(samples=[{**samples[5], "critical": True}, *samples[:5], *samples[6:]])
    many = [{**samples[0], "id": f"x{i}"} for i in range(196)]
    bad(samples=[*samples, *many])  # 201
    with pytest.raises(CalibrationSuiteError):
        parse_suite({**data, "task": {"title": "t", "instructions": "i"}, "x": 1.5})
    bad(thresholds={"min_catch_rate": 0.95})  # a float threshold (rule 1)
    bad(thresholds={"max_false_accept_rate": 0.05})
    bad(thresholds={"min_accept_rate": 0.8})
    loaded = parse_suite({k: v for k, v in data.items() if k not in ("thresholds", "valid_days")})
    assert loaded.valid_days == 30 and loaded.max_output_tokens == 1500
    t = loaded.thresholds
    assert (str(t.min_catch_rate), str(t.max_false_accept_rate), str(t.min_accept_rate)) == (
        "0.9500", "0.0500", "0.8000",
    )  # fmt: skip
    ninety_five = parse_suite({**data, "thresholds": {"min_catch_rate": "0.95"}})
    assert str(ninety_five.thresholds.min_catch_rate) == "0.9500"


def test_cal79_critical_bad_error_fails() -> None:
    suite = _suite(10, 10, critical_bad=(0,), thresholds=LOOSE)
    results = _good(suite)
    results[0] = R.BAD_ERROR
    assert not score_run(suite, results).passed


def test_suite_hash_changes_with_any_edit() -> None:
    a, b = _suite(), _suite(valid_days=31)
    assert a.suite_hash != b.suite_hash and len(a.suite_hash) == 64


def test_cal82_payload_small_and_has_no_outputs() -> None:
    suite = _suite(100, 100)
    payload = CalibrationRecord.ledger_payload(
        verifier="v1", model="fake/a", suite=suite, run_id="cal-x", results=_good(suite),
        cost=Decimal("0.09"), currency="USD", cost_complete=True,
    )  # fmt: skip
    import json

    text = json.dumps(payload)
    assert len(text) < 64_000 and "bad 0" not in text


# -- validators, refusals and gate helpers (coverage of the error paths) -------------------------


def _sample(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "x1", "output": {"text": "hi"}, "label": "good"}
    return {**base, **over}


def test_sample_output_must_be_canonical_and_small() -> None:
    data = _suite().model_dump(mode="json")
    for output in ({"x": 1.5}, {"text": "a" * cal.MAX_OUTPUT_BYTES}):
        samples = [*data["samples"][:-1], _sample(output=output)]
        with pytest.raises(CalibrationSuiteError, match="calibration_suite_invalid"):
            parse_suite({**data, "samples": samples})


def test_sample_label_must_match_its_flags() -> None:
    data = _suite().model_dump(mode="json")

    def refused(sample: dict[str, Any]) -> None:
        with pytest.raises(CalibrationSuiteError, match="calibration_suite_invalid"):
            parse_suite({**data, "samples": [*data["samples"][:-1], sample]})

    refused(_sample(defect_criteria=["c1"]))  # a good sample with a defect
    refused(_sample(critical=True))  # a good sample marked critical
    refused(_sample(label="bad"))  # a bad sample naming no defect
    refused(_sample(label="bad", defect_criteria=["c1", "c1"]))  # the same defect twice


def test_suite_and_config_ids_must_be_unique() -> None:
    data = _suite().model_dump(mode="json")
    dup_criteria = [*data["criteria"], dict(data["criteria"][0])]
    with pytest.raises(CalibrationSuiteError, match="calibration_suite_invalid"):
        parse_suite({**data, "criteria": dup_criteria})
    with pytest.raises(ValueError, match="suite ids must be unique"):
        cal.CalibrationConfig(suites=(_suite(), _suite()))


def test_run_with_no_bad_or_no_good_samples_never_passes() -> None:
    thresholds = _suite().thresholds
    only_bad = cal.count([R.CAUGHT, R.CAUGHT])
    only_good = cal.count([R.ACCEPTED, R.ACCEPTED])
    assert not cal.run_passes(thresholds, only_bad, [(False, R.CAUGHT)] * 2)
    assert not cal.run_passes(thresholds, only_good, [(False, R.ACCEPTED)] * 2)


def test_score_run_refuses_a_wrong_length_or_a_class_that_does_not_fit() -> None:
    suite = _suite()
    with pytest.raises(ValueError, match="exactly one result per sample"):
        score_run(suite, _good(suite)[:-1])
    swapped = _good(suite)
    swapped[0], swapped[-1] = swapped[-1], swapped[0]  # a good class for a bad sample
    with pytest.raises(ValueError, match="doesn't fit sample"):
        score_run(suite, swapped)


def test_a_record_whose_classes_do_not_fit_the_samples_does_not_reproduce() -> None:
    suite = _suite()
    rec = _record(suite, _good(suite), "2026-11-20T00:00:00Z", 1)
    outcomes = [o.model_dump(mode="json") for o in rec.outcomes]
    outcomes[0]["result"] = "accepted"  # sample b0 is bad
    tampered = _record(suite, _good(suite), "2026-11-20T00:00:00Z", 1, outcomes=outcomes)
    assert not cal.reproduces(tampered, suite)


def test_standing_needs_an_aware_now() -> None:
    suite = _suite()
    with pytest.raises(ValueError, match="timezone-aware"):
        standing(suite, [], verifier="v1", model="fake/a", now=datetime(2026, 11, 30, 12))


def test_gate_mismatch_names_what_the_suite_did_not_calibrate() -> None:
    suite = _suite()
    same: dict[str, Any] = {
        "notes": suite.notes,
        "max_output_tokens": suite.max_output_tokens,
        "criteria": suite.criteria,
    }
    assert cal.gate_mismatch(suite, **same) is None
    assert cal.gate_mismatch(suite, **{**same, "notes": "other"}) == (
        "gate notes differs from the suite"
    )
    assert cal.gate_mismatch(suite, **{**same, "max_output_tokens": 1}) == (
        "gate max_output_tokens differs from the suite"
    )
    other = (Criterion(id="c1", text="Something else"),)
    assert cal.gate_mismatch(suite, **{**same, "criteria": other}) == (
        "gate criteria differs from the suite"
    )


def test_gate_standing_reasons_for_a_missing_suite_and_a_mismatch() -> None:
    suite = _suite()
    config = cal.CalibrationConfig(suites=(suite,))
    common: dict[str, Any] = dict(
        max_output_tokens=suite.max_output_tokens, criteria=suite.criteria, records=[],
        verifier="v1", model="fake/a", now=NOW,
    )  # fmt: skip
    assert cal.gate_standing_reasons(config, ["nope"], notes=suite.notes, **common) == [
        f"nope: {cal.REASON_UNAVAILABLE}"
    ]
    assert cal.gate_standing_reasons(None, ["s1"], notes=suite.notes, **common) == [
        f"s1: {cal.REASON_UNAVAILABLE}"
    ]
    assert cal.gate_standing_reasons(config, ["s1"], notes="other", **common) == [
        "s1: gate notes differs from the suite"
    ]


# -- the plan check of a verifier gate's calibration_suites (rule 21), the quiet paths -----------

from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind  # noqa: E402

_ORG = OrgGraph(
    id="acme",
    name="Acme",
    roles=(
        Role(id="cos", name="CoS", kind=RoleKind.CHIEF_OF_STAFF),
        Role(id="v", name="Verifier", kind=RoleKind.VERIFIER, default_model="fake/verifier-1"),
    ),
    agents=(
        Agent(id="boss", name="Boss", role="cos"),
        Agent(id="vera", name="Vera", role="v", reports_to="boss"),
    ),
)


def _gate(kind: GateKind = GateKind.VERIFIER, **config: Any) -> GateSpec:
    return GateSpec(id="g1", kind=kind, checks=("c1",), config=config)


def _dod(gate: GateSpec) -> DefinitionOfDone:
    return DefinitionOfDone(criteria=(Criterion(id="c1", text="Greets"),), gates=(gate,))


def test_gate_calibration_problems_ignore_gates_that_cannot_be_checked() -> None:
    config = cal.CalibrationConfig(suites=(_suite(),))

    def problems(gate: GateSpec) -> list[str]:
        return cal.gate_calibration_problems(gate, _dod(gate), _ORG, config)

    assert problems(_gate(GateKind.RULE, rules=[])) == []  # not a verifier gate
    assert problems(_gate(verifier="Bad Id", calibration_suites=["s1"])) == []  # bad config
    assert problems(_gate(verifier="vera")) == []  # no calibration_suites: opt-in


def test_gate_calibration_problems_use_the_effective_model_of_an_unknown_agent() -> None:
    config = cal.CalibrationConfig(suites=(_suite(),))
    gate = _gate(verifier="ghost", calibration_suites=["s1"])
    only_fake = cal.gate_calibration_problems(
        gate, _dod(gate), _ORG, config, effective_ref=lambda _: "fake/a"
    )
    assert not any(cal.CALIBRATION_REAL_MODEL_GATE in p for p in only_fake)
    real = cal.gate_calibration_problems(
        gate, _dod(gate), _ORG, config, effective_ref=lambda _: "azure-openai/gpt"
    )
    assert any(cal.CALIBRATION_REAL_MODEL_GATE in p for p in real)
