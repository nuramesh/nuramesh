"""Verification line, presets, renames, ids and templates (app spec §5; T39). The blind-written
cases in spec/config-s5/ are run by test_config_s5_cases.py; these are the author's own tests."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from nuramesh.core.config import ConfigValidationError, NuraMeshConfig, parse_config
from nuramesh.core.org import IssueCode, OrgGraph, OrgValidationError
from nuramesh.core.policy import Action, ActionKind, Decision, evaluate
from nuramesh.core.presets import (
    GatePreset,
    GateTemplate,
    PolicyPreset,
    PresetConfig,
    assignment_changes,
    instantiate_gates,
    preset_changes,
    resolve_policies,
    rule_origins,
)
from nuramesh.core.renames import (
    RenameKind,
    check_ids_stable,
    id_changes,
    rename_in_config,
    valid_name,
)
from nuramesh.core.templates import (
    TemplateVersion,
    check_versions,
    config_from_template,
    save_as_template,
    template_document,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def org_data(**agent_changes: Any) -> dict[str, Any]:
    agents: dict[str, dict[str, Any]] = {
        "boss": {"id": "boss", "name": "Boss", "role": "cos"},
        "dev": {"id": "dev", "name": "Dev", "role": "dev", "reports_to": "boss"},
        "rhea": {"id": "rhea", "name": "Rhea", "role": "rev", "reports_to": "boss"},
    }
    for key, change in agent_changes.items():
        agents[key] = {**agents[key], **change}
    return {
        "id": "acme",
        "name": "Acme",
        "roles": [
            {"id": "cos", "name": "Chief of Staff", "kind": "chief_of_staff"},
            {"id": "dev", "name": "Developer"},
            {"id": "rev", "name": "Reviewer", "kind": "verifier"},
        ],
        "agents": list(agents.values()),
    }


def codes(data: dict[str, Any]) -> set[tuple[str, str | None]]:
    try:
        OrgGraph.build(data)
    except OrgValidationError as exc:
        return {(i.code.value, i.subject) for i in exc.issues}
    return set()


# ---- the verification line --------------------------------------------------------------------


def test_a_verifier_may_check_a_peer_and_reporting_is_a_separate_line() -> None:
    org = OrgGraph.build(org_data(rhea={"checks": ["dev"]}))
    assert org.checkers_of("dev") == ("rhea",)
    assert org.checkers_of("rhea") == ()
    assert org.agent("rhea").reports_to == "boss"  # the management line is untouched


def test_checks_default_to_nothing_and_leave_the_version_hash_alone() -> None:
    before = OrgGraph.build(org_data()).version_hash()
    assert OrgGraph.build(org_data(rhea={"checks": []})).version_hash() == before
    assert OrgGraph.build(org_data(rhea={"checks": ["dev"]})).version_hash() != before


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"rhea": {"checks": ["rhea"]}}, {(IssueCode.SELF_CHECK, "rhea")}),
        ({"rhea": {"checks": ["ghost"]}}, {(IssueCode.UNKNOWN_CHECKED, "rhea")}),
        ({"rhea": {"checks": ["dev", "dev"]}}, {(IssueCode.DUPLICATE_CHECK, "rhea")}),
        ({"dev": {"checks": ["boss"]}}, {(IssueCode.CHECKER_NOT_VERIFIER, "dev")}),
        # the verifier is above the work it checks
        (
            {"dev": {"reports_to": "rhea"}, "rhea": {"checks": ["dev"]}},
            {(IssueCode.VERIFIER_IN_CHAIN, "rhea")},
        ),
        # the verifier answers to the author
        (
            {"rhea": {"reports_to": "dev", "checks": ["dev"]}},
            {(IssueCode.VERIFIER_IN_CHAIN, "rhea")},
        ),
        # two levels up counts too
        (
            {
                "dev": {"reports_to": "rhea"},
                "boss": {"reports_to": None},
                "rhea": {"checks": ["dev"]},
            },
            {(IssueCode.VERIFIER_IN_CHAIN, "rhea")},
        ),
    ],
)
def test_verification_line_problems(
    changes: dict[str, Any], expected: set[tuple[Any, str]]
) -> None:
    found = codes(org_data(**changes))
    assert {(c, s) for c, s in found if c in {e[0].value for e in expected}} == {
        (c.value, s) for c, s in expected
    }


def test_a_verifier_checking_the_top_agent_is_refused_and_a_cousin_is_fine() -> None:
    assert (IssueCode.VERIFIER_IN_CHAIN.value, "rhea") in codes(org_data(rhea={"checks": ["boss"]}))
    # a verifier under the Chief of Staff checking a worker in another branch is independent
    data = org_data(rhea={"checks": ["dev"]})
    data["agents"].append({"id": "lead", "name": "Lead", "role": "dev", "reports_to": "boss"})
    data["agents"][1]["reports_to"] = "lead"
    assert codes(data) == set()


# ---- presets in the configuration ---------------------------------------------------------------


def deny_merge() -> dict[str, Any]:
    return {"action": "merge", "decision": "deny", "target": "*"}


def presets_data() -> dict[str, Any]:
    return {
        "policy": [
            {"id": "no-merge", "name": "No merge", "rules": [deny_merge()]},
            {
                "id": "write-code",
                "name": "Write code",
                "rules": [{"action": "write_file", "decision": "allow", "target": "src/**"}],
            },
        ],
        "assignments": [{"role": "dev", "policy_presets": ["no-merge", "write-code"]}],
    }


def config(**extra: Any) -> NuraMeshConfig:
    return parse_config({"org": org_data(), **extra})


def decision(cfg: NuraMeshConfig, role: str, kind: ActionKind, target: str) -> Decision:
    return evaluate(
        cfg.effective_policies.policy_for(role), Action(kind=kind, target=target)
    ).decision


def test_a_role_uses_presets_and_adds_rules_on_top_but_deny_always_wins() -> None:
    own = {
        "policies": [
            {
                "role": "dev",
                "rules": [
                    {"action": "merge", "decision": "allow", "target": "*"},  # cannot loosen
                    {"action": "write_file", "decision": "deny", "target": "src/secret/**"},
                ],
            }
        ]
    }
    cfg = config(presets=presets_data(), policies=own)
    assert decision(cfg, "dev", ActionKind.WRITE_FILE, "src/app/a.py") is Decision.ALLOW
    assert decision(cfg, "dev", ActionKind.MERGE, "main") is Decision.DENY  # preset deny wins
    assert decision(cfg, "dev", ActionKind.WRITE_FILE, "src/secret/k") is Decision.DENY  # own deny
    assert decision(cfg, "dev", ActionKind.USE_TOOL, "git") is Decision.DENY  # default deny
    assert rule_origins(cfg.policies, cfg.presets, "dev") == [
        "preset:no-merge",
        "preset:write-code",
        "role",
        "role",
    ]


def test_editing_a_preset_changes_every_role_that_uses_it() -> None:
    data = presets_data()
    data["assignments"].append({"role": "rev", "policy_presets": ["write-code"]})
    cfg = config(presets=data)
    assert decision(cfg, "rev", ActionKind.WRITE_FILE, "src/x.py") is Decision.ALLOW
    edited = {**data, "policy": [data["policy"][0], {**data["policy"][1], "rules": []}]}
    again = config(presets=edited)
    for role in ("dev", "rev"):
        assert decision(again, role, ActionKind.WRITE_FILE, "src/x.py") is Decision.DENY


def test_without_presets_the_effective_policies_are_the_policies() -> None:
    cfg = config()
    assert cfg.effective_policies == cfg.policies
    assert resolve_policies(cfg.policies, PresetConfig()) == cfg.policies


@pytest.mark.parametrize(
    ("mutate", "where"),
    [
        (lambda d: d["assignments"].append({"role": "ghost", "policy_presets": []}), "$.presets"),
        (lambda d: d["assignments"][0].update(policy_presets=["nope"]), "$.presets"),
        (lambda d: d["policy"].append(dict(d["policy"][0])), "$.presets"),
        (
            lambda d: d.update(gate=[_gate_preset(evals=["no-such-suite"])]),
            "$.presets.gate[0].evals",
        ),
    ],
)
def test_presets_are_validated_with_a_path(mutate: Any, where: str) -> None:
    data = presets_data()
    mutate(data)
    with pytest.raises(ConfigValidationError) as exc:
        config(presets=data)
    assert any(i.path.startswith(where) for i in exc.value.issues), exc.value.issues


def test_an_assignment_to_a_role_the_org_lacks_names_the_assignment() -> None:
    data = presets_data()
    data["assignments"][0]["role"] = "ghost"
    with pytest.raises(ConfigValidationError) as exc:
        config(presets=data)
    assert [i.path for i in exc.value.issues] == ["$.presets.assignments[0].role"]


def test_too_many_effective_rules_are_a_configuration_error() -> None:
    rule = {"action": "use_tool", "decision": "allow", "target": "t"}
    data = {
        "policy": [
            {
                "id": f"p{i}",
                "name": "P",
                "rules": [dict(rule, target=f"t{i}-{n}") for n in range(200)],
            }
            for i in range(3)
        ],
        "assignments": [{"role": "dev", "policy_presets": ["p0", "p1", "p2"]}],
    }
    with pytest.raises(ConfigValidationError) as exc:
        config(presets=data)
    assert "limit" in exc.value.issues[0].message


def _gate_preset(**changes: Any) -> dict[str, Any]:
    return {
        "id": "py-tests",
        "name": "Python tests",
        "gates": [
            {"id": "rule", "kind": "rule"},
            {"id": "tests", "kind": "test_command", "config": {"command": ["pytest"]}},
            {"id": "review", "kind": "verifier"},
        ],
        **changes,
    }


def test_gate_presets_are_ordered_cheapest_first_and_become_real_gates() -> None:
    preset = GatePreset.model_validate(_gate_preset())
    gates = instantiate_gates(preset, {"rule": ["a"], "tests": ["b"], "review": ["c"]})
    assert [g.id for g in gates] == ["rule", "tests", "review"]
    assert list(gates[1].config["command"]) == ["pytest"]
    with pytest.raises(ValueError, match="cheapest first"):
        GatePreset(
            id="bad",
            name="Bad",
            gates=(
                GateTemplate(id="v", kind=_verifier_kind()),
                GateTemplate(id="r", kind=_rule_kind()),
            ),
        )
    with pytest.raises(ValueError, match="say which criteria"):
        instantiate_gates(preset, {"rule": ["a"]})
    with pytest.raises(ValueError, match="doesn't have"):
        instantiate_gates(preset, {"rule": ["a"], "tests": ["b"], "review": ["c"], "typo": ["d"]})


def _verifier_kind() -> Any:
    from nuramesh.core.gates import GateKind

    return GateKind.VERIFIER


def _rule_kind() -> Any:
    from nuramesh.core.gates import GateKind

    return GateKind.RULE


def test_a_gate_preset_needs_a_required_gate() -> None:
    with pytest.raises(ValueError, match="required"):
        GatePreset.model_validate(
            _gate_preset(gates=[{"id": "r", "kind": "rule", "required": False}])
        )


def test_preset_changes_say_what_changed_and_who_is_affected() -> None:
    before = PresetConfig.model_validate(presets_data())
    renamed = PresetConfig.model_validate(
        {
            **presets_data(),
            "policy": [
                {**presets_data()["policy"][0], "name": "Renamed"},
                *presets_data()["policy"][1:],
            ],
        }
    )
    assert preset_changes(before, renamed) == []  # a rename is not a change
    edited_data = presets_data()
    edited_data["policy"][0]["rules"] = []
    edited_data["policy"].append({"id": "extra", "name": "Extra", "rules": []})
    edited = PresetConfig.model_validate(edited_data)
    changes = preset_changes(before, edited)
    assert [(c.id, c.change, c.roles) for c in changes] == [
        ("extra", "created", ()),
        ("no-merge", "updated", ("dev",)),
    ]
    assert changes[1].old_hash != changes[1].new_hash and changes[0].old_hash is None
    gone = preset_changes(edited, before)
    assert [(c.id, c.change) for c in gone] == [("extra", "removed"), ("no-merge", "updated")]
    assert preset_changes(None, None) == []
    assert assignment_changes(before, edited) == []
    other = PresetConfig.model_validate(
        {
            **presets_data(),
            "assignments": [{"role": "dev", "policy_presets": ["write-code", "no-merge"]}],
        }
    )
    assert assignment_changes(before, other) == ["dev"]  # order is part of the assignment
    assert assignment_changes(None, before) == ["dev"]


def test_preset_models_are_immutable_and_strict() -> None:
    preset = PolicyPreset(id="p", name="P")
    with pytest.raises(ValueError):
        preset.name = "Q"
    with pytest.raises(ValueError):
        PolicyPreset.model_validate({"id": "P!", "name": "x"})


# ---- renames and stable ids ----------------------------------------------------------------------


def test_a_rename_changes_one_name_and_no_id() -> None:
    cfg = config(presets=presets_data())
    new, record = rename_in_config(cfg, RenameKind.AGENT, "dev", "  Dana  ")
    assert new.org.agent("dev").name == "Dana"
    assert record.ledger_payload() == {
        "kind": "rename",
        "what": "agent",
        "id": "dev",
        "old_name": "Dev",
        "new_name": "Dana",
    }
    assert id_changes(cfg, new).removed == {k: frozenset() for k in id_changes(cfg, new).removed}
    check_ids_stable(cfg, new)
    # the agent keeps its manager and every other agent is untouched
    assert new.org.agent("dev").reports_to == "boss"
    assert [a.name for a in new.org.agents if a.id != "dev"] == ["Boss", "Rhea"]


@pytest.mark.parametrize(
    ("kind", "item", "name", "error"),
    [
        (RenameKind.AGENT, "ghost", "X", KeyError),
        (RenameKind.AGENT, "dev", "   ", ValueError),
        (RenameKind.AGENT, "dev", "x" * 121, ValueError),
        (RenameKind.AGENT, "dev", " Dev ", ValueError),  # unchanged after trimming
        (RenameKind.ORG, "other", "X", KeyError),
        (RenameKind.ROLE, "ghost", "X", KeyError),
        (RenameKind.POLICY_PRESET, "ghost", "X", KeyError),
        (RenameKind.GATE_PRESET, "ghost", "X", KeyError),
        (RenameKind.TEMPLATE, "t", "X", ValueError),
    ],
)
def test_rename_refusals(kind: RenameKind, item: str, name: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        rename_in_config(config(presets=presets_data()), kind, item, name)


def test_every_kind_of_rename_works() -> None:
    cfg = config(presets=presets_data())
    org, _ = rename_in_config(cfg, RenameKind.ORG, "acme", "Acme Inc")
    assert org.org.name == "Acme Inc" and org.org.id == "acme"
    role, _ = rename_in_config(cfg, RenameKind.ROLE, "dev", "Engineer")
    assert role.org.role("dev").name == "Engineer"
    preset, rec = rename_in_config(cfg, RenameKind.POLICY_PRESET, "no-merge", "Never merge")
    assert preset.presets is not None
    renamed = preset.presets.policy_preset("no-merge")
    assert renamed is not None and renamed.name == "Never merge"
    assert rec.old_name == "No merge"
    # a rename of a preset isn't a content change and doesn't alter what agents may do
    assert preset_changes(cfg.presets, preset.presets) == []
    assert preset.effective_policies == cfg.effective_policies


def test_dropping_an_id_is_refused_and_adding_one_is_fine() -> None:
    old = config(presets=presets_data())
    only_two = parse_config({"org": {**org_data(), "agents": org_data()["agents"][:2]}})
    changes = id_changes(old, only_two)
    assert changes.removed["agent"] == frozenset({"rhea"}) and changes.any_removed
    with pytest.raises(ValueError, match="rhea"):
        check_ids_stable(old, only_two)
    check_ids_stable(only_two, old)  # adding is fine
    assert id_changes(only_two, old).added["agent"] == frozenset({"rhea"})
    # a preset id counts too
    bare = config()
    with pytest.raises(ValueError, match="no-merge"):
        check_ids_stable(old, bare)


def test_valid_name_trims_and_limits() -> None:
    assert valid_name("  Ann ") == "Ann"
    with pytest.raises(ValueError):
        valid_name("")


# ---- templates --------------------------------------------------------------------------------


def saved(cfg: NuraMeshConfig, *, name: str = "My team", existing: Any = (), **kw: Any) -> Any:
    return save_as_template(
        cfg, template_id="my-team", name=name, saved_at=NOW, existing=existing, **kw
    )


def test_saving_makes_numbered_versions_and_never_repeats_one() -> None:
    cfg = config(presets=presets_data())
    v1, rename = saved(cfg)
    assert (v1.version, rename) == (1, None)
    with pytest.raises(ValueError, match="nothing changed since version 1"):
        saved(cfg, existing=[v1])
    changed, _ = rename_in_config(cfg, RenameKind.AGENT, "dev", "Dana")
    v2, rename = saved(changed, existing=[v1])
    assert v2.version == 2 and v2.document_hash != v1.document_hash and rename is None
    again, _ = saved(changed, existing=[v2, v1], description="now with a note")
    assert again.version == 3


def test_a_template_name_change_makes_a_version_and_a_rename_record() -> None:
    cfg = config()
    v1, _ = saved(cfg)
    v2, rename = saved(cfg, name="Renamed team", existing=[v1])
    assert v2.version == 2 and v2.document_hash == v1.document_hash
    assert rename is not None and (rename.old_name, rename.new_name) == ("My team", "Renamed team")
    assert rename.ledger_payload()["what"] == "template"


def test_template_versions_must_be_one_to_n_of_one_template() -> None:
    cfg = config()
    v1, _ = saved(cfg)
    v2, _ = saved(cfg, name="Other", existing=[v1])
    with pytest.raises(ValueError, match="gaps"):
        check_versions([v2])
    with pytest.raises(ValueError, match="gaps"):
        check_versions([v1, v1])
    stranger = v1.model_copy(update={"id": "other"})
    with pytest.raises(ValueError, match="different templates"):
        check_versions([v1, stranger])
    with pytest.raises(ValueError, match="not 'other'"):
        save_as_template(cfg, template_id="other", name="x", saved_at=NOW, existing=[v1])
    assert check_versions([]) is None and check_versions([v2, v1]) == v2


def test_a_template_keeps_the_reusable_parts_only() -> None:
    cfg = config(
        presets=presets_data(),
        budgets=[
            {
                "id": "b-run",
                "scope": "run",
                "scope_id": "r1",
                "limit": {"amount": "5", "currency": "USD"},
            },
            {
                "id": "b-dev",
                "scope": "agent",
                "scope_id": "dev",
                "limit": {"amount": "2", "currency": "USD"},
            },
        ],
        canon={"repository": "/srv/canon", "commit": "a" * 40, "paths": ["docs/**"]},
        price_overrides=[],
    )
    doc = template_document(cfg)
    assert set(doc) <= {"org", "policies", "budgets", "evals", "presets"}
    assert [b["id"] for b in doc["budgets"]] == ["b-dev"]  # the run budget names one run
    assert "canon" not in doc and "prices" not in doc


def test_using_a_template_makes_a_new_org_and_refuses_a_tampered_document() -> None:
    cfg = config(presets=presets_data())
    v1, _ = saved(cfg)
    fresh = config_from_template(v1, org_id="beta", org_name="Beta")
    assert (fresh.org.id, fresh.org.name) == ("beta", "Beta")
    assert [a.id for a in fresh.org.agents] == [a.id for a in cfg.org.agents]
    assert fresh.presets == cfg.presets and fresh.effective_policies == cfg.effective_policies
    broken = TemplateVersion(
        id="my-team",
        version=1,
        name="x",
        saved_at=NOW,
        document={"org": {**org_data(), "agents": []}},
    )
    with pytest.raises(ConfigValidationError):
        config_from_template(broken, org_id="beta", org_name="Beta")


def test_template_versions_validate_their_fields() -> None:
    doc = {"org": org_data()}
    with pytest.raises(ValueError, match="time zone"):
        TemplateVersion(id="t", version=1, name="n", saved_at=datetime(2026, 1, 1), document=doc)
    with pytest.raises(ValueError, match="needs an org"):
        TemplateVersion(id="t", version=1, name="n", saved_at=NOW, document={})
    with pytest.raises(ValueError, match="can't hold"):
        TemplateVersion(id="t", version=1, name="n", saved_at=NOW, document={**doc, "canon": {}})
    with pytest.raises(ValueError):
        TemplateVersion(id="t", version=0, name="n", saved_at=NOW, document=doc)
    with pytest.raises(ValueError):
        TemplateVersion(
            id="t", version=1, name="n", saved_at=NOW, document={**doc, "budgets": [1.5]}
        )


def test_money_in_budgets_stays_exact_in_a_template() -> None:
    cfg = config(
        budgets=[
            {
                "id": "b",
                "scope": "agent",
                "scope_id": "dev",
                "limit": {"amount": "2.10", "currency": "USD"},
            }
        ]
    )
    v1, _ = saved(cfg)
    back = config_from_template(v1, org_id="acme", org_name="Acme")
    assert back.budgets[0].limit.amount == Decimal("2.10")


# ---- people in the ledger and in templates (T42) ---------------------------------------------


def test_people_are_opaque_ids_never_emails_or_dotted_names() -> None:
    from nuramesh.core.ledger import EventType, next_event
    from nuramesh.core.people import is_human_id

    for ok in ("human:u_7k2x9q", "human:alex", "human:U-1", "human:" + "a" * 64):
        assert is_human_id(ok), ok
    for bad in (
        "human:jane@example.com",
        "human:jane.doe",
        "human:",
        "human:" + "a" * 65,
        "human:a b",
        "alex",
        "agent:x",
        "human:é",
    ):
        assert not is_human_id(bad), bad
    with pytest.raises(ValueError):
        next_event(
            None,
            stream="run:r1",
            type=EventType.DECISION_RECORDED,
            actor="human:jane@example.com",
            at=NOW,
        )
    # agents and services may still use dots and at-signs; only people are restricted
    next_event(
        None, stream="run:r1", type=EventType.DECISION_RECORDED, actor="service:ci.runner", at=NOW
    )


def test_a_template_leaves_out_a_policys_approvers() -> None:
    cfg = config(
        policies={
            "policies": [
                {
                    "role": "dev",
                    "rules": [{"action": "merge", "decision": "ask", "target": "*"}],
                    "approvers": ["human:u_7k2x9q"],
                }
            ]
        }
    )
    doc = template_document(cfg)
    (policy,) = doc["policies"]["policies"]
    assert "approvers" not in policy and policy["rules"][0]["decision"] == "ask"
    v1, _ = saved(cfg)
    used = config_from_template(v1, org_id="beta", org_name="Beta")
    assert used.policies.policies[0].approvers == ()
    assert cfg.policies.policies[0].approvers == ("human:u_7k2x9q",)  # the original keeps its own


def test_there_is_no_rename_of_a_person() -> None:
    assert {k.value for k in RenameKind} == {
        "org",
        "role",
        "agent",
        "policy_preset",
        "gate_preset",
        "template",
    }
