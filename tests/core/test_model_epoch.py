"""Model epochs, the floor and honouring a grant: spec/model-change section 9 (E1 to E20, P1, P2).

Expected values come from the spec's table, never from the code under test."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from autonomy_world import T, World, d

from nuramesh.core import autonomy as au
from nuramesh.core import model_epoch as me
from nuramesh.core.autonomy import Entry

M1 = {"a1": ("dev", "m1"), "a2": ("dev", "m1"), "w1": ("rev", "m1")}
CHANGED = {**M1, "a1": ("dev", "m2")}


def world(
    *, days: int = 30, kind: str = "use_tool", agents: Any = None
) -> tuple[World, dict[str, Entry]]:
    """The spec's world: one configuration at T-40d, then the grants g, g1, g2 at T-10d."""
    w = World()
    w.config(d(-40), **({} if agents is None else {"agents": agents}))
    w.ceiling(d(-40, 1), "allow")
    return w, grants(w, days=days, kind=kind)


def grants(w: World, *, days: int = 30, kind: str = "use_tool") -> dict[str, Entry]:
    return {
        "g": w.grant("g", days=days, kind=kind),
        "g1": w.grant("g1", subject=("agent", "a1"), days=days, kind=kind),
        "g2": w.grant("g2", subject=("agent", "a2"), days=days, kind=kind),
    }


def floor(w: World, at: datetime, kinds: Any, role: str = "dev") -> Entry:
    return w.decision(at, {"kind": "model_change_floor_set", "role": role, "kinds": kinds})


def drift(w: World, at: datetime, **payload: Any) -> Entry:
    return w.decision(at, {"kind": "model_drift_detected", **payload})


def state(w: World, grant: Entry, as_of: datetime = T) -> str:
    return me.honour(grant, w.entries, as_of).state


def epoch(w: World, kind: str, subject: str, as_of: datetime = T) -> int:
    return me.epoch_at(me.boundaries(kind, subject, w.entries), as_of)


def test_e1_no_change_is_epoch_zero_and_in_epoch() -> None:
    w, g = world()
    assert [epoch(w, "role", "dev"), epoch(w, "role", "rev"), epoch(w, "agent", "a1")] == [0, 0, 0]
    assert {k: state(w, v) for k, v in g.items()} == dict.fromkeys(g, "in_epoch")
    assert all(me.honour(v, w.entries, T).honoured for v in g.values())


def test_e2_a_grant_earned_under_one_epoch_is_not_honoured_under_another() -> None:
    w, g = world()
    w.config(d(-2), agents=CHANGED)
    assert epoch(w, "role", "dev") == 1 and epoch(w, "agent", "a1") == 1
    assert epoch(w, "agent", "a2") == 0 and epoch(w, "role", "rev") == 0
    assert state(w, g["g"]) == "epoch_changed" and state(w, g["g1"]) == "epoch_changed"
    assert not me.honour(g["g"], w.entries, T).honoured
    assert state(w, g["g2"]) == "in_epoch"
    assert {state(w, v, d(-3)) for v in g.values()} == {"in_epoch"}


def test_e3_the_old_identifier_coming_back_is_a_new_epoch() -> None:
    w, g = world()
    w.config(d(-2), agents=CHANGED)
    w.config(d(-1), agents=M1)
    assert epoch(w, "role", "dev") == 2 and epoch(w, "agent", "a1") == 2
    assert state(w, g["g"]) == "epoch_changed" and state(w, g["g1"]) == "epoch_changed"


def test_e4_a_grant_made_in_the_new_epoch_is_honoured() -> None:
    w, g = world()
    w.config(d(-2), agents=CHANGED)
    g3 = w.grant("g3", at=d(-1), kind="use_tool")
    result = me.honour(g3, w.entries, T)
    assert (result.state, result.own_epoch, result.honoured) == ("in_epoch", 1, True)
    assert state(w, g["g"]) == "epoch_changed"


def test_e5_the_floor_carries_a_grant_for_carry_days() -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"])
    g = grants(w)["g"]
    w.config(d(-2), agents=CHANGED)
    result = me.honour(g, w.entries, T)
    assert (result.state, result.honoured, result.carry_until) == ("carried", True, d(5))
    assert state(w, g, d(5)) == "carry_ended" and state(w, g, d(6)) == "carry_ended"
    assert not me.honour(g, w.entries, d(5)).honoured


def test_e6_a_carry_never_extends_the_expiry() -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"])
    g = grants(w, days=11)["g"]
    w.config(d(-2), agents=CHANGED)
    result = me.honour(g, w.entries, T)
    assert (result.state, result.carry_until) == ("carried", d(1))
    assert state(w, g, d(1)) == "carry_ended"


def test_e7_a_second_change_ends_the_carry_at_once() -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"])
    g = grants(w)["g"]
    w.config(d(-2), agents=CHANGED)
    w.config(d(-1), agents={**CHANGED, "a2": ("dev", "m2")})
    assert state(w, g, d(-1.5)) == "carried"
    assert state(w, g) == "epoch_changed"


def test_e8_a_floor_set_after_the_boundary_does_not_reach_back() -> None:
    w, g = world()
    w.config(d(-2), agents=CHANGED)
    floor(w, d(-1), ["use_tool"])
    assert state(w, g["g"]) == "epoch_changed"


def test_e9_a_kind_that_is_not_on_the_floor_is_not_carried() -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"])
    g = grants(w, kind="write_file")["g"]
    w.config(d(-2), agents=CHANGED)
    assert state(w, g) == "epoch_changed"


def test_e10_a_malformed_floor_entry_is_an_empty_floor() -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"])
    bad = floor(w, d(-15), ["use_tool", "run_command"])
    g = grants(w)["g"]
    change = w.config(d(-2), agents=CHANGED)
    assert me.floor_at(w.entries, "dev", bad.pos) == frozenset({"use_tool"})
    assert me.floor_at(w.entries, "dev", change.pos) == frozenset()
    assert state(w, g) == "epoch_changed"


@pytest.mark.parametrize("kinds", ["use_tool", [1], None, ["spend"], {"use_tool": True}])
def test_e10_floor_shapes_that_do_not_count(kinds: Any) -> None:
    w = World()
    w.config(d(-40))
    entry = floor(w, d(-20), kinds)
    assert me.floor_at(w.entries, "dev", entry.pos + 1) == frozenset()


def test_e11_a_drift_is_a_boundary_for_everyone_on_that_model() -> None:
    w, g = world()
    drift(w, d(-2), role="dev", model="m1")
    for kind, subject in [("role", "dev"), ("role", "rev"), ("agent", "a1"), ("agent", "w1")]:
        bounds = me.boundaries(kind, subject, w.entries)
        assert [b.cause for b in bounds] == ["drift"], (kind, subject)
    assert {state(w, v) for v in g.values()} == {"epoch_changed"}


def test_e12_a_drift_does_not_touch_subjects_on_another_model() -> None:
    w, g = world(agents={**M1, "w1": ("rev", "m9")})
    drift(w, d(-2), role="dev", model="m1")
    assert [epoch(w, "role", "rev"), epoch(w, "agent", "w1")] == [0, 0]
    assert [epoch(w, "role", "dev"), epoch(w, "agent", "a1"), epoch(w, "agent", "a2")] == [1, 1, 1]
    assert {k: state(w, v) for k, v in g.items()} == dict.fromkeys(g, "epoch_changed")


@pytest.mark.parametrize("payload", [{"role": "dev"}, {"role": "dev", "model": 7}, {}])
def test_e13_a_malformed_drift_is_a_boundary_for_every_subject(payload: dict[str, Any]) -> None:
    w, g = world(agents={**M1, "w1": ("rev", "m9")})
    drift(w, d(-2), **payload)
    for kind, subject in [("role", "dev"), ("role", "rev"), ("agent", "a1"), ("agent", "a2")]:
        assert epoch(w, kind, subject) == 1, (kind, subject)
    assert epoch(w, "agent", "w1") == 1
    assert {k: state(w, v) for k, v in g.items()} == dict.fromkeys(g, "epoch_changed")


def test_e13_a_drift_before_any_configuration_is_a_boundary_for_every_subject() -> None:
    w = World()
    drift(w, d(-41), role="dev", model="m7")
    w.config(d(-40))
    assert epoch(w, "role", "rev") == 1 and epoch(w, "agent", "a2") == 1


def _grant_with(w: World, **extra: Any) -> Entry:
    granted = d(-10)
    return w.decision(
        granted,
        {
            "kind": "autonomy_grant",
            "grant_id": "g",
            "subject_kind": "role",
            "subject_id": "dev",
            "role": "dev",
            "rule_key": 1,
            "rule_hash": "h1",
            "action_kind": "use_tool",
            "policy_contract": 3,
            "expires_in_days": 30,
            **extra,
        },
    )


@pytest.mark.parametrize(
    ("carried", "expected"),
    [
        ({"n": 0, "models": {"a1": "m1", "a2": "m1"}}, "in_epoch"),
        ({"n": 5, "models": {"a1": "m1", "a2": "m1"}}, "epoch_mismatch"),
        ({"n": 0, "models": {"a1": "m2", "a2": "m1"}}, "epoch_mismatch"),
        ({"n": True, "models": {"a1": "m1", "a2": "m1"}}, "epoch_mismatch"),
        ("0", "epoch_mismatch"),
    ],
)
def test_e14_the_carried_epoch_must_equal_the_derived_one(carried: Any, expected: str) -> None:
    w = World()
    w.config(d(-40))
    g = _grant_with(w, model_epoch=carried)
    result = me.honour(g, w.entries, T)
    assert result.state == expected
    assert result.honoured is (expected == "in_epoch")


def test_e14_the_field_a_grant_write_carries() -> None:
    w, g = world()
    w.config(d(-2), agents=CHANGED)
    assert me.epoch_field("role", "dev", w.entries, g["g"].pos) == {
        "n": 0,
        "models": {"a1": "m1", "a2": "m1"},
    }
    later = len(w.entries) + 1
    assert me.epoch_field("role", "dev", w.entries, later) == {
        "n": 1,
        "models": {"a1": "m2", "a2": "m1"},
    }
    assert me.epoch_field("agent", "a1", w.entries, later) == {"n": 1, "models": {"a1": "m2"}}


def test_e15_a_move_to_another_role_on_the_same_model_is_no_boundary() -> None:
    w, g = world()
    w.config(d(-2), agents={**M1, "a1": ("rev", "m1")})
    assert [epoch(w, "agent", "a1"), epoch(w, "role", "dev"), epoch(w, "role", "rev")] == [0, 0, 0]
    assert {k: state(w, v) for k, v in g.items()} == dict.fromkeys(g, "in_epoch")


def test_e16_a_new_member_is_tracked_from_its_first_record() -> None:
    w, g = world()
    w.config(d(-5), agents={**M1, "a3": ("dev", "m9")})
    assert epoch(w, "role", "dev") == 0
    assert {state(w, v) for v in g.values()} == {"in_epoch"}
    w.config(d(-2), agents={**M1, "a3": ("dev", "m10")})
    assert [b.at for b in me.boundaries("role", "dev", w.entries)] == [d(-2)]
    assert epoch(w, "role", "dev") == 1 and epoch(w, "agent", "a3") == 1
    assert [state(w, g[k]) for k in ("g", "g1", "g2")] == ["epoch_changed", "in_epoch", "in_epoch"]


def test_e17_no_model_to_a_model_is_a_change() -> None:
    w, g = world(agents={**M1, "a1": ("dev", None)})
    w.config(d(-2))
    assert epoch(w, "agent", "a1") == 1 and epoch(w, "role", "dev") == 1
    assert [state(w, g[k]) for k in ("g", "g1", "g2")] == [
        "epoch_changed",
        "epoch_changed",
        "in_epoch",
    ]


def test_e18_evidence_starts_at_the_latest_boundary() -> None:
    w, _ = world()
    change = w.config(d(-2), agents=CHANGED)
    assert me.evidence_start("role", "dev", w.entries, T) == change.pos
    assert me.evidence_start("agent", "a1", w.entries, T) == change.pos
    assert me.evidence_start("agent", "a2", w.entries, T) is None
    assert me.evidence_start("role", "dev", w.entries, d(-3)) is None


def test_e19_a_floor_is_per_role() -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"], role="rev")
    g = grants(w)["g"]
    w.config(d(-2), agents=CHANGED)
    assert state(w, g) == "epoch_changed"


def test_e20_a_malformed_grant_is_never_honoured() -> None:
    w = World()
    w.config(d(-40))
    g = _grant_with(w, rule_key=None)
    result = me.honour(g, w.entries, T)
    assert (result.state, result.honoured) == ("malformed", False)


def _p1_worlds() -> list[tuple[str, World, dict[str, Entry]]]:
    out = []
    w, g = world()
    w.config(d(-2), agents=CHANGED)
    out.append(("E2", w, g))
    w, g = world()
    w.config(d(-2), agents=CHANGED)
    w.config(d(-1), agents=M1)
    out.append(("E3", w, g))
    w, g = world()
    w.config(d(-2), agents={**M1, "a1": ("rev", "m1")})
    out.append(("E15", w, g))
    w, g = world()
    w.config(d(-5), agents={**M1, "a3": ("dev", "m9")})
    w.config(d(-2), agents={**M1, "a3": ("dev", "m10")})
    out.append(("E16", w, g))
    w, g = world(agents={**M1, "a1": ("dev", None)})
    w.config(d(-2))
    out.append(("E17", w, g))
    return out


P1_NOT_HONOURED = {
    "E2": {"g", "g1"},
    "E3": {"g", "g1"},
    "E15": set(),
    "E16": {"g"},
    "E17": {"g", "g1"},
}


def test_p1_with_no_floor_and_no_drift_the_epoch_rule_agrees_with_the_void_rule() -> None:
    for name, w, g in _p1_worlds():
        refused = set()
        for grant_id, item in g.items():
            parsed = au.parse_grant(item)
            assert parsed is not None
            void = au.void_reason(parsed, w.entries, T)
            if not me.honour(item, w.entries, T).honoured:
                refused.add(grant_id)
                # E16: the new member also widens the role, and 5d is checked before 5f.
                reason = "role_widened" if (name, grant_id) == ("E16", "g") else "model_changed"
                assert void is not None and void[0] == reason, (name, grant_id)
            else:
                assert void is None or void[0] != "model_changed", (name, grant_id)
        assert refused == P1_NOT_HONOURED[name], name


def _p2_expected(second_change: bool) -> list[str]:
    out = []
    for i in range(23):
        day = -3 + i / 2
        if day < -2:
            out.append("in_epoch")
        elif second_change:
            out.append("carried" if day < -1 else "epoch_changed")
        else:
            out.append("carried" if day < 5 else "carry_ended")
    return out


@pytest.mark.parametrize("second_change", [False, True])
def test_p2_once_not_honoured_never_honoured_again(second_change: bool) -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"])
    g = grants(w)["g"]
    w.config(d(-2), agents=CHANGED)
    if second_change:
        w.config(d(-1), agents={**CHANGED, "a2": ("dev", "m2")})
    steps = [d(-3) + timedelta(hours=12 * i) for i in range(23)]
    states = [me.honour(g, w.entries, at).state for at in steps]
    assert states == _p2_expected(second_change)
    seen = [me.honour(g, w.entries, at).honoured for at in steps]
    first_no = seen.index(False)
    assert not any(seen[first_no:])


@pytest.mark.parametrize(
    "carried",
    [
        {"n": True, "models": {"a1": "m1", "a2": "m1"}},
        {"n": 0, "models": ["a1", "a2"]},
        {"n": 0, "models": "m1"},
        {"n": 0, "models": None},
        {"n": 0},
        {"models": {"a1": "m1", "a2": "m1"}},
        {},
        None,
    ],
)
def test_a_malformed_model_epoch_field_is_a_mismatch(carried: Any) -> None:
    w = World()
    w.config(d(-40))
    result = me.honour(_grant_with(w, model_epoch=carried), w.entries, T)
    assert (result.state, result.honoured) == ("epoch_mismatch", False)


@pytest.mark.parametrize("days", [0, 8, -1])
def test_carry_days_can_only_be_made_shorter(days: int) -> None:
    w, g = world()
    with pytest.raises(ValueError, match="carry_days"):
        me.honour(g["g"], w.entries, T, carry_days=days)


def test_a_shorter_carry_is_allowed() -> None:
    w = World()
    w.config(d(-40))
    floor(w, d(-20), ["use_tool"])
    g = grants(w)["g"]
    w.config(d(-2), agents=CHANGED)
    assert me.honour(g, w.entries, T, carry_days=1).state == "carry_ended"
    assert me.honour(g, w.entries, d(-1.5), carry_days=1).state == "carried"


def test_floor_kinds_are_grantable_and_never_run_command() -> None:
    assert me.FLOOR_KINDS < au.GRANTABLE_KINDS
    assert "run_command" not in me.FLOOR_KINDS
    assert not me.FLOOR_KINDS & au.NEVER_GRANTABLE_KINDS


@pytest.mark.parametrize(
    ("first", "second", "boundary"),
    [
        (None, 5, True),
        (None, {"x": 1}, True),
        ("m1", 5, True),
        (5, "m1", True),
        (5, None, True),
        (5, 5, True),
        ({"x": 1}, {"x": 1}, True),
        (None, None, False),
        ("m1", "m1", False),
        ("m1", None, True),
        (None, "m1", True),
    ],
)
def test_e21_a_non_string_model_is_a_boundary_and_null_is_a_value(
    first: Any, second: Any, boundary: bool
) -> None:
    w = World()
    w.config(d(-40), agents={"a1": ("dev", first)})
    w.config(d(-2), agents={"a1": ("dev", second)})
    entries = list(w.entries)
    expected = 1 if boundary else 0
    assert len(me.boundaries("agent", "a1", entries)) == expected
    assert len(me.boundaries("role", "dev", entries)) == expected
