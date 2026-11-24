"""R2.11 dashboard formulas (spec/r211 section 3): recomputed by hand from the spec."""

from datetime import UTC, datetime
from typing import Any

from dash_helpers import AS_OF, START, World

from nuramesh.telemetry.dashboards import Window, compute

W = Window(start=START, end=AS_OF, as_of=AS_OF)
KEYS = {"as_of", "from", "to", "organization", "partial", "scanned_events", "dropped_values"}


def run(route: str, w: World, window: Window = W, cap: int = 1_000_000) -> dict[str, Any]:
    return compute(route, w.streams(), window, cap, "acme")


def outcomes(w: World, passed: int, failed: int, error: int) -> None:
    n = 0
    for outcome, count in (("passed", passed), ("failed", failed), ("error", error)):
        for _ in range(count):
            n += 1
            w.gate("r1", f"2026-01-02T00:00:{n:02d}Z", outcome, kind=None, task=f"t{n}")


def test_r211_01_pass_rate_and_envelope() -> None:
    w = World()
    outcomes(w, 3, 1, 2)
    out = run("verification", w)
    assert (out["passed"], out["failed"], out["error"], out["pass_rate"]) == (3, 1, 2, "0.7500")
    assert set(out) == KEYS | {"passed", "failed", "error", "pass_rate", "gates", "roles"}
    assert out["organization"] == "acme"
    assert out["from"] == "2026-01-01T00:00:00Z" and out["to"] == "2026-01-08T00:00:00Z"
    assert out["partial"] is False
    w2 = World()
    outcomes(w2, 0, 0, 2)
    assert run("verification", w2)["pass_rate"] is None


def test_r211_02_rounding_half_even() -> None:
    for passed, failed, want in (
        (1, 2, "0.3333"),
        (1, 7, "0.1250"),
        (1, 15, "0.0625"),
        (1, 31, "0.0312"),
    ):
        w = World()
        outcomes(w, passed, failed, 0)
        assert run("verification", w)["pass_rate"] == want


def test_r211_03_per_gate_and_role() -> None:
    w = World()
    w.gate("r1", "2026-01-02T00:00:01Z", "passed", gate="g2", kind=None, task="a")
    w.gate("r1", "2026-01-02T00:00:02Z", "failed", gate="g1", kind=None, task="b", role="qa")
    w.gate("r1", "2026-01-02T00:00:03Z", "passed", gate="g1", kind=None, task="c", role="qa")
    out = run("verification", w)
    assert [g["gate"] for g in out["gates"]] == ["g1", "g2"]
    g1 = out["gates"][0]
    assert (g1["passed"], g1["failed"], g1["error"], g1["pass_rate"]) == (1, 1, 0, "0.5000")
    assert set(g1) == {"gate", "passed", "failed", "error", "pass_rate"}
    assert [r["role"] for r in out["roles"]] == [None, "qa"]


def test_r211_04_catch_rate() -> None:
    w = World()
    n = 0
    for outcome, kind, count in (
        ("failed", "verifier", 4),
        ("passed", "verifier", 6),
        ("failed", "human", 2),
    ):
        for _ in range(count):
            n += 1
            w.gate(
                "r1", f"2026-01-02T00:{n // 60:02d}:{n % 60:02d}Z", outcome, kind=kind, task=f"t{n}"
            )
    w.gate("r1", "2026-01-03T00:00:00Z", "failed", kind=None, task="orphan")
    out = run("verifiers", w)
    assert out["catch_rate"] == "0.4000"
    assert (out["verifier_passed"], out["verifier_failed"]) == (6, 4)
    assert out["unknown_kind_results"] == 1
    assert set(out) == KEYS | {
        "verifier_passed",
        "verifier_failed",
        "catch_rate",
        "unknown_kind_results",
        "rounds_to_pass",
        "done_without_verifier_pass",
    }


def test_r211_05_rounds_to_pass() -> None:
    w = World()
    seq = {
        "a": ["passed"],
        "b": ["failed", "failed", "passed"],
        "c": ["failed"] * 4 + ["passed"],
        "d": ["failed"],
        "e": [],
        "f": ["failed", "passed", "passed"],
    }
    n = 0
    for task, results in seq.items():
        for i, outcome in enumerate(results, start=1):
            n += 1
            w.gate("r1", f"2026-01-02T01:00:{n:02d}Z", outcome, task=task, submission=i)
    for task in ("a", "b", "c", "e", "f"):
        w.done("r1", task, "2026-01-03T00:00:00Z")
    out = run("verifiers", w)
    assert out["rounds_to_pass"] == {"1": 1, "2": 1, "3": 1, "4+": 1}
    assert out["done_without_verifier_pass"] == 1


def test_r211_06_cost_exact_decimals_and_currencies() -> None:
    w = World()
    w.call("r1", "2026-01-02T00:00:01Z", cost="0.10", basis="actual")
    w.call("r1", "2026-01-02T00:00:02Z", cost="0.25", basis="provisional")
    w.call("r1", "2026-01-02T00:00:03Z", basis="unknown")
    w.call("r1", "2026-01-02T00:00:04Z")
    w.call("r1", "2026-01-02T00:00:05Z", cost="9", basis="reserved")
    out = run("cost-latency", w)
    usd = out["currencies"]["USD"]
    assert usd == {"total": "0.35", "calls": 5, "unknown_cost_calls": 3}
    assert set(out) == KEYS | {
        "currencies",
        "models",
        "roles",
        "p50",
        "p95",
        "max",
        "latency_skipped",
    }


def test_r211_07_latency_nearest_rank() -> None:
    def lat(values: list[Any]) -> dict[str, Any]:
        w = World()
        for i, v in enumerate(values):
            w.call("r1", f"2026-01-02T00:00:{i % 60:02d}Z", latency=v, cost="1", basis="actual")
        return run("cost-latency", w)

    out = lat([100 * i for i in range(1, 11)])
    assert (out["p50"], out["p95"], out["max"]) == (500, 1000, 1000)
    out = lat([42])
    assert (out["p50"], out["p95"], out["max"]) == (42, 42, 42)
    out = lat([100, -5, "x", 300])
    assert (out["latency_skipped"], out["p50"], out["p95"]) == (2, 100, 300)
    out = lat([True])
    assert out["p50"] is None and out["latency_skipped"] == 1
    out = lat(list(range(1, 21)))
    assert out["p95"] == 19
    out = lat(list(range(1, 101)))
    assert (out["p50"], out["p95"]) == (50, 95)


def test_r211_08_approvals_waiting() -> None:
    w = World()
    w.ask("r1", "2026-01-07T23:00:00Z", "r1a", "spend")
    w.ask("r1", "2026-01-07T23:30:00Z", "r2a", "deploy")
    w.ask("r1", "2026-01-07T22:00:00Z", "r3a")
    w.decide("r1", "2026-01-07T22:30:00Z", "r3a", True)
    w.ask("r1", "2026-01-07T21:00:00Z", "r4a")
    w.decide("r1", "2026-01-07T21:30:00Z", "r4a", False)
    w.ask("r2", "2026-01-07T20:00:00Z", "r5a")
    w.end("r2", "2026-01-07T20:30:00Z")
    out = run("approvals", w)
    assert out["waiting"] == 2 and out["oldest_waiting_seconds"] == 3600
    assert out["waiting_by_action"] == {"other": 1, "spend": 1}
    assert (out["decided_approved"], out["decided_denied"]) == (1, 1)
    assert set(out) == KEYS | {
        "waiting",
        "oldest_waiting_seconds",
        "waiting_by_action",
        "decided_approved",
        "decided_denied",
    }


def test_r211_09_window_edges() -> None:
    w = World()
    w.gate("r1", "2026-01-01T00:00:00Z", "failed", kind=None, task="t2")
    w.gate("r1", "2026-01-08T00:00:00Z", "passed", kind=None)
    out = run("verification", w)
    assert (out["passed"], out["failed"]) == (0, 1)


def test_r211_24_waiting_is_state_not_window() -> None:
    w = World()
    w.ask("r1", "2026-01-02T00:00:00Z", "a0")  # before from, still open
    w.ask("r1", "2026-01-07T00:00:00Z", "a6")  # after `to` of the second window
    w.ask("r1", "2026-01-07T12:00:00Z", "a7")
    w.ask("r1", "2026-01-08T00:00:01Z", "late")  # after as_of
    w.decide("r1", "2026-01-08T00:00:02Z", "a7", True)  # decided after as_of: still waiting
    out = run("approvals", w, Window(utc_("2026-01-05"), AS_OF, AS_OF))
    assert out["waiting"] == 3
    out = run("approvals", w, Window(utc_("2026-01-01"), utc_("2026-01-06"), AS_OF))
    assert out["waiting"] == 3
    assert (out["decided_approved"], out["decided_denied"]) == (0, 0)


def utc_(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def test_r211_25_gate_matching_over_the_whole_stream() -> None:
    w = World()
    w.gate("r1", "2026-01-02T00:00:00Z", "failed", start_at="2025-12-31T00:00:00Z")
    w.gate("r1", "2026-01-03T00:00:00Z", "failed", kind="banana", task="t2")
    out = run("verifiers", w)
    assert (out["verifier_failed"], out["unknown_kind_results"]) == (1, 1)
    capped = run("verifiers", w, cap=1)
    assert capped["verifier_failed"] == 1 and capped["partial"] is True


def test_r211_26_cap_order_and_counting() -> None:
    w = World()
    for i in range(1, 4):  # stream a: three matching entries
        w.gate("a", f"2026-01-02T00:00:0{i}Z", "passed", kind=None, task=f"t{i}")
    w.call("b", "2026-01-02T00:00:00Z")  # b: one non-matching, then two matching
    w.gate("b", "2026-01-02T00:00:01Z", "passed", kind=None, task="x")
    w.gate("b", "2026-01-02T00:00:02Z", "passed", kind=None, task="y")
    out = run("verification", w, cap=5)  # a: started + 3 = 4 window entries, b1 = run.started
    assert out["scanned_events"] == 5 and out["partial"] is True
    assert out["passed"] == 3
    full = run("verification", w)
    assert full["partial"] is False and full["passed"] == 5


def test_other_organizations_streams_are_not_read_by_compute_callers() -> None:
    w = World()
    w.gate("r1", "2026-01-02T00:00:00Z", "passed", kind=None)
    assert run("verification", w)["passed"] == 1


def test_the_value_guard_drops_what_is_not_an_id() -> None:
    from nuramesh.telemetry.dashboards import is_safe_string

    good = ["g1", "azure-openai/gpt-5.4", "a" * 31, "agent-" + "a" * 31, "qa"]
    bad = ["a" * 32, "x@example.test", "/etc/passwd", "src/app.py", "human:ann", "a/b/c", "../x"]
    assert all(is_safe_string(v) for v in good)
    assert not any(is_safe_string(v) for v in bad)
    assert not is_safe_string(5) and not is_safe_string(None)


def test_a_dropped_gate_id_is_counted_and_never_listed() -> None:
    w = World()
    w.gate("r1", "2026-01-02T00:00:00Z", "passed", gate="a" * 40, kind=None)
    w.gate("r1", "2026-01-02T00:00:01Z", "passed", gate="g1", kind=None, task="t2")
    out = run("verification", w)
    assert out["passed"] == 2 and [g["gate"] for g in out["gates"]] == ["g1"]
    assert out["dropped_values"] == {"value_shape": 1}
    assert "a" * 40 not in str(out)


def test_a_result_without_a_task_never_enters_rounds() -> None:
    w = World()
    w.gate("r1", "2026-01-02T00:00:00Z", "passed", task=None)
    out = run("verifiers", w)
    assert out["verifier_passed"] == 1 and out["rounds_to_pass"] == {
        "1": 0,
        "2": 0,
        "3": 0,
        "4+": 0,
    }


def test_a_start_is_matched_only_once() -> None:
    w = World()
    w.gate("r1", "2026-01-02T00:00:00Z", "error")  # start (verifier) + error completion
    w.gate("r1", "2026-01-02T00:00:05Z", "failed", kind=None)  # same key, no new start
    out = run("verifiers", w)
    assert out["unknown_kind_results"] == 1 and out["verifier_failed"] == 0


def test_money_is_exact_text_never_a_float() -> None:
    import json

    w = World()
    w.call("r1", "2026-01-02T00:00:01Z", cost="1.000", basis="actual", model="m2", role="qa")
    out = run("cost-latency", w)
    assert out["currencies"]["USD"]["total"] == "1.000"
    assert out["models"] == [
        {"model": "m2", "currency": "USD", "total": "1.000", "calls": 1, "unknown_cost_calls": 0}
    ]
    assert not floats_in(json.loads(json.dumps(out)))


def floats_in(value: Any) -> list[float]:
    """Every float anywhere in a nested answer (a figure must be text, an int or null)."""
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [f for v in value.values() for f in floats_in(v)]
    if isinstance(value, list):
        return [f for v in value for f in floats_in(v)]
    return []


def test_a_sum_is_the_exact_sum_not_a_rounded_one() -> None:
    w = World()
    w.call("r1", "2026-01-02T00:00:01Z", cost="12345678901234567890.123456789", basis="actual")
    w.call("r1", "2026-01-02T00:00:02Z", cost="0.000000001", basis="actual")
    out = run("cost-latency", w)
    assert out["currencies"]["USD"]["total"] == "12345678901234567890.123456790"
    assert not floats_in(out)
