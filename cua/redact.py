"""Redaction of sensitive data before it reaches the model or anything persisted.

Two mechanisms:
  - patterns from policy.yaml (SSNs, card numbers, balances...) per channel
  - known values registered at runtime (secrets, parameters marked sensitive), replaced
    wherever they appear, so a password is masked even if an app echoes it back

Limit (documented in REPORT.md): pattern redaction cannot recognise free-form PII such as
names. The structural defence is that artifacts store templates and locators, never values.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from .policy import Policy

Channel = Literal["model", "logs"]


def luhn_ok(candidate: str) -> bool:
    """Payment card numbers carry a Luhn check digit; most digit runs that look like cards don't."""
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


class Redactor:
    def __init__(self, policy: Policy, channel: Channel):
        self.channel = channel
        self.rules = [(r.name, re.compile(r.pattern), r.validate_) for r in policy.redaction if channel in r.channels]
        self.js_patterns = [r.pattern for r in policy.redaction if channel in r.channels]
        self._known: dict[str, str] = {}

    def add_known(self, value: str, label: str) -> None:
        if value and len(value) >= 3:
            self._known[value] = label

    def text(self, value: str) -> str:
        # Longest first so a secret containing another secret is fully masked.
        for known in sorted(self._known, key=len, reverse=True):
            value = value.replace(known, f"[REDACTED:{self._known[known]}]")
        for name, rx, validator in self.rules:
            def replace(m: re.Match, name=name, validator=validator) -> str:
                if validator == "luhn" and not luhn_ok(m.group(0)):
                    return m.group(0)
                return f"[REDACTED:{name}]"
            value = rx.sub(replace, value)
        return value

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value
