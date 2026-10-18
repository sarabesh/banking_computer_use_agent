"""Model client used by the discovery agent (the only place the LLM is called).

Replay never imports this module: that is the structural guarantee that production
execution is deterministic.
"""
from __future__ import annotations

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicModel:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium", max_tokens: int = 16000):
        self.name = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(max_retries=4)

    def respond(self, system: str, tools: list[dict], messages: list[dict]):
        return self.client.beta.messages.create(
            model=self.name,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            # One action per turn: every action gets a fresh observation, and the trace is linear.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            # The conversation only grows (append-only), so caching the prefix is cheap and safe.
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines a turn, the API retries it on a fallback model.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
