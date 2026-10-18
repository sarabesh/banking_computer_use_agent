"""Who is in control of the live session.

    AUTOMATION --escalate--> AWAITING_HUMAN --take_control--> HUMAN --resume--> AUTOMATION
                                   |  approve -----------------------------> AUTOMATION
                                   +--reject / abort / timeout--> FINISHED <-- done / abort

Enforced, not just reported: the surface refuses every automation action unless the
state is AUTOMATION, so automation and a human can never act on the screen at once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..evidence import utcnow


class ControlState(str, Enum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"
    FINISHED = "finished"


ALLOWED = {
    ControlState.AUTOMATION: {ControlState.AWAITING_HUMAN, ControlState.FINISHED},
    ControlState.AWAITING_HUMAN: {ControlState.HUMAN, ControlState.AUTOMATION, ControlState.FINISHED},
    ControlState.HUMAN: {ControlState.AUTOMATION, ControlState.FINISHED},
    ControlState.FINISHED: set(),
}


class ControlError(Exception):
    pass


@dataclass
class Control:
    on_change: Callable[[dict], None] | None = None
    state: ControlState = ControlState.AUTOMATION
    holder: str = "automation"
    history: list[dict] = field(default_factory=list)

    def transition(self, to: ControlState, *, by: str, reason: str) -> None:
        if to not in ALLOWED[self.state]:
            raise ControlError(f"cannot go from {self.state.value} to {to.value}")
        record = {"from": self.state.value, "to": to.value, "by": by, "reason": reason, "at": utcnow()}
        self.state = to
        self.holder = by if to == ControlState.HUMAN else ("automation" if to == ControlState.AUTOMATION else "-")
        self.history.append(record)
        if self.on_change:
            self.on_change(record)

    @property
    def automation_may_act(self) -> bool:
        return self.state == ControlState.AUTOMATION
