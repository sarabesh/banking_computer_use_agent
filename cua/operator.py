"""Operator CLI: the (deliberately minimal) human side of the handoff.

    python -m cua operator list                       open intervention requests
    python -m cua operator show <id>                  full context: why, where, screenshot
    python -m cua operator take <id>                  take control of the live browser window
    python -m cua operator resume <id>                hand control back to automation
    python -m cua operator done <id>                  "I finished the task myself"
    python -m cua operator approve|reject <id>        decide on an irreversible step
    python -m cua operator abort <id>                 stop the run
    python -m cua operator dialog <id> accept|dismiss answer a browser dialog raised while you hold control

It runs in a separate process from the automation and only talks to it through the inbox.
The automation enforces what is allowed in each control state; this CLI just asks.
"""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path

from .handoff import Inbox, InterventionResponse, allowed_now


def add_parser(sub, root: Path) -> None:
    p = sub.add_parser("operator", help="human operator side of the handoff")
    p.add_argument("--inbox", default=str(root / "runs" / "inbox"))
    p.add_argument("--operator", default=getpass.getuser(), help="your operator id (recorded with every action)")
    p.add_argument("--note", default=None, help="free-text note recorded with the action")
    p.add_argument("command", choices=["list", "show", "take", "resume", "done", "approve", "reject", "abort", "dialog"])
    p.add_argument("request_id", nargs="?")
    p.add_argument("answer", nargs="?", choices=["accept", "dismiss"])
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    inbox = Inbox(args.inbox)
    if args.command == "list":
        requests = inbox.open_requests()
        if not requests:
            print("No open intervention requests.")
        for req in requests:
            st = inbox.status(req.id)
            print(f"{req.id}  [{st.get('control', '?')}]  {req.trigger:<15} {req.capability or req.goal}\n"
                  f"    step: {req.step}   reason: {req.reason}\n"
                  f"    allowed: {', '.join(req.allowed)}" +
                  (f"\n    pending dialog(s): {st['pending_dialogs']}" if st.get("pending_dialogs") else ""))
        return 0
    if not args.request_id:
        print("This command needs a request id (see: operator list).")
        return 2
    try:
        req = inbox.request(args.request_id)
    except FileNotFoundError:
        print(f"No request {args.request_id}.")
        return 2
    if args.command == "show":
        print(json.dumps({"request": req.model_dump(), "status": inbox.status(req.id)}, indent=2))
        return 0
    action = {"take": "take_control", "dialog": f"dialog_{args.answer or 'dismiss'}"}.get(args.command, args.command)
    status = inbox.status(req.id)
    if status.get("closed"):
        print(f"{req.id} is already closed ({status.get('decision')}).")
        return 1
    possible = allowed_now(status.get("control", ""), req.allowed)
    if action not in possible:
        # Don't claim success for a command the automation will ignore.
        commands = sorted({"take_control": "take", "dialog_accept": "dialog", "dialog_dismiss": "dialog"}.get(a, a)
                          for a in possible)
        print(f"'{args.command}' is not possible for {req.id} right now (trigger: {req.trigger}, "
              f"control: {status.get('control', '?')}). Possible: {', '.join(commands) or 'nothing'}.")
        return 1
    dialog_id = None
    if args.command == "dialog":
        pending = status.get("pending_dialogs") or []
        if not pending:
            print("No dialog is waiting for an answer.")
            return 1
        dialog_id = pending[0]["id"]
    inbox.respond(InterventionResponse(request_id=req.id, action=action, operator=args.operator,
                                       note=args.note, dialog_id=dialog_id))
    hints = {"take_control": "You have the live browser window. Do what is needed, then run: "
                             f"python -m cua operator resume {req.id}   (or: done {req.id})",
             "resume": "Control goes back to automation.", "approve": "Automation will perform the step."}
    print(f"Sent '{action}' for {req.id} as {args.operator}. {hints.get(action, '')}")
    return 0
