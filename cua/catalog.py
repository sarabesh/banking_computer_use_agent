"""The agent-facing capability catalog for ONE institution: saved capabilities as tools.

The bank's AI agent decides WHAT to do; it discovers capabilities here by name, reads their
typed contract, and invokes one with typed arguments. HOW it is done is deterministic replay:
no model is involved in executing the call.

The catalog is built for ONE deployment, configured with the URL of that institution's
application. Which institution it is gets decided on each call from the application's first
screen (cua.tenants), and that tenant's override is applied - never shown to the model: tools
have no tenant argument and the model gets nothing from the registry.

What the catalog guarantees to the calling agent:
  - only APPROVED capabilities are offered (latest approved version per id); if the institution
    on screen has an override, replay refuses to run it unless that override is approved too
  - the agent never supplies URLs or credentials: the URL is deployment configuration
  - a compact, typed result: success + outputs, a business outcome, or a failure reason it
    can relay; never screenshots, paths or raw page data
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact import Capability
from .artifact.schema import PLACEHOLDER
from .policy import Policy
from .replay import Replayer, ReplayResult
from .surface.web import WebSurface
from .tenants import TenantRegistry, TenantResolver


def _semver(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


@dataclass
class Entry:
    capability: Capability        # the approved base capability; the tenant's override is applied per call


class CatalogError(Exception):
    pass


class Catalog:
    def __init__(self, capabilities_dir: str | Path, overrides_dir: str | Path, tenants_file: str | Path,
                 base_url: str):
        self.base_url = base_url
        self.overrides_dir = Path(overrides_dir)
        self.tenants_file = tenants_file
        self.entries: dict[str, Entry] = {}
        self.skipped: list[str] = []
        latest: dict[str, Capability] = {}
        for path in sorted(Path(capabilities_dir).glob("*/*.yaml")):
            try:
                cap = Capability.load(path)
            except ValueError as exc:
                self.skipped.append(f"{path.name}: {exc}")
                continue
            if cap.status != "approved":
                self.skipped.append(f"{cap.id}@{cap.version}: {cap.status}")
            elif cap.id not in latest or _semver(cap.version) > _semver(latest[cap.id].version):
                latest[cap.id] = cap
        self.entries = {cap_id: Entry(cap) for cap_id, cap in latest.items()}

    def tools(self) -> list[dict]:
        """One tool per approved capability, described by its typed contract."""
        tools = []
        for entry in self.entries.values():
            cap = entry.capability
            tool = cap.invocation_schema()
            outcomes = sorted(r.code for r in cap.outcomes if r.kind == "business")
            outputs = ", ".join(f"{n} ({o.type})" for n, o in cap.outputs.items())
            title = PLACEHOLDER.sub(lambda m: f"<{m.group(1)}>", cap.title)
            tool["description"] = (f"{title}. Returns: {outputs or 'nothing'}. Possible business outcomes "
                                   f"(legitimate answers, not errors): {', '.join(outcomes) or 'none'}. "
                                   f"Side effects: {cap.side_effects}. Version {cap.version}.")
            tools.append(tool)
        return tools

    def invoke(self, name: str, args: dict[str, Any], *, policy: Policy, runs_dir: str | Path,
               headless: bool = True) -> ReplayResult:
        entry = self.entries.get(name)
        if entry is None:
            raise CatalogError(f"no approved capability named '{name}'")
        if unknown := set(args) - set(entry.capability.inputs):
            raise CatalogError(f"'{name}' does not take {sorted(unknown)}")
        resolver = TenantResolver(TenantRegistry.load(self.tenants_file), self.overrides_dir)
        replayer = Replayer(WebSurface(policy, headless=headless), policy, Path(runs_dir), echo=lambda _: None,
                            tenant_resolver=resolver)
        return replayer.run(entry.capability, {k: str(v) for k, v in args.items()}, base_url=self.base_url)


def result_for_agent(result: ReplayResult) -> dict:
    """What the calling agent sees: the contract, not the internals."""
    out: dict[str, Any] = {"status": result.status, "capability": f"{result.capability}@{result.version}",
                           "run_id": result.run_id}
    if result.status == "success":
        out["outputs"] = result.outputs
    if result.outcome:
        out["outcome"] = result.outcome
    if result.error:
        out["error"] = {"kind": result.error.kind, "message": result.error.message}
    if result.escalate:
        out["escalated_to_human"] = True
    if result.warnings or result.low_confidence_steps:
        out["quality"] = {"warnings": result.warnings, "low_confidence_steps": result.low_confidence_steps}
    return out
