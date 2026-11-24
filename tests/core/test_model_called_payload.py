"""The `model.called` payload model: the optional `role` is a slug and nothing else (cost spec)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nuramesh.core.models import ModelCalledPayload

BASE = {"agent": "dev", "provider": "fake", "model": "m1", "outcome": "ok"}


def test_role_is_optional_so_old_entries_still_parse() -> None:
    assert ModelCalledPayload.model_validate(BASE).role is None


def test_role_is_kept_when_present_and_other_fields_are_tolerated() -> None:
    p = ModelCalledPayload.model_validate({**BASE, "role": "developer", "usage": {"x": 1}})
    assert p.role == "developer" and p.agent == "dev" and p.outcome == "ok"


@pytest.mark.parametrize("bad", ["Developer", "has space", "a@b.c", "", "x" * 64, 7])
def test_role_must_be_a_slug_not_free_text(bad: object) -> None:
    with pytest.raises(ValidationError):
        ModelCalledPayload.model_validate({**BASE, "role": bad})


def test_price_version_time_is_the_effective_time_not_the_call_time() -> None:
    known = {"at": "2026-10-20T10:00:00+00:00", "effective_from": "2026-01-01T00:00:00+00:00"}
    p = ModelCalledPayload.model_validate({**BASE, "price": known})
    assert p.price_version_at == datetime(2026, 1, 1, tzinfo=UTC)
    override = {"at": "2026-10-20T10:00:00+00:00", "override_set_at": "2026-10-01T00:00:00+00:00"}
    q = ModelCalledPayload.model_validate({**BASE, "price": override})
    assert q.price_version_at == datetime(2026, 10, 1, tzinfo=UTC)


def test_price_version_time_is_none_without_a_version_time_or_a_price() -> None:
    # `price.at` is the call time (spec/costs section 1): it is never taken for a version
    p = ModelCalledPayload.model_validate({**BASE, "price": {"at": "2026-01-01T00:00:00Z"}})
    assert p.price_version_at is None
    assert ModelCalledPayload.model_validate(BASE).price_version_at is None


@pytest.mark.parametrize("bad", ["not a time", "2026-01-01T00:00:00", 5])
def test_a_malformed_price_time_is_a_validation_error_at_parse(bad: object) -> None:
    with pytest.raises(ValidationError):
        ModelCalledPayload.model_validate({**BASE, "price": {"effective_from": bad}})
