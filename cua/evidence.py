"""Per-run evidence: a structured, redacted event log plus screenshots.

Every run gets runs/<timestamp>-<kind>-<slug>/ containing:
  events.jsonl   one JSON object per event (what happened and why), redacted
  step-NN.jpg    screenshots with sensitive text blacked out before capture
  trace.json / result.json / intervention.json as produced by the run
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .redact import Redactor


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, runs_dir: Path, kind: str, label: str, redactor: Redactor):
        # The 'T' keeps the id from ever looking like a 13-19 digit card number to the redactor.
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "run"
        self.run_id = f"{stamp}-{kind}-{slug}"
        self.dir = Path(runs_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._fh = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, type_: str, **data: Any) -> None:
        self._seq += 1
        record = {"seq": self._seq, "ts": utcnow(), "type": type_, **self.redactor.obj(_plain(data))}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(self.redactor.obj(_plain(data)), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def path(self, name: str) -> Path:
        return self.dir / name

    def close(self) -> None:
        self._fh.close()


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value
