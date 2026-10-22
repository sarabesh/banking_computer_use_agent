"""The discovery agent: an LLM-driven observe -> decide -> act loop against a live surface.

The model decides; this code observes, enforces policy, acts, detects stuck states, and
records a structured trace. Every action is one model turn, so each decision is made on a
fresh observation and the trace is strictly linear.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from ..evidence import RunLog, utcnow
from ..handoff import Control, Handoff, InterventionRequest
from ..policy import Decision, Policy
from ..redact import Redactor
from ..surface.base import ActionError, Element, Location, Observation, Surface
from .tools import TOOL_INPUTS, tool_definitions
from .trace import (DiscoveryTrace, FinalState, LocationSnapshot, OutputDecl, ParamDecl, TargetFingerprint,
                    TraceStep)

SYSTEM_PROMPT = """\
You are the discovery agent of a computer-use automation system for bank and credit-union \
back-office applications. You operate a live legacy application through tools, one action \
per turn, to accomplish an operator's goal. Your run is compiled into a deterministic, \
replayable automation that will later run without you, so demonstrate the canonical way a \
trained operator would do this task.

How you see the screen: after every action you get a fresh observation listing elements \
with refs (e1, e2, ...), grouped by frame, often with a screenshot. Refs are valid only for \
the latest observation. Fields often have no label; their caption is the visible text \
beside them. Table cells show their column header and row key. "(covered)" means something, \
typically a notice or dialog overlay, sits on top of the element: deal with that first. \
Some values are masked as [REDACTED:...]; you never need them.

Rules:
- Act only through the tools, only on refs from the latest observation. Never invent refs.
- A value you type or select that comes from the goal (member number, name, amount, choice) \
is an input of the capability: pass `parameter` with a snake_case name so it can be supplied \
per invocation. Fixed parts of the procedure need no parameter.
- Never type credentials as text. Use type_secret with the named secret.
- Capture data the goal asks for with extract on the element that shows it; the system reads \
the value from the screen. Use snake_case output names.
- Take the most direct path. Do not explore screens unrelated to the goal.
- Irreversible controls (post, confirm, transfer, delete) are refused by policy. Never try to \
work around that. If the goal truly requires one, call request_human.
- Read errors, notices and dialogs and respond deliberately: acknowledge known notices, fix \
input errors. If the application gives a legitimate negative answer (e.g. no such member), \
finish with status "failed" and a business_outcome code.
- If you are stuck or the screen makes no sense, call request_human instead of guessing.
- Never write personal or financial data (names, addresses, balances, identifiers other than \
the goal's own inputs) into reasons, summaries or evidence. Refer to it by role, e.g. "the \
member's name", "the balance cell". These texts are persisted; the data must not be.
- When the goal is reached and outputs are extracted, call finish with status "success" and \
describe what on screen proves it; that becomes the replay's success check."""


@dataclass
class ToolOutcome:
    text: str
    is_error: bool = False
    terminal: bool = False
    state_changing: bool = False


@dataclass
class DiscoveryResult:
    trace: DiscoveryTrace
    outputs: dict[str, str]          # actual values, returned to the caller only, never persisted
    run_dir: Path
    intervention: dict | None = None


@dataclass
class _RunState:
    obs: Observation
    image_b64: str | None = None
    no_change_streak: int = 0
    error_streak: int = 0
    nudges: int = 0
    outputs: dict[str, str] = field(default_factory=dict)
    intervention: dict | None = None


def snapshot(loc: Location) -> LocationSnapshot:
    return LocationSnapshot(url=loc.url, title=loc.title, frames=dict(loc.frames))


def render_observation(obs: Observation, redactor: Redactor) -> str:
    frames = ", ".join(f"{name}={path}" for name, path in obs.location.frames.items())
    lines = [f"Observation #{obs.seq}  title={obs.location.title!r}  frames: {frames}"]
    lines += [f"! Browser {d}" for d in obs.dialogs]
    lines += [f"! Policy blocked a request: {b}" for b in obs.blocked_requests]
    current = None
    for e in obs.elements:
        if e.frame != current:
            current = e.frame
            lines.append(f"\n[frame {e.frame} {e.frame_path}]")
        lines.append(_render_element(e))
    return redactor.text("\n".join(lines))


def _render_element(e: Element) -> str:
    parts = [e.ref, e.role]
    if e.name:
        parts.append(json.dumps(e.name, ensure_ascii=False))
    if e.caption:
        parts.append(f"caption={json.dumps(e.caption, ensure_ascii=False)}")
    if e.col_header:
        parts.append(f"col={json.dumps(e.col_header, ensure_ascii=False)}")
    if e.kind == "text" and e.col_header and e.row_key:
        parts.append(f"row={json.dumps(e.row_key, ensure_ascii=False)}")
    if e.kind == "control" and e.row_text and e.row_text not in (e.name, e.caption):
        parts.append(f"in row {json.dumps(e.row_text, ensure_ascii=False)}")
    if e.role in ("textbox",):
        parts.append("(filled)" if e.has_value else "(empty)")
    if e.options:
        parts.append(f"options={json.dumps(e.options, ensure_ascii=False)} selected={json.dumps(e.selected)}")
    if e.synthetic:
        parts.append("(clickable element, not a real button)")
    if e.occluded:
        parts.append("(covered)")
    return "  " + " ".join(parts)


class DiscoveryAgent:
    def __init__(self, model: Any, surface: Surface, policy: Policy, runs_dir: Path, *,
                 max_steps: int = 30, max_seconds: float = 600, vision: bool = True, stuck_after: int = 3,
                 on_escalate: Callable[[dict], None] | None = None, echo: Callable[[str], None] = print,
                 handoff: Handoff | None = None):
        self.model = model
        self.surface = surface
        self.policy = policy
        self.runs_dir = Path(runs_dir)
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.vision = vision
        self.stuck_after = stuck_after
        self.on_escalate = on_escalate
        self.echo = echo
        self.handoff = handoff
        self.tools = tool_definitions(sorted(policy.secrets))

    # ------------------------------------------------------------------ run

    def run(self, goal: str, start_url: str, *, keep_open: bool = False, tenant: str | None = None,
            identify: Callable[[Observation], Any] | None = None) -> DiscoveryResult:
        """The model is given the goal and the URL, nothing else about where it is. `identify`
        (cua.tenants.TenantRegistry.identify bound to the URL) decides from the first screen which
        institution this is - registering it if new - and is only recorded in the trace."""
        self.log_redactor = Redactor(self.policy, "logs")
        self.model_redactor = Redactor(self.policy, "model")
        log = RunLog(self.runs_dir, "discover", goal, self.log_redactor)
        self.log = log
        trace = DiscoveryTrace(run_id=log.run_id, goal=goal, start_url=start_url, tenant=tenant,
                               model=getattr(self.model, "name", "unknown"), started_at=utcnow())
        self.trace = trace
        self.control = Control(on_change=lambda rec: log.event("control_transferred", **rec))
        self.surface.control = self.control
        self._pending_gate: tuple[TraceStep, str, str | None] | None = None
        log.event("run_started", goal=goal, start_url=start_url, model=trace.model,
                  limits={"max_steps": self.max_steps, "max_seconds": self.max_seconds}, vision=self.vision)
        deadline = time.monotonic() + self.max_seconds
        state: _RunState | None = None
        try:
            self.surface.start(start_url)
            state = _RunState(obs=self.surface.observe())
            self._record_observation(state)
            if identify is not None:
                ident = identify(state.obs)
                trace.tenant = ident.tenant.id if ident.tenant else None
                log.event("tenant_identified", tenant=trace.tenant, how=ident.how, note=ident.note)
            messages: list[dict] = [{"role": "user", "content": self._initial_content(goal, start_url, state)}]
            while True:
                if len(trace.steps) >= self.max_steps:
                    self._abort(state, f"step limit reached ({self.max_steps} actions)")
                    break
                if time.monotonic() > deadline:
                    self._abort(state, f"time limit reached ({self.max_seconds:.0f}s)")
                    break
                response = self.model.respond(SYSTEM_PROMPT, self.tools, messages)
                messages.append({"role": "assistant", "content": response.content})
                self._count_usage(response)
                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    self._log_turn(response, None)
                    self._abort(state, f"model refused the turn: {details}")
                    break
                tool_use = next((b for b in response.content if b.type == "tool_use"), None)
                if tool_use is None:
                    self._log_turn(response, None)
                    state.nudges += 1
                    if state.nudges > 2:
                        note = self._escalate(state, "stuck", "the model stopped calling tools")
                        if note is None:
                            break
                        messages.append({"role": "user", "content": f"{note}\n\n"
                                         f"{render_observation(state.obs, self.model_redactor)}"})
                        continue
                    messages.append({"role": "user", "content": "Continue by calling exactly one tool."})
                    continue
                outcome = self._dispatch(tool_use, state, response)
                if outcome.terminal:
                    break
                outcome = self._after_action(outcome, state)
                if state.intervention:
                    break
                content: list[dict] = [{"type": "text", "text": f"{outcome.text}\n\n"
                                        f"{render_observation(state.obs, self.model_redactor)}"}]
                if state.image_b64:
                    content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                "data": state.image_b64}})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use.id, "content": content,
                     "is_error": outcome.is_error}]})
        except ActionError as exc:
            log.event("surface_error", kind=exc.kind, message=str(exc))
            trace.status, trace.summary = "aborted", f"surface error: {exc}"
        except Exception as exc:  # noqa: BLE001 - recorded as evidence, then re-raised
            log.event("crash", error=repr(exc))
            trace.status, trace.summary = "aborted", f"crash: {exc!r}"
            raise
        finally:
            trace.finished_at = utcnow()
            trace.control_history = self.control.history
            if trace.final is None and state is not None:
                trace.final = self._final_state(state.obs)
            log.write_json("trace.json", trace)
            log.event("run_finished", status=trace.status, summary=trace.summary, steps=len(trace.steps),
                      usage=trace.usage, outputs={k: v for k, v in (state.outputs if state else {}).items()})
            log.close()
            if not keep_open:
                self.surface.close()
        return DiscoveryResult(trace=trace, outputs=state.outputs if state else {}, run_dir=log.dir,
                               intervention=state.intervention if state else None)

    # ------------------------------------------------------------------ observation

    def _initial_content(self, goal: str, start_url: str, state: _RunState) -> list[dict]:
        secrets = "\n".join(f"- {name}: {ref.description}" for name, ref in sorted(self.policy.secrets.items()))
        text = (f"Goal: {goal}\nTarget application: {start_url}\n"
                f"Secrets available to type_secret:\n{secrets}\n\n"
                f"{render_observation(state.obs, self.model_redactor)}")
        content: list[dict] = [{"type": "text", "text": text}]
        if state.image_b64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": state.image_b64}})
        return content

    def _record_observation(self, state: _RunState) -> None:
        obs = state.obs
        self.current_observation = obs
        shot = self.log.path(f"step-{len(self.trace.steps):02d}-obs{obs.seq}.jpg")
        self.surface.screenshot(str(shot), self.log_redactor.js_patterns, mask_data=True)   # evidence: no data
        state.image_b64 = None
        if self.vision:
            raw = self.surface.screenshot(None, self.model_redactor.js_patterns)
            state.image_b64 = base64.b64encode(raw).decode()
        self.log.event("observation", obs_seq=obs.seq, location=snapshot(obs.location), signature=obs.signature(),
                       elements=len(obs.elements), dialogs=obs.dialogs, blocked_requests=obs.blocked_requests,
                       screenshot=shot.name)

    def _after_action(self, outcome: ToolOutcome, state: _RunState) -> ToolOutcome:
        before = state.obs.signature()
        state.obs = self.surface.observe()
        self._record_observation(state)
        changed = state.obs.signature() != before
        if self.trace.steps:
            last = self.trace.steps[-1]
            if last.after is None and last.status == "ok":
                last.after, last.screen_changed = snapshot(state.obs.location), changed
                last.dialogs = list(state.obs.dialogs)
        state.error_streak = state.error_streak + 1 if outcome.is_error else 0
        if outcome.state_changing and not outcome.is_error:
            state.no_change_streak = 0 if changed else state.no_change_streak + 1
        stuck = None
        if state.error_streak >= self.stuck_after:
            stuck = f"{state.error_streak} consecutive failed actions"
        elif state.no_change_streak >= self.stuck_after:
            stuck = f"{state.no_change_streak} consecutive actions did not change the screen"
        if stuck and (note := self._escalate(state, "stuck", stuck)):
            outcome.text = f"{outcome.text}\n\n{note}"
        return outcome

    # ------------------------------------------------------------------ actions

    def _dispatch(self, tool_use: Any, state: _RunState, response: Any) -> ToolOutcome:
        name, raw = tool_use.name, tool_use.input
        model_cls = TOOL_INPUTS.get(name)
        refusal = None
        if model_cls is None:
            refusal = f"Unknown tool '{name}'."
        else:
            try:
                args = model_cls.model_validate(raw)
            except ValidationError as exc:
                refusal = f"Invalid input for {name}: " + "; ".join(e["msg"] for e in exc.errors(include_url=False))
        if refusal is not None:
            self._log_turn(response, tool_use)
            self.log.event("action_result", tool=name, ok=False, result=refusal)   # every call gets a result
            return ToolOutcome(refusal, is_error=True)
        # Register sensitive values before anything (including the model turn) is logged.
        param = getattr(args, "parameter", None)
        if param is not None and param.sensitive:
            self.log_redactor.add_known(getattr(args, "text", None) or getattr(args, "option", ""), f"param:{param.name}")
        self._log_turn(response, tool_use)
        handler = getattr(self, f"_do_{name}")
        outcome = handler(args, state)
        self.log.event("action_result", tool=name, ok=not outcome.is_error, result=outcome.text)
        self.echo(f"  [{len(self.trace.steps):02d}] {name}: {self.log_redactor.text(outcome.text)[:140]}")
        return outcome

    def _step(self, state: _RunState, action: str, reason: str, element: Element | None,
              decision: Decision, **fields: Any) -> TraceStep:
        target = TargetFingerprint(**element.fingerprint()) if element else None
        step = TraceStep(index=len(self.trace.steps), action=action, reason=reason, target=target,
                         policy=decision, observation_seq=state.obs.seq, before=snapshot(state.obs.location),
                         **fields)
        self.trace.steps.append(step)
        return step

    def _resolve(self, ref: str, state: _RunState) -> Element | None:
        return state.obs.element(ref)

    def _gate(self, state: _RunState, action: str, reason: str, element: Element | None,
              decision: Decision, **fields: Any) -> ToolOutcome | None:
        """Apply a non-allow verdict: record it and tell the model. Returns None if allowed."""
        if decision.verdict == "allow":
            return None
        status = "blocked" if decision.verdict == "block" else "needs_approval"
        step = self._step(state, action, reason, element, decision, status=status, detail=decision.reason, **fields)
        if status == "needs_approval":
            # Remembered so an operator can approve exactly this action via request_human.
            self._pending_gate = (step, action, fields.get("key"))
        self.log.event("policy_verdict", action=action, verdict=decision.verdict, reason=decision.reason)
        if decision.verdict == "block":
            return ToolOutcome(f"Refused by policy: {decision.reason}.", is_error=True)
        return ToolOutcome(f"Not performed: {decision.reason} and requires a human decision. "
                           "If the goal requires this action, call request_human; otherwise finish.",
                           is_error=True)

    def _act(self, state: _RunState, step: TraceStep, fn: Callable[[], Any], ok_text: str) -> ToolOutcome:
        try:
            fn()
        except ActionError as exc:
            step.status, step.detail = "error", f"{exc.kind}: {exc}"
            return ToolOutcome(f"Action failed ({exc.kind}): {exc}", is_error=True, state_changing=True)
        return ToolOutcome(ok_text, state_changing=True)

    def _unknown_ref(self, ref: str) -> ToolOutcome:
        return ToolOutcome(f"There is no element '{ref}' in the latest observation.", is_error=True)

    def _do_click(self, args, state: _RunState) -> ToolOutcome:
        el = self._resolve(args.ref, state)
        if el is None:
            return self._unknown_ref(args.ref)
        decision = self.policy.check_action("click", el)
        if (gated := self._gate(state, "click", args.reason, el, decision)):
            return gated
        step = self._step(state, "click", args.reason, el, decision, status="ok")
        return self._act(state, step, lambda: self.surface.click(args.ref), f"Clicked {el.role} '{el.name}'.")

    def _do_type_text(self, args, state: _RunState) -> ToolOutcome:
        el = self._resolve(args.ref, state)
        if el is None:
            return self._unknown_ref(args.ref)
        decision = self.policy.check_action("type_text", el)
        binding = {"parameter": args.parameter.name} if args.parameter else {"literal": args.text}
        if (gated := self._gate(state, "type_text", args.reason, el, decision, **binding)):
            return gated
        if args.parameter:
            self._declare_param(args.parameter, args.text, None)
        step = self._step(state, "type_text", args.reason, el, decision, status="ok", **binding)
        label = el.caption or el.name or el.ref
        return self._act(state, step, lambda: self.surface.fill(args.ref, args.text), f"Typed into '{label}'.")

    def _do_type_secret(self, args, state: _RunState) -> ToolOutcome:
        el = self._resolve(args.ref, state)
        if el is None:
            return self._unknown_ref(args.ref)
        ref = self.policy.secrets.get(args.secret)
        if ref is None:
            return ToolOutcome(f"Unknown secret '{args.secret}'.", is_error=True)
        value = os.environ.get(ref.env)
        if not value:
            return ToolOutcome(f"Secret '{args.secret}' is not configured (set {ref.env}).", is_error=True)
        self.log_redactor.add_known(value, f"secret:{args.secret}")
        self.model_redactor.add_known(value, f"secret:{args.secret}")
        decision = self.policy.check_action("type_secret", el)
        if (gated := self._gate(state, "type_secret", args.reason, el, decision, secret=args.secret)):
            return gated
        step = self._step(state, "type_secret", args.reason, el, decision, status="ok", secret=args.secret)
        return self._act(state, step, lambda: self.surface.fill(args.ref, value),
                         f"Filled '{el.caption or el.ref}' with secret '{args.secret}'.")

    def _do_select_option(self, args, state: _RunState) -> ToolOutcome:
        el = self._resolve(args.ref, state)
        if el is None:
            return self._unknown_ref(args.ref)
        decision = self.policy.check_action("select_option", el)
        binding = {"parameter": args.parameter.name} if args.parameter else {"literal": args.option}
        if (gated := self._gate(state, "select_option", args.reason, el, decision, **binding)):
            return gated
        step = self._step(state, "select_option", args.reason, el, decision, status="ok", **binding)
        chosen: dict[str, str] = {}
        outcome = self._act(state, step, lambda: chosen.setdefault("label", self.surface.select(args.ref, args.option)),
                            f"Selected '{args.option}'.")
        if not outcome.is_error:
            if args.parameter:
                self._declare_param(args.parameter, chosen["label"], el.options)
            else:
                step.literal = chosen["label"]
            outcome.text = f"Selected '{chosen['label']}'."
        return outcome

    def _do_press_key(self, args, state: _RunState) -> ToolOutcome:
        submit = self.surface.pending_submit_label() if args.key == "Enter" else None
        decision = self.policy.check_action("press_key", key=args.key, submit_label=submit)
        if (gated := self._gate(state, "press_key", args.reason, None, decision, key=args.key)):
            return gated
        step = self._step(state, "press_key", args.reason, None, decision, status="ok", key=args.key,
                          detail=f"submits via '{submit}'" if submit else None)
        return self._act(state, step, lambda: self.surface.press(args.key), f"Pressed {args.key}.")

    def _do_wait(self, args, state: _RunState) -> ToolOutcome:
        decision = self.policy.check_action("wait")
        self._step(state, "wait", args.reason, None, decision, status="ok", seconds=args.seconds)
        time.sleep(args.seconds)
        return ToolOutcome(f"Waited {args.seconds:g}s.")

    def _do_extract(self, args, state: _RunState) -> ToolOutcome:
        el = self._resolve(args.ref, state)
        if el is None:
            return self._unknown_ref(args.ref)
        decision = self.policy.check_action("extract", el)
        step = self._step(state, "extract", args.reason, el, decision, status="ok", output=args.name)
        try:
            value = self.surface.read(args.ref)
        except ActionError as exc:
            step.status, step.detail = "error", str(exc)
            return ToolOutcome(f"Could not read the element: {exc}", is_error=True)
        if args.sensitive:
            self.log_redactor.add_known(value, f"output:{args.name}")
        state.outputs[args.name] = value
        if not any(o.name == args.name for o in self.trace.outputs):
            self.trace.outputs.append(OutputDecl(name=args.name, type=args.type, description=args.description,
                                                 sensitive=args.sensitive))
        where = ", ".join(x for x in (el.caption and f"caption '{el.caption}'", el.col_header and f"column '{el.col_header}'",
                                      el.row_key and f"row '{el.row_key}'") if x)
        return ToolOutcome(f"Extracted {args.name} = {self.model_redactor.text(value)!r}" + (f" ({where})." if where else "."))

    def _do_request_human(self, args, state: _RunState) -> ToolOutcome:
        note = self._escalate(state, args.kind, args.reason)
        if note is None:
            return ToolOutcome("Escalated to a human operator.", terminal=True)
        return ToolOutcome(note)

    def _do_finish(self, args, state: _RunState) -> ToolOutcome:
        trace = self.trace
        trace.status = "success" if args.status == "success" else "failed"
        trace.summary = args.summary
        trace.final = self._final_state(state.obs, args.success_evidence, args.business_outcome)
        return ToolOutcome(f"Run finished: {trace.status}.", terminal=True)

    # ------------------------------------------------------------------ bookkeeping

    def _declare_param(self, spec, example: str, options: list[str] | None) -> None:
        if any(p.name == spec.name for p in self.trace.parameters):
            return
        self.trace.parameters.append(ParamDecl(
            name=spec.name, type="enum" if options else spec.type, description=spec.description,
            sensitive=spec.sensitive, example=None if spec.sensitive else example,
            enum_values=options or None))

    def _final_state(self, obs: Observation, evidence: str | None = None, outcome: str | None = None) -> FinalState:
        return FinalState(location=snapshot(obs.location), headings=obs.headings(),
                          success_evidence=evidence, business_outcome=outcome)

    def _escalate(self, state: _RunState, kind: str, reason: str) -> str | None:
        """Raise an intervention request. Without a handoff the run ends escalated (None).
        With one, the live session is ceded to an operator; on their return we get back a note
        for the model describing what changed, and the run continues on the same session."""
        shot = self.log.path(f"intervention-{len(self.trace.steps):02d}.jpg")
        self.surface.screenshot(str(shot), self.log_redactor.js_patterns, mask_data=True)
        gate = self._pending_gate if kind == "approval_needed" else None
        if kind == "approval_needed" and gate is None:
            # The agent asked BEFORE attempting the action: approval is for the one irreversible
            # control on this screen, as classified by the policy (not by the model).
            gate = self._irreversible_on_screen(state, reason)
        request = InterventionRequest(
            run_id=self.trace.run_id, run_kind="discovery", goal=self.trace.goal,
            trigger="approval_needed" if gate else "stuck", reason=self.log_redactor.text(f"{kind}: {reason}"),
            step=f"#{len(self.trace.steps)}",
            screen={"frames": dict(state.obs.location.frames), "headings": self.log_redactor.obj(state.obs.headings())},
            screenshot=Path(os.path.relpath(shot, self.runs_dir)).as_posix(),
            recent_steps=[s.model_dump(include={"index", "action", "actor", "reason", "status", "detail"})
                          for s in self.trace.steps[-3:]],
            allowed=(["approve", "reject", "take_control", "resume", "abort"] if gate
                     else ["take_control", "resume", "abort"]))
        self.log.write_json("intervention.json", request)
        self.log.event("escalated", kind=kind, reason=reason, request_id=request.id)
        if self.on_escalate:
            self.on_escalate(request.model_dump())
        if self.handoff is not None:
            outcome = self.handoff.escalate(request, self.surface, self.control, self.log)
            self.trace.interventions.append({**outcome.summary(), "trigger": request.trigger, "reason": request.reason})
            if outcome.decision == "resumed":
                done = self._record_human_steps(outcome.human_events, state)
                return self._after_handoff(state, f"A human operator ({outcome.operator}) took control and "
                                                  f"handed it back. They did: {done or 'nothing'}.")
            if outcome.decision == "approved" and gate:
                return self._after_handoff(state, self._perform_approved(gate, outcome.operator, state))
        state.intervention = request.model_dump()
        self.trace.status, self.trace.summary = "escalated", f"{kind}: {reason}"
        self.trace.final = self._final_state(state.obs)
        return None

    def _irreversible_on_screen(self, state: _RunState, reason: str) -> tuple[TraceStep, str, str | None] | None:
        """The single control on screen the policy classifies as irreversible, recorded as the
        step awaiting approval. None if there is no such control, or more than one (ambiguous)."""
        candidates = [e for e in state.obs.elements
                      if e.kind == "control" and self.policy.is_irreversible_label(e.name)]
        if len(candidates) != 1:
            return None
        el = candidates[0]
        decision = Decision(verdict="needs_approval", reason=f"'{el.name}' is classified irreversible")
        step = self._step(state, "click", reason, el, decision, status="needs_approval", detail=decision.reason)
        self.log.event("policy_verdict", action="click", verdict="needs_approval", reason=decision.reason)
        return step, "click", None

    def _after_handoff(self, state: _RunState, note: str) -> str:
        state.no_change_streak = state.error_streak = state.nudges = 0
        state.obs = self.surface.observe()
        self._record_observation(state)
        self.trace.status = "aborted"           # still running; only finish() sets a final status
        return f"{note} Control is back with you; continue from the current screen."

    def _perform_approved(self, gate: tuple[TraceStep, str, str | None], operator: str | None,
                          state: _RunState) -> str:
        step, action, key = gate
        self._pending_gate = None
        previous = self.surface.dialog_policy
        self.surface.dialog_policy = lambda _t, _m: True   # the app's own "are you sure?" is part of it
        try:
            if action == "press_key":
                self.surface.press(key)
            else:
                el = self._refind(step.target, state.obs)
                if el is None:
                    return f"Operator {operator} approved, but the control is no longer on screen; nothing was done."
                self.surface.click(el.ref)
        except ActionError as exc:
            return f"Operator {operator} approved, but the action failed: {exc}"
        finally:
            self.surface.dialog_policy = previous
        step.status, step.detail = "ok", f"irreversible; approved by operator {operator}"
        self.log.event("approved_action", step=step.index, approved_by=operator)
        return f"Operator {operator} approved the irreversible action and it was performed."

    @staticmethod
    def _refind(target: TargetFingerprint | None, obs: Observation) -> Element | None:
        if target is None:
            return None
        hits = [e for e in obs.elements if e.frame == target.frame and e.role == target.role
                and e.name == target.name and e.css == target.css]
        return hits[0] if len(hits) == 1 else None

    def _record_human_steps(self, events: list[dict], state: _RunState) -> str:
        """Turn the operator's recorded actions into trace steps (actor=human), so a step the
        agent could not do becomes part of the compiled capability."""
        examples = {p.example: p.name for p in self.trace.parameters if p.example}
        kinds = {"click": "click", "fill": "type_text", "select": "select_option", "press": "press_key"}
        described = []
        for ev in events:
            el, action = ev.get("element"), kinds.get(ev["kind"])
            if action is None or el is None:
                if ev["kind"].startswith("dialog"):
                    described.append(ev["kind"].replace("_", " "))
                continue
            target = None
            if action != "press_key":
                fields = {k: el.get(k) for k in TargetFingerprint.model_fields if k in el}
                target = TargetFingerprint(**{**fields, "frame": ev["frame"], "frame_path": ev["frame_path"],
                                              "options": el.get("options") or []})
            bind: dict = {}
            label = el.get("caption") or el.get("name") or el.get("role")
            if action == "type_text":
                value = ev.get("value")
                if ev.get("secret") or value is None:
                    bind["detail"] = "credential typed by the operator; value not captured"
                elif value in examples:
                    bind["parameter"] = examples[value]
                else:
                    bind["literal"] = value
                described.append(f"typed into '{label}'")
            elif action == "select_option":
                bind["literal"] = ev.get("value")
                described.append(f"selected '{ev.get('value')}' in '{label}'")
            elif action == "press_key":
                bind["key"] = ev.get("key")
                described.append(f"pressed {ev.get('key')}")
            else:
                described.append(f"clicked {el.get('role')} '{label}'")
            self.trace.steps.append(TraceStep(
                index=len(self.trace.steps), action=action, actor="human", reason="performed by a human operator",
                target=target, status="ok", policy=Decision(verdict="allow", reason="performed by a human operator"),
                observation_seq=state.obs.seq, before=snapshot(state.obs.location),
                after=LocationSnapshot(url=state.obs.location.url, title="", frames={ev["frame"]: ev["frame_path"]}),
                screen_changed=True, **bind))
        for line in described:
            self.log.event("human_action", detail=line)
        return "; ".join(described)

    def _abort(self, state: _RunState, reason: str) -> None:
        self.trace.status, self.trace.summary = "aborted", reason
        self.log.event("aborted", reason=reason)

    def _count_usage(self, response: Any) -> None:
        usage = self.trace.usage
        usage.model_turns += 1
        u = getattr(response, "usage", None)
        if u is None:
            return
        usage.input_tokens += getattr(u, "input_tokens", 0) or 0
        usage.output_tokens += getattr(u, "output_tokens", 0) or 0
        usage.cache_read_input_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        usage.cache_creation_input_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

    def _log_turn(self, response: Any, tool_use: Any) -> None:
        thinking = [b.thinking for b in response.content if b.type == "thinking" and getattr(b, "thinking", "")]
        texts = [b.text for b in response.content if b.type == "text"]
        self.log.event("model_turn", request_id=getattr(response, "_request_id", None),
                       model=getattr(response, "model", None), stop_reason=response.stop_reason,
                       thinking_summary=thinking, text=texts,
                       tool=getattr(tool_use, "name", None), input=getattr(tool_use, "input", None))
