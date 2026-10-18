"""The replay result contract: what a calling agent gets back.

Three things are kept strictly apart (the brief's most common design mistake is to
conflate them):
  success           the flow completed and the checkpoint held; outputs are returned
  business_outcome  a legitimate answer the caller must handle (e.g. record_not_found)
  failed            something went wrong; `error` says which step, what was expected,
                    what was observed, and where the evidence is
plus needs_approval: the flow reached an irreversible step and stopped for a human.
Recoverable conditions never surface as a status: they are handled and listed per step.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ErrorKind = Literal[
    "not_approved", "invalid_input",                  # refused before touching the app
    "element_not_found", "ambiguous_element", "unexpected_screen", "action_failed",
    "policy_blocked", "output_parse_error", "success_check_failed",
    "outcome_failure", "recovery_exhausted", "unsafe_to_retry", "surface_error",
    "operator_rejected", "operator_aborted", "escalation_timeout", "resume_failed",   # human handoff
]


class StepResult(BaseModel):
    id: str
    action: str
    actor: Literal["automation", "human"] = "automation"
    status: Literal["ok", "failed", "needs_approval"] = "failed"
    detail: str | None = None
    approved_by: str | None = None       # operator who approved an irreversible step
    strategy: str | None = None          # which locator matched
    confidence: str | None = None
    tried: list[str] = Field(default_factory=list)    # e.g. ["caption:0", "field_name:1"]
    attempts: int = 0
    recovered: list[str] = Field(default_factory=list)   # recoverable outcomes handled on this step
    dialogs: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class ReplayError(BaseModel):
    kind: ErrorKind
    message: str
    step: str | None = None
    expected: str | None = None
    observed: str | None = None
    screenshot: str | None = None
    snapshot: str | None = None          # redacted element list of the screen at failure


class ReplayResult(BaseModel):
    status: Literal["success", "business_outcome", "failed", "needs_approval"]
    capability: str
    version: str
    content_hash: str | None = None
    outcome: str | None = None           # business or failure code from the outcome rules
    outputs: dict[str, str] = Field(default_factory=dict)   # typed values, serialised as strings
    error: ReplayError | None = None
    escalate: bool = False               # a human should look at this
    low_confidence_steps: list[str] = Field(default_factory=list)
    interventions: list[dict] = Field(default_factory=list)   # every handoff: trigger, decision, operator
    control_history: list[dict] = Field(default_factory=list)  # who held the session, when, and why
    steps: list[StepResult] = Field(default_factory=list)
    run_id: str
    started_at: str
    duration_ms: int = 0
