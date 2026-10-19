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

> Design and trade-offs: [REPORT.md](REPORT.md) · The flows as pictures: [docs/architecture.md](docs/architecture.md)
> · Recorded runs of every scenario, with the artifacts they used: [evidence/](evidence/README.md)

---

## Quick start: discover once, replay without an LLM

Windows PowerShell, from the project folder.

**1. Set up (once)**
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
Create your `.env` from the template. Every run needs it, because replay signs on with the
mock bank's test credentials stored there:
```bash
Copy-Item .env.example .env
```
Then open `.env` and put your Anthropic API key after `ANTHROPIC_API_KEY=`. Get one at
https://console.anthropic.com/. Only discovery (step 3) and the bank agent use the key: replay
and the tests run without it. `.env` is git-ignored, so the key never gets committed.

In every new terminal, `cd` to the project folder and run `.venv\Scripts\Activate.ps1`.

**2. Start the mock bank**, in its own terminal, and leave it running:
```bash
python -m target_app --tenant acme --port 5001
```

**3. Discover: the LLM does the task once** *(about $0.15)*. The LLM drives the app in a visible
browser, one action at a time, and records what it did and why:
```bash
python -m cua discover --goal "Look up member 12345 and read their current savings balance" --url http://127.0.0.1:5001/login
```
It signs on with **named secrets**, so it never sees the password. It marks `12345` as an input,
and it **extracts** the balance from the screen. The result is `runs/<time>-discover-.../trace.json`.

**4. Compile the trace into a capability**
```bash
$run = (Get-ChildItem runs -Directory -Filter "*-discover-*" | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
```
```bash
python -m cua compile "$run\trace.json" --id member_savings_balance --version 0.9.0 --out runs\demo-capabilities
```
Open `runs\demo-capabilities\member_savings_balance\0.9.0.yaml`. It has typed inputs and
outputs, a **locator bundle** for each step (semantic → relational → structural fallbacks),
the outcome rules, and the success checkpoint. It is a **draft**: unattended replay refuses
it until a human sets `status: approved`.

**5. Replay: no LLM, same result every time**. Your draft, as a supervised run:
```bash
python -m cua replay runs\demo-capabilities\member_savings_balance\0.9.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345 --allow-draft
```
The reviewed, approved capability, for another member:
```bash
python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5001 --input member_number=24680
```
Each step prints which locator found it and how confidently. The result carries the typed
`savings_balance`. Every run writes a folder under `runs/` with `events.jsonl`, `result.json`
or `trace.json`, and screenshots with the data masked. Exit codes: `0` success, `3` business
outcome, `4` needs approval, `1` failure.

**No API key, or no live services?** The test suite covers everything above, and more:
```bash
python -m pytest tests --basetemp=.pytest_tmp
```
101 tests in about 9 minutes, with no API key. A scripted model stands in for the LLM, a
scripted operator for the human, and the mock app runs in-process. `--basetemp` is only
needed where the system temp folder is locked down.

---

## More scenarios, case by case

Expand the one you need. Between scenarios, reset any faults you injected:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/reset
```

<details>
<summary><b>Business outcomes</b>: legitimate answers, not crashes</summary>

```bash
python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5001 --input member_number=99999
```
→ `business_outcome: record_not_found`.

```bash
python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12a45
```
→ `business_outcome: invalid_search_input` (the app's own `ERR-1042`).
</details>

<details>
<summary><b>Recoverable conditions</b>: handled, and the run continues</summary>

Turn on a blocking "System Notice" overlay, replay (the approved command from step 5), then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"interstitial_rate": 1.0}'
```
→ success. The result lists `recovered: system_notice` on the steps where it was dismissed.

Make every page 1.5 s slower, replay, then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"delay_ms": 1500}'
```
→ success, just slower: waits are bounded and there are no fixed sleeps.
</details>

<details>
<summary><b>Hard failures</b>: stop with a clear error and evidence</summary>

Persistent server errors, replay, then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"error_rate": 1.0}'
```
→ `failed: recovery_exhausted` after two bounded reloads, with `escalate: true`. The run
folder has `failure-*.jpg` (data blacked out), `failure-*.txt` (the screen's structure) and
`intervention.json` (the request a human would get).

Session expiry, replay, then reset:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"session_ttl_s": 0}'
```
→ `failed: session_expired`, naming the step.
</details>

<details>
<summary><b>Human handoff</b>: take over the live session, then hand it back</summary>

You are the **operator**. You need three terminals.

1. Simulate an outage: the `error_rate` command from *Hard failures*.
2. In terminal 2, replay with handoff enabled. A browser opens and stays open:
   ```bash
   python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345 --handoff
   ```
   It stops with `>> HUMAN NEEDED [int-xxxx]` and waits.
3. In terminal 3, the operator:
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
   (`automation → awaiting_human → human → automation`). Your clicks appear as `actor: human`.

Also try `operator show <id>`, `operator abort <id> --note "why"` and `operator done <id>`.
While you hold control, automation is locked out of the session. The CLI refuses a command
that isn't possible in the current state, such as `approve` on a request that didn't ask for
approval, and lists what is possible.

The operator commands read the inbox under the run's `--runs-dir` (by default `runs\inbox`).
If you replay with `--runs-dir X`, add `--inbox X\inbox` to them.

**If it can't be resolved:** `abort` or `reject`, or no answer within `--handoff-timeout`, ends
the run as `failed` with `operator_aborted`, `operator_rejected` or `escalation_timeout`,
naming who and why. **If the browser window is closed by mistake**, whether during
automation's turn or yours, the run ends as `failed: session_lost`. That's a structured
result, never a crash: the open request is closed in the inbox, and nothing is retried,
because a new session would mean signing on again mid-flow.
</details>

<details>
<summary><b>Approving an irreversible step</b>: a write that a human must confirm</summary>

`open_holiday_share` posts a new sub-account, and its **Confirm** click is classified
irreversible. Without `--handoff`, the replay stops there with `needs_approval` and nothing is
posted:
```bash
python -m cua replay capabilities\open_holiday_share\0.1.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345 --input "share_type=XMAS - Holiday Club" --input initial_deposit=25.00
```
Add `--handoff`, then run `python -m cua operator approve int-xxxx` from another terminal. The
automation clicks Confirm itself and returns the new share suffix and confirmation number.
</details>

<details>
<summary><b>A second institution</b>: reuse without re-recording</summary>

You are **onboarding Riverbend**: the same product, with different labels and column order.
Start it in its own terminal:
```bash
python -m target_app --tenant riverbend --port 5002
```
There's no discovery and no LLM. You only give its URL. Its first screen identifies it as
tenant `riverbend`, and that tenant's override is picked up, or created.

1. Probe the unchanged base capability at Riverbend, as a **supervised** run (`--allow-draft`):
   ```bash
   python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5002 --input member_number=12345 --overrides runs\demo-overrides --allow-draft
   ```
   → success, but some steps are found only by weak fallbacks, and the screen title differs.
   It degrades gracefully and says so (`low_confidence_steps`, `warnings`). Buttons and the
   result-row link are still found with high confidence, by **relational** locators. Because
   it drifted and Riverbend has no override yet, it ends with
   `Tenant override: drift detected. Draft written: ...riverbend.yaml`.
   Unattended replays (without `--allow-draft`) never write files; they only report drift.
   `python -m cua override suggest ...` does the same on demand; add `--force` to redo a draft.
2. Open `runs\demo-overrides\member_savings_balance\riverbend.yaml`. It holds only Riverbend's
   differences (three field captions, the `Current Bal` column, the screen title), pinned to
   the base's hash, as a **draft**. Review it, and set `status: approved`. Check especially
   that `Current Bal`, not `Avail Bal`, is the savings balance, since the base found that cell
   only by position. Generation is automatic; activation is a human decision.
3. Replay with it. Every step is high confidence, with no warnings:
   ```bash
   python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5002 --input member_number=12345 --overrides runs\demo-overrides
   ```

The reviewed override the catalog actually uses is
`overrides\member_savings_balance\riverbend.yaml`. The demo uses `runs\demo-overrides`, so it
leaves that file alone.
</details>

<details>
<summary><b>A bank nobody has registered yet</b>: the institution is recognised from its screen, not its URL</summary>

The URL only says where to open the app. The tenant registry [`tenants.yaml`](tenants.yaml)
holds no URLs. Before anything is typed, the first screen's **signature** is matched against
the registry. The signature is the institution's name (weighted most), the page title, and
the screen's labels and buttons.
- A registered institution is used, and its override applied.
- An unknown one is **registered automatically** (`auto_registered: true`, for review), so
  the next run matches it.
- An ambiguous screen runs the base capability with a warning rather than guessing.

Which hosts may be opened at all is still limited by `policy.yaml`'s allowlist.

Start a third mock tenant and replay against it:
```bash
python -m target_app --tenant lakeside --port 5003
```
```bash
python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5003 --input member_number=12345
```
→ `Tenant: lakeside_savings_bank - new institution 'Lakeside Savings Bank' registered ...`.
Run it again → `screen matches 'lakeside_savings_bank' ... signature 1.00`.

Lakeside is a copy of Acme's configuration (same product, version, title and labels). The
name decides, so it isn't mistaken for Acme. Swapping ports between banks changes nothing
either: each one is recognised by its own screen. This writes to `tenants.yaml`;
`git checkout tenants.yaml` undoes it.
</details>

<details>
<summary><b>The bank's AI agent</b>: calls capabilities by name <i>(a few cents per question)</i></summary>

You are **staff at a credit union** asking their AI assistant. The agent is deployed for one
institution, and sees only that institution's approved capabilities, as typed tools.
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

A write goes through the same approval gate, so the agent can't post anything by itself:
```bash
python -m cua agent --base-url http://127.0.0.1:5001 "Open a Holiday Club sub-account for member 12345 with an initial deposit of 25 dollars."
```
→ the replay stops at Confirm with `needs_approval`, and the agent says it is waiting for a
person to approve.

A request no capability covers is **not approximated** with another tool:
```bash
python -m cua agent --base-url http://127.0.0.1:5001 "What is the membership status of member 12345?"
```
→ the agent calls `report_unsupported`, which logs a `capability_gap` event, and answers
"not supported yet". The CLI prints the `discover` command for that goal, for a human to run
(Quick start step 3). The agent never starts discovery itself, because discovery drives the
live system.

The agent decides **what** to call; the call itself is deterministic replay. The agent never
sees the app, a browser, a URL, an institution's name or a credential: only the contract and
the typed result.
</details>

---

## Where things are

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
| `capabilities/`, `overrides/`, `tenants.yaml` | reviewed artifacts; the tenant registry (institutions and their screen signatures, no URLs) |
| `target_app/` | the mock legacy bank app (hostile markup, three tenants, fault injection) |
| `evidence/` | recorded runs of every scenario above, plus copies of the artifacts they used (`evidence/artifacts/`) |
| `docs/architecture.md` | the flows as diagrams |

<details>
<summary><b>The mock bank</b> (<code>target_app/</code>)</summary>

A stand-in for a legacy core-banking teller app, since we have no real bank system. It is
hostile on purpose:
- server-rendered pages and table layouts, with the work area in an `<iframe>`;
- no ids or test ids, and field names like `q1`;
- captions in neighbouring cells instead of `<label>`s;
- a clickable `<div>` posing as a button;
- errors returned as HTTP 200 pages.

Three tenants share the code and differ only by configuration (`target_app/tenants/*.json`):
- **Acme**, the base;
- **Riverbend**, with different labels, column order and product version;
- **Lakeside**, Acme's exact configuration under another name: the "unregistered bank".

All data is fictional (SSNs in the never-issued 900 range). Sign-on details for looking
around at http://127.0.0.1:5001 are in `target_app/seed/users.json`.

Fault switches are for the test harness only; `/__control` is blocked by the automation's
policy.

| Key | Effect |
|---|---|
| `interstitial_rate` | chance a page shows a blocking System Notice overlay |
| `delay_ms` | latency on every work page |
| `error_rate` | chance a page returns an Application Error (500) |
| `session_ttl_s` | idle seconds before the session expires |
| `confirm_dialog` | native `confirm()` before posting a new share |

Plus `POST /__control/expire-sessions` and `POST /__control/reset`.
</details>
