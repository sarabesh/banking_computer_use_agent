"""The handoff coordinator: pause automation, cede the live session, wait, resume.

It never opens a new session. The browser the automation was driving stays open and is
handed to the operator as-is; while they hold control, their actions are recorded with the
same element fingerprints the agent uses. The wait is in-process: Playwright's event loop
keeps running (pump), so recording and dialogs keep working while we wait.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .control import Control, ControlError, ControlState
from .inbox import Inbox, InterventionRequest, InterventionResponse

Decision = Literal["resumed", "done", "approved", "rejected", "aborted", "timeout"]


@dataclass
class HandoffOutcome:
    request_id: str
    decision: Decision
    operator: str | None = None
    note: str | None = None
    human_events: list[dict] = field(default_factory=list)
    waited_s: float = 0.0

    def summary(self) -> dict:
        return {"request_id": self.request_id, "decision": self.decision, "operator": self.operator,
                "note": self.note, "human_actions": len(self.human_events), "waited_s": round(self.waited_s, 1)}


class Handoff:
    def __init__(self, inbox: Inbox, *, response_timeout_s: float = 900.0, human_timeout_s: float = 1800.0,
                 poll_ms: int = 400, echo: Callable[[str], None] = print,
                 on_poll: Callable[[ControlState, Any], None] | None = None):
        self.inbox = inbox
        self.response_timeout_s = response_timeout_s
        self.human_timeout_s = human_timeout_s
        self.poll_ms = poll_ms
        self.echo = echo
        self.on_poll = on_poll          # test seam: lets a simulated operator act in the same thread

    def escalate(self, request: InterventionRequest, surface: Any, control: Control, log: Any) -> HandoffOutcome:
        control.transition(ControlState.AWAITING_HUMAN, by="automation", reason=f"{request.trigger}: {request.reason}")
        self.inbox.post(request)
        self._status(request, control, surface)
        log.event("intervention_requested", **request.model_dump(exclude={"recent_steps"}))
        self.echo(f"\n  >> HUMAN NEEDED [{request.id}] {request.trigger}: {request.reason}\n"
                  f"     operator: python -m cua operator take {request.id}   (allowed: {', '.join(request.allowed)})\n")
        t0 = time.monotonic()
        state_since = t0
        seen = 0
        while True:
            surface.pump(self.poll_ms)
            if self.on_poll:
                self.on_poll(control.state, request)
            for resp in self.inbox.responses(request.id, seen):
                seen += 1
                outcome = self._apply(resp, request, surface, control, log, t0)
                if outcome is not None:
                    self._status(request, control, surface, decision=outcome.decision, closed=True)
                    log.event("intervention_closed", **outcome.summary())
                    return outcome
                state_since = time.monotonic()
                self._status(request, control, surface)
            if surface.pending_dialogs():
                self._status(request, control, surface)
            limit = self.human_timeout_s if control.state == ControlState.HUMAN else self.response_timeout_s
            if time.monotonic() - state_since > limit:
                events = surface.stop_recording() if control.state == ControlState.HUMAN else []
                control.transition(ControlState.FINISHED, by="automation", reason="no operator response in time")
                outcome = HandoffOutcome(request.id, "timeout", human_events=events, waited_s=time.monotonic() - t0)
                self._status(request, control, surface, decision="timeout", closed=True)
                log.event("intervention_closed", **outcome.summary())
                return outcome

    def _apply(self, resp: InterventionResponse, request: InterventionRequest, surface: Any, control: Control,
               log: Any, t0: float) -> HandoffOutcome | None:
        state = control.state
        # While waiting: the request's own options (take control / approve / reject / abort).
        # While a human has control: hand back, answer a dialog, abort - and "done" if offered.
        allowed_now = {
            ControlState.AWAITING_HUMAN: set(request.allowed) - {"resume", "done"},
            ControlState.HUMAN: {"resume", "abort", "dialog_accept", "dialog_dismiss"} | ({"done"} & set(request.allowed)),
        }.get(state, set())
        if resp.action not in allowed_now:
            log.event("operator_response_ignored", action=resp.action, state=state.value, operator=resp.operator)
            return None
        log.event("operator_response", action=resp.action, operator=resp.operator, note=resp.note)
        waited = time.monotonic() - t0
        try:
            if resp.action == "take_control":
                control.transition(ControlState.HUMAN, by=resp.operator, reason="operator took control")
                surface.start_recording()
                self.echo(f"  >> {resp.operator} has control of the live session. Automation is paused.")
                return None
            if resp.action in ("dialog_accept", "dialog_dismiss"):
                surface.answer_dialog(resp.dialog_id, resp.action == "dialog_accept")
                return None
            if resp.action == "resume":
                events = surface.stop_recording()
                control.transition(ControlState.AUTOMATION, by=resp.operator, reason="operator handed control back")
                self.echo(f"  >> control returned to automation ({len(events)} human action(s) recorded)")
                return HandoffOutcome(request.id, "resumed", resp.operator, resp.note, events, waited)
            if resp.action == "approve":
                control.transition(ControlState.AUTOMATION, by=resp.operator, reason="operator approved the step")
                return HandoffOutcome(request.id, "approved", resp.operator, resp.note, [], waited)
            events = surface.stop_recording() if state == ControlState.HUMAN else []
            decision = {"done": "done", "reject": "rejected", "abort": "aborted"}[resp.action]
            if decision == "done":
                control.transition(ControlState.AUTOMATION, by=resp.operator, reason="operator completed the task")
            else:
                control.transition(ControlState.FINISHED, by=resp.operator, reason=f"operator chose {resp.action}")
            return HandoffOutcome(request.id, decision, resp.operator, resp.note, events, waited)
        except ControlError as exc:
            log.event("operator_response_ignored", action=resp.action, error=str(exc))
            return None

    def _status(self, request: InterventionRequest, control: Control, surface: Any, **extra) -> None:
        self.inbox.set_status(request.id, control=control.state.value, holder=control.holder,
                              pending_dialogs=surface.pending_dialogs(), **extra)
