"""Allowlist + risk classification. Enforced in code around every action; the LLM only
ever sees the verdicts."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .surface.base import Element

Verdict = Literal["allow", "block", "needs_approval"]


class Decision(BaseModel):
    verdict: Verdict
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class SecretRef(BaseModel):
    env: str
    description: str


class RedactionRule(BaseModel):
    name: str
    pattern: str
    channels: list[Literal["model", "logs"]]
    # Optional check on each match to cut false positives (e.g. timestamps that look like cards).
    validate_: Literal["luhn"] | None = Field(default=None, alias="validate")


class Policy(BaseModel):
    allowed_origins: list[str]
    blocked_path_prefixes: list[str] = Field(default_factory=list)
    allowed_actions: list[str]
    allowed_keys: list[str] = Field(default_factory=list)
    irreversible_controls: list[str] = Field(default_factory=list)
    secrets: dict[str, SecretRef] = Field(default_factory=dict)
    redaction: list[RedactionRule] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def check_url(self, url: str) -> Decision:
        parts = urlsplit(url)
        if parts.scheme in ("data", "blob", "about"):
            return Decision(verdict="allow", reason="inline resource")
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.allowed_origins:
            return Decision(verdict="block", reason=f"origin {origin} is not on the allowlist")
        for prefix in self.blocked_path_prefixes:
            if parts.path.startswith(prefix):
                return Decision(verdict="block", reason=f"path {parts.path} is blocked by policy")
        return Decision(verdict="allow", reason="allowlisted origin")

    def is_irreversible_label(self, label: str | None) -> bool:
        if not label:
            return False
        text = label.strip()
        return any(re.fullmatch(p, text, flags=re.IGNORECASE) for p in self.irreversible_controls)

    def check_action(self, action: str, element: "Element | None" = None, *,
                     key: str | None = None, submit_label: str | None = None) -> Decision:
        if action not in self.allowed_actions:
            return Decision(verdict="block", reason=f"action type '{action}' is not allowed")
        if action == "press_key":
            if key not in self.allowed_keys:
                return Decision(verdict="block", reason=f"key '{key}' is not allowed")
            if key == "Enter" and self.is_irreversible_label(submit_label):
                return Decision(verdict="needs_approval",
                                reason=f"Enter would submit via irreversible control '{submit_label}'")
        if action == "click" and element is not None:
            if self.is_irreversible_label(element.name):
                return Decision(verdict="needs_approval",
                                reason=f"'{element.name}' is classified irreversible")
        return Decision(verdict="allow", reason="permitted by policy")
