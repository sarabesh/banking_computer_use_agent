"""Suggest a tenant override from a supervised replay at that tenant (no LLM).

For every step the base capability could only find through a weak fallback (field name,
CSS path, position), look at the element that actually matched and propose the semantic
locator a human would use at THIS tenant - its caption, button text or column header.
Steps already found with high confidence (semantic or relational locators) need nothing.
The result is a DRAFT for a human to review: the suggestion is only as good as the
fallback that found the element, which is exactly why approval is required.
"""
from __future__ import annotations

from pathlib import Path

from ..evidence import utcnow
from ..replay.result import ReplayResult
from .compiler import FIELD_ROLES
from .overrides import OverrideProvenance, StepOverride, SuccessOverride, TenantOverride, find_override
from .schema import CaptionLocator, Capability, RoleLocator, Step, TableCellLocator


def _semantic_from(matched: dict, step: Step):
    base = {loc.by: loc for loc in step.target.locators}
    if matched.get("kind") == "text" and matched.get("col_header") and "table_cell" in base:
        return TableCellLocator(column=matched["col_header"], row_key=base["table_cell"].row_key)
    if matched.get("caption") and (matched.get("role") in FIELD_ROLES or matched.get("kind") == "text"):
        return CaptionLocator(role=matched["role"], caption=matched["caption"])
    if matched.get("kind") == "control" and matched.get("name"):
        row_key = base["role"].row_key if "role" in base else None
        return RoleLocator(role=matched["role"], name=matched["name"], row_key=row_key)
    return None


DRIFT_ERRORS = {"element_not_found", "ambiguous_element", "success_check_failed"}


def drifted(result: ReplayResult) -> bool:
    """Did this replay show signs that the tenant's screens differ from the base?"""
    return bool(result.low_confidence_steps or result.warnings
                or (result.error is not None and result.error.kind in DRIFT_ERRORS))


def auto_suggest(base: Capability, result: ReplayResult, tenant: str, overrides_dir) -> tuple[Path | None, str]:
    """After a SUPERVISED replay at a tenant: if it drifted and the tenant has no override yet,
    write a draft one. Never overwrites an existing override and never approves anything -
    activation stays a human decision. Returns (path written or None, reason)."""
    if not drifted(result):
        return None, "no drift: the base capability works here with high confidence"
    existing = find_override(overrides_dir, base.id, tenant)
    if existing is not None:
        return None, f"drift detected but an override already exists ({existing}); " \
                     f"re-run `cua override suggest --force` to replace it with a new draft"
    return suggest_override(base, result, tenant).save(overrides_dir), "drift detected"


def suggest_override(base: Capability, result: ReplayResult, tenant: str) -> TenantOverride:
    notes: list[str] = []
    latest = {sr.id: sr for sr in result.steps if sr.actor == "automation" and sr.status == "ok"}
    changes: dict[str, StepOverride] = {}
    for step in base.steps:
        if step.target is None:
            continue
        sr = latest.get(step.id)
        if sr is None:
            if result.error and result.error.step == step.id:
                notes.append(f"{step.id}: not found at this tenant ({result.error.kind}: {result.error.observed}); "
                             "needs a human - add a locator by hand or re-run discovery here")
            continue
        if sr.confidence == "high":
            continue
        loc = _semantic_from(sr.matched or {}, step)
        if loc is None:
            notes.append(f"{step.id}: found via {sr.strategy} ({sr.confidence}); no semantic locator derivable")
            continue
        changes[step.id] = StepOverride(locators=[loc])
        notes.append(f"{step.id}: found via {sr.strategy} ({sr.confidence}) -> suggest "
                     + ", ".join(f"{k}={v}" for k, v in loc.model_dump(exclude_none=True).items()))

    success = None
    text_missing = any(w.startswith("success_text_not_shown") for w in result.warnings)
    if text_missing:
        focus = next((s.target.frame for s in reversed(base.steps) if s.target), "main")
        titles = [h.split(": ", 1)[1] for h in result.final_screen.get("headings", []) if h.startswith(f"{focus}: ")]
        if titles:
            success = SuccessOverride(texts=titles[:1])
            notes.append(f"success: the final screen is titled {titles[0]!r} here, not {base.success.texts}")
    if result.status not in ("success",):
        notes.append(f"the supervised replay ended '{result.status}'"
                     + (f" ({result.error.kind})" if result.error else "") + "; review before relying on this")

    return TenantOverride(
        capability=base.id, tenant=tenant, base_version=base.version,
        base_hash=base.content_hash or base.compute_hash(), steps=changes, success=success,
        description=f"Suggested from a supervised replay at {tenant}. Review every locator before approving.",
        provenance=OverrideProvenance(created_at=utcnow(), suggested_from_run=result.run_id, notes=notes))
