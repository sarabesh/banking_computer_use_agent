"""Deterministic replay: execute a capability with given inputs, no LLM anywhere.

Per step:
  1. wait (bounded) until the target's frame shows the expected document and the target
     resolves to exactly one element, checking the outcome rules on every observation:
       recoverable -> handle it (dismiss / reload, bounded) and keep waiting
       business    -> stop: legitimate answer for the caller
       failure     -> stop: clear error, maybe escalate
  2. policy check, then act; transient action errors (stale / covered) are retried in the wait
  3. verify the step's postcondition (`expect`)
After the last step the success checkpoint must hold. Every exit produces a structured
ReplayResult plus redacted evidence (events, screenshots, a snapshot of the failing screen).
"""
from __future__ import annotations

import os
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, TypeVar

from ..artifact.schema import Capability, Step, Target
from ..discovery.agent import render_observation
from ..evidence import RunLog, utcnow
from ..handoff import Control, Handoff, HandoffOutcome, InterventionRequest
from ..policy import Policy
from ..redact import Redactor
from ..surface.base import ActionError, Observation, Surface
from .locate import frame_mismatches, match_rule, missing_texts, render, resolve
from .result import ReplayError, ReplayResult, StepResult

T = TypeVar("T")
TRANSIENT = {"stale_ref", "covered", "timeout"}
POLICY_ACTION = {"fill": "type_text", "fill_secret": "type_secret", "click": "click", "select": "select_option",
                 "press": "press_key", "extract": "extract"}


class _Stop(Exception):
    """Ends the replay early with a final status."""

    def __init__(self, status: str, *, outcome: str | None = None, error: ReplayError | None = None,
                 escalate: bool = False):
        super().__init__(status)
        self.status, self.outcome, self.error, self.escalate = status, outcome, error, escalate


def parse_value(kind: str, raw: str) -> str:
    """Typed parsing of an extracted/input value; returns a canonical string form."""
    text = raw.strip()
    if kind == "decimal":
        cleaned = re.sub(r"[$,\s]", "", text)
        negative = cleaned.startswith("(") and cleaned.endswith(")")
        return str(-Decimal(cleaned.strip("()")) if negative else Decimal(cleaned))
    if kind == "integer":
        return str(int(text.replace(",", "")))
    if kind == "date":
        return date.fromisoformat(text).isoformat()
    if kind == "boolean":
        if text.lower() not in ("true", "false", "yes", "no"):
            raise ValueError(f"not a boolean: {text!r}")
        return str(text.lower() in ("true", "yes")).lower()
    return text


class Replayer:
    def __init__(self, surface: Surface, policy: Policy, runs_dir: Path, *, step_timeout: float = 10.0,
                 poll_interval: float = 0.3, echo: Callable[[str], None] = print,
                 handoff: Handoff | None = None, max_interventions: int = 3):
        self.surface = surface
        self.policy = policy
        self.runs_dir = Path(runs_dir)
        self.step_timeout = step_timeout
        self.poll_interval = poll_interval
        self.echo = echo
        self.handoff = handoff          # None: unattended. Escalations are returned to the caller.
        self.max_interventions = max_interventions

    # ------------------------------------------------------------------ public

    def run(self, cap: Capability, inputs: dict[str, str], *, base_url: str,
            allow_draft: bool = False) -> ReplayResult:
        self.cap = cap
        self.redactor = Redactor(self.policy, "logs")
        self.log = RunLog(self.runs_dir, "replay", cap.id, self.redactor)
        self.inputs = dict(inputs)
        self.outputs: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
        self.side_effects_done = False
        self.steps: list[StepResult] = []
        self.current: StepResult | None = None
        self.last_obs: Observation | None = None
        self.interventions: list[dict] = []
        self.control = Control(on_change=lambda rec: self.log.event("control_transferred", **rec))
        self.surface.control = self.control
        started, t0 = utcnow(), time.monotonic()
        for name, spec in cap.inputs.items():
            if spec.sensitive and name in inputs:
                self.redactor.add_known(str(inputs[name]), f"input:{name}")
        self.log.event("replay_started", capability=cap.id, version=cap.version, content_hash=cap.content_hash,
                       status=cap.status, allow_draft=allow_draft, base_url=base_url,
                       inputs={k: v for k, v in inputs.items()})
        try:
            try:
                self._check_approval(allow_draft)
                self._check_inputs()
                self.surface.start(base_url.rstrip("/") + cap.app.entry_path)
                self._run_steps()
                status, outcome, error, escalate = "success", None, None, False
            except _Stop as stop:
                status, outcome, error, escalate = stop.status, stop.outcome, stop.error, stop.escalate
                if (escalate or status == "needs_approval") and self.handoff is None:
                    # Unattended: route the intervention request to the caller with the result.
                    request = self._intervention_request(stop, self._current_step())
                    self.log.write_json("intervention.json", request)
                    self.interventions.append({"request_id": request.id, "trigger": request.trigger,
                                               "reason": request.reason, "step": request.step,
                                               "decision": "returned_to_caller"})
            except ActionError as exc:
                status, outcome, escalate = "failed", None, True
                error = self._error("surface_error", str(exc), step=self.current.id if self.current else None)
        except Exception as exc:  # a bug, not an app condition: record it, then let it surface
            self.log.event("crash", error=repr(exc))
            self.log.close()
            self.surface.close()
            raise
        result = ReplayResult(
            status=status, capability=cap.id, version=cap.version, content_hash=cap.content_hash,
            outcome=outcome, outputs=self.outputs if status == "success" else {}, error=error, escalate=escalate,
            low_confidence_steps=[s.id for s in self.steps if s.confidence == "low"],
            interventions=self.interventions, control_history=self.control.history,
            steps=self.steps, run_id=self.log.run_id, started_at=started,
            duration_ms=int((time.monotonic() - t0) * 1000))
        self.log.write_json("result.json", result)
        self.log.event("replay_finished", status=status, outcome=outcome, error=error, escalate=escalate,
                       duration_ms=result.duration_ms)
        self.log.close()
        self.surface.close()
        return result

    # ------------------------------------------------------------------ preconditions

    def _check_approval(self, allow_draft: bool) -> None:
        if self.cap.status == "approved":
            return
        if self.cap.status == "draft" and allow_draft:
            self.log.event("supervised_run", reason="draft capability replayed with --allow-draft")
            return
        raise _Stop("failed", error=self._error(
            "not_approved", f"capability is '{self.cap.status}'; unattended replay requires 'approved'",
            snapshot=False))

    def _check_inputs(self) -> None:
        problems = []
        if unknown := set(self.inputs) - set(self.cap.inputs):
            problems.append(f"unknown inputs {sorted(unknown)}")
        for name, spec in self.cap.inputs.items():
            if name not in self.inputs:
                if spec.required:
                    problems.append(f"missing required input '{name}'")
                continue
            value = str(self.inputs[name])
            try:
                if spec.type in ("integer", "decimal", "date", "boolean"):
                    parse_value(spec.type, value)
            except (ValueError, InvalidOperation):
                problems.append(f"input '{name}' is not a valid {spec.type}")
            if spec.enum and value not in spec.enum:
                problems.append(f"input '{name}' must be one of {spec.enum}")
            if spec.pattern and not re.fullmatch(spec.pattern, value):
                problems.append(f"input '{name}' does not match {spec.pattern}")
        if problems:
            raise _Stop("failed", error=self._error("invalid_input", "; ".join(problems), snapshot=False))

    # ------------------------------------------------------------------ steps & handoff

    def _run_steps(self) -> None:
        """Run every step, then the success check. With a handoff configured, a stop that a
        human can resolve pauses here instead of ending the run, on the same live session."""
        steps = self.cap.steps
        self.index = 0
        approvals: dict[int, str] = {}
        while True:
            try:
                if self.index < len(steps):
                    self._run_step(steps[self.index], approved_by=approvals.pop(self.index, None))
                    self.index += 1
                    continue
                self._check_success()
                return
            except _Stop as stop:
                if not self._can_hand_off(stop):
                    raise
                step = self._current_step()
                outcome = self._hand_off(stop, step)
                if outcome.decision == "approved":
                    approvals[self.index] = outcome.operator
                elif outcome.decision == "resumed":
                    if stop.status == "needs_approval":
                        self.side_effects_done = True       # the human may have performed it themselves
                    self.index = self._resync(self.index)
                elif outcome.decision == "done":
                    self.index = self._collect_after_human(self.index)
                else:
                    kind = {"rejected": "operator_rejected", "aborted": "operator_aborted",
                            "timeout": "escalation_timeout"}[outcome.decision]
                    raise _Stop("failed", outcome=f"operator_{outcome.decision}", escalate=False, error=self._error(
                        kind, f"intervention {outcome.request_id}: {outcome.decision}"
                              + (f" by {outcome.operator}" if outcome.operator else "")
                              + (f" ({outcome.note})" if outcome.note else ""),
                        step=step.id if step else None, snapshot=False))

    HANDOFFABLE = {"element_not_found", "ambiguous_element", "unexpected_screen", "recovery_exhausted",
                   "outcome_failure", "action_failed", "unsafe_to_retry", "success_check_failed"}

    def _can_hand_off(self, stop: _Stop) -> bool:
        """Business outcomes are answers, not problems; pre-flight refusals have no session."""
        if self.handoff is None or len(self.interventions) >= self.max_interventions:
            return False
        if stop.status == "needs_approval":
            return True
        return stop.status == "failed" and stop.error is not None and stop.error.kind in self.HANDOFFABLE

    def _current_step(self) -> Step | None:
        idx = getattr(self, "index", 0)
        return self.cap.steps[idx] if idx < len(self.cap.steps) else None

    def _intervention_request(self, stop: _Stop, step: Step | None) -> InterventionRequest:
        approval = stop.status == "needs_approval"
        obs = self.last_obs
        shot = None
        if obs is not None:
            path = self.log.path(f"intervention-{len(self.interventions) + 1}.jpg")
            try:
                self.surface.screenshot(str(path), self.redactor.js_patterns, mask_data=True)
                shot = str(path.resolve())
            except Exception:  # noqa: BLE001 - evidence is best effort
                pass
        reason = (stop.error.message if stop.error else stop.status) + (f" [{stop.outcome}]" if stop.outcome else "")
        return InterventionRequest(
            run_id=self.log.run_id, run_kind="replay", capability=f"{self.cap.id}@{self.cap.version}",
            trigger="approval_needed" if approval else "unrecoverable", reason=self.redactor.text(reason),
            step=step.id if step else "success_check",
            screen={"frames": obs.location.frames if obs else {}, "headings": obs.headings() if obs else []},
            screenshot=shot,
            recent_steps=[s.model_dump(include={"id", "action", "actor", "status", "strategy"}) for s in self.steps[-3:]],
            allowed=(["approve", "reject", "take_control", "resume", "abort"] if approval
                     else ["take_control", "resume", "done", "abort"]))

    def _hand_off(self, stop: _Stop, step: Step | None) -> HandoffOutcome:
        request = self._intervention_request(stop, step)
        outcome = self.handoff.escalate(request, self.surface, self.control, self.log)
        self._record_human_actions(outcome.human_events)
        self.interventions.append({**outcome.summary(), "trigger": request.trigger, "reason": request.reason,
                                   "step": request.step})
        self.attempts.clear()            # recovery budgets restart after a human fixed things
        return outcome

    def _record_human_actions(self, events: list[dict]) -> None:
        for event in events:
            el = event.get("element") or {}
            label = el.get("caption") or el.get("name") or el.get("role") or ""
            what = {"click": f"clicked {el.get('role')} '{label}'",
                    "fill": f"typed into '{label}'" + ("" if event.get("secret") else f": {event.get('value')!r}"),
                    "select": f"selected '{event.get('value')}' in '{label}'",
                    "press": f"pressed {event.get('key')} in '{label}'",
                    "dialog": f"saw {event.get('type')} dialog '{event.get('message')}'",
                    "dialog_answer": f"{'accepted' if event.get('accepted') else 'dismissed'} dialog"}.get(
                event["kind"], event["kind"])
            where = f" ({event['frame']} {event['frame_path']})" if event.get("frame") else ""
            sr = StepResult(id=f"human_{len([s for s in self.steps if s.actor == 'human']) + 1}",
                            action=event["kind"], actor="human", status="ok", detail=what + where)
            self.steps.append(sr)
            self.log.event("human_action", step=sr.id, action=sr.action, detail=sr.detail)

    def _screen_groups(self) -> list[tuple[int, Step]]:
        """Steps grouped by the screen they act on; the first step of each group is where a
        resumed run can safely re-enter (re-filling a field on that screen is idempotent)."""
        groups: list[tuple[int, Step]] = []
        key = None
        for idx, st in enumerate(self.cap.steps):
            if st.target is None:
                continue
            k = (st.target.frame, st.target.frame_path)
            if k != key:
                groups.append((idx, st))
                key = k
        return groups

    def _resync(self, current: int) -> int:
        """After the human hands back: continue at the first step whose screen is showing."""
        obs = self._observe_handling_outcomes()
        groups = self._screen_groups()

        def showing(st: Step) -> bool:
            t = st.target
            on_screen = not frame_mismatches({t.frame: t.frame_path}, obs, self.inputs) or t.frame not in obs.location.frames
            return on_screen and resolve(t, obs, self.inputs).element is not None

        here = max((n for n, (idx, _) in enumerate(groups) if idx <= current), default=0)
        for idx, st in groups[here:]:
            if showing(st):
                self.log.event("resumed_at", step=st.id, index=idx)
                return idx
        s = self.cap.success
        if not frame_mismatches(s.frames, obs, self.inputs) and all(o in self.outputs for o in s.outputs):
            return len(self.cap.steps)
        if not self.side_effects_done:      # going back is only safe if nothing irreversible ran
            for idx, st in reversed(groups[:here]):
                if showing(st):
                    self.log.event("resumed_at", step=st.id, index=idx, direction="back")
                    return idx
        raise _Stop("failed", escalate=True, error=self._error(
            "resume_failed", "after the human handed back control, the screen matches no remaining step",
            observed="; ".join(f"{k}={v}" for k, v in obs.location.frames.items())))

    def _collect_after_human(self, current: int) -> int:
        """The operator says they finished the task: read any outputs still owed, then let the
        success checkpoint decide."""
        obs = self._observe_handling_outcomes()
        for st in self.cap.steps[current:]:
            if st.action == "extract" and st.output not in self.outputs:
                res = resolve(st.target, obs, self.inputs)
                if res.element is not None:
                    self._extract(st, res.element.ref)
        return len(self.cap.steps)

    def _run_step(self, step: Step, approved_by: str | None = None) -> None:
        t0 = time.monotonic()
        sr = StepResult(id=step.id, action=step.action, approved_by=approved_by)
        self.steps.append(sr)
        self.current = sr
        self.log.event("step_started", step=step.id, action=step.action, approved_by=approved_by)
        if step.requires_approval and not approved_by:
            sr.status = "needs_approval"
            raise _Stop("needs_approval", escalate=True, error=self._error(
                "policy_blocked", "irreversible step requires a human decision", step=step.id))
        deadline = time.monotonic() + self.step_timeout
        # An approved irreversible step: the app's own "are you sure?" dialog is part of it.
        previous_policy = self.surface.dialog_policy
        if approved_by:
            self.surface.dialog_policy = lambda _t, _m: True
        try:
            if step.action == "press":
                self._observe_handling_outcomes()
                submit = self.surface.pending_submit_label() if step.key == "Enter" else None
                self._enforce_policy(step, None, approved=bool(approved_by), key=step.key, submit_label=submit)
                sr.attempts = 1
                self._perform(step, lambda: self.surface.press(step.key))
            else:
                self._act_on_target(step, sr, deadline, approved=bool(approved_by))
        finally:
            self.surface.dialog_policy = previous_policy

        if step.expect:
            expect = step.expect.frames
            ok = self._wait(lambda o: not frame_mismatches(expect, o, self.inputs) or None,
                            time.monotonic() + self.step_timeout)
            if ok is None:
                raise _Stop("failed", error=self._error(
                    "unexpected_screen", "the step did not lead to the expected screen", step=step.id,
                    expected=", ".join(f"{k}={render(v, self.inputs)}" for k, v in expect.items()),
                    observed="; ".join(frame_mismatches(expect, self.last_obs, self.inputs))))
        sr.status = "ok"
        sr.duration_ms = int((time.monotonic() - t0) * 1000)
        self.log.event("step_ok", step=step.id, strategy=sr.strategy, confidence=sr.confidence,
                       attempts=sr.attempts, recovered=sr.recovered, dialogs=sr.dialogs, duration_ms=sr.duration_ms)
        self.echo(f"  [ok] {step.id:<28} via {sr.strategy or '-':<11} ({sr.confidence or '-'})"
                  f"{'  recovered: ' + ', '.join(sr.recovered) if sr.recovered else ''}")

    def _act_on_target(self, step: Step, sr: StepResult, deadline: float, approved: bool = False) -> None:
        target = step.target
        while True:
            def ready(obs: Observation):
                if frame_mismatches({target.frame: target.frame_path}, obs, self.inputs) \
                        and target.frame in obs.location.frames:
                    return None
                res = resolve(target, obs, self.inputs)
                return res if res.element else None

            res = self._wait(ready, deadline)
            if res is None:
                self._fail_unresolved(step)
            sr.strategy, sr.confidence = res.strategy, res.confidence
            sr.tried = [f"{by}:{n}" for by, n in res.tried]
            sr.attempts += 1
            el = res.element
            self._enforce_policy(step, el, approved=approved)
            try:
                if step.action == "click":
                    self._perform(step, lambda: self.surface.click(el.ref), raise_transient=True)
                elif step.action == "fill":
                    value = render(step.value, self.inputs)
                    self._perform(step, lambda: self.surface.fill(el.ref, value), raise_transient=True)
                elif step.action == "fill_secret":
                    value = self._secret(step)
                    self._perform(step, lambda: self.surface.fill(el.ref, value), raise_transient=True)
                elif step.action == "select":
                    value = render(step.value, self.inputs)
                    self._perform(step, lambda: self.surface.select(el.ref, value), raise_transient=True)
                elif step.action == "extract":
                    self._extract(step, el.ref)
                return
            except ActionError as exc:
                if exc.kind not in TRANSIENT or time.monotonic() > deadline:
                    raise _Stop("failed", error=self._error("action_failed", f"{exc.kind}: {exc}", step=step.id))
                self.log.event("transient_action_error", step=step.id, kind=exc.kind)

    def _perform(self, step: Step, fn: Callable[[], None], raise_transient: bool = False) -> None:
        try:
            fn()
        except ActionError as exc:
            if raise_transient and exc.kind in TRANSIENT:
                raise
            raise _Stop("failed", error=self._error("action_failed", f"{exc.kind}: {exc}", step=step.id))
        if step.requires_approval:
            self.side_effects_done = True

    def _extract(self, step: Step, ref: str) -> None:
        raw = self.surface.read(ref)
        spec = self.cap.outputs[step.output]
        if spec.sensitive:
            self.redactor.add_known(raw, f"output:{step.output}")
        try:
            value = parse_value(spec.type, raw)
        except (ValueError, InvalidOperation):
            raise _Stop("failed", error=self._error(
                "output_parse_error", f"could not read '{step.output}' as {spec.type}", step=step.id,
                expected=spec.type, observed=raw))
        if spec.sensitive:
            self.redactor.add_known(value, f"output:{step.output}")
        self.outputs[step.output] = value

    def _secret(self, step: Step) -> str:
        ref = self.policy.secrets.get(step.secret)
        value = os.environ.get(ref.env) if ref else None
        if not value:
            raise _Stop("failed", error=self._error(
                "action_failed", f"secret '{step.secret}' is not configured", step=step.id, snapshot=False))
        self.redactor.add_known(value, f"secret:{step.secret}")
        return value

    def _enforce_policy(self, step: Step, element, approved: bool = False, **kw) -> None:
        decision = self.policy.check_action(POLICY_ACTION[step.action], element, **kw)
        if decision.verdict == "needs_approval" and approved:
            # A named operator approved exactly this step; "block" verdicts are never overridable.
            self.log.event("approved_action", step=step.id, reason=decision.reason,
                           approved_by=self.current.approved_by if self.current else None)
            return
        if decision.verdict != "allow":
            # An approved artifact can't sneak past the policy: irreversible controls not marked
            # requires_approval, or actions/keys outside the allowlist, stop here.
            raise _Stop("failed", escalate=True, error=self._error(
                "policy_blocked", decision.reason, step=step.id))

    def _check_success(self) -> None:
        s = self.cap.success
        focus = next((st.target.frame for st in reversed(self.cap.steps) if st.target), None)

        def ok(obs: Observation):
            return not frame_mismatches(s.frames, obs, self.inputs) and not missing_texts(s.texts, obs, focus) or None

        if self._wait(ok, time.monotonic() + self.step_timeout) is None:
            obs = self.last_obs
            observed = frame_mismatches(s.frames, obs, self.inputs) + \
                [f"text '{t}' not shown" for t in missing_texts(s.texts, obs, focus)]
            raise _Stop("failed", error=self._error(
                "success_check_failed", "final screen does not match the success checkpoint",
                expected=f"frames {s.frames}, texts {s.texts}", observed="; ".join(observed)))
        if missing := [o for o in s.outputs if o not in self.outputs]:
            raise _Stop("failed", error=self._error("success_check_failed", f"outputs not extracted: {missing}"))

    # ------------------------------------------------------------------ waiting & outcomes

    def _wait(self, predicate: Callable[[Observation], T | None], deadline: float) -> T | None:
        """Observe until predicate returns a value or the deadline passes. Outcome rules are
        applied on every observation, so a notice is handled and an error page stops us."""
        while True:
            obs = self._observe_handling_outcomes()
            value = predicate(obs)
            if value is not None:
                return value
            if time.monotonic() > deadline:
                return None
            time.sleep(self.poll_interval)

    def _observe_handling_outcomes(self) -> Observation:
        while True:
            obs = self.surface.observe()
            self.last_obs = obs
            if self.current is not None and obs.dialogs:
                self.current.dialogs.extend(obs.dialogs)
            if not self._apply_outcome_rules(obs):
                return obs

    def _apply_outcome_rules(self, obs: Observation) -> bool:
        """True if a recoverable condition was handled (caller re-observes)."""
        step = self.current.id if self.current else None
        for rule in self.cap.outcomes:
            hit = match_rule(rule, obs)
            if hit is None:
                continue
            self.log.event("outcome_detected", code=rule.code, kind=rule.kind, step=step, frame=hit.frame)
            if rule.kind == "business":
                raise _Stop("business_outcome", outcome=rule.code)
            if rule.kind == "failure":
                raise _Stop("failed", outcome=rule.code, escalate=rule.escalate, error=self._error(
                    "outcome_failure", rule.description, step=step, observed=hit.name))
            recover = rule.recover
            n = self.attempts[rule.code] = self.attempts.get(rule.code, 0) + 1
            if n > recover.max_attempts:
                raise _Stop("failed", outcome=rule.code, escalate=True, error=self._error(
                    "recovery_exhausted", f"'{rule.code}' persisted after {recover.max_attempts} recoveries",
                    step=step, observed=hit.name))
            if recover.only_if_no_side_effects and self.side_effects_done:
                raise _Stop("failed", outcome=rule.code, escalate=True, error=self._error(
                    "unsafe_to_retry", f"'{rule.code}' after an irreversible step; not retrying", step=step))
            try:
                if recover.action == "click":
                    res = resolve(Target(frame=hit.frame, frame_path="*", locators=[recover.target]), obs, self.inputs)
                    if res.element is None:
                        raise ActionError("not_found", f"recovery control for '{rule.code}' not found")
                    self.surface.click(res.element.ref)
                else:
                    self.surface.reload(hit.frame)
            except ActionError as exc:
                raise _Stop("failed", outcome=rule.code, escalate=True, error=self._error(
                    "recovery_exhausted", f"could not recover from '{rule.code}': {exc}", step=step))
            if self.current is not None:
                self.current.recovered.append(rule.code)
            self.log.event("recovered", code=rule.code, action=recover.action, attempt=n, step=step)
            return True
        return False

    # ------------------------------------------------------------------ failures

    def _fail_unresolved(self, step: Step) -> None:
        obs, target = self.last_obs, step.target
        mismatch = frame_mismatches({target.frame: target.frame_path}, obs, self.inputs)
        if mismatch and target.frame in obs.location.frames:
            raise _Stop("failed", error=self._error(
                "unexpected_screen", "the target's screen never appeared", step=step.id,
                expected=f"{target.frame}={render(target.frame_path, self.inputs)}", observed="; ".join(mismatch)))
        res = resolve(target, obs, self.inputs)
        kind = "ambiguous_element" if res.ambiguous else "element_not_found"
        raise _Stop("failed", error=self._error(
            kind, "no locator strategy matched exactly one element", step=step.id,
            expected=", ".join(loc.by for loc in target.locators),
            observed=", ".join(f"{by} matched {n}" for by, n in res.tried)))

    def _error(self, kind: str, message: str, *, step: str | None = None, expected: str | None = None,
               observed: str | None = None, snapshot: bool = True) -> ReplayError:
        shot = snap = None
        if snapshot and self.last_obs is not None:
            name = f"failure-{step or 'run'}"
            try:
                self.surface.screenshot(str(self.log.path(f"{name}.jpg")), self.redactor.js_patterns, mask_data=True)
                shot = f"{name}.jpg"
            except Exception:  # noqa: BLE001 - evidence is best effort, never masks the real error
                pass
            snapshot = render_observation(self.last_obs.without_data(), self.redactor)
            self.log.path(f"{name}.txt").write_text(snapshot, encoding="utf-8")
            snap = f"{name}.txt"
        error = ReplayError(kind=kind, message=message, step=step, expected=expected, observed=observed,
                            screenshot=shot, snapshot=snap)
        self.log.event("replay_error", **error.model_dump())
        return error
