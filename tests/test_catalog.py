"""Agent-facing catalog for one institution: only approved capabilities are offered, as typed
tools; the agent invokes one by name and gets a typed result from deterministic replay. The
tenant is fixed when the catalog is built - tools have no tenant argument. The 'agent' here
is scripted, so the contract is tested without API calls."""
import json
from dataclasses import dataclass, field

import pytest
import yaml

from cua.bank_agent import BankAgent
from cua.catalog import Catalog

from .scripted_model import Block, Response
from .test_overrides import riverbend_override, riverbend_probe  # noqa: F401 - module fixtures reused
from .test_replay import capability, policy_for, serve  # noqa: F401


@dataclass
class ScriptedAgentModel:
    """Plays the bank's AI agent: a fixed sequence of tool calls, then a text answer."""
    calls: list[tuple[str, dict]]
    answer: str = "done"
    name: str = "scripted-agent"
    seen_tools: list = field(default_factory=list)
    seen_system: str = ""
    turn: int = 0

    def respond(self, system, tools, messages):
        self.seen_tools, self.seen_system = tools, system
        self.turn += 1
        if self.turn <= len(self.calls):
            tool, args = self.calls[self.turn - 1]
            return Response([Block("tool_use", id=f"tu_{self.turn}", name=tool, input=args)])
        return Response([Block("text", text=self.answer)], stop_reason="end_turn")


@pytest.fixture
def world(tmp_path, capability, riverbend_override, secrets_env):  # noqa: F811
    """Two live tenants, an approved capability (plus a draft newer one), an approved override."""
    _, acme_server, acme = serve("acme")
    _, river_server, river = serve("riverbend")
    caps = tmp_path / "capabilities"
    approved = capability.model_copy(update={"version": "0.3.0", "status": "approved"})
    approved.save(caps)
    capability.model_copy(update={"version": "0.2.0", "status": "approved"}).save(caps)
    capability.model_copy(update={"version": "0.4.0", "status": "draft"}).save(caps)
    overrides = tmp_path / "overrides"
    riverbend_override.model_copy(update={"status": "approved", "base_version": "0.3.0",
                                          "base_hash": approved.compute_hash()}).save(overrides)
    tenants = tmp_path / "tenants.yaml"
    tenants.write_text(yaml.safe_dump({"tenants": {
        "acme": {"institution": "Acme Community Credit Union"},
        "riverbend": {"institution": "Riverbend Federal Credit Union"}}}), encoding="utf-8")
    urls = {"acme": acme, "riverbend": river}
    yield {"catalog": lambda tenant: Catalog(caps, overrides, tenants, urls.get(tenant, "http://127.0.0.1:1")),
           "policy": policy_for(acme, river), "urls": urls,
           "dir": tmp_path, "overrides": overrides}
    acme_server.shutdown()
    river_server.shutdown()


def ask(world, tenant, calls, answer="ok"):
    model = ScriptedAgentModel(calls, answer)
    agent = BankAgent(model, world["catalog"](tenant), world["policy"], world["dir"] / "runs", echo=lambda _: None)
    return agent.ask("What is the savings balance?"), model


def test_only_the_latest_approved_version_is_offered(world):
    catalog = world["catalog"]("acme")
    assert catalog.entries["member_savings_balance"].capability.version == "0.3.0"   # not the 0.4.0 draft
    assert any("0.4.0: draft" in s for s in catalog.skipped)


def test_tools_are_typed_contracts_with_no_tenant_argument(world):
    [tool] = world["catalog"]("acme").tools()
    schema = tool["input_schema"]
    assert tool["name"] == "member_savings_balance"
    assert list(schema["properties"]) == ["member_number"] and schema["required"] == ["member_number"]
    assert schema["additionalProperties"] is False
    assert "record_not_found" in tool["description"] and "savings_balance (decimal)" in tool["description"]


def test_institution_with_an_unapproved_override_is_refused_at_call_time(world):
    """The tenant is only known once its screen is seen, so the override's approval is enforced
    then: the base is offered, but replay refuses to run it with an unreviewed override."""
    path = world["overrides"] / "member_savings_balance" / "riverbend.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: approved", "status: draft"), encoding="utf-8")
    answer, _ = ask(world, "riverbend", [("member_savings_balance", {"member_number": "12345"})])
    result = answer.calls[0]["result"]
    assert result["status"] == "failed" and result["error"]["kind"] == "not_approved"


def test_agent_invokes_a_capability_by_name_and_gets_typed_outputs(world):
    answer, model = ask(world, "acme", [("member_savings_balance", {"member_number": "24680"})],
                        answer="Member 24680 has a savings balance of $12,890.05.")
    [call] = answer.calls
    assert call["result"]["status"] == "success" and call["result"]["outputs"] == {"savings_balance": "12890.05"}
    assert model.seen_tools[0]["name"] == "member_savings_balance"
    # Nothing from the tenant registry reaches the model: no tenant id, institution or URL.
    tools_text = json.dumps(model.seen_tools)
    for leaked in ("acme", "Acme Community Credit Union", "Riverbend", world["urls"]["acme"]):
        assert leaked not in model.seen_system and leaked not in tools_text
    # The agent's own log is redacted too - including the balance inside its final answer.
    log = (answer.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "12890.05" not in log and "12,890.05" not in log and "capability_invoked" in log


def test_an_agent_built_for_another_tenant_uses_that_tenants_override(world):
    answer, _ = ask(world, "riverbend", [("member_savings_balance", {"member_number": "12345"})])
    result = answer.calls[0]["result"]
    assert result["status"] == "success" and result["outputs"] == {"savings_balance": "1523.47"}
    assert "quality" not in result                                       # every step high confidence


def test_business_outcome_reaches_the_agent_as_an_answer_not_an_error(world):
    answer, _ = ask(world, "acme", [("member_savings_balance", {"member_number": "99999"})])
    result = answer.calls[0]["result"]
    assert result["status"] == "business_outcome" and result["outcome"] == "record_not_found"
    assert "error" not in result


def test_the_agent_cannot_reach_another_tenant_or_an_unknown_capability(world):
    answer, _ = ask(world, "acme", [("member_savings_balance", {"member_number": "12345", "tenant": "riverbend"}),
                                    ("delete_all_members", {})])
    assert "does not take ['tenant']" in answer.calls[0]["result"]["error"]
    assert "no approved capability named 'delete_all_members'" in answer.calls[1]["result"]["error"]



def test_agent_never_sees_internals(world):
    answer, _ = ask(world, "acme", [("member_savings_balance", {"member_number": "12345"})])
    payload = json.dumps(answer.calls[0]["result"])
    assert "screenshot" not in payload and "base_url" not in payload and "127.0.0.1" not in payload


def test_a_request_no_capability_covers_is_recorded_as_a_gap_not_approximated(world):
    goal = "Look up member 12345's membership status"
    answer, model = ask(world, "acme", [("report_unsupported", {"goal": goal})],
                        answer="That is not supported yet.")
    assert answer.unsupported == goal and answer.calls == []           # no capability was run
    assert [t["name"] for t in model.seen_tools] == ["member_savings_balance", "report_unsupported"]
    log = (answer.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "capability_gap" in log and "capability_invoked" not in log
