"""Compiler + schema: a scripted discovery run compiles into a valid, canonicalised,
round-trippable capability; inconsistent or tampered artifacts are rejected."""
from pathlib import Path

import pytest
import yaml

from cua.artifact import AppProfile, Capability, CompileError, compile_trace

from .scripted_model import ref
from .test_discovery import LOGIN, LOOKUP, R, run

ROOT = Path(__file__).resolve().parents[1]
PROFILE = AppProfile.load(ROOT / "apps" / "coreline_teller.yaml")

EXTRACT_AND_FINISH = [
    lambda o: ("extract", {"ref": ref(o, col_header="Balance", row_key="0000"), "name": "savings_balance",
                           "type": "decimal", "description": "Regular Savings balance", "reason": R}),
    lambda o: ("finish", {"status": "success", "summary": "read balance",
                          "success_evidence": "Member Summary for member 12345 is shown"}),
]


@pytest.fixture
def compiled(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    trace = run(tmp_path, policy, base, LOGIN + LOOKUP + EXTRACT_AND_FINISH).trace
    return compile_trace(trace, PROFILE, capability_id="member_savings_balance", policy=policy), policy


def test_compiles_contract_and_steps(compiled):
    report, _ = compiled
    cap = report.capability
    assert cap.inputs["member_id"].example == "12345"
    assert cap.outputs["savings_balance"].sensitive is True
    assert cap.secrets == ["teller_password", "teller_user"]
    assert cap.side_effects == "none"
    assert [s.action for s in cap.steps] == ["fill_secret", "fill_secret", "click", "fill", "press", "click", "extract"]
    assert cap.steps[3].value == "{{member_id}}"


def test_locators_are_semantic_first_and_canonicalised(compiled):
    cap = compiled[0].capability
    by_id = {s.id: s for s in cap.steps}
    member_field = by_id["fill_member_id"].target
    assert member_field.frame == "work"
    assert [loc.by for loc in member_field.locators][:2] == ["caption", "field_name"]
    select = by_id["click_select"]
    assert select.target.locators[0].model_dump(exclude_none=True) == \
        {"by": "role", "role": "link", "name": "Select", "row_key": "{{member_id}}"}
    assert select.expect.frames == {"work": "/w/member/{{member_id}}"}
    balance = by_id["extract_savings_balance"].target
    assert balance.locators[0].by == "table_cell" and balance.locators[0].column == "Balance"
    # Data is never located by its value, and nothing redacted leaks into a locator.
    assert not any(loc.by == "role" for loc in balance.locators)
    assert "[REDACTED" not in cap.to_yaml().split("success:")[0]


def test_success_checkpoint_and_outcomes(compiled):
    cap = compiled[0].capability
    assert cap.success.frames["work"] == "/w/member/{{member_id}}"
    assert cap.success.texts == ["Member Summary"] and cap.success.outputs == ["savings_balance"]
    assert "{{member_id}}" in cap.success.description
    codes = [o.code for o in cap.outcomes]
    assert codes[:2] == ["system_notice", "app_error"]           # recoverable rules evaluated first
    assert "record_not_found" in codes and "invalid_search_input" in codes
    assert "member_restricted" not in codes                        # its screen is never visited


def test_yaml_round_trip_is_lossless_and_hash_stable(compiled, tmp_path):
    cap = compiled[0].capability
    path = cap.save(tmp_path / "caps")
    loaded = Capability.load(path)
    assert loaded.model_dump(exclude={"content_hash"}) == cap.model_dump(exclude={"content_hash"})
    assert loaded.content_hash == cap.compute_hash()
    # Approving or re-versioning does not change what the capability does.
    assert loaded.model_copy(update={"status": "approved", "version": "1.0.0"}).compute_hash() == loaded.content_hash
    assert "requires_approval: false" not in path.read_text()      # quiet defaults omitted


def test_tampered_artifact_is_rejected(compiled, tmp_path):
    path = compiled[0].capability.save(tmp_path / "caps")
    original = path.read_text()
    assert "name: Select" in original
    path.write_text(original.replace("name: Select", "name: Delete"), encoding="utf-8")
    with pytest.raises(ValueError, match="content changed"):
        Capability.load(path)


def test_inconsistent_artifact_is_rejected(compiled):
    data = compiled[0].capability.model_dump(mode="json")
    data["steps"][3]["value"] = "{{account_id}}"
    with pytest.raises(ValueError, match="undeclared inputs"):
        Capability.model_validate(data)
    data = compiled[0].capability.model_dump(mode="json")
    data["outputs"]["extra"] = {"type": "string", "description": "x"}
    with pytest.raises(ValueError, match="declared outputs"):
        Capability.model_validate(data)
    with pytest.raises(ValueError, match="unknown fields|Extra inputs"):
        Capability.model_validate({**compiled[0].capability.model_dump(mode="json"), "stepz": []})


def test_invocation_schema_for_agents(compiled):
    tool = compiled[0].capability.invocation_schema()
    assert tool["name"] == "member_savings_balance"
    assert tool["input_schema"]["required"] == ["member_id"]


def test_only_successful_runs_compile(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    trace = run(tmp_path, policy, base, [lambda o: ("finish", {"status": "failed", "summary": "x"})]).trace
    with pytest.raises(CompileError, match="only successful"):
        compile_trace(trace, PROFILE)


def test_recovery_clicks_during_discovery_are_not_compiled_as_steps(tmp_path, live_app, policy, secrets_env):
    """A notice appeared on every page during discovery and the agent acknowledged it each time.
    Those clicks are runtime recoveries (outcome rule system_notice), not the procedure: the
    capability must not contain them, or every notice-free replay would fail."""
    app, base = live_app
    app.config["FAULTS"].update({"interstitial_rate": 1.0})
    ack = lambda o: ("click", {"ref": ref(o, role="button", name="Acknowledge"), "reason": R})  # noqa: E731
    script = LOGIN + [ack, LOOKUP[0], LOOKUP[1], ack, LOOKUP[2], ack] + EXTRACT_AND_FINISH
    trace = run(tmp_path, policy, base, script).trace
    assert trace.status == "success"
    assert sum(1 for s in trace.steps if s.target and s.target.name == "Acknowledge") == 3
    report = compile_trace(trace, PROFILE, capability_id="member_savings_balance", policy=policy)
    assert not any("acknowledge" in s.id for s in report.capability.steps)
    assert [s.action for s in report.capability.steps] == \
        ["fill_secret", "fill_secret", "click", "fill", "press", "click", "extract"]
    assert sum("handled by outcome rule 'system_notice'" in line for line in report.skipped) == 3


def test_profile_file_is_valid_yaml_with_ordered_kinds():
    rules = yaml.safe_load((ROOT / "apps" / "coreline_teller.yaml").read_text())["outcomes"]
    kinds = [r["kind"] for r in rules]
    assert kinds.index("recoverable") < kinds.index("failure") < kinds.index("business")
