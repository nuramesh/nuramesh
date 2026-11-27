"""R1.27-H rule cases R-1 to R-13: who may show reason and note text."""

import copy
from collections.abc import Mapping
from types import MappingProxyType

import pytest

from nuramesh.core.redaction import reason_text_visible, redact_payload

HASH = "a" * 64
HIDDEN_ACTORS = [
    "gate:rule",
    "gate:test_command",
    "gate",
    "systems",
    "System",
    "system:model-governance",
    "system:catalog-seed",
    "operator:bob",
    "human:u1",
    "service:cli",
    "agent:x",
    "api:k",
    "",
    None,
]


def test_r1_only_the_exact_actor_system_may_show_text() -> None:
    assert reason_text_visible("system") is True


@pytest.mark.parametrize("actor", HIDDEN_ACTORS)
def test_r2_every_other_actor_is_hidden(actor: str | None) -> None:
    assert reason_text_visible(actor) is False


def test_r3_a_person_reason_is_hidden_and_the_input_is_unchanged() -> None:
    source = {"reason": "abc", "from": "a"}
    before = copy.deepcopy(source)
    payload, hidden = redact_payload("task.transitioned", "human:u1", source)
    assert payload == {"reason": None, "from": "a"} and hidden == ["reason"]
    assert source == before


def test_r4_system_keeps_its_text() -> None:
    payload, hidden = redact_payload("task.transitioned", "system", {"reason": "abc", "from": "a"})
    assert payload == {"reason": "abc", "from": "a"} and hidden == []


def test_r5_nested_values_in_lists_and_mappings_are_hidden_names_once() -> None:
    source = {"x": [{"reason": "deep"}, {"note": "n"}], "y": {"note": "m"}}
    payload, hidden = redact_payload("model.called", "human:u1", source)
    assert payload == {"x": [{"reason": None}, {"note": None}], "y": {"note": None}}
    assert hidden == ["note", "reason"]


def test_r6_hash_and_code_keys_are_never_touched() -> None:
    source = {"reason_hash": HASH, "reason_withheld": "org_key_destroyed", "why": "w"}
    payload, hidden = redact_payload("task.transitioned", "human:u1", source)
    assert payload == source and hidden == []


@pytest.mark.parametrize("value", [None, ""])
def test_r7_empty_values_stay_and_are_not_listed(value: str | None) -> None:
    payload, hidden = redact_payload("task.transitioned", "human:u1", {"reason": value})
    assert payload == {"reason": value} and hidden == []


def test_r8_a_non_string_value_is_hidden_too() -> None:
    payload, hidden = redact_payload("task.transitioned", "human:u1", {"reason": 123})
    assert payload == {"reason": None} and hidden == ["reason"]


def test_r9_an_incident_note_is_removed_for_system_too() -> None:
    kind = {"kind": "engine_incident"}
    payload, hidden = redact_payload("decision.recorded", "system", {**kind, "note": "n"})
    assert "note" not in payload and payload["note_present"] is True and hidden == ["note"]
    payload, hidden = redact_payload("decision.recorded", "system", {**kind, "note": ""})
    assert "note" not in payload and payload["note_present"] is False and hidden == []
    payload, hidden = redact_payload("decision.recorded", "system", dict(kind))
    assert "note" not in payload and payload["note_present"] is False and hidden == []


def test_r10_the_actor_does_not_matter_for_the_incident_note() -> None:
    source = {"kind": "engine_incident", "note": "n"}
    payload, hidden = redact_payload("decision.recorded", "human:u1", source)
    assert "note" not in payload and payload["note_present"] is True and hidden == ["note"]


def test_r11_another_decision_kind_keeps_the_ordinary_rule() -> None:
    source = {"kind": "step_restarted", "note": "n"}
    payload, hidden = redact_payload("decision.recorded", "human:u1", source)
    assert payload["note"] is None and "note_present" not in payload and hidden == ["note"]


def test_r12_a_non_string_incident_note_is_not_present() -> None:
    source = {"kind": "engine_incident", "note": 123}
    payload, hidden = redact_payload("decision.recorded", "system", source)
    assert "note" not in payload and payload["note_present"] is False and hidden == ["note"]


def test_r13_nested_values_of_an_incident_are_hidden_for_a_person() -> None:
    source = {
        "kind": "engine_incident",
        "note": "n",
        "details": {"note": "deep", "reason": "r"},
    }
    payload, hidden = redact_payload("decision.recorded", "human:u1", source)
    assert "note" not in payload and payload["note_present"] is True
    assert payload["details"] == {"note": None, "reason": None}
    assert hidden == ["note", "reason"]


def test_frozen_payloads_are_walked_and_come_back_plain() -> None:
    frozen = MappingProxyType({"details": ({"reason": "y"}, MappingProxyType({"why": "z"}))})
    payload, hidden = redact_payload("budget.adjusted", "human:u3", frozen)
    assert payload == {"details": [{"reason": None}, {"why": "z"}]} and hidden == ["reason"]
    assert type(payload) is dict and not isinstance(payload["details"], Mapping | tuple)


def test_every_free_text_field_name_in_the_core_models_has_a_rule() -> None:
    """Spec 8 risk 1: a new reason- or note-like field in a core model must be decided here (add
    it to HIDDEN_KEYS in core/redaction.py, or to KNOWN_NOT_PERSON_TEXT with the reason)."""
    import dataclasses
    import importlib
    import pkgutil
    import re

    from pydantic import BaseModel

    import nuramesh.core as core
    from nuramesh.core.redaction import HIDDEN_KEYS

    # Codes, counts or hashes, or a distinct field name read as stored. `decision_reason` is the one
    # model field that IS a person's plain text (pre-T47 approvals): no ledger payload writes it
    # (`policy.py` writes only `decision_reason_hash`) and `approval_json` nulls it on every route.
    # `void_reason` (GrantState): a code the system writes for a void grant (config_changed,
    # promotion, ...), never a person's text.
    # `reason_sha256` (Merge Gate 6.8 `vm-reset-1`, 6.10 `vm-override-1`): the 64-hex SHA-256 of
    # the admin's reason file, never the text (SV-10, GT-16: "reason hash"; LA-1 and L-02 allow only
    # ids, hashes, states, reason codes and times in a `merge.*` payload). The digest shape is
    # pinned by `test_a_reason_sha256_field_can_only_hold_a_digest` below.
    # `tier_reasons` (6.1 evidence bundle): codes the collector computes from the changed paths and
    # the tier list at the base tip (EB-4), such as `tier3_path` (TL-17: codes only, never a path);
    # a service writes them, never a person, and EB-6 keeps verdict text, prompts and personal
    # data out of the bundle.
    # `reason_code` (PolicyDecision, NG-10 (c), NG-11): a closed code the policy evaluator sets from
    # module constants (`wildcard_private_address`, `denied_by_rule`, ...), never a person's text;
    # the free text beside it is `reason`, which has its own rule.
    known_not_person_text = {
        "revoke_reason", "by_reason", "reasons", "finish_reason", "decision_reason",
        "decision_reason_hash", "notes", "gate_notes", "reason_days",
        "void_reason", "wrong_reason", "reason_sha256", "tier_reasons", "reason_code",
        "comment_id",  # an integer forge comment id (VerdictRecord, UnparsedRecord): no text
    }  # fmt: skip
    pattern = re.compile(r"reason|note|comment|explanation|justification|remark|rationale")
    found: set[str] = set()
    for info in pkgutil.iter_modules(core.__path__):
        module = importlib.import_module(f"nuramesh.core.{info.name}")
        for obj in vars(module).values():
            if not isinstance(obj, type) or obj.__module__ != module.__name__:
                continue
            if issubclass(obj, BaseModel):
                names = list(obj.model_fields)
            elif dataclasses.is_dataclass(obj):
                names = [f.name for f in dataclasses.fields(obj)]
            else:
                continue
            found.update(n for n in names if pattern.search(n))
    assert found <= set(HIDDEN_KEYS) | known_not_person_text, sorted(
        found - set(HIDDEN_KEYS) - known_not_person_text
    )


def test_a_reason_sha256_field_can_only_hold_a_digest() -> None:
    """`reason_sha256` is exempt from hiding only because it cannot carry text: every core model
    field of that name must be a strict 64-hex string (a digest), and at least two exist."""
    import importlib
    import pkgutil

    from pydantic import BaseModel

    import nuramesh.core as core

    checked = 0
    for info in pkgutil.iter_modules(core.__path__):
        module = importlib.import_module(f"nuramesh.core.{info.name}")
        for obj in vars(module).values():
            if not isinstance(obj, type) or obj.__module__ != module.__name__:
                continue
            if not issubclass(obj, BaseModel) or "reason_sha256" not in obj.model_fields:
                continue
            field = obj.model_fields["reason_sha256"]
            patterns = [getattr(item, "pattern", None) for item in field.metadata]
            assert field.annotation is str and patterns == [r"^[0-9a-f]{64}$"], obj.__name__
            checked += 1
    assert checked >= 2
