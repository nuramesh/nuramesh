"""Frozen policy models stay immutable and input-hiding (#1144 F1 to F3)."""

import pytest
from pydantic import PrivateAttr, ValidationError

from nuramesh.core.calibration import CalibrationSample
from nuramesh.core.policy import Action, ActionKind, Policy, PolicySet, _Frozen

FAKE = "obviously-fake-test-secret-not-a-key"


def _action() -> Action:
    return Action(kind=ActionKind.NETWORK, target="example.com")


def test_f1_underscore_setattr_is_refused() -> None:
    action = _action()
    with pytest.raises(ValidationError) as info:
        action._extra = FAKE
    assert FAKE not in str(info.value)
    assert "_extra" not in str(info.value.errors())
    assert "_extra" not in action.__dict__


def test_f2_model_copy_update_is_validated() -> None:
    with pytest.raises(ValidationError) as info:
        _action().model_copy(update={"target": f"{FAKE}\n"})
    assert FAKE not in str(info.value)
    assert FAKE not in str(info.value.errors())


def test_f2_model_copy_update_unknown_field_refused() -> None:
    with pytest.raises(ValidationError):
        _action().model_copy(update={"bogus": 1})


def test_f2_replace_is_validated() -> None:
    with pytest.raises(ValidationError):
        _action().__replace__(kind="not-a-kind")  # type: ignore[attr-defined]


def test_f2_valid_copy_still_works() -> None:
    copy = _action().model_copy(update={"target": "other.example.com"})
    assert copy.target == "other.example.com"
    assert _action().model_copy() == _action()


def test_f3_calibration_errors_hide_input() -> None:
    with pytest.raises(ValidationError) as info:
        CalibrationSample.model_validate({"id": FAKE, "bogus": FAKE})
    assert FAKE not in str(info.value)
    assert FAKE not in repr(info.value)


def test_declared_private_attr_can_still_be_set() -> None:
    class WithPrivate(_Frozen):
        name: str = "x"
        _note: str = PrivateAttr(default="")

    model = WithPrivate()
    model._note = "set"
    assert model._note == "set"
    with pytest.raises(ValidationError):
        model._undeclared = "no"


def test_model_copy_update_marks_updated_keys_as_set() -> None:
    # Like pydantic: set fields of the original plus the update keys (defaults stay unset).
    original = _action()
    update = {"target": "other.example.com"}
    copy = original.model_copy(update=update)
    assert copy.model_fields_set == original.model_fields_set | update.keys()


def test_model_copy_on_policy_set_with_unset_solo_limit() -> None:
    # B1: the limit is unset by default; an explicit None is refused, so a copy must not pass it.
    original = PolicySet(policies=(Policy(role="dev"),))
    copy = original.model_copy(update={"policies": ()})
    assert copy.policies == ()
    assert copy.solo_approvals_per_hour is None
    assert "solo_approvals_per_hour" not in copy.model_dump(exclude_unset=True)
    with pytest.raises(ValidationError):
        original.model_copy(update={"solo_approvals_per_hour": None})


def test_replace_routes_through_validated_model_copy() -> None:
    with pytest.raises(ValidationError):
        _action().__replace__(target=1234)  # type: ignore[attr-defined]


def test_m3_pending_legacy_reason_is_refused_as_approval_error() -> None:
    from datetime import UTC, datetime

    from nuramesh.core.policy import Approval, ApprovalError, ApprovalMode, decide_approval

    pending = Approval(
        id="ap-1",
        run_id="r1",
        agent_id="agent-1",
        action=_action(),
        requested_at=datetime(2026, 1, 1, tzinfo=UTC),
        decision_reason="old plain text",
    )
    with pytest.raises(ApprovalError):
        decide_approval(
            pending,
            approver="human:ann",
            approve=True,
            reason_hash="a" * 64,
            at=datetime(2026, 1, 2, tzinfo=UTC),
            mode=next(iter(ApprovalMode)),
        )
