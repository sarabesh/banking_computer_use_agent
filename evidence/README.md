# Evidence

Every run below was produced by hand from the CLI against the mock legacy bank
(`target_app/`), in order, on 2026-10-19. Each numbered folder is one scenario; inside it is
one folder per run (`<timestamp>-<kind>-<slug>/`) and, for runs that involved a human, the
`inbox/` the automation and the operator used to talk.

Three institutions run the same mock product with different labels and layouts:

| URL | Institution | Tenant id |
|---|---|---|
| `http://127.0.0.1:5001` | Acme Community Credit Union | `acme` |
| `http://127.0.0.1:5002` | Riverbend Federal Credit Union | `riverbend` |
| `http://127.0.0.1:5003` | Lakeside Savings Bank (unknown at first) | `lakeside_savings_bank`, auto-registered |

## The saved artifacts (`artifacts/`)

Copies of the artifacts these runs produced and used, byte-identical to the live ones in
`capabilities/` and `overrides/`:

| File | Produced by | Used by | `content_hash` |
|---|---|---|---|
| `member_savings_balance-0.4.0.yaml` | discovery 01 → compile → approved | replays 02–10, 14–18, agent 19–21 | `sha256:b549e39a…` |
| `open_holiday_share-0.1.0.yaml` | discovery 11 → compile → approved | replays 12–13, agent 22 | `sha256:2cb8c080…` |
| `member_savings_balance-riverbend-override.yaml` | suggested in 16 → reviewed → approved | Riverbend replays 17, 21 | `sha256:1efc208e…` |

Each run's `replay_started` event and `result.json` carry the same hash, so you can check which
exact artifact a run executed.

## What is in a run folder

| File | Written by | What it is |
|---|---|---|
| `events.jsonl` | every run | Append-only event log (redacted for logs): each observation, model turn, step, recovery, policy verdict, handoff and result. |
| `trace.json` | discovery | What the LLM did, step by step, with element fingerprints and reasons. The compiler's input. |
| `step-NN-obsN.jpg` | discovery | The screen at each step, with data blacked out. |
| `result.json` | replay | The typed result: status, outputs (redacted), per-step locator strategy and confidence, warnings, error with step / expected / observed, interventions and control history. |
| `failure-<step>.jpg` / `.txt` | replay, on a stop | Screenshot with data masked, and the screen's structure (no data). |
| `intervention.json`, `intervention-N.jpg` | escalations | The request a human gets: trigger, reason, step, recent steps, allowed actions. |
| `inbox/<id>.request.json`, `.status.json`, `.responses.jsonl` | handoff | The request, the live control state, and every operator command (who and when). |

Nothing here contains a password, an API key or a member's personal data: secrets are typed
by name (`type_secret`) and never logged, money values and the member's name are redacted
(`[REDACTED:money]`), and screenshots are masked when they are taken. A scan of every text file
for the `.env` secrets and the 24 PII values in the seed data finds none.

## Summary

| # | Scenario | Result | Shows |
|---|---|---|---|
| 01 | Discovery: savings balance | trace, 7 steps | LLM discovery → trace |
| 02 | Replay, member 12345 | `success` | Deterministic replay, no LLM |
| 03 | Replay, member 24680 | `success` | Same artifact, different input |
| 04 | Replay, member 99999 | `business_outcome: record_not_found` | Business outcome ≠ error |
| 05 | Replay, member `12a45` | `business_outcome: invalid_search_input` | App validation surfaced as an outcome |
| 06 | System Notice overlay on every page | `success`, 3 recoveries | Recoverable condition handled |
| 07 | Every page 1.5 s slow | `success` | Bounded waits, no sleeps |
| 08 | Persistent server error | `failed: recovery_exhausted` | Bounded recovery, clear failure |
| 09 | Session expired | `failed: session_expired` | Unsafe-to-retry condition stops |
| 10 | Server error, `--handoff` | `success` after a human took over | Live-session handoff and resume |
| 11 | Discovery: open a sub-account | trace, 13 steps, approved by human | Discovery of a write, approval gate |
| 12 | Replay the write, no handoff | `needs_approval` | Irreversible step never auto-runs |
| 13 | Replay the write, `--handoff` | `success` after approval | Human approves, automation posts |
| 14 | Riverbend, base capability only | `success`, low confidence + warning | Graceful degradation, drift detected |
| 15 | Riverbend, override for an old base | `failed: override_invalid` | Overrides pinned to a base hash |
| 16 | Riverbend, supervised suggest | draft override written | Override generated automatically |
| 17 | Riverbend, approved override | `success`, all high confidence | Cross-tenant reuse (stretch #5) |
| 18 | Lakeside, never seen before (×2) | registered, then matched | Tenant identified from the screen |
| 19 | Agent: balance of 24680 | answered via tool | Agent-facing catalog (stretch #1) |
| 20 | Agent: does 99999 exist? | "doesn't exist" | Business outcome relayed by the agent |
| 21 | Agent deployed for Riverbend | answered via override | Agent unaware of tenants |
| 22 | Agent: open a sub-account | "waiting for approval" | Agent can't bypass the approval gate |
| 23 | Agent: membership status | "not supported yet" → discovery | Capability gap, back into discovery |

---

## Discovery and the capability artifact

### 01 — Discovery of a read flow
```bash
python -m cua discover --goal "Look up member 12345 and read their current savings balance" --url http://127.0.0.1:5001/login --runs-dir evidence\01-discovery-lookup
```
The LLM got only the goal and the URL. It identified the institution from the sign-on screen
(`acme`, signature 1.00), signed on with the named secrets, searched Member Inquiry, opened the
member and extracted the Regular Savings balance: **7 steps, 8 model turns, no human**. The
trace was then compiled and reviewed:
```bash
python -m cua compile evidence\01-discovery-lookup\<run>\trace.json --id member_savings_balance --version 0.4.0
```
→ `capabilities/member_savings_balance/0.4.0.yaml` (draft), approved by setting
`status: approved`. Every later lookup replay uses this file.

### 11 — Discovery of a write flow, with an approval
```bash
python -m cua discover --goal "Open a new Holiday Club sub-account for member 12345 with an initial deposit of 25 dollars, and post it" --url http://127.0.0.1:5001/login --handoff --runs-dir evidence\11-discovery-open-share
```
The LLM filled the form and reached the review screen. Before pressing **Confirm**, which the
policy classes as irreversible, the run paused (`intervention_requested`, trigger
`approval_needed`). The operator approved from a second terminal:
```bash
python -m cua operator approve int-2a6f2622 --inbox evidence\11-discovery-open-share\inbox
```
The automation then clicked Confirm itself (`approved_action`, `approved_by: sarab`) and extracted the new share suffix and confirmation number:
**13 steps, 1 intervention**. Compiled as `capabilities/open_holiday_share/0.1.0.yaml`. The
compiler turned the share type and amount into typed inputs (`share_type` enum,
`initial_deposit` decimal) and marked Confirm `requires_approval`.

---

## Deterministic replay (no LLM)

All replays below run `capabilities\member_savings_balance\0.4.0.yaml` unless noted, e.g.:
```bash
python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345 --runs-dir evidence\02-replay-success
```

### 02, 03 — Success
Member 12345 and member 24680: all 7 steps found with high confidence (`caption`, `role`,
`table_cell` locators), the success checkpoint passed, `savings_balance` returned (redacted in
the log). About 9 s each, no model calls.

### 04, 05 — Business outcomes
Member `99999` ends as `business_outcome: record_not_found`, and `12a45` as
`business_outcome: invalid_search_input`, after 4 of the steps. These are answers, not errors: the
outcome rules in `apps/coreline_teller.yaml` recognise the app's own messages
(`ERR-1404`, `ERR-1042`), and nothing escalates.

---

## Error handling

Faults are switched on in the mock app before each run and reset afterwards, e.g.:
```bash
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5001/__control/faults -ContentType 'application/json' -Body '{"interstitial_rate": 1.0}'
```

### 06 — Recoverable: a blocking System Notice (`interstitial_rate: 1.0`)
An overlay covers the page after sign-on, search and select. The `system_notice` outcome rule
is recoverable: the replay dismissed it each time (`recovered` × 3, on `click_sign_on`,
`click_go`, `click_select`) and finished with `success`. Dismissing it is not a step in the
capability: the outcome rule handles it wherever it appears, which is also why the compiler
drops such clicks if a discovery run happens to record them.

### 07 — Slow application (`delay_ms: 1500`)
Every page takes 1.5 s longer. The same 7 steps pass, in 13.4 s instead of about 9 s. Waits are
bounded and condition-based (in-flight navigation is tracked), so there are no fixed sleeps and
no retries.

### 08 — Hard failure: persistent server error (`error_rate: 1.0`)
After sign-on the app shows *Server Error in '/CoreLine' Application*. The `app_error` rule
allows 2 reloads (`recovered` × 2). The third time the run stops: `failed`, error kind
`recovery_exhausted`, step `click_sign_on`, observed text quoted, `escalate: true`.
`failure-click_sign_on.jpg/.txt` and `intervention.json` show what a human would be handed.

### 09 — Hard failure: session expired (`session_ttl_s: 0`)
The app answers `SEC-440 Your session has timed out`. This is a failure outcome. The replay does **not**
sign on again by itself, because re-running sign-on in the middle of a flow is not known to be
safe. It stops with `failed: session_expired` and names the step.

---

## Human escalation and handoff

### 10 — A human takes over the live session and hands it back
Same outage as 08, but with `--handoff`:
```bash
python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345 --handoff --runs-dir evidence\10-handoff-human
```
After the 2 reloads failed, the run paused instead of failing (`intervention_requested`,
trigger `unrecoverable`) and kept the browser open. The operator ran `operator take`, ended
the outage, clicked **Member Inquiry** in the *same* window, and ran `operator resume`. The
human's click is in the result as a step with `actor: human` ("clicked link 'Member
Inquiry'"). Automation resynchronised on the search screen (`resumed_at: fill_member_number`)
and finished with `success`. `control_history` shows
`automation → awaiting_human → human → automation`, and the intervention records the operator,
the decision `resumed` and the wait time (170 s).

### 12 — The write capability without an operator
```bash
python -m cua replay capabilities\open_holiday_share\0.1.0.yaml --base-url http://127.0.0.1:5001 --input member_number=12345 --input "share_type=XMAS - Holiday Club" --input initial_deposit=25.00 --runs-dir evidence\12-write-needs-approval
```
10 steps run (sign on, search, open the form, fill it, review), then it stops at `click_confirm`
with `needs_approval` (`policy_blocked: irreversible step requires a human decision`).
Nothing is posted.

### 13 — The write capability, approved by a human
The same command with `--handoff`. At Confirm the run paused (trigger `approval_needed`). The
operator approved (`operator approve int-c3d48bb7`), the automation clicked Confirm, and the
sub-account was opened: `success`, `new_share_suffix` and `confirmation_number` returned.

The two extract steps are flagged `low_confidence` (found by `css`), because the confirmation
page puts its values in bare text with no label to anchor them.

---

## Heterogeneity and multi-tenant reuse (stretch goal 5)

Riverbend runs the same product with different field captions (`Operator:`,
`PIN / Password:`, `Account Number:`), columns in a different order, and a different title on
the member screen.

### 14 — The Acme-discovered base, unchanged, at Riverbend
```bash
python -m cua replay capabilities\member_savings_balance\0.4.0.yaml --base-url http://127.0.0.1:5002 --input member_number=12345 --runs-dir evidence\14-riverbend-base-only
```
The first screen identifies `riverbend`, and its signature is recorded. All 7 steps still pass, through the
fallback layers of each locator bundle: the captions don't match, so fields are found by
`field_name` (medium), buttons and the result row by the relational `form_submit` and
`row_control` locators (high), and the balance cell only by `css` (low). The result is `success`, but
it says so: `low_confidence_steps: [extract_savings_balance]` and the warning that the final
screen is not titled *Member Summary*. It degrades gracefully and reports the drift.

### 15 — An override reviewed against an older base is refused
Riverbend's override was still pinned to base 0.3.0 when the base moved to 0.4.0:
`failed: override_invalid`, "reviewed against base 0.3.0 (sha256:dc95…), not this base 0.4.0
(sha256:b549…); re-run `cua override suggest` and re-review". The replay never starts.
An override can't silently apply to a base it wasn't reviewed against.

### 16 — The override suggested automatically
A supervised replay at Riverbend (`override suggest`, or `--allow-draft`) records which steps
needed fallbacks. It writes a **draft**
`overrides/member_savings_balance/riverbend.yaml` containing only Riverbend's differences: the
three captions, the `Current Bal` cell by column and row key, and the screen title *Account
Holder Detail*. The draft is pinned to base 0.4.0's hash, with a note for each change. A human reviewed it and set
`status: approved`. Generation is automatic; activation is a human decision.

### 17 — Riverbend with its approved override
Same command as 14. `tenant: riverbend`, override `sha256:1efc…` applied. All 7 steps are
high confidence (`caption`, `form_submit`, `row_control`, `table_cell`), with no warnings. One base
capability, discovered once at Acme, now serves both banks.

### 18 — A bank never seen before
Two replays against `http://127.0.0.1:5003`. In the first, no registered signature matches (the closest
was `acme` at 0.58, below the 0.75 threshold), so the institution on screen is **registered** as
`lakeside_savings_bank` with `auto_registered: true` for review, and the run succeeds on the
base. In the second, the screen **matches** the new entry with signature 1.00. The registry stores
no URLs; the URL only says where to look. See `tenants.yaml`.

---

## The agent-facing catalog (stretch goal 1)

The bank's AI agent is deployed for one URL. It sees only approved capabilities, as typed tools
with no tenant argument, plus `report_unsupported`. It never sees URLs, the tenant registry,
credentials or screenshots. Each folder holds the agent's run and the replay run it caused.

### 19 — A read question
```bash
python -m cua agent --base-url http://127.0.0.1:5001 "What is the current savings balance of member 24680?" --runs-dir evidence\19-agent-balance
```
The LLM called `member_savings_balance(member_number=24680)`. The replay returned `success`, and
the agent answered with the balance (redacted in its log).

### 20 — A business outcome
"Does member 99999 exist?" → the tool returned `business_outcome: record_not_found`, and the
agent answered that the member doesn't exist, treating the outcome as an answer rather than an error.

### 21 — The same agent, deployed for Riverbend
`--base-url http://127.0.0.1:5002`. The agent and its tools are identical. The replay
identified `riverbend` from the screen and applied its approved override (`sha256:1efc…`),
without the agent knowing which bank it is.

### 22 — A write request through the agent
```bash
python -m cua agent --base-url http://127.0.0.1:5001 "Open a Holiday Club sub-account for member 12345 with an initial deposit of 25 dollars." --runs-dir evidence\22-agent-write-needs-approval
```
The LLM chose `open_holiday_share` with typed arguments (`share_type` from the enum). The replay
filled the form and stopped at Confirm: `needs_approval`. The agent told the user that a person must
approve the step and that no confirmation number exists yet. The agent cannot get past the
approval gate.

### 23 — A request no capability covers, and its discovery
```bash
python -m cua agent --base-url http://127.0.0.1:5001 "What is the membership status of member 12345?" --runs-dir evidence\23-agent-unsupported
```
The LLM saw that no tool returns membership status and called `report_unsupported` with the goal
"Look up the membership status of member 12345." It ran no capability. A `capability_gap`
event was logged, and the agent answered that the request is not supported yet. The CLI printed the
discovery command for that goal. A human then ran it (second run in the same folder):
```bash
python -m cua discover --goal "Look up the membership status of member 12345." --url http://127.0.0.1:5001/login
```
→ `success` in 6 steps: signed on, searched by member number, and read the status from the
results row. From here it follows the same path as 01: compile, review, approve, and it appears in the
catalog. The agent never starts discovery itself, because discovery drives the live system.
