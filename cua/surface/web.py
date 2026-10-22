"""Web implementation of the Surface, on Playwright.

Perception walks *every* frame (legacy apps love framesets/iframes) and builds a semantic
element list. Policy is enforced at the network layer too: any request outside the
allowlist is aborted by the browser, regardless of what clicked it.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Dialog, Error as PlaywrightError, Frame, Request, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..policy import Policy
from .base import ActionError, Element, Location, Observation

_HERE = Path(__file__).parent
_DESCRIBE_JS = (_HERE / "describe.js").read_text(encoding="utf-8")
OBSERVE_JS = "() => {\n" + _DESCRIBE_JS + "\n" + (_HERE / "observe.js").read_text(encoding="utf-8") + "\n}"
RECORDER_JS = "(() => {\n" + _DESCRIBE_JS + "\n" + (_HERE / "recorder.js").read_text(encoding="utf-8") + "\n})();"

MASK_JS = "([patterns, maskData]) => {\n" + _DESCRIBE_JS + """
  const H = cuaHelpers();
  const res = patterns.map(p => new RegExp(p));
  let n = 0;
  const mask = el => {
    if (el.hasAttribute('data-cua-mask')) return;
    el.setAttribute('data-cua-mask', el.getAttribute('style') || '');
    el.style.background = '#000'; el.style.color = '#000'; n++;
  };
  // 1. Anything matching a redaction pattern (SSNs, card numbers, balances...).
  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const el = walker.currentNode.parentElement;
    if (el && res.some(r => r.test(walker.currentNode.textContent))) mask(el);
  }
  // 2. Evidence only: every data value - text with a label beside it or a column header
  //    above it, and the contents of input fields - since names and addresses have no
  //    pattern. Labels, headers and buttons stay visible, so the screen remains readable.
  if (maskData && document.body) {
    for (const el of document.body.querySelectorAll('*')) {
      if (['INPUT', 'TEXTAREA'].includes(el.tagName) && !H.isButtonInput(el)) { mask(el); continue; }
      if (!H.isCandidate(el)) continue;
      const d = H.describe(el);
      if (d.kind === 'text' && (d.caption || d.col_header)) mask(el);
    }
  }
  return n;
}"""

UNMASK_JS = """() => {
  for (const el of document.querySelectorAll('[data-cua-mask]')) {
    const s = el.getAttribute('data-cua-mask');
    if (s) el.setAttribute('style', s); else el.removeAttribute('style');
    el.removeAttribute('data-cua-mask');
  }
}"""

PENDING_SUBMIT_JS = """() => {
  const a = document.activeElement;
  if (!a || a === document.body || a.tagName === 'IFRAME' || a.tagName === 'FRAME') return null;
  const form = a.form || a.closest('form');
  if (!form) return '';
  const b = form.querySelector('input[type=submit], input[type=image], button:not([type=button]):not([type=reset])');
  return b ? (b.value || b.innerText || b.alt || '').trim() : '';
}"""

# Dialog policy: given (type, message) return True to accept, False to dismiss.
DialogPolicy = Callable[[str, str], bool]


def dismiss_all(_type: str, _message: str) -> bool:
    return False


class WebSurface:
    def __init__(self, policy: Policy, *, headless: bool = False, action_timeout_ms: int = 5000,
                 load_timeout_ms: int = 15000, dialog_policy: DialogPolicy = dismiss_all):
        self.policy = policy
        self.headless = headless
        self.action_timeout_ms = action_timeout_ms
        self.load_timeout_ms = load_timeout_ms
        self.dialog_policy = dialog_policy
        self._seq = 0
        self._handles: dict[str, tuple[Frame, int, int]] = {}
        self._dialogs: list[str] = []
        self._blocked: list[str] = []
        self._inflight_docs: set[Request] = set()
        self._pw =self._browser = self.context = self.page = None
        self.control = None                    # cua.handoff.Control, set by the run that owns the session
        self._recording = False
        self._human_events: list[dict] = []
        self._pending_dialogs: dict[int, Dialog] = {}
        self._dialog_seq = 0
        self._lost: str | None = None           # why the live session is gone (window closed, crash)

    # ------------------------------------------------------------------ lifecycle

    def start(self, url: str) -> None:
        decision = self.policy.check_url(url)
        if not decision.allowed:
            raise ActionError("blocked", f"start URL refused: {decision.reason}")
        self._lost = None
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        self.context.route("**/*", self._guard)
        self.context.expose_binding("__cua_record", self._on_record)
        self.context.add_init_script(RECORDER_JS)
        self.page = self.context.new_page()
        # A closed window, context or browser ends the live session; every later call reports it.
        self.page.on("close", lambda _p: self._mark_lost("the browser page was closed"))
        self.page.on("crash", lambda _p: self._mark_lost("the browser page crashed"))
        self._browser.on("disconnected", lambda _b: self._mark_lost("the browser was closed"))
        self.page.on("dialog", self._on_dialog)
        self.page.on("request", self._on_request_start)
        self.page.on("requestfinished", self._on_request_end)
        self.page.on("requestfailed", self._on_request_end)
        try:
            self.page.goto(url, wait_until="load", timeout=self.load_timeout_ms)
        except PlaywrightError as exc:
            # The app is down or not answering: an environment condition, reported as such
            # (a structured result for the caller), not a crash.
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            raise ActionError("unreachable", f"could not open {url}: {reason}") from exc

    def _mark_lost(self, reason: str) -> None:
        if self._lost is None:
            self._lost = reason

    def _alive(self) -> None:
        if self._lost:
            raise ActionError("session_lost", f"the live session was lost: {self._lost}")

    def _closed_error(self, exc: PlaywrightError) -> bool:
        """Was this Playwright error caused by the session going away?"""
        if "has been closed" in exc.message or "Target closed" in exc.message:
            self._mark_lost("the browser page, context or browser was closed")
        return self._lost is not None

    def close(self) -> None:
        self._lost = self._lost or "closed by the run"
        for closer in (self.context, self._browser):
            try:
                if closer:
                    closer.close()
            except PlaywrightError:
                pass
        if self._pw:
            self._pw.stop()

    def _guard(self, route: Route) -> None:
        decision = self.policy.check_url(route.request.url)
        if decision.allowed:
            route.continue_()
        else:
            self._blocked.append(f"{route.request.url} ({decision.reason})")
            route.abort("blockedbyclient")

    def _on_request_start(self, request: Request) -> None:
        if request.resource_type == "document":
            self._inflight_docs.add(request)

    def _on_request_end(self, request: Request) -> None:
        self._inflight_docs.discard(request)

    def _on_dialog(self, dialog: Dialog) -> None:
        if self._human_in_control():
            # The human decides: park the dialog until the operator answers it (cua operator dialog).
            self._dialog_seq += 1
            self._pending_dialogs[self._dialog_seq] = dialog
            self._human_events.append({"kind": "dialog", "dialog_id": self._dialog_seq, "type": dialog.type,
                                       "message": dialog.message, "at": time.time()})
            return
        accept = self.dialog_policy(dialog.type, dialog.message)
        self._dialogs.append(f"{dialog.type} dialog '{dialog.message}' was {'accepted' if accept else 'dismissed'}")
        dialog.accept() if accept else dialog.dismiss()

    # ------------------------------------------------------------------ control & human handoff

    def _human_in_control(self) -> bool:
        return self.control is not None and self.control.state.value == "human"

    def _check_control(self) -> None:
        if self.control is not None and not self.control.automation_may_act:
            raise ActionError("not_in_control", f"automation does not hold control ({self.control.state.value})")

    def _on_record(self, source: dict, payload: dict) -> None:
        if not self._recording:
            return                     # automation's own actions are already in its trace
        frame = source["frame"]
        name = next((self._frame_name(f, i) for i, f in enumerate(self.page.frames) if f == frame), "main")
        self._human_events.append({**payload, "frame": name, "frame_path": urlsplit(frame.url).path,
                                   "at": time.time()})

    def start_recording(self) -> None:
        self._human_events, self._recording = [], True

    def stop_recording(self) -> list[dict]:
        self._recording = False
        events, self._human_events = self._human_events, []
        return events

    def pending_dialogs(self) -> list[dict]:
        return [{"id": i, "type": d.type, "message": d.message} for i, d in self._pending_dialogs.items()]

    def answer_dialog(self, dialog_id: int | None, accept: bool) -> None:
        dialog = self._pending_dialogs.pop(dialog_id, None) if dialog_id is not None else None
        if dialog is None and dialog_id is None and len(self._pending_dialogs) == 1:
            dialog = self._pending_dialogs.pop(next(iter(self._pending_dialogs)))
        if dialog is None:
            raise ActionError("not_found", f"no pending dialog {dialog_id}")
        self._human_events.append({"kind": "dialog_answer", "accepted": accept, "message": dialog.message,
                                   "at": time.time()})
        dialog.accept() if accept else dialog.dismiss()

    def pump(self, ms: int) -> None:
        """Wait while keeping the browser's event loop running (recording, dialogs)."""
        self._alive()
        try:
            self.page.wait_for_timeout(ms)
        except PlaywrightError as exc:
            if self._closed_error(exc):
                self._alive()
            raise

    # ------------------------------------------------------------------ perception

    def _settle(self) -> None:
        """Wait until any navigation the last action caused (in any frame) has finished.

        Load state alone is not enough: right after a click the old document still reports
        'load' because the new request has not started yet. So: give the action a moment to
        start requests, wait for in-flight document requests to drain, then for every frame's
        load event."""
        self._alive()
        try:
            self.page.wait_for_timeout(150)
            deadline = time.monotonic() + self.load_timeout_ms / 1000
            while self._inflight_docs and time.monotonic() < deadline:
                self.page.wait_for_timeout(50)
        except PlaywrightError as exc:
            if self._closed_error(exc):
                self._alive()
            raise
        for frame in self.page.frames:
            try:
                frame.wait_for_load_state("load", timeout=max(1, int((deadline - time.monotonic()) * 1000)))
            except (PlaywrightTimeout, PlaywrightError):
                pass
        self._alive()

    def _frame_name(self, frame: Frame, index: int) -> str:
        if frame == self.page.main_frame:
            return "main"
        return frame.name or f"frame{index}"

    def location(self) -> Location:
        frames = {}
        for i, frame in enumerate(self.page.frames):
            if not frame.is_detached():
                frames[self._frame_name(frame, i)] = urlsplit(frame.url).path
        try:
            title = self.page.title()
        except PlaywrightError:
            title = ""
        return Location(url=urlsplit(self.page.url).path, title=title, frames=frames)

    def _read_frames(self) -> list[tuple[Frame, int, list[dict]]]:
        """Evaluate the observer in every frame; retry if a frame navigates mid-read."""
        for _attempt in range(3):
            self._settle()
            results = []
            try:
                for i, frame in enumerate(self.page.frames):
                    if not frame.is_detached():
                        results.append((frame, i, frame.evaluate(OBSERVE_JS)))
                return results
            except PlaywrightError: 
                continue
        return results

    def observe(self) -> Observation:
        self._alive()
        self._seq += 1
        self._handles = {}
        elements: list[Element] = []
        for frame, i, raw in self._read_frames():
            fname, fpath = self._frame_name(frame, i), urlsplit(frame.url).path
            for item in raw:
                ref = f"e{len(elements) + 1}"
                index = item.pop("i")
                elements.append(Element(ref=ref, frame=fname, frame_path=fpath, **item))
                self._handles[ref] = (frame, index, self._seq)
        self._alive()          # frames vanishing because the window closed is not "an empty screen"
        obs = Observation(seq=self._seq, location=self.location(), elements=elements,
                          dialogs=self._dialogs, blocked_requests=self._blocked)
        self._dialogs, self._blocked = [], []
        return obs

    # ------------------------------------------------------------------ actions

    def _handle(self, ref: str):
        self._alive()
        if ref not in self._handles:
            raise ActionError("stale_ref", f"unknown ref '{ref}': use a ref from the latest observation")
        frame, index, _seq = self._handles[ref]
        try:
            handle = frame.evaluate_handle("i => window.__cua_elems && window.__cua_elems[i]", index).as_element()
        except PlaywrightError as exc:
            raise ActionError("stale_ref", f"ref '{ref}' is no longer on the page ({exc.message.splitlines()[0]})")
        if handle is None:
            raise ActionError("stale_ref", f"ref '{ref}' is no longer on the page")
        return handle

    def _run(self, fn) -> None:
        try:
            fn()
        except PlaywrightTimeout as exc:
            msg = exc.message
            if "intercepts pointer events" in msg:
                raise ActionError("covered", "the element is covered by another element (for example a dialog or notice overlay)")
            if "not editable" in msg or "not an <input>" in msg:
                raise ActionError("not_editable", "the element is not an editable field")
            raise ActionError("timeout", f"timed out waiting for the element to be actionable: {msg.splitlines()[0]}")
        except PlaywrightError as exc:
            if self._closed_error(exc):
                self._alive()
            raise ActionError("other", exc.message.splitlines()[0])
        self._settle()

    def click(self, ref: str) -> None:
        self._check_control()
        handle = self._handle(ref)
        self._run(lambda: handle.click(timeout=self.action_timeout_ms))

    def fill(self, ref: str, text: str) -> None:
        # Click first, as an operator would: Playwright's fill() alone ignores overlays,
        # which would let automation type into a field a human could not reach.
        self._check_control()
        handle = self._handle(ref)

        def do() -> None:
            handle.click(timeout=self.action_timeout_ms)
            handle.fill(text, timeout=self.action_timeout_ms)
        self._run(do)

    def select(self, ref: str, option: str) -> str:
        self._check_control()
        handle = self._handle(ref)
        labels = handle.evaluate("el => el.options ? Array.from(el.options).map(o => o.text.trim()) : null")
        if labels is None:
            raise ActionError("not_editable", "the element is not a drop-down list")
        match = next((l for l in labels if l == option), None) or \
            next((l for l in labels if l.lower().startswith(option.lower())), None)
        if match is None:
            raise ActionError("no_such_option", f"no option matching '{option}'; options are {labels}")
        self._run(lambda: handle.select_option(label=match, timeout=self.action_timeout_ms))
        return match

    def press(self, key: str) -> None:
        self._check_control()
        self._run(lambda: self.page.keyboard.press(key))

    def reload(self, frame_name: str) -> None:
        """Reload one frame's document (e.g. after a transient error page inside it)."""
        self._check_control()
        for i, frame in enumerate(self.page.frames):
            if not frame.is_detached() and self._frame_name(frame, i) == frame_name:
                url = frame.url
                self._run(lambda: frame.goto(url, timeout=self.load_timeout_ms))
                return
        raise ActionError("not_found", f"no frame named '{frame_name}' to reload")

    def read(self, ref: str) -> str:
        handle = self._handle(ref)
        value = handle.evaluate("el => ('value' in el && el.tagName !== 'BUTTON') ? el.value : el.innerText")
        return " ".join((value or "").split())

    def pending_submit_label(self) -> str | None:
        """Label of the submit control Enter would trigger in the focused form, if any."""
        for frame in self.page.frames:
            try:
                label = frame.evaluate(PENDING_SUBMIT_JS)
            except PlaywrightError:
                continue
            if label is not None:
                return label or None
        return None

    def screenshot(self, path: str | None, mask_patterns: list[str], mask_data: bool = False) -> bytes:
        """Screenshot with sensitive text blacked out in-page before capture. mask_data=True
        (used for persisted evidence) also blacks out every data value and field content."""
        self._alive()
        frames = [f for f in self.page.frames if not f.is_detached()]
        for frame in frames:
            try:
                frame.evaluate(MASK_JS, [mask_patterns, mask_data])
            except PlaywrightError:
                pass
        try:
            return self.page.screenshot(path=path, type="jpeg", quality=60)
        finally:
            for frame in frames:
                try:
                    frame.evaluate(UNMASK_JS)
                except PlaywrightError:
                    pass
