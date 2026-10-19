# Banking Computer-Use Automation

An LLM **discovers** how to do a task in a legacy back-office app (no API, hostile markup);
the run is **compiled** into a typed, versioned capability; the capability is **replayed
deterministically** with no LLM - with business outcomes, recoverable conditions and hard
failures kept apart; a **human can take over the live session** and hand it back; and the
bank's AI agent can **call capabilities by name** from a catalog.

```
goal ──► discover (LLM) ──► trace ──► compile ──► capability.yaml ──► review/approve
                                                          │
          bank AI agent ──► catalog ──► replay (no LLM) ◄─┘──► result: success / business outcome / failure
                                             │
                                  stuck or approval ──► operator takes the live session ──► resume
```

> This README is a hands-on walkthrough. Design and trade-offs: [REPORT.md](REPORT.md).
> Recorded runs of every scenario: [evidence/](evidence/README.md).

---

## 1. Setup (once)

Windows PowerShell, from the project folder:

```bash
python -m venv .venv
```
```bash
.venv\Scripts\Activate.ps1
```
```bash
pip install -r requirements.txt
```
```bash
playwright install chromium
```

Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` (needed only for scenarios A and I).
The teller credentials in it are the mock app's test accounts.

**Every new terminal:** `cd` to the project folder and run `.venv\Scripts\Activate.ps1`.

## 2. Start the mock banks

Institutions running the same vendor product ("CoreLine Teller"), configured differently.
Leave each running in its own terminal:

```bash
python -m target_app --tenant acme --port 5001
```
```bash
python -m target_app --tenant riverbend --port 5002
```

Optional, for the "bank nobody has registered yet" demo in scenario I:
```bash
python -m target_app --tenant lakeside --port 5003
```

Open http://127.0.0.1:5001 to look around (sign-on details: `target_app/seed/users.json`).

**Note:** the system identifies each bank from its first screen and keeps what it learns in
[`tenants.yaml`](tenants.yaml) - the first replay at Acme or Riverbend records its screen
signature there, and a new bank (Lakeside) gets an entry. `git checkout tenants.yaml` resets it.

**Between scenarios**, reset any injected faults (PowerShell):
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/reset
```

---

## 3. Scenarios - play the user

Each run writes a folder under `runs/` (`events.jsonl`, `result.json` or `trace.json`,
screenshots). Exit codes: `0` success, `3` business outcome, `4` needs approval, `1` failure.

### A. Discover a capability with the LLM  *(costs ~$0.15)*

You are the **automation engineer**. Give a goal; Claude drives the live app in a visible
browser, one action at a time, and records how.

```bash
python -m cua discover --goal "Look up member 12345 and read their current savings balance" --url http://127.0.0.1:5001/login
```

Watch for: it signs on with **named secrets** (it never sees the password), marks `12345`
as an input, and **extracts** the balance from the screen (it never types the value).
Result: `runs/<time>-discover-.../trace.json`.

### B. Compile the trace into a capability

```bash
$run = (Get-ChildItem runs -Directory -Filter "*-discover-*" | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
```
```bash
python -m cua compile "$run\trace.json" --id member_savings_balance --version 0.9.0 --out runs\demo-capabilities
```

Open `runs\demo-capabilities\member_savings_balance\0.9.0.yaml`: typed inputs/outputs, each
step's **locator bundle** (semantic → relational → structural fallback), outcome rules, the
success checkpoint. It is a **draft**: replay refuses it unattended. (The demo writes to
`runs\` so the reviewed `capabilities\` stay untouched.)

### C. Replay - no LLM

You are the **production system**. The approved capability is
`capabilities\member_savings_balance\0.3.0.yaml`.

```bash
python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345
```
```bash
python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5001 --input member_number=24680
```

Each step prints which locator found it and how confidently; the result carries the typed
`savings_balance`. To replay your own draft from B, add `--allow-draft` (a supervised run).

### D. Business outcomes - legitimate answers, not crashes

```bash
python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5001 --input member_number=99999
```
→ `business_outcome: record_not_found`.

```bash
python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12a45
```
→ `business_outcome: invalid_search_input` (the app's own `ERR-1042`).

### E. Recoverable conditions - handled, the run continues

Turn on a blocking "System Notice" overlay, replay (command from C), then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"interstitial_rate": 1.0}'
```
→ success; the result lists `recovered: system_notice` on the steps where it was dismissed.

Make every page 1.5 s slow, replay, then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"delay_ms": 1500}'
```
→ success, just slower (bounded waits, no fixed sleeps).

### F. Hard failures - stop with a clear error and evidence

Persistent server errors, replay, then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"error_rate": 1.0}'
```
→ `failed: recovery_exhausted` after two bounded reloads; `escalate: true`; the run folder
has `failure-*.jpg` (data blacked out), `failure-*.txt` (screen structure) and
`intervention.json` (the request a human would get).

Session expiry, replay, then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"session_ttl_s": 0}'
```
→ `failed: session_expired`, naming the step.

### G. Human handoff - take over the live session, hand it back

You are the **operator**. Three terminals.

1. Simulate an outage: the `error_rate` command from F.
2. Terminal 2 - replay with handoff enabled (a browser opens and stays open):
   ```bash
   python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345 --handoff
   ```
   It stops with `>> HUMAN NEEDED [int-xxxx]` and waits.
3. Terminal 3 - the operator:
   ```bash
   python -m cua operator list
   ```
   ```bash
   python -m cua operator take int-xxxx
   ```
   End the outage (the reset command), then **in the browser window** click *Member Inquiry*.
   ```bash
   python -m cua operator resume int-xxxx
   ```
4. Terminal 2 continues from the search screen and finishes. The result shows
   `interventions` (who, decision, how long) and `control_history`
   (`automation → awaiting_human → human → automation`); your clicks appear as `actor: human`.

Also try `operator show <id>`, `operator abort <id> --note "why"`, `operator done <id>`. While
you hold control, automation is locked out of the session.

**If it can't be resolved:** `abort`/`reject` (or no answer within `--handoff-timeout`) ends the
run as `failed` with `operator_aborted` / `operator_rejected` / `escalation_timeout`, naming who
and why. **If the browser window is closed by mistake** - by automation's turn or yours - the
run ends as `failed: session_lost` (a structured result, never a crash), the open request is
closed in the inbox, and nothing is retried: a new session would mean signing on again mid-flow.

### H. A second institution - reuse without re-recording

You are **onboarding Riverbend** (same product, different labels and column order). No
discovery, no LLM. You only give its URL; its first screen identifies it as tenant
`riverbend`, and that tenant's override is picked (or created).

1. Probe the unchanged base capability at Riverbend, as a **supervised** run (`--allow-draft`):
   ```bash
   python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5002 --input member_number=12345 --overrides runs\demo-overrides --allow-draft
   ```
   → success, but some steps are found only by weak fallbacks and the screen title differs:
   it degrades gracefully and says so (`low_confidence_steps`, `warnings`). Buttons and the
   result-row link are still found with high confidence by **relational** locators.
   Because it drifted and Riverbend has no override yet, it ends with
   `Tenant override: drift detected. Draft written: ...riverbend.yaml`.
   (Unattended replays - no `--allow-draft` - never write files; they only report drift.
   `python -m cua override suggest ...` does the same on demand, `--force` to redo a draft.)
2. Open `runs\demo-overrides\member_savings_balance\riverbend.yaml`: only Riverbend's
   differences (three field captions, the `Current Bal` column, the screen title), pinned to
   the base's hash, as a **draft**. Review it - especially that `Current Bal` (not `Avail Bal`)
   is the savings balance, since the base found that cell only by position - and set
   `status: approved`. Generation is automatic; activation is a human decision.
3. Replay with it - every step high confidence, no warnings:
   ```bash
   python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5002 --input member_number=12345 --overrides runs\demo-overrides
   ```

(The reviewed override actually used by the catalog is `overrides\member_savings_balance\riverbend.yaml`;
the demo uses `runs\demo-overrides` so it doesn't touch it.)

### I. The bank's AI agent calls the capability  *(costs a few cents per question)*

You are **staff at a credit union** asking their AI assistant. The agent is deployed for one
institution and sees only that institution's approved capabilities, as typed tools.

```bash
python -m cua catalog --base-url http://127.0.0.1:5001
```
```bash
python -m cua agent --base-url http://127.0.0.1:5001 "What is the current savings balance of member 24680?"
```
```bash
python -m cua agent --base-url http://127.0.0.1:5001 "Does member 99999 exist?"
```
```bash
python -m cua agent --base-url http://127.0.0.1:5002 "Look up member 12345's savings balance."
```

The agent decides **what** to call; the call is deterministic replay. It never sees the
app, a browser, a URL or a credential - only the contract and the typed result.

**How it knows which institution it's talking to - from the screen, not the URL.** The URL
only says where to open the app; the tenant registry [`tenants.yaml`](tenants.yaml) holds no
URLs. Before anything is typed, the first screen's **signature** - the institution's name
(weighted most), the page title, and the screen's labels and buttons - is matched against
the registry: a registered institution is used (and its override applied); an unknown one is
**registered automatically** (`auto_registered: true`, for review) so the next run matches it;
an ambiguous screen runs the base capability with a warning rather than guessing. The model
never sees the registry, the URL, or the institution's name. Which hosts may be opened at all
is still limited by `policy.yaml`'s allowlist.

Try it with a bank nobody has registered yet - start a third mock tenant:
```bash
python -m target_app --tenant lakeside --port 5003
```
```bash
python -m cua replay capabilities\member_savings_balance\0.3.0.yaml --base-url http://127.0.0.1:5003 --input member_number=12345
```
→ `Tenant: lakeside_savings_bank - new institution 'Lakeside Savings Bank' registered ...`;
run it again → `screen matches 'lakeside_savings_bank' ... signature 1.00`. Lakeside is a copy of
Acme's configuration (same product, version, title and labels) - the name decides, so it is not
mistaken for Acme. Swapping ports between banks changes nothing either: each is recognised by
its own screen. (This writes to `tenants.yaml`; `git checkout tenants.yaml` undoes it.)

### J. Everything without live services

```bash
python -m pytest tests --basetemp=.pytest_tmp
```

97 tests, ~8 minutes, no API key: a scripted model stands in for the LLM, a scripted operator
for the human, and the mock app runs in-process. `--basetemp` is only needed where the
system temp folder is locked down.


---

## 4. Where things are

| Path | What |
|---|---|
| `cua/discovery/` | LLM agent loop, tools, trace |
| `cua/artifact/` | capability schema, compiler, tenant overrides, override suggestion |
| `cua/replay/` | deterministic replay, locator resolution, result contract |
| `cua/handoff/` + `cua/operator.py` | control state, inbox, human-action recording, operator CLI |
| `cua/catalog.py`, `cua/bank_agent.py` | agent-facing catalog, the bank AI agent |
| `cua/tenants.py` | which institution is on screen: signature matching, auto-registration of new ones |
| `cua/surface/` | the web surface (Playwright) and in-page perception (`describe.js`) |
| `policy.yaml` | allowlist, irreversible actions, secrets by name, redaction |
| `apps/coreline_teller.yaml` | the vendor product's runtime conditions (shared by all capabilities) |
| `capabilities/`, `overrides/`, `tenants.yaml` | reviewed artifacts; the tenant registry (institutions and their screen signatures - no URLs) |
| `target_app/` | the mock legacy bank app (hostile markup, two tenants, fault injection) |
| `evidence/` | recorded runs of every scenario above |

## 5. The mock bank (`target_app/`)

A stand-in for a legacy core-banking teller app - we have no real bank system. Hostile on
purpose: server-rendered pages, table layouts, the work area in an `<iframe>`, no ids or test
ids, field names like `q1`, captions in neighbouring cells instead of `<label>`s, a clickable
`<div>` posing as a button, errors returned as HTTP 200 pages. Three tenants share the code and
differ only by configuration (`target_app/tenants/*.json`): **Acme** (the base), **Riverbend**
(different labels, column order and product version) and **Lakeside** (Acme's exact
configuration under another name - the "unregistered bank"). All data is fictional (SSNs in
the never-issued 900 range).

Fault switches (test harness only - `/__control` is blocked by the automation's policy):

| Key | Effect |
|---|---|
| `interstitial_rate` | chance a page shows a blocking System Notice overlay |
| `delay_ms` | latency on every work page |
| `error_rate` | chance a page returns an Application Error (500) |
| `session_ttl_s` | idle seconds before the session expires |
| `confirm_dialog` | native `confirm()` before posting a new share |

Plus `POST /__control/expire-sessions` and `POST /__control/reset`.
