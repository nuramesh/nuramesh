"""A policy validation error never carries the rejected input (#853 d1)."""

import json

import pytest
from pydantic import ValidationError

from nuramesh.core.policy import Action, ActionKind, Approval, _scrubbed

FAKE = "obviously-fake-test-secret-not-a-key"


def _assert_clean(exc: ValidationError) -> None:
    assert FAKE not in str(exc)
    assert FAKE not in repr(exc)
    assert FAKE not in str(exc.errors())
    assert FAKE not in exc.json()
    assert FAKE not in json.dumps(exc.errors(include_context=True), default=str)


def test_action_error_hides_input() -> None:
    with pytest.raises(ValidationError) as info:
        Action(kind=ActionKind.NETWORK, target=f"user:{FAKE}@example.com/")
    _assert_clean(info.value)


def test_action_bad_field_type_hides_input() -> None:
    with pytest.raises(ValidationError) as info:
        Action.model_validate({"kind": FAKE, "target": "x"})
    _assert_clean(info.value)
    assert info.value.errors()[0]["loc"] == ("kind",)


def test_approval_from_dict_hides_input() -> None:
    with pytest.raises(ValidationError) as info:
        Approval.model_validate(
            {
                "id": "a1",
                "run_id": "r1",
                "agent_id": "ag",
                "action": {"kind": "network", "target": f"{FAKE}@example.com/x"},
                "requested_at": "2026-01-01T00:00:00Z",
            }
        )
    _assert_clean(info.value)


def test_model_validate_json_hides_input() -> None:
    with pytest.raises(ValidationError) as info:
        Action.model_validate_json(json.dumps({"kind": "network", "target": f"{FAKE}@h/"}))
    _assert_clean(info.value)


def _chain_clean(exc: ValidationError) -> None:
    _assert_clean(exc)
    assert exc.__context__ is None
    assert exc.__cause__ is None
    assert FAKE not in str(exc.args)


def _action_dict() -> dict[str, object]:
    return {"kind": "network", "target": f"{FAKE}@h/"}


def _policy_dict() -> dict[str, object]:
    return {"role": "worker", "rules": [{"action": FAKE, "decision": "allow"}]}


def _entry_points() -> list[object]:
    from pydantic import TypeAdapter

    from nuramesh.core.policy import Policy, PolicySet

    bad = _action_dict()
    return [
        lambda: Action(**bad),  # type: ignore[arg-type]
        lambda: Action.model_validate(bad),
        lambda: Action.model_validate_json(json.dumps(bad)),
        lambda: Action.model_validate_strings(bad),
        lambda: TypeAdapter(Action).validate_python(bad),
        lambda: TypeAdapter(Action).validate_json(json.dumps(bad)),
        lambda: Approval.model_validate(
            {"id": "a", "run_id": "r", "agent_id": "x", "action": bad, "requested_at": "2026-01-01"}
        ),
        lambda: PolicySet.model_validate({"policies": [_policy_dict()]}),
        lambda: Policy.model_validate(_policy_dict()),
    ]


@pytest.mark.parametrize("index", range(9))
def test_no_context_cause_or_input_anywhere(index: int) -> None:
    call = _entry_points()[index]
    with pytest.raises(ValidationError) as info:
        call()  # type: ignore[operator]
    _chain_clean(info.value)


def test_frozen_setattr_scrubbed() -> None:
    action = Action(kind=ActionKind.NETWORK, target="example.com")
    with pytest.raises(ValidationError) as info:
        action.target = FAKE
    _chain_clean(info.value)
    assert info.value.errors()[0]["type"] == "frozen_instance"


def test_extra_key_name_not_in_loc() -> None:
    with pytest.raises(ValidationError) as info:
        Action.model_validate({"kind": "network", "target": "x", FAKE: 1})
    _chain_clean(info.value)
    assert FAKE not in str(info.value.errors()[0]["loc"])
    assert info.value.errors()[0]["type"] == "extra_forbidden"


def test_malformed_json_document_not_in_error() -> None:
    # json_invalid fails before the wrap validator runs; the document may be a stored row.
    with pytest.raises(ValidationError) as info:
        Approval.model_validate_json('{"' + FAKE + '": [1,')
    _chain_clean(info.value)
    assert info.value.errors()[0]["type"] == "json_invalid"


def test_malformed_json_bytes_not_in_error() -> None:
    with pytest.raises(ValidationError) as info:
        Action.model_validate_json(('{"target": "' + FAKE).encode())
    _chain_clean(info.value)


def test_frozen_setattr_name_not_in_loc() -> None:
    action = Action(kind=ActionKind.NETWORK, target="example.com")
    with pytest.raises(ValidationError) as info:
        setattr(action, FAKE, 1)
    _chain_clean(info.value)
    assert FAKE not in str(info.value.errors()[0]["loc"])
    assert FAKE not in info.value.json()
    assert info.value.errors()[0]["type"] == "frozen_instance"


def test_frozen_delattr_name_not_in_loc() -> None:
    action = Action(kind=ActionKind.NETWORK, target="example.com")
    with pytest.raises(ValidationError) as info:
        delattr(action, FAKE)
    _chain_clean(info.value)
    assert FAKE not in str(info.value.errors()[0]["loc"])
    assert FAKE not in info.value.json()
    assert info.value.errors()[0]["type"] == "frozen_instance"


def test_frozen_field_name_not_in_loc() -> None:
    # `frozen_field` is raised for a frozen field of a non-frozen model; no policy model is one, so
    # the scrub is exercised directly with a pydantic-built error of that type.
    raw = ValidationError.from_exception_data(
        "Action", [{"type": "frozen_field", "loc": (FAKE,), "input": FAKE}]
    )
    scrubbed = _scrubbed(raw)
    line = scrubbed.errors()[0]
    assert line["type"] == "frozen_field"
    assert FAKE not in str(line["loc"])
    assert FAKE not in scrubbed.json()
