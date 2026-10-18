"""Tools the discovery agent can call. Each requires a `reason`, which becomes the "why"
column of the evidence log. Inputs are validated with the Pydantic models below before
anything touches the application."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

SNAKE = r"^[a-z][a-z0-9_]{0,39}$"

REF = {"type": "string", "description": "Element ref from the LATEST observation, e.g. 'e12'."}
REASON = {"type": "string", "description": "One sentence: why this action, in terms of the goal."}
PARAMETER = {
    "type": "object",
    "description": "Set when the value comes from the goal (it varies per invocation). Omit for fixed values.",
    "properties": {
        "name": {"type": "string", "description": "snake_case parameter name, e.g. member_id"},
        "type": {"type": "string", "enum": ["string", "integer", "decimal", "date", "enum"]},
        "description": {"type": "string"},
        "sensitive": {"type": "boolean", "description": "True for PII or confidential values (never logged)."},
    },
    "required": ["name", "type", "description"],
    "additionalProperties": False,
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict: #to be used by the LLM to know what tools it can call and how to call them
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required,
                         "additionalProperties": False},
    }


def tool_definitions(secret_names: list[str]) -> list[dict]: #to be used by the LLM to know what tools it can call and how to call them
    return [
        _tool("click", "Click a link, button or other clickable element.",
              {"ref": REF, "reason": REASON}, ["ref", "reason"]),
        _tool("type_text", "Replace the contents of a text field with the given text.",
              {"ref": REF, "text": {"type": "string"}, "parameter": PARAMETER, "reason": REASON},
              ["ref", "text", "reason"]),
        _tool("type_secret", "Fill a field with a named credential. You never see the value.",
              {"ref": REF, "secret": {"type": "string", "enum": secret_names}, "reason": REASON},
              ["ref", "secret", "reason"]),
        _tool("select_option", "Choose an option in a drop-down list by its visible label.",
              {"ref": REF, "option": {"type": "string"}, "parameter": PARAMETER, "reason": REASON},
              ["ref", "option", "reason"]),
        _tool("press_key", "Press a key in the focused element (Enter submits the focused form).",
              {"key": {"type": "string", "enum": ["Enter", "Tab", "Escape"]}, "reason": REASON},
              ["key", "reason"]),
        _tool("wait", "Wait for a slow screen to finish loading, then observe again.",
              {"seconds": {"type": "number", "minimum": 0.5, "maximum": 10}, "reason": REASON},
              ["seconds", "reason"]),
        _tool("extract", "Capture data the goal asks for. The system reads the value from the element itself.",
              {"ref": REF,
               "name": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
               "type": {"type": "string", "enum": ["string", "integer", "decimal", "date", "boolean"]},
               "description": {"type": "string"},
               "sensitive": {"type": "boolean", "description": "True for PII/financial data (default true)."},
               "reason": REASON},
              ["ref", "name", "type", "description", "reason"]),
        _tool("request_human", "Stop and ask a human operator to intervene.",
              {"kind": {"type": "string", "enum": ["stuck", "approval_needed", "unclear_goal", "unexpected_state"]},
               "reason": REASON},
              ["kind", "reason"]),
        _tool("finish", "End the run. Use status 'success' only when the goal is reached and outputs extracted.",
              {"status": {"type": "string", "enum": ["success", "failed"]},
               "summary": {"type": "string"},
               "success_evidence": {"type": "string",
                                    "description": "Required for status 'success': what on the current screen "
                                                   "proves the goal was reached."},
               "business_outcome": {"type": "string",
                                    "description": "For a legitimate negative answer, a snake_case code, e.g. member_not_found."}},
              ["status", "summary"]),
    ]


# Pydantic models for validating tool inputs. The LLM is expected to produce JSON that matches these models.

class ParameterSpec(BaseModel):
    name: str = Field(pattern=SNAKE)
    type: Literal["string", "integer", "decimal", "date", "enum"]
    description: str
    sensitive: bool = False


class ClickIn(BaseModel):
    ref: str
    reason: str


class TypeTextIn(BaseModel):
    ref: str
    text: str
    reason: str
    parameter: ParameterSpec | None = None


class TypeSecretIn(BaseModel):
    ref: str
    secret: str
    reason: str


class SelectOptionIn(BaseModel):
    ref: str
    option: str
    reason: str
    parameter: ParameterSpec | None = None


class PressKeyIn(BaseModel):
    key: Literal["Enter", "Tab", "Escape"]
    reason: str


class WaitIn(BaseModel):
    seconds: float = Field(ge=0.5, le=10)
    reason: str


class ExtractIn(BaseModel):
    ref: str
    name: str = Field(pattern=SNAKE)
    type: Literal["string", "integer", "decimal", "date", "boolean"]
    description: str
    reason: str
    sensitive: bool = True


class RequestHumanIn(BaseModel):
    kind: Literal["stuck", "approval_needed", "unclear_goal", "unexpected_state"]
    reason: str


class FinishIn(BaseModel):
    status: Literal["success", "failed"]
    summary: str
    success_evidence: str | None = None
    business_outcome: str | None = Field(default=None, pattern=SNAKE)

    @model_validator(mode="after")
    def _success_needs_evidence(self) -> "FinishIn":
        # The evidence becomes the replay's success checkpoint description; a success claim
        # without it would compile into a capability whose checkpoint nobody can review.
        if self.status == "success" and not (self.success_evidence or "").strip():
            raise ValueError("finish with status 'success' requires success_evidence: "
                             "describe what on the current screen proves the goal was reached")
        return self


TOOL_INPUTS: dict[str, type[BaseModel]] = {
    "click": ClickIn, "type_text": TypeTextIn, "type_secret": TypeSecretIn, "select_option": SelectOptionIn,
    "press_key": PressKeyIn, "wait": WaitIn, "extract": ExtractIn, "request_human": RequestHumanIn,
    "finish": FinishIn,
}
