"""Mock legacy core-banking teller app ("CoreLine Teller").

Deliberately hostile to automation, like the real long tail of bank back-office apps:
  - server-rendered pages, table layouts, and the work area inside an <iframe>
  - no ids / test ids, meaningless field names (q1, f2, ...), generic class names
  - inputs whose captions sit in a neighbouring <td> instead of a <label>
  - one action rendered as a clickable <div>, not a button
  - errors returned as HTTP 200 pages with an error code in red text

Runtime failures (brief §3.3) are switchable via FaultConfig: blocking system-notice
interstitials, latency, application errors, session expiry, and a native confirm dialog.
Business outcomes (not found, restricted member, permission denied, validation errors)
come from the data and user roles.

The same app serves multiple "tenants" (institutions running the same vendor product,
configured differently) via tenants/*.json: different labels, branding, column order.
"""
from __future__ import annotations

import json
import random
import re
import secrets
import time
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, session

from .data import SHARE_TYPES, BankState, money, parse_amount
from .faults import FaultConfig

TENANT_DIR = Path(__file__).parent / "tenants"
MEMBER_NO_RE = re.compile(r"^\d{5}$")
LAST_NAME_RE = re.compile(r"^[A-Za-z' -]{1,30}$")


def load_tenant(tenant_id: str) -> dict:
    path = TENANT_DIR / f"{tenant_id}.json"
    if not path.exists():
        available = sorted(p.stem for p in TENANT_DIR.glob("*.json"))
        raise ValueError(f"unknown tenant {tenant_id!r}; available: {available}")
    return json.loads(path.read_text(encoding="utf-8"))


def create_app(tenant: str = "acme", faults: FaultConfig | None = None, seed: int | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)

    tenant_cfg = load_tenant(tenant)
    labels = tenant_cfg["labels"]
    faults = faults or FaultConfig()
    rng = random.Random(seed)
    state = {"bank": BankState.load(), "session_gen": 0}
    app.config.update(TENANT=tenant_cfg, FAULTS=faults, STATE=state)

    @app.context_processor
    def inject_globals():
        return {"T": tenant_cfg, "L": labels, "user": session.get("display"), "notice": g.get("notice", False)}

    def message(title: str, text: str, code: str | None = None, link: str | None = None,
                link_text: str | None = None, target: str | None = None, status: int = 200):
        html = render_template("message.html", title=title, text=text, code=code,
                               link=link, link_text=link_text, target=target)
        return html, status

    def session_problem():
        """Legacy-style: problems come back as HTTP 200 pages, not redirects or 401s."""
        if not session.get("user"):
            return message("Not Signed On", "You must sign on to use this function.",
                           link="/login", link_text=labels["sign_on"], target="_top")
        expired = (session.get("gen") != state["session_gen"]
                   or time.time() - session.get("seen", 0) > faults.session_ttl_s)
        if expired:
            session.clear()
            return message("Session Expired",
                           "Your session has timed out due to inactivity. Please sign on again.",
                           code="SEC-440", link="/login", link_text=labels["sign_on"], target="_top")
        session["seen"] = time.time()
        return None

    def work_page(fn):
        """Wraps pages shown inside the work frame: session checks + fault injection."""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            problem = session_problem()
            if problem:
                return problem
            if faults.delay_ms:
                time.sleep(faults.delay_ms / 1000)
            if rng.random() < faults.error_rate:
                ref = f"{rng.getrandbits(32):08x}"
                return render_template("app_error.html", ref=ref), 500
            g.notice = request.method == "GET" and rng.random() < faults.interstitial_rate
            return fn(*args, **kwargs)
        return wrapper

    def officer_only(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("role") != "officer":
                return message("Access Denied",
                               "Your user profile does not permit this function. Contact your supervisor.",
                               code="SEC-403", link="/w/search", link_text=labels["new_search"])
            return fn(*args, **kwargs)
        return wrapper

    def get_member(member_no: str):
        return state["bank"].members.get(member_no)

    # ---------------------------------------------------------------- sign on / shell

    @app.get("/")
    def index():
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            user = state["bank"].users.get(request.form.get("u", "").strip())
            if user and secrets.compare_digest(user["password"], request.form.get("p", "")):
                session.clear()
                session.update(user=user["user_id"], role=user["role"], display=user["display"],
                               seen=time.time(), gen=state["session_gen"])
                return redirect("/main")
            error = "SEC-101 Invalid user ID or password."
        return render_template("login.html", error=error)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/main")
    def main():
        problem = session_problem()
        if problem:
            return problem
        return render_template("shell.html")

    # ---------------------------------------------------------------- inquiry

    @app.get("/w/search")
    @work_page
    def search():
        return render_template("search.html", error=None, q1="", q2="")

    @app.get("/w/results")
    @work_page
    def results():
        q1 = request.args.get("q1", "").strip()
        q2 = request.args.get("q2", "").strip()
        error = None
        if not q1 and not q2:
            error = "ERR-1001 Enter a member number or last name."
        elif q1 and not MEMBER_NO_RE.match(q1):
            error = f"ERR-1042 {labels['member_no']} must be exactly 5 digits."
        elif q2 and not LAST_NAME_RE.match(q2):
            error = f"ERR-1043 {labels['last_name']} contains invalid characters."
        if error:
            return render_template("search.html", error=error, q1=q1, q2=q2)
        rows = state["bank"].search(q1, q2)
        return render_template("results.html", rows=rows)

    @app.get("/w/member/<member_no>")
    @work_page
    def member_detail(member_no: str):
        member = get_member(member_no)
        if not member:
            return message(labels["member_title"], labels["no_results"], code="ERR-1404",
                           link="/w/search", link_text=labels["new_search"])
        cols = tenant_cfg["account_columns"]
        headers = [labels[f"col_{c}"] for c in cols]
        rows = [[money(a[c]) if c in ("balance", "available") else a[c] for c in cols]
                for a in member["accounts"]]
        return render_template("member.html", m=member, headers=headers, rows=rows)

    # ---------------------------------------------------------------- open sub-account

    @app.route("/w/member/<member_no>/open", methods=["GET", "POST"])
    @work_page
    @officer_only
    def open_share(member_no: str):
        member = get_member(member_no)
        if not member:
            return message(labels["open_title"], labels["no_results"], code="ERR-1404",
                           link="/w/search", link_text=labels["new_search"])
        if member["status"] != "ACTIVE":
            return message(labels["open_title"],
                           f"Member is {member['status']}. New shares cannot be opened. Refer to supervisor.",
                           code="ERR-3301", link=f"/w/member/{member_no}", link_text=labels["back"])
        form = {"f1": "", "f2": "", "f3": ""}
        error = None
        if request.method == "POST":
            form = {k: request.form.get(k, "").strip() for k in form}
            amount = parse_amount(form["f3"])
            if form["f1"] not in SHARE_TYPES:
                error = f"ERR-2202 Select a {labels['share_type']}."
            elif len(form["f2"]) > 20:
                error = f"ERR-2205 {labels['nickname']} may not exceed 20 characters."
            elif amount is None:
                error = f"ERR-2201 {labels['deposit']} must be a dollar amount."
            elif amount < SHARE_TYPES[form["f1"]][1]:
                desc, minimum = SHARE_TYPES[form["f1"]]
                error = f"ERR-2210 Minimum opening deposit for {desc} is {money(minimum)}."
            else:
                session["pending"] = {"member_no": member_no, "type": form["f1"],
                                      "nickname": form["f2"], "deposit": str(amount)}
                return redirect(f"/w/member/{member_no}/open/review")
        return render_template("open_form.html", m=member, form=form, error=error, share_types=SHARE_TYPES)

    @app.get("/w/member/<member_no>/open/review")
    @work_page
    @officer_only
    def open_review(member_no: str):
        pending = session.get("pending")
        if not pending or pending["member_no"] != member_no:
            return message(labels["review_title"], "No pending request for this member.", code="ERR-2299",
                           link=f"/w/member/{member_no}", link_text=labels["back"])
        member = get_member(member_no)
        return render_template("review.html", m=member, p=pending, confirm_dialog=faults.confirm_dialog,
                               type_desc=SHARE_TYPES[pending["type"]][0], deposit=money(pending["deposit"]))

    @app.post("/w/member/<member_no>/open/confirm")
    @work_page
    @officer_only
    def open_confirm(member_no: str):
        pending = session.pop("pending", None)
        if not pending or pending["member_no"] != member_no:
            return message(labels["review_title"], "No pending request for this member.", code="ERR-2299",
                           link=f"/w/member/{member_no}", link_text=labels["back"])
        suffix, confirmation = state["bank"].add_share(
            member_no, pending["type"], pending["nickname"], parse_amount(pending["deposit"]))
        return render_template("done.html", m=get_member(member_no), suffix=suffix, confirmation=confirmation)

    # ---------------------------------------------------------------- test harness (not part of the "app")

    @app.get("/__control/faults")
    def get_faults():
        return jsonify(faults.to_dict())

    @app.post("/__control/faults")
    def set_faults():
        try:
            faults.update(request.get_json(force=True) or {})
        except (ValueError, TypeError) as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(faults.to_dict())

    @app.post("/__control/expire-sessions")
    def expire_sessions():
        state["session_gen"] += 1
        return jsonify(session_gen=state["session_gen"])

    @app.post("/__control/reset")
    def reset():
        state["bank"] = BankState.load()
        faults.update(FaultConfig().to_dict())
        return jsonify(ok=True)

    return app
