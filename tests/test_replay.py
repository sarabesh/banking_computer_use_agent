"""Deterministic replay against the live mock app: success, parameterisation, business
outcomes, recoverable conditions, hard failures, approval gating, cross-tenant drift.

The capability is compiled once (from a scripted discovery run) and then replayed against
fresh app instances on other ports: the artifact carries no base URL, the caller supplies it."""
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from cua.artifact import AppProfile, compile_trace
from cua.discovery import DiscoveryAgent
from cua.policy import Policy
from cua.replay import Replayer, parse_value
from cua.surface.web import WebSurface
from target_app import FaultConfig, create_app

from .scripted_model import ScriptedModel, ref
from .test_discovery import LOGIN, R

ROOT = Path(__file__).resolve().parents[1]
CLICK_GO = [
    lambda o: ("type_text", {"ref": ref(o, role="textbox", caption="Member #:"), "text": "12345", "reason": R,
                             "parameter": {"name": "member_number", "type": "string", "description": "member"}}),
    lambda o: ("click", {"ref": ref(o, role="button", name="Go"), "reason": R}),
    lambda o: ("click", {"ref": ref(o, role="link", name="Select"), "reason": R}),
    lambda o: ("extract", {"ref": ref(o, col_header="Balance", row_key="0000"), "name": "savings_balance",
                           "type": "decimal", "description": "Regular Savings balance", "reason": R}),
    lambda o: ("finish", {"status": "success", "summary": "ok", "success_evidence": "Member Summary shown"}),
]


def serve(tenant="acme", **faults):
    app = create_app(tenant=tenant, faults=FaultConfig(**{"confirm_dialog": False, **faults}), seed=1)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return app, server, f"http://127.0.0.1:{server.server_port}"


def policy_for(*bases):
    p = Policy.load(ROOT / "policy.yaml")
    p.allowed_origins = list(bases)
    return p


@pytest.fixture(scope="module")
def capability(tmp_path_factory):
    import os
    import json
    users = {u["role"]: u for u in json.loads((ROOT / "target_app/seed/users.json").read_text())}
    os.environ["TELLER_USER"], os.environ["TELLER_PASSWORD"] = users["officer"]["user_id"], users["officer"]["password"]
    _, server, base = serve()
    policy = policy_for(base)
    model = ScriptedModel(LOGIN + CLICK_GO)
    agent = DiscoveryAgent(model, WebSurface(policy, headless=True), policy, tmp_path_factory.mktemp("disc"),
                           echo=lambda _: None)
    model.agent = agent
    trace = agent.run("look up member 12345 and read their savings balance", f"{base}/login").trace
    server.shutdown()
    cap = compile_trace(trace, AppProfile.load(ROOT / "apps/coreline_teller.yaml"),
                        capability_id="member_savings_balance", policy=policy).capability
    return cap.model_copy(update={"status": "approved"})


def replay(cap, tmp_path, member="12345", tenant="acme", setup=None, **faults):
    app, server, base = serve(tenant, **faults)
    try:
        if setup:
            setup(app)
        policy = policy_for(base)
        r = Replayer(WebSurface(policy, headless=True), policy, tmp_path, step_timeout=4, echo=lambda _: None)
        return r.run(cap, {"member_number": member}, base_url=base), r.log.dir
    finally:
        server.shutdown()


def test_success_returns_typed_outputs_and_evidence(capability, tmp_path):
    result, run_dir = replay(capability, tmp_path)
    assert result.status == "success", result.error
    assert result.outputs == {"savings_balance": "1523.47"}
    assert all(s.confidence == "high" for s in result.steps)
    persisted = (run_dir / "result.json").read_text() + (run_dir / "events.jsonl").read_text()
    assert "1523.47" not in persisted and "1,523.47" not in persisted       # sensitive output redacted


def test_same_capability_other_member(capability, tmp_path):
    result, _ = replay(capability, tmp_path, member="24680")
    assert result.status == "success" and result.outputs == {"savings_balance": "12890.05"}


def test_unknown_member_is_a_business_outcome(capability, tmp_path):
    result, _ = replay(capability, tmp_path, member="99999")
    assert result.status == "business_outcome" and result.outcome == "record_not_found"
    assert result.error is None and result.outputs == {}


def test_malformed_member_number_is_reported_by_the_app(capability, tmp_path):
    result, _ = replay(capability, tmp_path, member="12a45")
    assert result.status == "business_outcome" and result.outcome == "invalid_search_input"


def test_system_notice_is_recovered(capability, tmp_path):
    result, _ = replay(capability, tmp_path, interstitial_rate=1.0)
    assert result.status == "success", result.error
    assert any("system_notice" in s.recovered for s in result.steps)


def test_persistent_app_error_exhausts_recovery_with_evidence(capability, tmp_path):
    result, run_dir = replay(capability, tmp_path, error_rate=1.0)
    assert result.status == "failed" and result.outcome == "app_error"
    assert result.error.kind == "recovery_exhausted" and result.escalate
    assert (run_dir / result.error.screenshot).exists() and (run_dir / result.error.snapshot).exists()


def test_session_expiry_is_a_hard_failure(capability, tmp_path):
    result, _ = replay(capability, tmp_path, session_ttl_s=0)
    assert result.status == "failed" and result.outcome == "session_expired"
    assert result.error.kind == "outcome_failure" and result.error.step is not None


def test_slow_app_is_waited_for(capability, tmp_path):
    result, _ = replay(capability, tmp_path, delay_ms=1500)
    assert result.status == "success", result.error


def test_draft_needs_explicit_supervision(capability, tmp_path):
    draft = capability.model_copy(update={"status": "draft"})
    result, _ = replay(draft, tmp_path)
    assert result.status == "failed" and result.error.kind == "not_approved"


def test_bad_inputs_rejected_before_touching_the_app(capability, tmp_path):
    policy = policy_for("http://127.0.0.1:1")
    r = Replayer(WebSurface(policy, headless=True), policy, tmp_path, echo=lambda _: None)
    result = r.run(capability, {}, base_url="http://127.0.0.1:1")
    assert result.error.kind == "invalid_input" and "missing required input" in result.error.message
    result = r.run(capability, {"member_number": "1", "extra": "x"}, base_url="http://127.0.0.1:1")
    assert "unknown inputs" in result.error.message


def test_other_tenant_degrades_to_fallbacks_and_detects_drift(capability, tmp_path):
    """Riverbend runs the same product with different labels and column order. Semantic
    locators miss, structural fallbacks carry the flow, and the success checkpoint catches
    that the screen is not what was recorded: drift is reported, never silently accepted."""
    result, run_dir = replay(capability, tmp_path, tenant="riverbend")
    snapshot = (run_dir / result.error.snapshot).read_text(encoding="utf-8")
    assert "Avery" not in snapshot and "Quinn" not in snapshot     # names have no regex; structure-only snapshot
    assert "Account Holder" in snapshot and "‹value›" in snapshot   # labels kept, values replaced
    strategies = {s.id: s.strategy for s in result.steps}
    assert strategies["fill_member_number"] == "field_name"      # caption 'Account Number:' differs
    assert strategies["click_select"] == "href"                   # link text 'Open' differs
    assert result.low_confidence_steps                            # e.g. 'Search' button via css
    assert result.status == "failed" and result.error.kind == "success_check_failed"


def test_evidence_masking_hides_data_values_but_keeps_labels(tmp_path):
    from cua.surface.web import MASK_JS
    app, server, base = serve()
    policy = policy_for(base)
    surface = WebSurface(policy, headless=True)
    try:
        surface.start(f"{base}/login")
        page = surface.page
        page.fill("input[name=u]", "officer1")
        page.fill("input[name=p]", "demo-officer-pw")
        page.click("input[type=submit]")
        page.wait_for_url("**/main")
        work = page.frame(name="work")
        work.goto(f"{base}/w/member/12345")
        work.evaluate(MASK_JS, [[], True])
        masked = lambda text: work.evaluate(  # noqa: E731
            "t => [...document.querySelectorAll('[data-cua-mask]')].some(e => e.innerText.trim() === t)", text)
        assert masked("Avery Quinn") and masked("41 Elm St, Springfield") and masked("$1,523.47")
        assert not masked("Name:") and not masked("Balance") and not masked("Open Sub-Account")
    finally:
        surface.close()
        server.shutdown()


def test_replay_never_loads_the_llm_client():
    code = "import sys, cua.replay, cua.surface.web; sys.exit('anthropic' in sys.modules or 'cua.llm' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


@pytest.mark.parametrize("kind,raw,want", [("decimal", "$1,523.47", "1523.47"), ("decimal", "($12.00)", "-12.00"),
                                           ("integer", "1,204", "1204"), ("boolean", "Yes", "true")])
def test_parse_value(kind, raw, want):
    assert parse_value(kind, raw) == want
