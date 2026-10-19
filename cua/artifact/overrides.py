"""Per-tenant overrides: specialise HOW a capability finds things at one institution, never
WHAT it does.

The base capability is recorded once and made as label-independent as possible (relational
locators, structural success check). An override holds only what genuinely differs at one
tenant running the same vendor product - typically a few labels - so a change for one
institution never touches the artifact every other institution runs.

Rules enforced by apply_override():
  - locators may be ADDED to existing steps (tried first; base locators stay as fallback)
  - success texts may be replaced; outcome rules may be ADDED (never removed or renamed)
  - a step may be made stricter (requires_approval: true), never looser
  - no steps added, removed or reordered; no action, value, secret, input or output changes
  - the override is pinned to the exact base it was reviewed against (base_hash); a
    recompiled base refuses old overrides until they are re-reviewed (drift management)
  - unattended replay needs BOTH the base and the override approved
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from .schema import SNAKE, Capability, Locator, OutcomeRule, _Model, _prune


class OverrideError(ValueError):
    pass


class StepOverride(_Model):
    locators: list[Locator] = Field(default_factory=list)
    requires_approval: Literal[True] | None = None     # only ever stricter


class SuccessOverride(_Model):
    texts: list[str]


class OverrideProvenance(_Model):
    created_at: str
    suggested_from_run: str | None = None
    notes: list[str] = Field(default_factory=list)


class TenantOverride(_Model):
    override_version: Literal["1"] = "1"
    capability: str = Field(pattern=SNAKE)
    tenant: str = Field(pattern=SNAKE)
    base_version: str
    base_hash: str
    status: Literal["draft", "approved", "deprecated"] = "draft"
    description: str | None = None
    steps: dict[str, StepOverride] = Field(default_factory=dict)
    success: SuccessOverride | None = None
    outcomes: list[OutcomeRule] = Field(default_factory=list)     # additions only
    provenance: OverrideProvenance
    content_hash: str | None = None

    def compute_hash(self) -> str:
        body = self.model_dump(mode="json", exclude={"content_hash", "status", "provenance"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def path(self, root: str | Path) -> Path:
        return Path(root) / self.capability / f"{self.tenant}.yaml"

    def save(self, root: str | Path) -> Path:
        path = self.path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_copy(update={"content_hash": self.compute_hash()}).model_dump(mode="json", exclude_none=True)
        header = (f"# Tenant override: '{self.capability}' at tenant '{self.tenant}'.\n"
                  "# Only how things are found here differs from the base. Review, then set status: approved.\n")
        path.write_text(header + yaml.safe_dump(_prune(data), sort_keys=False, allow_unicode=True,
                                                width=10_000, default_flow_style=None), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "TenantOverride":
        ov = cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
        if ov.content_hash and ov.content_hash != ov.compute_hash():
            raise OverrideError(f"{path}: content changed since it was hashed; re-review it")
        return ov


def find_override(root: str | Path, capability_id: str, tenant: str) -> Path | None:
    path = Path(root) / capability_id / f"{tenant}.yaml"
    return path if path.exists() else None


def apply_override(base: Capability, ov: TenantOverride) -> Capability:
    """The effective capability for one tenant. Raises OverrideError on any rule violation."""
    if ov.capability != base.id:
        raise OverrideError(f"override is for '{ov.capability}', not '{base.id}'")
    base_hash = base.content_hash or base.compute_hash()
    if ov.base_hash != base_hash:
        raise OverrideError(f"override for '{ov.tenant}' was reviewed against base {ov.base_version} "
                            f"({ov.base_hash[:19]}...), not this base {base.version} ({base_hash[:19]}...); "
                            "re-run `cua override suggest` and re-review")
    steps = {s.id: s for s in base.steps}
    if unknown := set(ov.steps) - set(steps):
        raise OverrideError(f"override names steps the capability does not have: {sorted(unknown)}")
    base_codes = {r.code for r in base.outcomes}
    if clash := {r.code for r in ov.outcomes} & base_codes:
        raise OverrideError(f"override may add outcome rules, not redefine base ones: {sorted(clash)}")

    data = base.model_dump(mode="json")
    for step in data["steps"]:
        change = ov.steps.get(step["id"])
        if change is None:
            continue
        if change.locators:
            if step.get("target") is None:
                raise OverrideError(f"step '{step['id']}' has no target to add locators to")
            added = [loc.model_dump(mode="json") for loc in change.locators]
            step["target"]["locators"] = added + [loc for loc in step["target"]["locators"] if loc not in added]
        if change.requires_approval:
            step["requires_approval"] = True
    if ov.success is not None:
        data["success"]["texts"] = ov.success.texts
    data["outcomes"] = [r.model_dump(mode="json") for r in ov.outcomes] + data["outcomes"]
    statuses = {base.status, ov.status}
    data["status"] = "deprecated" if "deprecated" in statuses else ("approved" if statuses == {"approved"} else "draft")
    if any(s.get("requires_approval") for s in data["steps"]):
        data["side_effects"] = "writes"
    data["content_hash"] = None
    return Capability.model_validate(data)
