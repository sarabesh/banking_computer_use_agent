"""Human-in-the-loop handoff, end to end against the live mock app.

The simulated operator acts exactly like a real one: it sends commands through the operator
CLI code (into the inbox), and while it holds control it drives the SAME live browser with
real clicks and keystrokes - which the recorder captures. It runs inside the handoff's wait
loop (on_poll) only because Playwright objects can't be shared across threads."""
import argparse
import json
from pathlib import Path

import pytest

from cua.artifact import AppProfile, compile_trace
from cua.artifact.schema import Capability, RoleLocator, Step, Success, Target
from cua.discovery import DiscoveryAgent
from cua.handoff import Control, ControlState, Handoff, Inbox
from cua.operator import run as operator_cli
from cua.replay import Replayer
from cua.surface.base import ActionError
from cua.surface.web import WebSurface

from .scripted_model import ScriptedModel, ref
from .test_discovery import LOGIN, LOOKUP, R
from .test_replay import CLICK_GO, policy_for, serve

ROOT = Path(__file__).resolve().parents[1]
PROFILE = AppProfile.load(ROOT / "apps/coreline_teller.yaml")
OPERATOR = "ops-anna"


def send(inbox: Inbox, command: str, request_id: str) -> None:
    operator_cli(argparse.Namespace(inbox=str(inbox.root), operator=OPERATOR, note=f"test {command}",
                                    command=command, request_id=request_id, answer=None))


class Operator:
    """Scripted operator: takes control, optionally does manual work, then answers."""

    def __init__(self, inbox, surface, *, decide="resume", work=None):
        self.inbox, self.surface, self.decide, self.work = inbox, surface, decide, work
        self.seen: list[str] = []
        self.done_work: set[str] = set()

    def __call__(self, state, request):
        if request.id not in self.seen:
            self.seen.append(request.id)
            if len(self.seen) > 1:              # anything beyond the scenario: stop, don't hang
                send(self.inbox, "abort", request.id)
                return
            send(self.inbox, "take" if self.work else self.decide, request.id)
        elif state == ControlState.HUMAN and request.id not in self.done_work:
            self.done_work.add(request.id)
            self.work(self.surface.page)          # manual steps in the live session
            send(self.inbox, self.decide, request.id)


def sign_back_in(page, users):
    page.frame(name="work").get_by_role("link", name="Sign On").click()
    page.wait_for_url("**/login")
    page.fill("input[name=u]", users["user_id"])
    page.fill("input[name=p]", users["password"])
    page.click("input[type=submit]")
    page.wait_for_url("**/main")
    page.frame(name="work").wait_for_selector("input[name=q1]")


@pytest.fixture
def lookup_capability(tmp_path, secrets_env):
    _, server, base = serve()
    policy = policy_for(base)
    model = ScriptedModel(LOGIN + CLICK_GO)
    agent = DiscoveryAgent(model, WebSurface(policy, headless=True), policy, tmp_path / "disc", echo=lambda _: None)
    model.agent = agent
    trace = agent.run("look up member 12345 and read their savings balance", f"{base}/login").trace
    server.shutdown()
    cap = compile_trace(trace, PROFILE, capability_id="member_savings_balance", policy=policy).capability
    return cap.model_copy(update={"status": "approved"})


def test_session_expiry_handed_to_human_who_signs_in_again_then_replay_resumes(lookup_capability, tmp_path, secrets_env):
    app, server, base = serve()
    policy = policy_for(base)
    surface = WebSurface(policy, headless=True)
    inbox = Inbox(tmp_path / "inbox")
    operator = Operator(inbox, surface, work=lambda page: sign_back_in(page, secrets_env))

    def expire_after_login(line):
        if "click_sign_on" in line:
            app.config["STATE"]["session_gen"] += 1          # the session dies mid-run

    replayer = Replayer(surface, policy, tmp_path, step_timeout=4, echo=expire_after_login,
                        handoff=Handoff(inbox, poll_ms=150, echo=lambda _: None, on_poll=operator))
    try:
        result = replayer.run(lookup_capability, {"member_number": "12345"}, base_url=base)
    finally:
        server.shutdown()

    assert result.status == "success", result.error
    assert result.outputs == {"savings_balance": "1523.47"}
    [intervention] = result.interventions
    assert intervention["trigger"] == "unrecoverable" and "session_expired" in intervention["reason"]
    assert intervention["decision"] == "resumed" and intervention["operator"] == OPERATOR
    # Who was in control, in order - on one session.
    assert [(h["from"], h["to"]) for h in result.control_history] == [
        ("automation", "awaiting_human"), ("awaiting_human", "human"), ("human", "automation")]
    human = [s for s in result.steps if s.actor == "human"]
    assert any("User ID" in (s.detail or "") for s in human)
    # Replay re-entered at the start of the search screen, not at the failed click.
    last_human = max(i for i, s in enumerate(result.steps) if s.actor == "human")
    resumed = [s.id for s in result.steps[last_human + 1:]]
    assert resumed[:2] == ["fill_member_number", "click_go"], resumed
    persisted = "".join(p.read_text(encoding="utf-8") for p in replayer.log.dir.glob("*.json*"))
    assert secrets_env["password"] not in persisted
    request = json.loads((tmp_path / "inbox" / f"{intervention['request_id']}.request.json").read_text())
    assert request["capability"].startswith("member_savings_balance@") and request["step"] and request["screen"]["frames"]
    assert not Path(request["screenshot"]).is_absolute()                 # no machine paths in evidence
    assert (tmp_path / "inbox").parent.joinpath(request["screenshot"]).exists()


@pytest.fixture
def open_share_capability(tmp_path, secrets_env):
    """Open a sub-account and POST it: the Confirm step is irreversible (requires approval)."""
    _, server, base = serve()
    policy = policy_for(base)
    script = LOGIN + LOOKUP + [
        lambda o: ("click", {"ref": ref(o, name="Open Sub-Account"), "reason": R}),
        lambda o: ("select_option", {"ref": ref(o, role="combobox"), "option": "XMAS", "reason": R}),
        lambda o: ("type_text", {"ref": ref(o, role="textbox", caption="Initial Deposit:"), "text": "25", "reason": R}),
        lambda o: ("click", {"ref": ref(o, role="button", name="Continue"), "reason": R}),
        lambda o: ("finish", {"status": "success", "summary": "on review screen", "success_evidence": "review"}),
    ]
    model = ScriptedModel(script)
    agent = DiscoveryAgent(model, WebSurface(policy, headless=True), policy, tmp_path / "disc", echo=lambda _: None)
    model.agent = agent
    trace = agent.run("open a holiday club share for member 12345", f"{base}/login").trace
    server.shutdown()
    cap = compile_trace(trace, PROFILE, capability_id="open_holiday_share", policy=policy).capability
    confirm = Step(id="click_confirm", action="click", why="Post the new share", requires_approval=True,
                   target=Target(frame="work", frame_path="/w/member/{{member_id}}/open/review",
                                 locators=[RoleLocator(role="button", name="Confirm")]))
    data = cap.model_dump(mode="json")
    data.update(status="approved", side_effects="writes", steps=data["steps"] + [confirm.model_dump(mode="json")],
                success=Success(frames={"work": "/w/member/{{member_id}}/open/confirm"},
                                texts=["Confirmation #"]).model_dump(mode="json"))
    return Capability.model_validate(data)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_irreversible_step_waits_for_an_operator_decision(open_share_capability, tmp_path, decision):
    app, server, base = serve(confirm_dialog=True)
    policy = policy_for(base)
    surface = WebSurface(policy, headless=True)
    inbox = Inbox(tmp_path / "inbox")
    replayer = Replayer(surface, policy, tmp_path, step_timeout=4, echo=lambda _: None,
                        handoff=Handoff(inbox, poll_ms=150, echo=lambda _: None,
                                        on_poll=Operator(inbox, surface, decide=decision)))
    try:
        result = replayer.run(open_share_capability, {"member_id": "12345"}, base_url=base)
        shares = len(app.config["STATE"]["bank"].members["12345"]["accounts"])
    finally:
        server.shutdown()

    [intervention] = result.interventions
    assert intervention["trigger"] == "approval_needed" and intervention["step"] == "click_confirm"
    if decision == "approve":
        assert result.status == "success", result.error
        confirm = [s for s in result.steps if s.id == "click_confirm" and s.status == "ok"]
        assert confirm and confirm[0].approved_by == OPERATOR
        assert shares == 4                                    # the share was really posted, once
    else:
        assert result.status == "failed" and result.error.kind == "operator_rejected"
        assert result.outcome == "operator_rejected" and shares == 3   # nothing posted


def test_unattended_escalation_routes_the_request_to_the_caller(lookup_capability, tmp_path):
    _, server, base = serve(error_rate=1.0)
    policy = policy_for(base)
    replayer = Replayer(WebSurface(policy, headless=True), policy, tmp_path, step_timeout=3, echo=lambda _: None)
    try:
        result = replayer.run(lookup_capability, {"member_number": "12345"}, base_url=base)
    finally:
        server.shutdown()
    assert result.status == "failed" and result.escalate
    assert result.interventions[0]["decision"] == "returned_to_caller"
    request = json.loads((replayer.log.dir / "intervention.json").read_text())
    assert request["trigger"] == "unrecoverable" and "app_error" in request["reason"]


def test_discovery_irreversible_step_is_approved_by_an_operator_then_compiles_as_gated(tmp_path, live_app, policy,
                                                                                      secrets_env):
    """Discovery reaches Confirm, policy refuses it, the agent asks for approval, an operator
    approves, the action is performed - and the compiled capability marks that step as
    requiring approval on every future replay."""
    app, base = live_app
    surface = WebSurface(policy, headless=True)
    inbox = Inbox(tmp_path / "inbox")
    confirm = lambda o: ("click", {"ref": ref(o, role="button", name="Confirm"), "reason": R})  # noqa: E731
    script = LOGIN + LOOKUP + [
        lambda o: ("click", {"ref": ref(o, name="Open Sub-Account"), "reason": R}),
        lambda o: ("select_option", {"ref": ref(o, role="combobox"), "option": "XMAS", "reason": R}),
        lambda o: ("type_text", {"ref": ref(o, role="textbox", caption="Initial Deposit:"), "text": "25", "reason": R}),
        lambda o: ("click", {"ref": ref(o, role="button", name="Continue"), "reason": R}),
        confirm,                                                                       # refused by policy
        lambda o: ("request_human", {"kind": "approval_needed", "reason": "posting the share is irreversible"}),
        lambda o: ("finish", {"status": "success", "summary": "share posted",
                              "success_evidence": "Sub-Account Opened screen with a confirmation number"}),
    ]
    model = ScriptedModel(script)
    agent = DiscoveryAgent(model, surface, policy, tmp_path / "disc", echo=lambda _: None,
                           handoff=Handoff(inbox, poll_ms=150, echo=lambda _: None,
                                           on_poll=Operator(inbox, surface, decide="approve")))
    model.agent = agent
    result = agent.run("open a holiday club share for member 12345 with $25 and post it", f"{base}/login")

    assert result.trace.status == "success", result.trace.summary
    [intervention] = result.trace.interventions
    assert intervention["trigger"] == "approval_needed" and intervention["decision"] == "approved"
    gated = next(s for s in result.trace.steps if s.target and s.target.name == "Confirm")
    assert gated.status == "ok" and f"approved by operator {OPERATOR}" in gated.detail
    assert len(app.config["STATE"]["bank"].members["12345"]["accounts"]) == 4          # really posted
    cap = compile_trace(result.trace, PROFILE, capability_id="open_holiday_share", policy=policy).capability
    step = next(s for s in cap.steps if s.target and any(getattr(l, "name", None) == "Confirm" for l in s.target.locators))
    assert step.requires_approval and cap.side_effects == "writes"


def test_discovery_asks_for_approval_before_trying_and_the_operator_approves(tmp_path, live_app, policy,
                                                                             secrets_env):
    """What a real model did: on the review screen it asked for approval FIRST, without clicking
    Confirm. Approval then applies to the one irreversible control on that screen."""
    app, base = live_app
    surface = WebSurface(policy, headless=True)
    inbox = Inbox(tmp_path / "inbox")
    script = LOGIN + LOOKUP + [
        lambda o: ("click", {"ref": ref(o, name="Open Sub-Account"), "reason": R}),
        lambda o: ("select_option", {"ref": ref(o, role="combobox"), "option": "XMAS", "reason": R}),
        lambda o: ("type_text", {"ref": ref(o, role="textbox", caption="Initial Deposit:"), "text": "25", "reason": R}),
        lambda o: ("click", {"ref": ref(o, role="button", name="Continue"), "reason": R}),
        lambda o: ("request_human", {"kind": "approval_needed", "reason": "Confirm is irreversible; needs approval"}),
        lambda o: ("finish", {"status": "success", "summary": "posted", "success_evidence": "Sub-Account Opened"}),
    ]
    model = ScriptedModel(script)
    agent = DiscoveryAgent(model, surface, policy, tmp_path / "disc", echo=lambda _: None,
                           handoff=Handoff(inbox, poll_ms=150, echo=lambda _: None,
                                           on_poll=Operator(inbox, surface, decide="approve")))
    model.agent = agent
    result = agent.run("open a holiday club share for member 12345 with $25 and post it", f"{base}/login")

    assert result.trace.status == "success", result.trace.summary
    [intervention] = result.trace.interventions
    assert intervention["trigger"] == "approval_needed" and intervention["decision"] == "approved"
    assert len(app.config["STATE"]["bank"].members["12345"]["accounts"]) == 4            # posted, once
    cap = compile_trace(result.trace, PROFILE, capability_id="open_holiday_share", policy=policy).capability
    assert any(s.requires_approval for s in cap.steps) and cap.side_effects == "writes"


def test_operator_cli_refuses_a_command_that_is_not_possible(tmp_path, capsys):
    from cua.handoff import InterventionRequest
    inbox = Inbox(tmp_path / "inbox")
    req = InterventionRequest(run_id="r", run_kind="discovery", trigger="stuck", reason="x",
                              allowed=["take_control", "resume", "abort"])
    inbox.post(req)
    inbox.set_status(req.id, control="awaiting_human", holder="-", pending_dialogs=[])
    code = operator_cli(argparse.Namespace(inbox=str(inbox.root), operator=OPERATOR, note=None,
                                           command="approve", request_id=req.id, answer=None))
    out = capsys.readouterr().out
    assert code == 1 and "'approve' is not possible" in out and "Possible: abort, take" in out
    assert inbox.responses(req.id) == []                       # nothing sent that would be ignored


def test_window_closed_mid_run_is_a_clear_failure_not_a_crash(lookup_capability, tmp_path):
    _, server, base = serve()
    policy = policy_for(base)
    surface = WebSurface(policy, headless=True)
    closed = []

    def close_after_go(line):
        if "click_go" in line and not closed:
            closed.append(True)
            surface.context.close()                  # someone closes the window by mistake

    replayer = Replayer(surface, policy, tmp_path, step_timeout=3, echo=close_after_go)
    try:
        result = replayer.run(lookup_capability, {"member_number": "12345"}, base_url=base)
    finally:
        server.shutdown()
    assert result.status == "failed" and result.outcome == "session_lost" and result.escalate
    assert result.error.kind == "session_lost" and result.error.step == "click_select"
    assert (replayer.log.dir / "result.json").exists()
    assert result.control_history[-1]["to"] == "finished"


def test_window_closed_while_human_holds_control_closes_the_request(lookup_capability, tmp_path):
    _, server, base = serve(error_rate=1.0)
    policy = policy_for(base)
    surface = WebSurface(policy, headless=True)
    inbox = Inbox(tmp_path / "inbox")
    seen: list[str] = []

    def operator(state, request):            # takes control, then closes the window - never resumes
        if not seen:
            seen.append(request.id)
            send(inbox, "take", request.id)
        elif state == ControlState.HUMAN:
            surface.page.context.close()

    replayer = Replayer(surface, policy, tmp_path, step_timeout=3, echo=lambda _: None,
                        handoff=Handoff(inbox, poll_ms=150, echo=lambda _: None, on_poll=operator))
    try:
        result = replayer.run(lookup_capability, {"member_number": "12345"}, base_url=base)
    finally:
        server.shutdown()
    assert result.status == "failed" and result.error.kind == "session_lost"
    [request_id] = seen
    status = inbox.status(request_id)
    assert status["control"] == "finished" and status["decision"] == "session_lost" and status["closed"]
    assert inbox.open_requests() == []                # nobody is left looking at a dead request


def test_automation_cannot_act_while_a_human_holds_control(tmp_path, live_app, policy):
    _, base = live_app
    surface = WebSurface(policy, headless=True)
    surface.control = Control()
    surface.start(f"{base}/login")
    try:
        target = next(e for e in surface.observe().elements if e.name == "Sign On")
        surface.control.transition(ControlState.AWAITING_HUMAN, by="automation", reason="test")
        surface.control.transition(ControlState.HUMAN, by=OPERATOR, reason="test")
        with pytest.raises(ActionError) as exc:
            surface.click(target.ref)
        assert exc.value.kind == "not_in_control"
    finally:
        surface.close()


def test_stuck_discovery_human_teaches_the_step_and_the_agent_continues(tmp_path, live_app, policy, secrets_env):
    _, base = live_app
    surface = WebSurface(policy, headless=True)
    inbox = Inbox(tmp_path / "inbox")

    def human_searches(page):
        work = page.frame(name="work")
        work.fill("input[name=q1]", "12345")
        work.click("input[type=submit]")
        work.wait_for_selector("text=Select")

    poke = lambda o: ("click", {"ref": ref(o, role="cell", name="Member Inquiry", frame="work"), "reason": R})  # noqa: E731
    script = LOGIN + [poke, poke, poke] + [
        lambda o: ("click", {"ref": ref(o, role="link", name="Select"), "reason": R}),
        lambda o: ("extract", {"ref": ref(o, col_header="Balance", row_key="0000"), "name": "savings_balance",
                               "type": "decimal", "description": "balance", "reason": R}),
        lambda o: ("finish", {"status": "success", "summary": "ok", "success_evidence": "Member Summary"}),
    ]
    model = ScriptedModel(script)
    agent = DiscoveryAgent(model, surface, policy, tmp_path / "disc", echo=lambda _: None,
                           handoff=Handoff(inbox, poll_ms=150, echo=lambda _: None,
                                           on_poll=Operator(inbox, surface, work=human_searches)))
    model.agent = agent
    result = agent.run("look up member 12345 and read their savings balance", f"{base}/login")

    assert result.trace.status == "success" and result.outputs == {"savings_balance": "$1,523.47"}
    assert result.trace.interventions[0]["decision"] == "resumed"
    human = [s for s in result.trace.steps if s.actor == "human"]
    assert {s.action for s in human} >= {"type_text", "click"}
    # What the human did is part of the flow: the compiled capability includes it.
    cap = compile_trace(result.trace, PROFILE, capability_id="taught_lookup", policy=policy).capability
    assert any(s.action == "fill" and s.target.frame == "work" and s.value == "12345" for s in cap.steps)
