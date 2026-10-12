"""Fault injection for the mock target app.

The real environment's interesting failures are runtime conditions, not layout
drift (brief §3.3). This config lets a demo or test switch each one on
deterministically, either at startup (CLI flags) or live via /__control/faults.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass
class FaultConfig:
    # Probability [0, 1] that a work page shows a blocking "System Notice" overlay
    # that must be acknowledged before anything underneath can be clicked.
    interstitial_rate: float = 0.0
    # Fixed latency added to every work page (simulates a slow core system).
    delay_ms: int = 0
    # Probability [0, 1] that a work page returns an "Application Error" page (HTTP 500).
    error_rate: float = 0.0
    # Idle seconds before the session expires and pages show "Session Expired".
    session_ttl_s: int = 900
    # Native JS confirm() dialog when posting a new share (an "unexpected dialog").
    confirm_dialog: bool = True

    def update(self, changes: dict) -> None:
        known = {f.name: f.type for f in fields(self)}
        unknown = set(changes) - set(known)
        if unknown:
            raise ValueError(f"unknown fault keys: {sorted(unknown)}")
        for key, value in changes.items():
            current = getattr(self, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
                if not 0.0 <= value <= 1.0:
                    raise ValueError(f"{key} must be between 0 and 1")
            setattr(self, key, value)

    def to_dict(self) -> dict:
        return asdict(self)
