"""Per-tenant overrides on top of a robust base capability.

Flow under test: base capability (recorded on Acme) -> supervised replay on Riverbend ->
suggested override -> replay with override: every step high confidence. Plus the safety
rules: an override can specialise how things are found, never what the capability does."""
import pytest

from cua.artifact import OverrideError, TenantOverride, apply_override
from cua.artifact.schema import Capability
from cua.artifact.suggest import auto_suggest, drifted, suggest_override
from cua.replay import Replayer
from cua.surface.web import WebSurface

from .test_replay import capability, policy_for, serve  # noqa: F401 - module fixture reused


def replay_at(cap, tmp_path, tenant, override_hash=None, allow_draft=False):
    _, server, base = serve(tenant)
    policy = policy_for(base)
    try:
        r = Replayer(WebSurface(policy, headless=True), policy, tmp_path, step_timeout=4, echo=lambda _: None)
        return r.run(cap, {"member_number": "12345"}, base_url=base, tenant=tenant, override_hash=override_hash,
                     allow_draft=allow_draft)
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def riverbend_probe(capability, tmp_path_factory):  # noqa: F811
    # Supervised, like `cua override suggest`: it carries on past weak matches to see them all.
    return replay_at(capability, tmp_path_factory.mktemp("probe"), "riverbend", allow_draft=True)


@pytest.fixture(scope="module")
def riverbend_override(capability, riverbend_probe):  # noqa: F811
    return suggest_override(capability, riverbend_probe, "riverbend")


def test_supervised_replay_with_drift_writes_a_draft_once(capability, riverbend_probe, tmp_path):  # noqa: F811
    path, reason = auto_suggest(capability, riverbend_probe, "riverbend", tmp_path)
    assert path is not None and reason == "drift detected"
    written = TenantOverride.load(path)
    assert written.status == "draft" and "fill_member_number" in written.steps   # never auto-approved
    again, reason = auto_suggest(capability, riverbend_probe, "riverbend", tmp_path)
    assert again is None and "already exists" in reason                       # never overwritten


def test_no_drift_writes_nothing(capability, tmp_path):  # noqa: F811
    result = replay_at(capability, tmp_path / "runs", "acme")
    assert not drifted(result)
    path, reason = auto_suggest(capability, result, "acme", tmp_path / "overrides")
    assert path is None and "no drift" in reason and not (tmp_path / "overrides").exists()


def test_suggestion_contains_only_what_differs(riverbend_override):
    ov = riverbend_override
    assert ov.status == "draft" and ov.base_hash
    # Buttons and the row link are found by relational locators already: nothing to override.
    assert set(ov.steps) == {"fill_teller_user", "fill_teller_password", "fill_member_number", "extract_savings_balance"}
    captions = {sid: s.locators[0].caption for sid, s in ov.steps.items() if s.locators[0].by == "caption"}
    assert captions == {"fill_teller_user": "Operator:", "fill_teller_password": "PIN / Password:",
                        "fill_member_number": "Account Number:"}
    balance = ov.steps["extract_savings_balance"].locators[0]
    assert (balance.by, balance.column, balance.row_key) == ("table_cell", "Current Bal", "0000")
    assert ov.success.texts == ["Account Holder Detail"]


def test_override_makes_every_step_high_confidence(capability, riverbend_override, tmp_path):  # noqa: F811
    reviewed = riverbend_override.model_copy(update={"status": "approved"})   # the human review step
    effective = apply_override(capability, reviewed)
    assert effective.status == "approved"
    result = replay_at(effective, tmp_path, "riverbend", override_hash="sha256:test")
    assert result.status == "success", result.error
    assert result.outputs == {"savings_balance": "1523.47"}
    assert result.low_confidence_steps == [] and result.warnings == []
    assert all(s.confidence == "high" for s in result.steps)
    assert result.tenant == "riverbend" and result.override == "sha256:test"


def test_base_is_untouched_for_the_original_tenant(capability, riverbend_override, tmp_path):  # noqa: F811
    result = replay_at(capability, tmp_path, "acme")
    assert result.status == "success" and not result.warnings
    assert {s.strategy for s in result.steps} <= {"caption", "role", "table_cell"}   # its own semantic locators


def test_override_locators_go_first_and_base_ones_stay_as_fallback(capability, riverbend_override):  # noqa: F811
    effective = apply_override(capability, riverbend_override)
    field = next(s for s in effective.steps if s.id == "fill_member_number").target.locators
    assert field[0].by == "caption" and field[0].caption == "Account Number:"
    assert field[1].by == "caption" and field[1].caption == "Member #:"          # base kept
    assert [s.id for s in effective.steps] == [s.id for s in capability.steps]    # same flow
    assert [s.action for s in effective.steps] == [s.action for s in capability.steps]


def test_override_is_pinned_to_the_base_it_was_reviewed_against(capability, riverbend_override):  # noqa: F811
    recompiled = capability.model_copy(update={"title": capability.title + " (changed)"})
    with pytest.raises(OverrideError, match="reviewed against base"):
        apply_override(recompiled, riverbend_override)


@pytest.mark.parametrize("change,message", [
    ({"steps": {"no_such_step": {"locators": [{"by": "role", "role": "button", "name": "X"}]}}}, "does not have"),
    ({"outcomes": [{"code": "record_not_found", "kind": "business", "description": "x",
                    "match": {"text": "x"}}]}, "not redefine"),
    ({"capability": "other_capability"}, "not 'member_savings_balance'"),
])
def test_override_rules_are_enforced(capability, riverbend_override, change, message):  # noqa: F811
    bad = TenantOverride.model_validate({**riverbend_override.model_dump(mode="json"), **change})
    with pytest.raises(OverrideError, match=message):
        apply_override(capability, bad)


def test_override_cannot_loosen_or_change_behaviour(riverbend_override):
    data = riverbend_override.model_dump(mode="json")
    with pytest.raises(ValueError):    # requires_approval may only be set to true
        TenantOverride.model_validate({**data, "steps": {"click_go": {"requires_approval": False}}})
    with pytest.raises(ValueError):    # there is no field for actions, values or secrets
        TenantOverride.model_validate({**data, "steps": {"click_go": {"action": "fill", "value": "x"}}})


def test_both_base_and_override_must_be_approved(capability, riverbend_override):  # noqa: F811
    assert apply_override(capability, riverbend_override).status == "draft"           # override still draft
    approved = riverbend_override.model_copy(update={"status": "approved"})
    assert apply_override(capability, approved).status == "approved"
    draft_base = capability.model_copy(update={"status": "draft"})
    assert apply_override(draft_base, approved).status == "draft"


def test_override_round_trip_and_tamper_detection(riverbend_override, tmp_path):
    path = riverbend_override.save(tmp_path)
    loaded = TenantOverride.load(path)
    assert loaded.compute_hash() == loaded.content_hash == riverbend_override.compute_hash()
    path.write_text(path.read_text(encoding="utf-8").replace("Current Bal", "Avail Bal"), encoding="utf-8")
    with pytest.raises(OverrideError, match="content changed"):
        TenantOverride.load(path)


def test_compiler_emits_relational_locators(capability):  # noqa: F811
    by_id = {s.id: [loc.by for loc in s.target.locators] for s in capability.steps if s.target}
    assert by_id["click_sign_on"][:2] == ["role", "form_submit"]
    assert by_id["click_go"][:2] == ["role", "form_submit"]
    assert by_id["click_select"][:2] == ["role", "row_control"]
    with pytest.raises(ValueError, match="not an earlier step"):
        data = capability.model_dump(mode="json")
        sign_on = next(s for s in data["steps"] if s["id"] == "click_sign_on")
        sign_on["target"]["locators"][1]["of_step"] = "click_go"
        Capability.model_validate(data)
