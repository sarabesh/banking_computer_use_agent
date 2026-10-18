"""Discovery loop end to end against the live mock app, with a scripted model in place of
the LLM. Covers recording, parameter/secret/output binding, policy refusals, stuck
detection and redaction of persisted evidence."""
import json
from pathlib import Path

from cua.discovery import DiscoveryAgent
from cua.surface.web import WebSurface

from .scripted_model import ScriptedModel, ref

R = "scripted step"
LOGIN = [
    lambda o: ("type_secret", {"ref": ref(o, role="textbox", caption="User ID:"), "secret": "teller_user", "reason": R}),
    lambda o: ("type_secret", {"ref": ref(o, role="textbox", caption="Password:"), "secret": "teller_password", "reason": R}),
    lambda o: ("click", {"ref": ref(o, role="button", name="Sign On"), "reason": R}),
]
LOOKUP = [
    lambda o: ("type_text", {"ref": ref(o, role="textbox", caption="Member #:"), "text": "12345", "reason": R,
                             "parameter": {"name": "member_id", "type": "string", "description": "5-digit member number"}}),
    lambda o: ("press_key", {"key": "Enter", "reason": R}),
    lambda o: ("click", {"ref": ref(o, role="link", name="Select"), "reason": R}),
]


def run(tmp_path, policy, base, script, **kw):
    model = ScriptedModel(script)
    agent = DiscoveryAgent(model, WebSurface(policy, headless=True), policy, tmp_path, echo=lambda _: None, **kw)
    model.agent = agent
    return agent.run("look up member 12345 and read their savings balance", f"{base}/login")


def test_happy_path_records_a_parameterised_trace(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    script = LOGIN + LOOKUP + [
        lambda o: ("extract", {"ref": ref(o, col_header="Balance", row_key="0000"), "name": "savings_balance",
                               "type": "decimal", "description": "Regular Savings balance", "reason": R}),
        lambda o: ("finish", {"status": "success", "summary": "read balance",
                              "success_evidence": "Member Summary shows the Shares / Loans table"}),
    ]
    result = run(tmp_path, policy, base, script)
    trace = result.trace
    assert trace.status == "success"
    assert result.outputs == {"savings_balance": "$1,523.47"}
    assert [p.name for p in trace.parameters] == ["member_id"]
    assert [s.action for s in trace.steps] == ["type_secret", "type_secret", "click", "type_text",
                                               "press_key", "click", "extract"]
    member_step = trace.steps[3]
    assert member_step.parameter == "member_id" and member_step.target.caption == "Member #:"
    assert member_step.target.frame == "work" and member_step.target.field_name == "q1"
    assert trace.steps[6].target.col_header == "Balance" and trace.steps[6].target.row_key == "0000"
    assert trace.steps[5].after.frames["work"] == "/w/member/12345"

    # Nothing sensitive persisted: no password, no SSN, no balance.
    persisted = "".join(p.read_text(encoding="utf-8") for p in result.run_dir.glob("*.json*"))
    assert secrets_env["password"] not in persisted
    assert "900-12-3456" not in persisted
    assert "1,523.47" not in persisted
    assert "[REDACTED:" in persisted
    assert list(result.run_dir.glob("step-*.jpg"))


def test_irreversible_control_is_refused_and_recorded(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    script = LOGIN + LOOKUP + [
        lambda o: ("click", {"ref": ref(o, name="Open Sub-Account"), "reason": R}),
        lambda o: ("select_option", {"ref": ref(o, role="combobox"), "option": "XMAS", "reason": R}),
        lambda o: ("type_text", {"ref": ref(o, role="textbox", caption="Initial Deposit:"), "text": "25", "reason": R}),
        lambda o: ("click", {"ref": ref(o, role="button", name="Continue"), "reason": R}),
        lambda o: ("click", {"ref": ref(o, role="button", name="Confirm"), "reason": R}),
        lambda o: ("finish", {"status": "success", "summary": "reached review", "success_evidence": "review screen"}),
    ]
    trace = run(tmp_path, policy, base, script).trace
    confirm = trace.steps[-1]
    assert confirm.target.name == "Confirm"
    assert confirm.status == "needs_approval" and confirm.policy.verdict == "needs_approval"
    assert trace.steps[-2].after.frames["work"].endswith("/open/review")
    assert trace.steps[7].literal == "XMAS - Holiday Club"   # option resolved to its full label


def test_enter_on_irreversible_form_is_refused(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    script = LOGIN + [lambda o: ("press_key", {"key": "Enter", "reason": R})]
    # On the search page Enter submits "Go": allowed. Check the policy path directly too.
    trace = run(tmp_path, policy, base, script, max_steps=4).trace
    assert trace.steps[-1].status == "ok"
    assert policy.check_action("press_key", key="Enter", submit_label="Confirm").verdict == "needs_approval"


def test_stuck_detection_escalates_with_context(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    click_title = lambda o: ("click", {"ref": ref(o, name="Teller Sign On"), "reason": R})  # noqa: E731
    result = run(tmp_path, policy, base, [click_title] * 5)
    assert result.trace.status == "escalated"
    request = json.loads((result.run_dir / "intervention.json").read_text())
    assert request["trigger"] == "stuck" and "did not change the screen" in request["reason"]
    assert request["goal"] and request["step"] and request["screen"]["frames"]
    assert Path(request["screenshot"]).exists()


def test_covered_element_is_reported(tmp_path, live_app, policy, secrets_env):
    app, base = live_app
    app.config["FAULTS"].update({"interstitial_rate": 1.0})
    script = LOGIN + [
        lambda o: ("type_text", {"ref": ref(o, role="textbox", caption="Member #:"), "text": "12345", "reason": R}),
    ]
    trace = run(tmp_path, policy, base, script, max_steps=4).trace
    assert trace.steps[-1].status == "error" and "covered" in trace.steps[-1].detail


def test_success_without_evidence_is_refused_back_to_the_model(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    script = [
        lambda o: ("finish", {"status": "success", "summary": "done"}),                       # no evidence
        lambda o: ("finish", {"status": "success", "summary": "done", "success_evidence": "sign-on screen"}),
    ]
    trace = run(tmp_path, policy, base, script).trace
    assert trace.status == "success" and trace.final.success_evidence == "sign-on screen"
    events = [json.loads(line) for line in (tmp_path.glob("*/events.jsonl").__next__()).read_text().splitlines()]
    refused = [e for e in events if e["type"] == "action_result" and not e["ok"]]
    assert refused and "requires success_evidence" in refused[0]["result"]


def test_unknown_ref_and_bad_input_are_errors_not_crashes(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    script = [
        lambda o: ("click", {"ref": "e999", "reason": R}),
        lambda o: ("extract", {"ref": "e1", "name": "Bad Name", "type": "string", "description": "x", "reason": R}),
        lambda o: ("finish", {"status": "failed", "summary": "gave up"}),
    ]
    trace = run(tmp_path, policy, base, script).trace
    assert trace.status == "failed" and trace.steps == []
