"""The operator inbox: how an intervention request reaches a human and how they answer.

A directory shared by the automation process and the operator CLI (a separate process):
    <id>.request.json     the request, with the context the operator needs
    <id>.status.json      live state written by the automation: who is in control, dialogs
    <id>.responses.jsonl  the operator's commands, appended in order
In production this seam becomes a ticket queue / operator console; the request, status
and response contracts stay the same.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..evidence import utcnow

Trigger = Literal["stuck", "unrecoverable", "approval_needed"]
OperatorAction = Literal["take_control", "resume", "done", "approve", "reject", "abort",
                         "dialog_accept", "dialog_dismiss"]


class InterventionRequest(BaseModel):
    id: str = Field(default_factory=lambda: f"int-{uuid.uuid4().hex[:8]}")
    created_at: str = Field(default_factory=utcnow)
    run_id: str
    run_kind: Literal["discovery", "replay"]
    capability: str | None = None
    goal: str | None = None
    trigger: Trigger
    reason: str
    step: str | None = None
    screen: dict = Field(default_factory=dict)        # frames + headings at the moment it stopped
    screenshot: str | None = None                     # redacted image, relative to the runs folder (the inbox's parent)
    recent_steps: list[dict] = Field(default_factory=list)
    allowed: list[OperatorAction]


class InterventionResponse(BaseModel):
    request_id: str
    action: OperatorAction
    operator: str
    at: str = Field(default_factory=utcnow)
    note: str | None = None
    dialog_id: int | None = None


class Inbox:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, request_id: str, suffix: str) -> Path:
        return self.root / f"{request_id}.{suffix}"

    # automation side
    def post(self, request: InterventionRequest) -> Path:
        path = self._path(request.id, "request.json")
        path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
        return path

    def set_status(self, request_id: str, **status) -> None:
        self._path(request_id, "status.json").write_text(
            json.dumps({"request_id": request_id, "updated_at": utcnow(), **status}, indent=2), encoding="utf-8")

    def responses(self, request_id: str, start: int = 0) -> list[InterventionResponse]:
        path = self._path(request_id, "responses.jsonl")
        if not path.exists():
            return []
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [InterventionResponse.model_validate_json(ln) for ln in lines[start:]]

    # operator side
    def respond(self, response: InterventionResponse) -> None:
        with self._path(response.request_id, "responses.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(response.model_dump_json() + "\n")

    def request(self, request_id: str) -> InterventionRequest:
        return InterventionRequest.model_validate_json(self._path(request_id, "request.json").read_text(encoding="utf-8"))

    def status(self, request_id: str) -> dict:
        path = self._path(request_id, "status.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def open_requests(self) -> list[InterventionRequest]:
        out = []
        for path in sorted(self.root.glob("*.request.json")):
            req = InterventionRequest.model_validate_json(path.read_text(encoding="utf-8"))
            if self.status(req.id).get("control") != "finished" and not self.status(req.id).get("closed"):
                out.append(req)
        return out
