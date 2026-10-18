"""A stand-in for the LLM: plays a fixed script of tool calls, choosing refs from the live
observation. Lets the discovery loop, policy, redaction and evidence be tested end to end
against the real app and browser without API calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from cua.surface.base import Observation


@dataclass
class Block:
    type: str
    id: str | None = None
    name: str | None = None
    input: dict | None = None
    text: str | None = None
    thinking: str | None = None


@dataclass
class Response:
    content: list[Block]
    stop_reason: str = "tool_use"
    model: str = "scripted"
    usage: Any = None


Step = Callable[[Observation], tuple[str, dict]]


@dataclass
class ScriptedModel:
    script: list[Step]
    name: str = "scripted"
    agent: Any = None
    calls: int = field(default=0)

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Response:
        if self.calls >= len(self.script):
            return Response([Block("text", text="(script exhausted)")], stop_reason="end_turn")
        tool, args = self.script[self.calls](self.agent.current_observation)
        self.calls += 1
        return Response([Block("tool_use", id=f"tu_{self.calls}", name=tool, input=args)])


def ref(obs: Observation, **match: Any) -> str:
    """First element whose attributes equal all of `match` (e.g. role='textbox', caption='Member #:')."""
    for e in obs.elements:
        if all(getattr(e, k) == v for k, v in match.items()):
            return e.ref
    raise AssertionError(f"no element matching {match} in observation #{obs.seq}")
