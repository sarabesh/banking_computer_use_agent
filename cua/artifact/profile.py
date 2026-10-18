"""App profile: what a vendor product's runtime conditions look like.

Written once per product by a human, shared by every capability and every tenant that runs
the product. The compiler copies the rules that apply to the screens a flow visits into the
capability, so each capability stays self-contained and reviewable.
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import yaml
from pydantic import Field

from .schema import OutcomeRule, _Model


class AppProfile(_Model):
    id: str
    product: str
    versions: str | None = None
    outcomes: list[OutcomeRule] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "AppProfile":
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def rules_for(self, visited_paths: set[str]) -> list[OutcomeRule]:
        """Rules with no screen restriction, plus those whose screens the flow visits."""
        return [r for r in self.outcomes
                if not r.screens or any(fnmatch(p, g) for p in visited_paths for g in r.screens)]
