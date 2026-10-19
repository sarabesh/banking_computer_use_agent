"""Compile a successful discovery trace into a capability artifact.

What the compiler decides (and the trace alone does not say):
  - which recorded steps belong to the flow: only steps that succeeded; failed attempts,
    policy refusals and discovery-time waits are left out (replay has its own waiting)
  - how each target is located on replay: an ordered locator bundle, most robust first,
    never using an element's current *value* and never a redacted string
  - canonicalisation: concrete input values become templates, so /w/member/12345 becomes
    /w/member/{{member_number}} and the "Select" link is the one in the {{member_number}} row
  - postconditions: every step that changed screens gets an `expect` for the new screen
  - the success checkpoint, and which app-profile outcome rules apply to the screens visited
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..discovery.trace import DiscoveryTrace, ParamDecl, TargetFingerprint, TraceStep
from ..policy import Policy
from .profile import AppProfile
from .schema import (LOCATOR_CONFIDENCE, AppRef, CaptionLocator, Capability, CssLocator, Expect,
                     FieldNameLocator, FormSubmitLocator, HrefLocator, InputSpec, OutputSpec, PositionLocator,
                     Provenance, RoleLocator, RowControlLocator, Step, Success, TableCellLocator, Target)

COMPILER_VERSION = "0.2.0"     # 0.2.0: relational locators (row_control, form_submit)
PLACEHOLDER_STRIP = re.compile(r"\{\{[^}]*\}\}")
FIELD_ROLES = {"textbox", "combobox", "checkbox", "radio"}
ACTIONS = {"type_text": "fill", "type_secret": "fill_secret", "click": "click", "select_option": "select",
           "press_key": "press", "extract": "extract"}


class CompileError(Exception):
    pass


@dataclass
class CompileReport:
    capability: Capability
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _usable(text: str | None) -> bool:
    return bool(text) and "[REDACTED" not in text


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "step"


class _Canon:
    """Replaces concrete example values of (non-sensitive) inputs with {{name}} templates."""

    def __init__(self, params: list[ParamDecl]):
        self.values = {p.example: p.name for p in params if p.example and not p.sensitive and len(p.example) >= 2}

    def value(self, text: str | None) -> str | None:
        if text is None:
            return None
        return f"{{{{{self.values[text]}}}}}" if text in self.values else text

    def path(self, path: str) -> str:
        return "/".join(self.value(seg) or "" for seg in path.split("/"))

    def text(self, text: str) -> str:
        for example, name in self.values.items():
            text = re.sub(rf"\b{re.escape(example)}\b", f"{{{{{name}}}}}", text)
        return text


def _locators(t: TargetFingerprint, canon: _Canon) -> list:
    """Ordered most to least robust. Each strategy must later match exactly one element."""
    locs: list = []
    is_data = t.kind == "text"
    if is_data and t.col_header and t.row_key and _usable(t.row_key):
        locs.append(TableCellLocator(column=t.col_header, row_key=canon.value(t.row_key)))
    if t.caption and _usable(t.caption) and (t.role in FIELD_ROLES or is_data):
        locs.append(CaptionLocator(role=t.role, caption=t.caption))
    in_table_row = bool(t.row_key) and t.row_key not in (t.name, t.caption) and _usable(t.row_key)
    if not is_data and _usable(t.name):
        locs.append(RoleLocator(role=t.role, name=t.name, row_key=canon.value(t.row_key) if in_table_row else None))
    if not is_data and in_table_row and t.role in ("link", "button"):
        # Label-independent twin of the role locator: survives the link being renamed.
        locs.append(RowControlLocator(role=t.role, row_key=canon.value(t.row_key)))
    if t.field_name:
        locs.append(FieldNameLocator(tag=t.tag, field_name=t.field_name))
    if t.href and not urlsplit(t.href).scheme:
        locs.append(HrefLocator(href=canon.path(t.href)))
    if t.css:
        locs.append(CssLocator(css=t.css))
    if t.bbox:
        locs.append(PositionLocator(bbox=[round(v) for v in t.bbox]))
    return locs


STRUCTURAL = ("field_name", "href", "css", "position")


def _add_relational(locs: list, loc) -> None:
    """Relational locators go after the semantic ones and before the structural fallbacks."""
    at = next((i for i, existing in enumerate(locs) if existing.by in STRUCTURAL), len(locs))
    locs.insert(at, loc)


def _is_submit(t: TargetFingerprint) -> bool:
    # Traces recorded before perception reported is_submit fall back to the control's type.
    return t.is_submit or t.input_type in ("submit", "image") or (t.tag == "button" and t.role == "button")


def _recovery_rule(step: TraceStep, recoveries: list) -> str | None:
    """The recoverable outcome rule whose recovery control this click hit, if any."""
    if step.action != "click" or step.target is None:
        return None
    name = " ".join(step.target.name.split()).casefold()
    for rule in recoveries:
        target = rule.recover.target
        if step.target.role == target.role and name == " ".join(target.name.split()).casefold():
            return rule.code
    return None


def _changed_frames(step: TraceStep, canon: _Canon) -> dict[str, str]:
    if not step.after or not step.screen_changed:
        return {}
    before = step.before.frames
    return {name: canon.path(path) for name, path in step.after.frames.items() if before.get(name) != path}


def compile_trace(trace: DiscoveryTrace, profile: AppProfile, *, capability_id: str | None = None,
                  version: str = "0.1.0", policy: Policy | None = None,
                  trace_sha256: str | None = None) -> CompileReport:
    if trace.status != "success":
        raise CompileError(f"only successful discovery runs can be compiled (this run: {trace.status})")
    if trace.final is None:
        raise CompileError("trace has no final state to build a success checkpoint from")

    canon = _Canon(trace.parameters)
    recoveries = [r for r in profile.outcomes
                  if r.kind == "recoverable" and r.recover.action == "click" and r.recover.target is not None]
    warnings: list[str] = []
    skipped: list[str] = []
    steps: list[Step] = []
    visited: set[str] = set()
    used_ids: set[str] = set()
    fields_on_screen: list[tuple[str, TargetFingerprint]] = []   # (step id, target) of filled fields

    for s in trace.steps:
        visited.update(s.before.frames.values())
        if s.after:
            visited.update(s.after.frames.values())
        if s.status != "ok" or s.action not in ACTIONS:
            skipped.append(f"#{s.index} {s.action} ({s.status}{': ' + s.detail if s.detail else ''})")
            continue
        action = ACTIONS[s.action]
        recovery = _recovery_rule(s, recoveries)
        if recovery is not None:
            # e.g. the agent acknowledged a maintenance notice: a runtime condition, not part of
            # the procedure. Replay handles it through the outcome rule whenever it appears.
            skipped.append(f"#{s.index} click '{s.target.name}': a recovery action, handled by outcome rule "
                           f"'{recovery}' at replay")
            continue
        if action in ("fill", "select") and not (s.parameter or s.literal):
            # e.g. an operator typed a credential during a handoff: value deliberately not captured
            skipped.append(f"#{s.index} {s.action} by {s.actor} ({s.detail or 'no value recorded'})")
            warnings.append(f"step #{s.index} was done by a human without a recordable value; review the flow")
            continue
        target = None
        if s.target is not None:
            locs = _locators(s.target, canon)
            if not locs:
                raise CompileError(f"step #{s.index}: no usable locator for its target")
            if all(LOCATOR_CONFIDENCE[loc.by] == "low" for loc in locs):
                warnings.append(f"step #{s.index} can only be located structurally/positionally (low confidence)")
            target = Target(frame=s.target.frame, frame_path=canon.path(s.target.frame_path), locators=locs)

        label = s.parameter or s.output or s.secret or (s.target and (s.target.caption or s.target.name)) or s.key or action
        base = f"{'fill' if action == 'fill_secret' else action}_{_slug(label)}"[:40].rstrip("_")
        step_id, n = base, 2
        while step_id in used_ids:
            step_id, n = f"{base}_{n}", n + 1
        used_ids.add(step_id)

        if target is not None and action == "click" and _is_submit(s.target):
            # "The button that submits the form whose field step X filled": same frame, same
            # screen and, when perception knows it, the same form.
            field = next((sid for sid, ft in reversed(fields_on_screen)
                          if ft.frame == s.target.frame and ft.frame_path == s.target.frame_path
                          and (not s.target.form or not ft.form or ft.form == s.target.form)), None)
            if field:
                _add_relational(target.locators, FormSubmitLocator(of_step=field))
        if target is not None and action in ("fill", "fill_secret", "select"):
            fields_on_screen.append((step_id, s.target))

        value = f"{{{{{s.parameter}}}}}" if s.parameter else s.literal
        changed = _changed_frames(s, canon)
        irreversible = bool(policy and s.target and policy.is_irreversible_label(s.target.name))
        steps.append(Step(id=step_id, action=action, why=canon.text(s.reason), target=target,
                          value=value if action in ("fill", "select") else None,
                          secret=s.secret, key=s.key, output=s.output,
                          expect=Expect(frames=changed) if changed else None,
                          requires_approval=irreversible))

    inputs = {p.name: InputSpec(type=p.type, description=p.description, sensitive=p.sensitive,
                                example=p.example, enum=p.enum_values) for p in trace.parameters}
    for name, spec in inputs.items():
        if spec.pattern is None and spec.enum is None:
            warnings.append(f"input '{name}' has no validation pattern; a reviewer may add one")
    outputs = {o.name: OutputSpec(type=o.type, description=o.description, sensitive=o.sensitive)
               for o in trace.outputs}

    final = trace.final
    last_target = next((s.target for s in reversed(steps) if s.target), None)
    focus = last_target.frame if last_target else "main"
    texts = [h.split(": ", 1)[1] for h in final.headings if h.startswith(f"{focus}: ")]
    texts = [canon.text(t) for t in texts if _usable(t)][:2]
    if not texts:
        warnings.append("success checkpoint has no screen text; it relies on frame paths only")
    success = Success(frames={k: canon.path(v) for k, v in final.location.frames.items()},
                      texts=texts, outputs=list(outputs),
                      description=canon.text(final.success_evidence) if final.success_evidence else None)

    goal = canon.text(trace.goal)
    cap = Capability(
        id=capability_id or _slug(PLACEHOLDER_STRIP.sub("", goal))[:40].strip("_"),
        version=version,
        title=goal,
        description=f"{goal}. Recorded by an LLM-driven discovery run and compiled for deterministic replay.",
        app=AppRef(product=profile.product, profile=profile.id, versions=profile.versions,
                   entry_path=urlsplit(trace.start_url).path or "/"),
        side_effects="writes" if any(s.requires_approval for s in steps) else "none",
        inputs=inputs, outputs=outputs, secrets=sorted({s.secret for s in steps if s.secret}),
        steps=steps, outcomes=profile.rules_for(visited), success=success,
        provenance=Provenance(discovered_by=trace.model, run_id=trace.run_id, discovered_at=trace.started_at,
                              discovered_at_tenant=trace.tenant,
                              goal=goal, trace_sha256=trace_sha256, compiler_version=COMPILER_VERSION),
    )
    return CompileReport(capability=cap, warnings=warnings, skipped=skipped)
