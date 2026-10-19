"""The capability artifact: a typed, versioned, reviewable description of a recorded flow.

It is the contract between discovery (which produces it), a human reviewer (who approves
it) and replay / calling agents (which execute it). It is deliberately independent of the
model transcript and of any automation library: targets are described the way an operator
would describe them, with ordered fallbacks.

Templates: "{{name}}" anywhere in a step value, target or expectation refers to an input.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1"
SNAKE = r"^[a-z][a-z0-9_]{0,39}$"
SEMVER = r"^\d+\.\d+\.\d+$"
PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

Confidence = Literal["high", "medium", "low"]
ValueType = Literal["string", "integer", "decimal", "date", "boolean", "enum"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")   # typos in a hand-edited artifact fail loudly


# ---------------------------------------------------------------- locators
# Ordered most -> least robust inside a target. Replay takes the first strategy that
# matches exactly ONE element, and reports which one (and its confidence) it used.

class RoleLocator(_Model):
    """A control by its role and visible name: button 'Go'. Optionally inside a table row."""
    by: Literal["role"] = "role"
    role: str
    name: str
    row_key: str | None = None


class CaptionLocator(_Model):
    """A field (or key/value cell) by the visible label beside it: textbox 'Member #:'."""
    by: Literal["caption"] = "caption"
    role: str
    caption: str


class TableCellLocator(_Model):
    """A data cell by column header and row key. Never by its current value."""
    by: Literal["table_cell"] = "table_cell"
    column: str
    row_key: str


class RowControlLocator(_Model):
    """Relational, label-independent: the only control of this role in the table row whose
    key is row_key ("the link in the {{member_number}} row"), whatever its text says."""
    by: Literal["row_control"] = "row_control"
    role: str
    row_key: str


class FormSubmitLocator(_Model):
    """Relational, label-independent: the submit control of the form that holds the field
    used by step `of_step` ("the button that submits the search form"), whatever its text."""
    by: Literal["form_submit"] = "form_submit"
    of_step: str


class FieldNameLocator(_Model):
    """The form field's name attribute: stable in server-rendered apps, meaningless to humans."""
    by: Literal["field_name"] = "field_name"
    tag: str
    field_name: str


class HrefLocator(_Model):
    by: Literal["href"] = "href"
    href: str


class CssLocator(_Model):
    """Structural path: breaks on any layout change. Last structural resort."""
    by: Literal["css"] = "css"
    css: str


class PositionLocator(_Model):
    """Screen position at discovery time (x, y, w, h): the vision-style fallback."""
    by: Literal["position"] = "position"
    bbox: list[int] = Field(min_length=4, max_length=4)


Locator = Annotated[Union[RoleLocator, CaptionLocator, TableCellLocator, RowControlLocator, FormSubmitLocator,
                          FieldNameLocator, HrefLocator, CssLocator, PositionLocator], Field(discriminator="by")]

# Confidence is a property of the strategy, not of one instance of it.
LOCATOR_CONFIDENCE: dict[str, Confidence] = {
    "role": "high", "caption": "high", "table_cell": "high", "row_control": "high", "form_submit": "high",
    "field_name": "medium", "href": "medium",
    "css": "low", "position": "low",
}


class Target(_Model):
    frame: str                      # frame/window the control lives in ("main" = top level)
    frame_path: str                 # document the frame shows when the step runs (template)
    locators: list[Locator] = Field(min_length=1)


# ---------------------------------------------------------------- steps

class Expect(_Model):
    """Postcondition checked after the step: which document each frame must show."""
    frames: dict[str, str]


StepAction = Literal["fill", "fill_secret", "click", "select", "press", "extract"]


class Step(_Model):
    id: str = Field(pattern=SNAKE)
    action: StepAction
    why: str
    target: Target | None = None
    value: str | None = None        # literal or "{{input}}" (fill / select)
    secret: str | None = None       # logical secret name (fill_secret)
    key: str | None = None          # press
    output: str | None = None       # extract
    expect: Expect | None = None
    requires_approval: bool = False  # irreversible: replay pauses for a human decision

    @model_validator(mode="after")
    def _fields_match_action(self) -> "Step":
        needs = {"fill": ("target", "value"), "fill_secret": ("target", "secret"), "click": ("target",),
                 "select": ("target", "value"), "press": ("key",), "extract": ("target", "output")}
        missing = [f for f in needs[self.action] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"step '{self.id}' ({self.action}) is missing {missing}")
        return self


# ---------------------------------------------------------------- contract

class InputSpec(_Model):
    type: ValueType
    description: str
    required: bool = True
    sensitive: bool = False
    pattern: str | None = None      # optional caller-side validation, added by a reviewer
    enum: list[str] | None = None
    example: str | None = None      # never present for sensitive inputs


class OutputSpec(_Model):
    type: ValueType
    description: str
    sensitive: bool = True


# ---------------------------------------------------------------- outcomes
# The error taxonomy. "Business" = a legitimate answer the caller must handle (no such
# member). "Recoverable" = replay handles it and continues (dismiss a notice, retry a
# transient error). "Failure" = stop with a debuggable error, possibly escalate.

class Match(_Model):
    text: str | None = None
    regex: str | None = None
    frame: str | None = None        # None: any frame

    @model_validator(mode="after")
    def _one_condition(self) -> "Match":
        if (self.text is None) == (self.regex is None):
            raise ValueError("match needs exactly one of text / regex")
        return self


class Recovery(_Model):
    action: Literal["click", "reload"]
    target: RoleLocator | None = None
    max_attempts: int = Field(default=2, ge=1, le=5)
    # Retrying after an error is only safe if no irreversible step may have run.
    only_if_no_side_effects: bool = False


class OutcomeRule(_Model):
    code: str = Field(pattern=SNAKE)
    kind: Literal["business", "recoverable", "failure"]
    description: str
    match: Match
    recover: Recovery | None = None
    escalate: bool = False          # failure that a human should look at, not just the caller
    screens: list[str] = Field(default_factory=list)   # glob paths where it applies; empty = all

    @model_validator(mode="after")
    def _recovery_only_when_recoverable(self) -> "OutcomeRule":
        if (self.kind == "recoverable") != (self.recover is not None):
            raise ValueError(f"outcome '{self.code}': recover is required for, and only for, recoverable rules")
        return self


class Success(_Model):
    """The checkpoint. Structural and required: every frame shows its expected document, every
    output was extracted, and no outcome rule fired. `texts` are any-of corroborating
    evidence: if none is shown (e.g. another tenant titles the screen differently) the run
    still succeeds, with a warning, so drift degrades gracefully instead of failing."""
    frames: dict[str, str]
    texts: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    description: str | None = None


# ---------------------------------------------------------------- the capability

class AppRef(_Model):
    product: str
    profile: str                    # app profile the outcome rules came from
    versions: str | None = None
    entry_path: str                 # base URL is supplied per tenant at run time


class Provenance(_Model):
    discovered_by: str
    run_id: str
    discovered_at: str
    discovered_at_tenant: str | None = None     # the institution whose system the flow was recorded on
    goal: str
    trace_sha256: str | None = None
    compiler_version: str


class Capability(_Model):
    schema_version: Literal["1"] = SCHEMA_VERSION
    id: str = Field(pattern=SNAKE)
    version: str = Field(pattern=SEMVER)
    status: Literal["draft", "approved", "deprecated"] = "draft"
    title: str
    description: str
    app: AppRef
    side_effects: Literal["none", "writes"]
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    outcomes: list[OutcomeRule] = Field(default_factory=list)
    success: Success
    provenance: Provenance
    content_hash: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Capability":
        problems: list[str] = []
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            problems.append("step ids must be unique")
        for n, step in enumerate(self.steps):
            for loc in (step.target.locators if step.target else []):
                if loc.by == "form_submit" and loc.of_step not in ids[:n]:
                    problems.append(f"step '{step.id}': form_submit refers to '{loc.of_step}', not an earlier step")
        used = set(PLACEHOLDER.findall(json.dumps(self.model_dump(mode="json", exclude={"provenance"}))))
        if undeclared := used - set(self.inputs):
            problems.append(f"templates reference undeclared inputs {sorted(undeclared)}")
        if unused := set(self.inputs) - used:
            problems.append(f"inputs never used by any step {sorted(unused)}")
        extracted = {s.output for s in self.steps if s.action == "extract"}
        if extracted != set(self.outputs):
            problems.append(f"declared outputs {sorted(self.outputs)} != extracted {sorted(extracted)}")
        if missing := set(self.success.outputs) - set(self.outputs):
            problems.append(f"success requires unknown outputs {sorted(missing)}")
        secrets_used = {s.secret for s in self.steps if s.secret}
        if secrets_used != set(self.secrets):
            problems.append(f"declared secrets {sorted(self.secrets)} != used {sorted(secrets_used)}")
        if self.side_effects == "none" and any(s.requires_approval for s in self.steps):
            problems.append("a capability with approval-gated steps has side effects")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------ identity & storage

    def compute_hash(self) -> str:
        """Hash of what the capability *does*. Excludes status/version/provenance, so approving
        it or bumping the version doesn't change it, but any change to steps or contract does."""
        body = self.model_dump(mode="json", exclude={"content_hash", "status", "version", "provenance"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def to_yaml(self) -> str:
        data = self.model_copy(update={"content_hash": self.compute_hash()}).model_dump(mode="json", exclude_none=True)
        header = (f"# Capability '{self.id}' v{self.version} - generated by `cua compile`.\n"
                  "# Review before setting status: approved. Unattended replay requires approval.\n")
        # Inline short lists/maps, never wrap long strings: keeps reviews and git diffs readable.
        return header + yaml.safe_dump(_prune(data), sort_keys=False, allow_unicode=True,
                                       width=10_000, default_flow_style=None)

    def save(self, root: str | Path) -> Path:
        path = Path(root) / self.id / f"{self.version}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_yaml(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Capability":
        cap = cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
        if cap.content_hash and cap.content_hash != cap.compute_hash():
            raise ValueError(f"{path}: content changed since it was hashed; recompile or bump the version")
        return cap

    def invocation_schema(self) -> dict:
        """The capability as a callable tool for an AI agent: name, description, typed inputs."""
        kinds = {"string": "string", "integer": "integer", "decimal": "string", "date": "string",
                 "boolean": "boolean", "enum": "string"}
        props = {}
        for name, spec in self.inputs.items():
            prop: dict = {"type": kinds[spec.type], "description": spec.description}
            if spec.enum:
                prop["enum"] = spec.enum
            if spec.pattern:
                prop["pattern"] = spec.pattern
            props[name] = prop
        return {"name": self.id, "description": self.description,
                "input_schema": {"type": "object", "properties": props, "additionalProperties": False,
                                 "required": [n for n, s in self.inputs.items() if s.required]}}


# Flags that are noise at their default value. Omitting them is lossless: loading restores
# the default, and the content hash is computed on the full model, not on the YAML text.
_QUIET_DEFAULTS = {"requires_approval": False, "escalate": False, "screens": [],
                   "only_if_no_side_effects": False, "required": True, "outcomes": []}


def _prune(value):
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if not (k in _QUIET_DEFAULTS and v == _QUIET_DEFAULTS[k])}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value
