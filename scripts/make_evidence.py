"""Regenerate the replay evidence in /evidence/ from a capability, reproducibly.

    python scripts/make_evidence.py capabilities/member_savings_balance/0.2.0.yaml

Each scenario starts a fresh mock app (its own port), injects one condition through the
mock's fault switches, and replays the capability with the real engine - no LLM. The
handoff scenario uses a simulated operator that goes through the real operator CLI code and
inbox, and acts in the same live browser (see tests/test_handoff.py for the same pattern).

The LLM discovery run itself is NOT regenerated here (it costs tokens and is non-
deterministic by nature); copy a real run with --discovery-run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from cua.artifact import Capability  # noqa: E402
from cua.handoff import ControlState, Handoff, Inbox  # noqa: E402
from cua.operator import run as operator_cli  # noqa: E402
from cua.policy import Policy  # noqa: E402
from cua.replay import Replayer  # noqa: E402
from cua.surface.web import WebSurface  # noqa: E402
from target_app import FaultConfig, create_app  # noqa: E402


def serve(tenant: str = "acme", **faults):
    app = create_app(tenant=tenant, faults=FaultConfig(**{"confirm_dialog": False, **faults}), seed=1)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return app, server, f"http://127.0.0.1:{server.server_port}"


class SimulatedOperator:
    """Takes control, signs back in on the same browser, hands control back."""

    def __init__(self, inbox: Inbox, surface: WebSurface):
        self.inbox, self.surface, self.state = inbox, surface, "new"

    def send(self, command: str, request_id: str) -> None:
        operator_cli(argparse.Namespace(inbox=str(self.inbox.root), operator="ops-demo", command=command,
                                        request_id=request_id, answer=None,
                                        note="simulated operator for the evidence run"))

    def __call__(self, state, request):
        if self.state == "new":
            self.state = "taken"
            self.send("take", request.id)
        elif self.state == "taken" and state == ControlState.HUMAN:
            self.state = "worked"
            page = self.surface.page
            page.frame(name="work").get_by_role("link", name="Sign On").click()
            page.wait_for_url("**/login")
            page.fill("input[name=u]", os.environ["TELLER_USER"])
            page.fill("input[name=p]", os.environ["TELLER_PASSWORD"])
            page.click("input[type=submit]")
            page.wait_for_url("**/main")
            page.frame(name="work").wait_for_selector("input[name=q1]")
            self.send("resume", request.id)


SCENARIOS = [
    # name, what it demonstrates, member, tenant, faults, handoff
    ("01-replay-success", "the happy path, all steps via primary locators", "12345", "acme", {}, False),
    ("02-replay-success-other-member", "same capability, different input", "24680", "acme", {}, False),
    ("03-business-outcome-not-found", "legitimate negative answer, not a crash", "99999", "acme", {}, False),
    ("04-business-outcome-invalid-input", "app-side validation error mapped to a business outcome", "12a45", "acme", {}, False),
    ("05-recovered-system-notice", "blocking interstitial dismissed, run continues", "12345", "acme", {"interstitial_rate": 1.0}, False),
    ("06-recovered-slow-app", "1.5 s latency on every page, bounded waiting", "12345", "acme", {"delay_ms": 1500}, False),
    ("07-failure-app-error-escalated", "persistent app error: bounded reloads, then hard failure + intervention request returned to the caller", "12345", "acme", {"error_rate": 1.0}, False),
    ("08-failure-session-expired", "session expiry: hard failure with the failing step identified", "12345", "acme", {"session_ttl_s": 0}, False),
    ("09-drift-other-tenant", "same vendor product, other tenant: fallback locators carry the flow, success checkpoint reports drift", "12345", "riverbend", {}, False),
    ("10-handoff-session-expired", "session dies mid-run; operator takes the live session, signs in again, hands back; replay resumes", "12345", "acme", {}, True),
]


def run_scenario(cap: Capability, out: Path, name: str, member: str, tenant: str, faults: dict, handoff: bool) -> dict:
    app, server, base = serve(tenant, **faults)
    policy = Policy.load(ROOT / "policy.yaml")
    policy.allowed_origins = [base]
    surface = WebSurface(policy, headless=True)
    work = out / "_work" / name
    hand = None
    echo = lambda _line: None  # noqa: E731
    if handoff:
        inbox = Inbox(work / "inbox")
        hand = Handoff(inbox, poll_ms=200, echo=echo, on_poll=SimulatedOperator(inbox, surface))

        def echo(line, app=app):      # noqa: F811 - kill the session right after sign-on
            if "click_sign_on" in line:
                app.config["STATE"]["session_gen"] += 1
    replayer = Replayer(surface, policy, work, step_timeout=4, echo=echo, handoff=hand)
    try:
        result = replayer.run(cap, {next(iter(cap.inputs)): member}, base_url=base, allow_draft=True)
    finally:
        server.shutdown()
    dest = out / name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(replayer.log.dir, dest)
    if handoff:
        shutil.copytree(work / "inbox", dest / "inbox")
    return {"scenario": name, "status": result.status, "outcome": result.outcome,
            "error": result.error.kind if result.error else None,
            "interventions": [i.get("decision") for i in result.interventions],
            "low_confidence_steps": result.low_confidence_steps, "duration_ms": result.duration_ms}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capability")
    parser.add_argument("--out", default=str(ROOT / "evidence"))
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    cap = Capability.load(args.capability)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    summary = []
    for name, what, member, tenant, faults, handoff in SCENARIOS:
        row = run_scenario(cap, out, name, member, tenant, faults, handoff)
        row["demonstrates"] = what
        summary.append(row)
        print(f"{name:<38} {row['status']:<17} {row['outcome'] or row['error'] or ''}")
    shutil.rmtree(out / "_work", ignore_errors=True)
    (out / "scenarios.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
