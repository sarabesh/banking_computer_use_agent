from pathlib import Path

from cua.policy import Policy
from cua.redact import Redactor
from cua.surface.base import Element

POLICY = Policy.load(Path(__file__).resolve().parents[1] / "policy.yaml")


def el(name: str) -> Element:
    return Element(ref="e1", kind="control", role="button", name=name, frame="work", frame_path="/w", tag="input")


def test_url_allowlist():
    assert POLICY.check_url("http://127.0.0.1:5001/w/search").allowed
    assert not POLICY.check_url("https://evil.example.com/").allowed
    assert not POLICY.check_url("http://127.0.0.1:5001/__control/faults").allowed


def test_irreversible_controls_need_approval():
    assert POLICY.check_action("click", el("Confirm")).verdict == "needs_approval"
    assert POLICY.check_action("click", el("POST")).verdict == "needs_approval"
    assert POLICY.check_action("click", el("Delete share")).verdict == "needs_approval"
    assert POLICY.check_action("click", el("Go")).verdict == "allow"
    # Full-match only: "Confirmation details" is a reversible link.
    assert POLICY.check_action("click", el("Confirmation details")).verdict == "allow"


def test_unlisted_action_and_key_blocked():
    assert POLICY.check_action("drag").verdict == "block"
    assert POLICY.check_action("press_key", key="F12").verdict == "block"


def test_redaction_channels():
    text = "SSN 900-12-3456, balance $1,523.47, mail a@b.co"
    logs = Redactor(POLICY, "logs").text(text)
    model = Redactor(POLICY, "model").text(text)
    assert "900-12-3456" not in logs and "1,523.47" not in logs and "a@b.co" not in logs
    assert "900-12-3456" not in model and "$1,523.47" in model   # the model may see balances


def test_card_redaction_uses_luhn_to_avoid_false_positives():
    r = Redactor(POLICY, "logs")
    assert r.text("card 4111 1111 1111 1111") == "card [REDACTED:card_number]"   # valid test card
    assert r.text("run 20261012-141347-discover") == "run 20261012-141347-discover"  # timestamp kept


def test_known_values_redacted():
    r = Redactor(POLICY, "logs")
    r.add_known("hunter2-secret", "secret:pw")
    assert r.text("typed hunter2-secret ok") == "typed [REDACTED:secret:pw] ok"
