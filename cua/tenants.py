"""Tenants: which institution's system is on the screen.

The URL only says where to open the application. Which institution it is gets decided purely
from what its first screen SHOWS - the institution's name - so the registry holds no URLs at
all: URLs move (ports get swapped, hosts change); an institution's name on its own screens
does not.

  - the screen shows a registered institution  -> that tenant
  - the screen shows no registered institution -> a new tenant is registered from what it shows
    (auto_registered, for a human to review), so the next run matches it
  - nothing identifiable on screen            -> no tenant: the base capability, with a warning

Internal only: no model ever sees the registry or decides anything based on it. It runs before
anything is typed, so a tenant's override - and in production its credentials - only ever go
to the institution actually on the screen. Which hosts may be opened at all is still limited
by policy.yaml's allowlist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .surface.base import Observation

HEADER = """\
# Tenant registry (internal configuration - never shown to any model). No URLs: which
# institution a run is talking to is decided from the application's first screen, by the
# entry whose `identity` (default: `institution`) is shown there. When an institution is seen
# for the first time it is added automatically, marked `auto_registered: true` for a human to
# review (name, id). Credentials are resolved per run from the policy's logical secrets (a
# per-tenant vault path in production; one shared .env for the local mock).
"""


NAME_WEIGHT, TITLE_WEIGHT, TEXT_WEIGHT = 5.0, 2.0, 1.0
MATCH_THRESHOLD, MIN_MARGIN = 0.75, 0.10


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class Signature:
    """What an institution's first screen looks like, structure only (never data values): its
    page title and its short texts - headings, field labels, button names, product line."""
    title: str = ""
    texts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Tenant:
    id: str
    institution: str
    identity: str = ""              # text its own screens show; defaults to the institution name
    auto_registered: bool = False   # added by the system on first sight; review it
    signature: Signature | None = None

    @property
    def identity_text(self) -> str:
        return self.identity or self.institution


def signature_of(obs: "Observation") -> Signature:
    structure = obs.without_data()
    texts = {_norm(e.name) for e in structure.elements
             if e.frame == "main" and 2 <= len(e.name.strip()) <= 60 and "‹value›" not in e.name}
    return Signature(_norm(obs.location.title or ""), frozenset(texts))


def score(tenant: Tenant, screen: Signature, shown_text: str) -> tuple[float, list[str], list[str]]:
    """Weighted share of the tenant's signature present on this screen, plus what is missing
    from / new on the screen compared with it. The institution's name weighs most: tenants of
    one vendor product share most of their screens; the name is what actually differs."""
    name = _norm(tenant.identity_text)
    has_name = name in shown_text
    if tenant.signature is None:
        return (1.0 if has_name else 0.0), [], []
    sig = tenant.signature
    own_texts = {t for t in sig.texts if name not in t}            # the name is scored once, above
    total = NAME_WEIGHT + (TITLE_WEIGHT if sig.title else 0) + TEXT_WEIGHT * len(own_texts)
    got = (NAME_WEIGHT if has_name else 0) + (TITLE_WEIGHT if sig.title and sig.title == screen.title else 0)
    got += TEXT_WEIGHT * len(own_texts & screen.texts)
    screen_texts = {t for t in screen.texts if name not in t}
    missing = sorted(own_texts - screen.texts) + (["(title) " + sig.title] if sig.title and sig.title != screen.title else [])
    new = sorted(screen_texts - own_texts)
    return got / total, missing, new


@dataclass
class Identification:
    tenant: Tenant | None
    how: str                        # matched | registered | unidentified
    note: str
    changed_registry: bool = False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "tenant"


def guess_identity(obs: "Observation") -> str | None:
    """The institution's name as its first screen shows it: the TOP-MOST short text of the main
    frame (it sits in the header), skipping texts with digits ('CoreLine Teller 7.4.2' is the
    product and its version, not the owner) and field labels ('User ID:')."""
    texts = sorted((e for e in obs.elements if e.kind == "text" and e.frame == "main" and e.bbox),
                   key=lambda e: (e.bbox[1], e.bbox[0]))
    for e in texts:
        name = e.name.strip()
        if 3 <= len(name) <= 60 and not any(ch.isdigit() for ch in name) and not name.endswith(":"):
            return name
    return None


@dataclass
class TenantRegistry:
    tenants: dict[str, Tenant] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "TenantRegistry":
        path = Path(path)
        raw = {}
        if path.exists():
            raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("tenants", {}) or {}
        tenants = {}
        for tid, t in raw.items():
            sig = t.get("signature")
            signature = Signature(_norm(sig.get("title", "")), frozenset(_norm(x) for x in sig.get("texts", []))) \
                if sig else None
            tenants[tid] = Tenant(tid, t.get("institution", tid), t.get("identity", ""),
                                  bool(t.get("auto_registered", False)), signature)
        return cls(tenants, path)

    def save(self) -> None:
        if self.path is None:
            return
        data = {"tenants": {}}
        for t in self.tenants.values():
            entry = {"institution": t.institution, "identity": t.identity or None,
                     "auto_registered": t.auto_registered or None}
            if t.signature is not None:
                entry["signature"] = {"title": t.signature.title, "texts": sorted(t.signature.texts)}
            data["tenants"][t.id] = {k: v for k, v in entry.items() if v is not None}
        self.path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=10_000),
                             encoding="utf-8")

    def identify(self, obs: "Observation") -> Identification:
        """Decide which institution this is from its first screen's signature; learn new ones."""
        screen = signature_of(obs)
        shown = _norm(" | ".join(e.name for e in obs.elements))
        ranked = sorted(((score(t, screen, shown), t) for t in self.tenants.values()),
                        key=lambda st: st[0][0], reverse=True)
        best = ranked[0] if ranked else None
        runner_up = ranked[1][0][0] if len(ranked) > 1 else 0.0
        if best and best[0][0] >= MATCH_THRESHOLD:
            (value, missing, new), tenant = best
            if value - runner_up < MIN_MARGIN:
                return Identification(None, "unidentified",
                                      f"ambiguous: screen matches '{tenant.id}' ({value:.2f}) and another tenant "
                                      f"({runner_up:.2f}) about equally; running the base capability")
            note = f"screen matches '{tenant.id}' ({tenant.institution}) signature {value:.2f}"
            changed = False
            if tenant.signature is None:             # first time seen with a signature: learn it
                tenant = replace(tenant, signature=screen)
                self.tenants[tenant.id] = tenant
                self.save()
                note += "; signature recorded"
                changed = True
            elif missing or new:                     # a drift signal - the reviewed reference is kept
                note += f"; screen differs from its signature: missing {missing}, new {new}"
            return Identification(tenant, "matched", note, changed)
        identity = guess_identity(obs)
        if identity is None:
            return Identification(None, "unidentified",
                                  "no institution name found on the first screen; running the base capability")
        tid, n = _slug(identity), 2
        while tid in self.tenants:
            tid, n = f"{_slug(identity)}_{n}", n + 1
        tenant = Tenant(tid, identity, identity, auto_registered=True, signature=screen)
        self.tenants[tid] = tenant
        self.save()
        closest = f"; closest registered was '{best[1].id}' at {best[0][0]:.2f}" if best else ""
        return Identification(tenant, "registered", f"new institution {identity!r} registered as tenant '{tid}' "
                                                    f"(auto_registered: review the entry){closest}", True)


@dataclass
class Resolved:
    tenant: str | None
    capability: object                # the effective Capability: base, or base + that tenant's override
    override_hash: str | None
    identification: Identification


class TenantResolver:
    """Used by replay on the first screen: identify the institution, then pick its override."""

    def __init__(self, registry: TenantRegistry, overrides_dir: str | Path, apply_overrides: bool = True):
        self.registry, self.overrides_dir, self.apply_overrides = registry, Path(overrides_dir), apply_overrides

    def resolve(self, base, obs: "Observation") -> Resolved:
        from .artifact import TenantOverride, apply_override, find_override
        ident = self.registry.identify(obs)
        cap, override_hash = base, None
        if ident.tenant is not None and self.apply_overrides:
            path = find_override(self.overrides_dir, base.id, ident.tenant.id)
            if path is not None:
                override = TenantOverride.load(path)
                cap, override_hash = apply_override(base, override), override.content_hash
        return Resolved(ident.tenant.id if ident.tenant else None, cap, override_hash, ident)
