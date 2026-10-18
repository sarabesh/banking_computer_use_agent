# Evidence

End-to-end evidence for the brief's required thread: **goal → LLM-driven discovery → saved
capability → deterministic replay (inputs, outputs, error handling) → human handoff of the
live session**, plus replays that hit errors and exceptional states.

All data is fictional (mock credit-union app). Everything here is **redacted at write time**:
credentials, SSNs, dates of birth, balances and extracted outputs are masked in logs, and
evidence screenshots / failure snapshots keep the screen's *structure* (labels, headers,
buttons) but black out every *data value* — names and addresses have no regex, so the
surface's own knowledge of "this text is a value" is used instead.

## The capability

[`capability/member_savings_balance-0.2.0.yaml`](capability/member_savings_balance-0.2.0.yaml)
— compiled from run 00 by `python -m cua compile`. Typed input `member_number`, typed
output `savings_balance` (decimal, sensitive), two secrets by name, 7 steps with ordered
locator bundles, per-step postconditions, the outcome rules for the screens it visits, a
success checkpoint, provenance and a content hash.

## Runs

| # | Folder | Kind | Result | Demonstrates |
|---|---|---|---|---|
| 00 | `00-discovery-llm-run/` | **Real LLM discovery** (claude-opus-5-5) | success | The agent signs on (credentials by name only), searches, opens the member, extracts the balance. `events.jsonl` has every decision with its reason and the API request ids. |
| 01 | `01-replay-success/` | Replay, no LLM | success | All 7 steps resolved by their primary (high-confidence) locator; typed output returned |
| 02 | `02-replay-success-other-member/` | Replay | success | Same capability, different input (`24680`) |
| 03 | `03-business-outcome-not-found/` | Replay | `business_outcome: record_not_found` | A legitimate negative answer, not a crash, not an escalation |
| 04 | `04-business-outcome-invalid-input/` | Replay | `business_outcome: invalid_search_input` | The app's own validation error (`ERR-1042`) mapped to a business outcome |
| 05 | `05-recovered-system-notice/` | Replay, injected interstitial | success | Blocking notice detected and dismissed (recoverable), run continues |
| 06 | `06-recovered-slow-app/` | Replay, 1.5 s latency | success | Bounded, condition-based waiting (no fixed sleeps) |
| 07 | `07-failure-app-error-escalated/` | Replay, persistent app error | `failed: recovery_exhausted` | Two bounded reloads, then a hard failure with step/expected/observed, a redacted screenshot + screen snapshot, and an `intervention.json` returned to the caller (unattended mode) |
| 08 | `08-failure-session-expired/` | Replay, session expiry | `failed: session_expired` | Hard failure identified by the app-profile rule, failing step named |
| 09 | `09-drift-other-tenant/` | Replay on tenant **Riverbend** | `failed: success_check_failed` | Same vendor product, different labels/column order: fallback locators (`field_name`, `href`, `css`) carry the flow and are reported as low confidence; the success checkpoint refuses to call it a success. Drift is detected, never silently accepted. |
| 10 | `10-handoff-session-expired/` | Replay + **handoff** (simulated operator) | success | Session dies mid-run → intervention request → operator takes the **same live browser**, signs in again → hands back → replay re-enters at the search screen and finishes. `inbox/` holds the request, status and the operator's commands. |
| 11 | `11-handoff-manual-real-operator/` | Replay + **handoff by a real person** | success | Persistent app error → request → a human operator took control via `python -m cua operator take`, clicked *Member Inquiry* in the live window, and resumed after 108 s. Recorded on capability v0.1.0 (identical steps, compiled before v0.2.0). |

`scenarios.json` summarises runs 01–10.

## Reading a run folder

| File | Contents |
|---|---|
| `events.jsonl` | One event per line: observations, model turns (discovery), steps, outcomes detected, recoveries, `control_transferred`, `human_action`, errors |
| `trace.json` | (discovery) the structured trace the capability is compiled from — not the transcript |
| `result.json` | (replay) the structured result contract: status, outcome, outputs (redacted), error with step / expected / observed, interventions, control history, per-step strategy and confidence |
| `step-*.jpg` | (discovery) a redacted screenshot after every action |
| `failure-*.jpg`, `failure-*.txt` | the screen at a failure: redacted screenshot and structure-only element snapshot |
| `intervention*.json`, `inbox/` | the intervention request and, for handoffs, the operator's responses |

## Reproducing

Runs 01–10 are regenerated deterministically (no LLM, no API key needed beyond the mock's
test credentials in `.env`):

```bash
python scripts/make_evidence.py capabilities/member_savings_balance/0.2.0.yaml
```

Run 00 needs a model key (`python -m cua discover ...`, see the top-level README); 11 needs a person.
