import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from cua.policy import Policy
from target_app import FaultConfig, create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def live_app():
    """The mock bank app on a free local port, served in a background thread."""
    app = create_app(tenant="acme", faults=FaultConfig(confirm_dialog=True), seed=3)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def policy(live_app):
    _, base = live_app
    p = Policy.load(ROOT / "policy.yaml")
    p.allowed_origins = [base]
    return p


@pytest.fixture
def secrets_env(monkeypatch):
    import json
    users = {u["role"]: u for u in json.loads((ROOT / "target_app" / "seed" / "users.json").read_text())}
    monkeypatch.setenv("TELLER_USER", users["officer"]["user_id"])
    monkeypatch.setenv("TELLER_PASSWORD", users["officer"]["password"])
    return users["officer"]
