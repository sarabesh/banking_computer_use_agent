"""The surface abstraction: how we perceive and act on an application.

Everything above this layer (discovery agent, artifact, replay) talks about *elements as a
human operator would describe them* - role, visible name, the caption next to a field, the
column/row a cell sits in, which frame/window it lives in. Nothing above this layer knows
about Playwright, CSS or the DOM. A desktop implementation (Windows UI Automation exposes
the same role/name/tree concepts) or a screenshot+OCR implementation for surfaces with no
tree at all would implement the same Protocol.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from typing import Literal, Protocol


class ActionError(Exception):
    """An action could not be performed. `kind` lets callers react without string matching."""

    def __init__(self, kind: Literal["stale_ref", "not_found", "covered", "timeout", "not_editable",
                                     "no_such_option", "blocked", "not_in_control", "session_lost",
                                     "unreachable", "other"],
                 message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class Element:
    ref: str                      # handle valid only for the observation it came from
    kind: Literal["control", "text"]
    role: str                     # link, button, textbox, combobox, cell, text, ...
    name: str                     # accessible name / visible text
    frame: str                    # "main" or the frame/window name
    frame_path: str               # path of the frame's document (e.g. /w/search)
    tag: str
    caption: str | None = None    # visible label beside an unlabeled field ("Member #:")
    field_name: str | None = None
    dom_id: str | None = None
    input_type: str | None = None
    href: str | None = None
    synthetic: bool = False       # clickable non-control (e.g. <div onclick>)
    has_value: bool = False       # field contains something (the value itself is never captured)
    options: list[str] = field(default_factory=list)
    selected: str | None = None
    col_header: str | None = None  # table column header above this cell
    row_key: str | None = None     # first cell of this element's table row
    row_text: str | None = None    # whole row text (context only, never persisted)
    css: str | None = None         # structural path: last-resort locator
    bbox: list[float] | None = None
    occluded: bool = False         # something (an overlay) covers it
    form: str | None = None        # identity of the form the control belongs to
    is_submit: bool = False        # it is that form's submit control

    def fingerprint(self) -> dict:
        """Everything a locator could be built from. Excludes row_text, which can hold PII."""
        data = asdict(self)
        for key in ("ref", "row_text", "has_value", "occluded"):
            data.pop(key)
        return data


@dataclass
class Location:
    url: str
    title: str
    frames: dict[str, str]        # frame name -> document path


@dataclass
class Observation:
    seq: int
    location: Location
    elements: list[Element]
    dialogs: list[str] = field(default_factory=list)       # native dialogs since last observation
    blocked_requests: list[str] = field(default_factory=list)

    def element(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def signature(self) -> str:
        """Changes when the screen meaningfully changes; used for stuck detection."""
        parts = [self.location.url, *sorted(self.location.frames.values())]
        parts += [f"{e.frame}|{e.role}|{e.name}|{e.has_value}|{e.occluded}" for e in self.elements]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def without_data(self) -> "Observation":
        """The screen's structure without its data, for persisting: labels, headers and
        controls are kept; every data value (text with a caption or column header) and all
        table-row context are replaced. Pattern redaction can't recognise names; this can."""
        def strip(e: Element) -> Element:
            is_value = e.kind == "text" and bool(e.caption or e.col_header)
            return replace(e, name="‹value›" if is_value else e.name, row_text=None,
                           row_key="‹value›" if e.row_key and e.kind == "control" and e.row_key != e.caption else e.row_key)
        return replace(self, elements=[strip(e) for e in self.elements])

    def headings(self, limit: int = 6) -> list[str]:
        """Short texts at the top of each frame: a cheap description of 'which screen is this'."""
        texts = [e for e in self.elements if e.kind == "text" and 2 <= len(e.name) <= 60]
        texts.sort(key=lambda e: (e.frame != "main", e.bbox[1] if e.bbox else 0))
        return [f"{e.frame}: {e.name}" for e in texts[:limit]]


class Surface(Protocol):
    def start(self, url: str) -> None: ...
    def observe(self) -> Observation: ...
    def location(self) -> Location: ...
    def click(self, ref: str) -> None: ...
    def fill(self, ref: str, text: str) -> None: ...
    def select(self, ref: str, option: str) -> str: ...
    def press(self, key: str) -> None: ...
    def reload(self, frame_name: str) -> None: ...
    # Human handoff: the session is ceded to an operator, whose actions are recorded.
    def start_recording(self) -> None: ...
    def stop_recording(self) -> list[dict]: ...
    def pending_dialogs(self) -> list[dict]: ...
    def answer_dialog(self, dialog_id: int | None, accept: bool) -> None: ...
    def pump(self, ms: int) -> None: ...
    def read(self, ref: str) -> str: ...
    def pending_submit_label(self) -> str | None: ...
    def screenshot(self, path: str | None, mask_patterns: list[str], mask_data: bool = False) -> bytes: ...
    def close(self) -> None: ...
