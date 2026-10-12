"""Behavioural tests for the mock target app: every business outcome and injected fault
the replay engine will later need to recognise must actually be producible."""
import json
from pathlib import Path

import pytest

from target_app import FaultConfig, create_app

USERS = {u["role"]: u for u in json.loads(
    (Path(__file__).parents[1] / "target_app" / "seed" / "users.json").read_text())}


@pytest.fixture
def app():
    return create_app(tenant="acme", faults=FaultConfig(confirm_dialog=False), seed=7)


def login(client, role="officer"):
    user = USERS[role]
    return client.post("/login", data={"u": user["user_id"], "p": user["password"]})


def text(resp):
    return resp.get_data(as_text=True)


def test_bad_credentials(app):
    resp = app.test_client().post("/login", data={"u": "officer1", "p": "wrong"})
    assert "SEC-101" in text(resp)


def test_work_pages_require_sign_on(app):
    resp = app.test_client().get("/w/search")
    assert resp.status_code == 200 and "Not Signed On" in text(resp)


def test_lookup_reads_savings_balance(app):
    c = app.test_client()
    assert login(c).headers["Location"] == "/main"
    results = text(c.get("/w/results?q1=12345"))
    assert "QUINN, AVERY" in results and "1 record(s) found" in results
    detail = text(c.get("/w/member/12345"))
    assert "Regular Savings" in detail and "$1,523.47" in detail


def test_last_name_search_returns_multiple(app):
    c = app.test_client()
    login(c)
    assert "2 record(s) found" in text(c.get("/w/results?q2=quinn"))


def test_not_found_is_a_page_not_a_crash(app):
    c = app.test_client()
    login(c)
    resp = c.get("/w/results?q1=99999")
    assert resp.status_code == 200 and "No records match criteria." in text(resp)


@pytest.mark.parametrize("query,code", [("", "ERR-1001"), ("q1=12a45", "ERR-1042"), ("q2=x1", "ERR-1043")])
def test_search_validation(app, query, code):
    c = app.test_client()
    login(c)
    assert code in text(c.get(f"/w/results?{query}"))


def test_open_sub_account_happy_path(app):
    c = app.test_client()
    login(c)
    resp = c.post("/w/member/12345/open", data={"f1": "XMAS", "f2": "Gifts", "f3": "50"})
    assert resp.headers["Location"] == "/w/member/12345/open/review"
    review = text(c.get("/w/member/12345/open/review"))
    assert "Review New Sub-Account" in review and "$50.00" in review
    done = text(c.post("/w/member/12345/open/confirm"))
    assert "Confirmation #" in done and "0003" in done
    assert "Gifts" in text(c.get("/w/member/12345"))


def test_open_minimum_deposit_validation(app):
    c = app.test_client()
    login(c)
    assert "ERR-2210" in text(c.post("/w/member/12345/open", data={"f1": "MMKT", "f2": "", "f3": "100"}))


def test_restricted_member_cannot_open(app):
    c = app.test_client()
    login(c)
    assert "ERR-3301" in text(c.get("/w/member/30001/open"))


def test_read_only_user_is_denied(app):
    c = app.test_client()
    login(c, role="teller_ro")
    assert "SEC-403" in text(c.get("/w/member/12345/open"))


def test_confirm_without_pending_request(app):
    c = app.test_client()
    login(c)
    assert "ERR-2299" in text(c.post("/w/member/12345/open/confirm"))


def test_forced_session_expiry(app):
    c = app.test_client()
    login(c)
    c.post("/__control/expire-sessions")
    resp = c.get("/w/search")
    assert "SEC-440" in text(resp) and "Session Expired" in text(resp)


def test_injected_faults(app):
    c = app.test_client()
    login(c)
    c.post("/__control/faults", json={"interstitial_rate": 1.0})
    assert "SYSTEM NOTICE" in text(c.get("/w/search"))
    c.post("/__control/faults", json={"interstitial_rate": 0.0, "error_rate": 1.0})
    resp = c.get("/w/search")
    assert resp.status_code == 500 and "Server Error" in text(resp)


def test_fault_control_rejects_unknown_keys(app):
    resp = app.test_client().post("/__control/faults", json={"bogus": 1})
    assert resp.status_code == 400


def test_confirm_dialog_rendered_when_enabled():
    app = create_app(faults=FaultConfig(confirm_dialog=True))
    c = app.test_client()
    login(c)
    c.post("/w/member/12345/open", data={"f1": "REG", "f2": "", "f3": "10"})
    assert "return confirm(" in text(c.get("/w/member/12345/open/review"))


def test_second_tenant_same_product_different_labels():
    c = create_app(tenant="riverbend").test_client()
    login(c)
    detail = text(c.get("/w/member/12345"))
    assert "Account Holder Detail" in detail and "Current Bal" in detail and "Add Share" in detail
