"""Deterministic locator resolution and screen checks, in plain Python over an Observation.

Nothing here touches a browser: it matches recorded locators against the surface's element
list. So replay targeting works unchanged on any Surface that produces Elements (web today,
a desktop UI Automation or terminal surface later).

Rule: a strategy is only trusted if it matches exactly ONE element. Zero means "try the
next strategy"; more than one means "ambiguous, try the next strategy" - never guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Callable

from ..artifact.schema import LOCATOR_CONFIDENCE, PLACEHOLDER, OutcomeRule, Target
from ..surface.base import Element, Observation


def norm(text: str | None) -> str:
    """Compare like a human reads: case, spacing and a trailing colon don't matter."""
    return " ".join((text or "").split()).casefold().rstrip(":").strip()


def render(text: str | None, inputs: dict[str, str]) -> str | None:
    if text is None:
        return None
    return PLACEHOLDER.sub(lambda m: inputs[m.group(1)], text)


@dataclass
class Resolution:
    element: Element | None
    strategy: str | None = None
    confidence: str | None = None
    tried: list[tuple[str, int]] = field(default_factory=list)   # (strategy, number of matches)

    @property
    def ambiguous(self) -> bool:
        return any(n > 1 for _, n in self.tried)


def _pool(obs: Observation, frame: str) -> list[Element]:
    """Elements of the target's frame. If the app has no such frame (another tenant may not
    use frames at all), search everywhere rather than fail."""
    if frame in obs.location.frames:
        return [e for e in obs.elements if e.frame == frame]
    return list(obs.elements)


def _iou(a: list[float], b: list[float]) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    w, h = min(ax2, bx2) - max(a[0], b[0]), min(ay2, by2) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def _matches(loc, e: Element, inputs: dict[str, str]) -> bool:
    by = loc.by
    if by == "role":
        return (e.kind == "control" and e.role == loc.role and norm(e.name) == norm(render(loc.name, inputs))
                and (loc.row_key is None or norm(e.row_key) == norm(render(loc.row_key, inputs))))
    if by == "caption":
        return e.role == loc.role and norm(e.caption) == norm(loc.caption)
    if by == "table_cell":
        return (e.kind == "text" and norm(e.col_header) == norm(loc.column)
                and norm(e.row_key) == norm(render(loc.row_key, inputs)))
    if by == "row_control":
        return e.kind == "control" and e.role == loc.role and norm(e.row_key) == norm(render(loc.row_key, inputs))
    if by == "field_name":
        return e.tag == loc.tag and e.field_name == loc.field_name
    if by == "href":
        return e.href == render(loc.href, inputs)
    if by == "css":
        return e.css == loc.css
    return False


def resolve(target: Target, obs: Observation, inputs: dict[str, str],
            form_of: Callable[[str, Observation], str | None] | None = None) -> Resolution:
    """form_of(step_id, obs) -> the form identity of the field that step uses, needed by
    form_submit locators; without it they are skipped."""
    pool = _pool(obs, target.frame)
    tried: list[tuple[str, int]] = []
    for loc in target.locators:
        if loc.by == "position":
            hits = [e for e in pool if e.bbox and _iou(e.bbox, loc.bbox) >= 0.5]
        elif loc.by == "form_submit":
            form = form_of(loc.of_step, obs) if form_of else None
            hits = [e for e in pool if form and e.kind == "control" and e.is_submit and e.form == form]
        else:
            hits = [e for e in pool if _matches(loc, e, inputs)]
        tried.append((loc.by, len(hits)))
        if len(hits) == 1:
            return Resolution(hits[0], loc.by, LOCATOR_CONFIDENCE[loc.by], tried)
    return Resolution(None, tried=tried)


def frame_mismatches(expected: dict[str, str], obs: Observation, inputs: dict[str, str]) -> list[str]:
    """Human-readable list of frames not showing the expected document (empty = all good)."""
    out = []
    for name, path in expected.items():
        want, have = render(path, inputs), obs.location.frames.get(name)
        if have != want:
            out.append(f"frame '{name}' expected {want}, showing {have or '(absent)'}")
    return out


def missing_texts(texts: list[str], obs: Observation, frame: str | None = None) -> list[str]:
    shown = [norm(e.name) for e in obs.elements if frame is None or e.frame == frame]
    return [t for t in texts if not any(norm(t) in s for s in shown)]


def match_rule(rule: OutcomeRule, obs: Observation) -> Element | None:
    """The element whose text triggers the rule on this screen, if any."""
    if rule.screens and not any(fnmatch(p, g) for p in obs.location.frames.values() for g in rule.screens):
        return None
    for e in obs.elements:
        if rule.match.frame and e.frame != rule.match.frame:
            continue
        if rule.match.text is not None and rule.match.text.casefold() in e.name.casefold():
            return e
        if rule.match.regex is not None and re.search(rule.match.regex, e.name):
            return e
    return None
