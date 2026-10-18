"""Command line:  python -m cua discover --goal "..." --url http://127.0.0.1:5001/login"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def cmd_discover(args: argparse.Namespace) -> int:
    from .discovery import DiscoveryAgent
    from .llm import AnthropicModel
    from .policy import Policy
    from .surface.web import WebSurface

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2
    policy = Policy.load(args.policy)
    surface = WebSurface(policy, headless=args.headless)
    handoff = None
    if args.handoff:
        from .handoff import Handoff, Inbox
        handoff = Handoff(Inbox(Path(args.runs_dir) / "inbox"), response_timeout_s=args.handoff_timeout)
    agent = DiscoveryAgent(AnthropicModel(args.model, effort=args.effort), surface, policy, Path(args.runs_dir),
                           max_steps=args.max_steps, max_seconds=args.max_seconds, vision=not args.no_vision,
                           handoff=handoff)
    print(f"Discovery: {args.goal}\nTarget: {args.url}  model={args.model} effort={args.effort}")
    result = agent.run(args.goal, args.url)
    trace = result.trace
    print(f"\nStatus: {trace.status}  ({trace.summary})")
    if trace.final and trace.final.business_outcome:
        print(f"Business outcome: {trace.final.business_outcome}")
    print(f"Parameters: {[p.name for p in trace.parameters]}")
    for name, value in result.outputs.items():  # returned to the caller; never persisted unredacted
        print(f"Output {name} = {value}")
    u = trace.usage
    print(f"Steps: {len(trace.steps)}  model turns: {u.model_turns}  tokens in/out: {u.input_tokens}/{u.output_tokens}"
          f"  cache read: {u.cache_read_input_tokens}")
    print(f"Evidence: {result.run_dir}")
    return 0 if trace.status == "success" else 1


def cmd_compile(args: argparse.Namespace) -> int:
    import hashlib

    from .artifact import AppProfile, CompileError, compile_trace
    from .discovery.trace import DiscoveryTrace
    from .policy import Policy

    trace_path = Path(args.trace)
    raw = trace_path.read_bytes()
    trace = DiscoveryTrace.model_validate_json(raw)
    if "[REDACTED" in trace.run_id:          # traces from before the Luhn fix over-redacted the id
        trace.run_id = trace_path.parent.name
    try:
        report = compile_trace(trace, AppProfile.load(args.profile), capability_id=args.id, version=args.version,
                               policy=Policy.load(args.policy), trace_sha256=hashlib.sha256(raw).hexdigest())
    except (CompileError, ValueError) as exc:
        print(f"Cannot compile: {exc}", file=sys.stderr)
        return 1
    cap = report.capability
    path = cap.save(args.out)
    print(f"Capability {cap.id} v{cap.version} ({cap.status}) -> {path}")
    print(f"  inputs: {list(cap.inputs)}  outputs: {list(cap.outputs)}  secrets: {cap.secrets}  "
          f"side effects: {cap.side_effects}")
    print(f"  steps: {[s.id for s in cap.steps]}")
    print(f"  outcome rules: {[o.code for o in cap.outcomes]}")
    for line in report.skipped:
        print(f"  skipped {line}")
    for line in report.warnings:
        print(f"  warning: {line}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    import json

    from .artifact import Capability
    from .policy import Policy
    from .replay import Replayer
    from .surface.web import WebSurface

    try:
        cap = Capability.load(args.capability)
    except ValueError as exc:
        print(f"Cannot load capability: {exc}", file=sys.stderr)
        return 1
    inputs = {}
    for item in args.input:
        name, sep, value = item.partition("=")
        if not sep:
            print(f"--input expects name=value, got {item!r}", file=sys.stderr)
            return 1
        inputs[name] = value
    policy = Policy.load(args.policy)
    handoff = None
    if args.handoff:
        from .handoff import Handoff, Inbox
        handoff = Handoff(Inbox(Path(args.runs_dir) / "inbox"), response_timeout_s=args.handoff_timeout)
    replayer = Replayer(WebSurface(policy, headless=args.headless), policy, Path(args.runs_dir),
                        step_timeout=args.step_timeout, handoff=handoff)
    print(f"Replay {cap.id} v{cap.version} ({cap.status}) against {args.base_url}  - no LLM involved")
    result = replayer.run(cap, inputs, base_url=args.base_url, allow_draft=args.allow_draft)
    # The caller gets the full result, including outputs; the persisted result.json is redacted.
    print(json.dumps(result.model_dump(mode="json", exclude={"steps"}), indent=2))
    print(f"Evidence: {replayer.log.dir}")
    return {"success": 0, "business_outcome": 3, "needs_approval": 4}.get(result.status, 1)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(prog="cua", description="Computer-use automation for legacy back-office apps")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run against a live application")
    d.add_argument("--goal", required=True, help="natural-language goal")
    d.add_argument("--url", required=True, help="entry point of the target application")
    d.add_argument("--policy", default=str(ROOT / "policy.yaml"))
    d.add_argument("--runs-dir", default=str(ROOT / "runs"))
    d.add_argument("--model", default="claude-opus-5-5")
    d.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    d.add_argument("--max-steps", type=int, default=30)
    d.add_argument("--max-seconds", type=float, default=600)
    d.add_argument("--headless", action="store_true")
    d.add_argument("--no-vision", action="store_true", help="send only the element list, no screenshots")
    d.add_argument("--handoff", action="store_true", help="when stuck or needing approval, pause for a human operator")
    d.add_argument("--handoff-timeout", type=float, default=900, help="seconds to wait for an operator")
    d.set_defaults(func=cmd_discover)

    c = sub.add_parser("compile", help="compile a successful discovery trace into a capability artifact")
    c.add_argument("trace", help="path to runs/<run>/trace.json")
    c.add_argument("--id", help="capability id (snake_case), e.g. member_savings_balance")
    c.add_argument("--version", default="0.1.0")
    c.add_argument("--profile", default=str(ROOT / "apps" / "coreline_teller.yaml"))
    c.add_argument("--policy", default=str(ROOT / "policy.yaml"))
    c.add_argument("--out", default=str(ROOT / "capabilities"))
    c.set_defaults(func=cmd_compile)

    r = sub.add_parser("replay", help="deterministically replay a capability (no LLM)")
    r.add_argument("capability", help="path to capabilities/<id>/<version>.yaml")
    r.add_argument("--base-url", required=True, help="the tenant's application URL, e.g. http://127.0.0.1:5001")
    r.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--allow-draft", action="store_true", help="supervised run of a capability not yet approved")
    r.add_argument("--policy", default=str(ROOT / "policy.yaml"))
    r.add_argument("--runs-dir", default=str(ROOT / "runs"))
    r.add_argument("--step-timeout", type=float, default=10.0)
    r.add_argument("--headless", action="store_true")
    r.add_argument("--handoff", action="store_true",
                   help="on an unrecoverable condition or an approval step, pause for a human operator")
    r.add_argument("--handoff-timeout", type=float, default=900, help="seconds to wait for an operator")
    r.set_defaults(func=cmd_replay)

    from .operator import add_parser as add_operator_parser
    add_operator_parser(sub, ROOT)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
