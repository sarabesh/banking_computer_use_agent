"""The discovery trace: a structured record of what the agent did, step by step.

This is the input to the artifact compiler. It is deliberately NOT the model transcript:
each step holds the action, the full fingerprint of the element it acted on (every
attribute a locator could later be built from), the parameter/secret/output bindings,
the policy verdict and the screen before/after. The transcript lives only in events.jsonl
as evidence.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..policy import Decision

ParamType = Literal["string", "integer", "decimal", "date", "enum"]
OutputType = Literal["string", "integer", "decimal", "date", "boolean"]


class TargetFingerprint(BaseModel):
    kind: str
    role: str
    name: str
    frame: str
    frame_path: str
    tag: str
    caption: str | None = None
    field_name: str | None = None
    dom_id: str | None = None
    input_type: str | None = None
    href: str | None = None
    synthetic: bool = False
    options: list[str] = Field(default_factory=list)
    selected: str | None = None
    col_header: str | None = None
    row_key: str | None = None
    css: str | None = None
    bbox: list[float] | None = None
    form: str | None = None
    is_submit: bool = False


class LocationSnapshot(BaseModel):
    url: str
    title: str
    frames: dict[str, str]


class ParamDecl(BaseModel):
    name: str
    type: ParamType
    description: str
    sensitive: bool = False
    example: str | None = None        # never kept for sensitive parameters
    enum_values: list[str] | None = None


class OutputDecl(BaseModel):
    name: str
    type: OutputType
    description: str
    sensitive: bool = True


class TraceStep(BaseModel):
    index: int
    action: Literal["click", "type_text", "type_secret", "select_option", "press_key", "wait", "extract"]
    reason: str
    actor: Literal["agent", "human"] = "agent"    # human = performed by an operator during a handoff
    target: TargetFingerprint | None = None
    parameter: str | None = None       # value supplied per invocation
    literal: str | None = None         # fixed value that is part of the procedure
    secret: str | None = None          # logical secret name, never the value
    key: str | None = None
    seconds: float | None = None
    output: str | None = None
    status: Literal["ok", "error", "blocked", "needs_approval"]
    detail: str | None = None
    policy: Decision
    observation_seq: int
    before: LocationSnapshot
    after: LocationSnapshot | None = None
    screen_changed: bool | None = None
    dialogs: list[str] = Field(default_factory=list)


class FinalState(BaseModel):
    location: LocationSnapshot
    headings: list[str]
    success_evidence: str | None = None
    business_outcome: str | None = None


class Usage(BaseModel):
    model_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class DiscoveryTrace(BaseModel):
    trace_version: Literal["1"] = "1"
    run_id: str
    goal: str
    start_url: str
    tenant: str | None = None         # derived from start_url by the registry; never shown to the model
    model: str
    started_at: str
    finished_at: str | None = None
    status: Literal["success", "failed", "escalated", "aborted"] = "aborted"
    summary: str = ""
    parameters: list[ParamDecl] = Field(default_factory=list)
    outputs: list[OutputDecl] = Field(default_factory=list)
    steps: list[TraceStep] = Field(default_factory=list)
    final: FinalState | None = None
    interventions: list[dict] = Field(default_factory=list)
    control_history: list[dict] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
