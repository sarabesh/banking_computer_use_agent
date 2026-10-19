"""A stand-in for the bank's AI agent: it answers a staff question by calling capabilities
from the catalog. It decides WHAT to call; the call itself is deterministic replay.

This is the product boundary from the brief: "the agent-facing product decides what to do;
this system is how it reliably and safely does it". The model here never sees the UI,
never drives a browser and never sees credentials - it sees typed tool contracts and typed
results.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .catalog import Catalog, CatalogError, result_for_agent
from .evidence import RunLog
from .policy import Policy
from .redact import Redactor

SYSTEM_PROMPT = """\
You are the member-service assistant of a credit union's operations team. You answer staff \
questions by calling the institution's capabilities (your tools). Each tool runs a reviewed, \
deterministic automation in the institution's own system and returns typed results.

Rules:
- Call a tool whenever the answer depends on member or account data. Never guess values.
- A business outcome such as record_not_found is a legitimate answer: say so plainly.
- If a tool reports a failure or an escalation, say the lookup could not be completed and why. \
Do not retry more than once.
- needs_approval means a human must approve that step in the system: say it is waiting for approval.
- If none of the capability tools does what is asked, do not approximate it with another tool: \
call report_unsupported, then say the request is not supported yet.
- Keep the answer short and factual."""


# Not a capability: the structured "no tool fits" signal. The gap is recorded so a human can run
# discovery for it; the agent never starts discovery itself (that drives the live system).
REPORT_UNSUPPORTED = {
    "name": "report_unsupported",
    "description": "Use when none of the other tools can do what is asked. Records the request for the "
                   "automation team, who can discover, review and add a capability for it.",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["goal"], "properties": {
        "goal": {"type": "string", "description": "The requested task as one imperative sentence, as a teller "
                                                  "would do it in the institution's system"}}},
}


@dataclass
class AgentAnswer:
    text: str
    calls: list[dict] = field(default_factory=list)
    run_dir: Path | None = None
    unsupported: str | None = None      # the goal, when no capability fits


class BankAgent:
    def __init__(self, model: Any, catalog: Catalog, policy: Policy, runs_dir: str | Path, *,
                 max_turns: int = 6, headless: bool = True, echo: Callable[[str], None] = print):
        self.model, self.catalog, self.policy = model, catalog, policy
        self.runs_dir = Path(runs_dir)
        self.max_turns, self.headless, self.echo = max_turns, headless, echo

    def ask(self, question: str) -> AgentAnswer:
        redactor = Redactor(self.policy, "logs")
        log = RunLog(self.runs_dir, "agent", question, redactor)
        tools = self.catalog.tools() + [REPORT_UNSUPPORTED]
        system = SYSTEM_PROMPT       # nothing from the tenant registry reaches the model
        log.event("agent_started", question=question, model=getattr(self.model, "name", None),
                  base_url=self.catalog.base_url, catalog=[{"name": t["name"], "description": t["description"]} for t in tools[:-1]])
        messages: list[dict] = [{"role": "user", "content": question}]
        answer = AgentAnswer(text="", run_dir=log.dir)
        try:
            for _ in range(self.max_turns):
                response = self.model.respond(system, tools, messages)
                messages.append({"role": "assistant", "content": response.content})
                tool_use = next((b for b in response.content if b.type == "tool_use"), None)
                texts = [b.text for b in response.content if b.type == "text" and b.text]
                log.event("agent_turn", request_id=getattr(response, "_request_id", None),
                          stop_reason=response.stop_reason, text=texts,
                          tool=getattr(tool_use, "name", None), input=getattr(tool_use, "input", None))
                if response.stop_reason == "refusal" or tool_use is None:
                    answer.text = "\n".join(texts) or "(no answer)"
                    break
                if tool_use.name == REPORT_UNSUPPORTED["name"]:
                    answer.unsupported = str(tool_use.input.get("goal", ""))
                    log.event("capability_gap", goal=answer.unsupported)
                    self.echo(f"  -> no capability fits; gap recorded: {answer.unsupported}")
                    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use.id,
                                     "content": json.dumps({"status": "recorded"})}]})
                    continue
                payload, is_error = self._invoke(tool_use.name, dict(tool_use.input), redactor, log)
                answer.calls.append({"tool": tool_use.name, "input": dict(tool_use.input), "result": payload})
                self.echo(f"  -> {tool_use.name}({json.dumps(tool_use.input)}) = {payload['status'] if 'status' in payload else 'error'}")
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use.id, "content": json.dumps(payload),
                     "is_error": is_error}]})
            else:
                answer.text = "(stopped: turn limit reached)"
            log.event("agent_answer", text=answer.text, calls=len(answer.calls))
        finally:
            log.close()
        return answer

    def _invoke(self, name: str, args: dict, redactor: Redactor, log: RunLog) -> tuple[dict, bool]:
        try:
            result = self.catalog.invoke(name, args, policy=self.policy, runs_dir=self.runs_dir,
                                         headless=self.headless)
        except CatalogError as exc:
            log.event("capability_refused", tool=name, input=args, reason=str(exc))
            return {"error": str(exc)}, True
        entry = self.catalog.entries[name]
        for out, spec in entry.capability.outputs.items():
            if spec.sensitive and out in result.outputs:
                redactor.add_known(result.outputs[out], f"output:{out}")   # the answer text is redacted too
        payload = result_for_agent(result)
        log.event("capability_invoked", tool=name, input=args, result=payload, replay_run=result.run_id)
        return payload, result.status == "failed"
