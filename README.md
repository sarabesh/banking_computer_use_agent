# Banking Computer-Use Agent

Work in progress. This file will grow into the full README (setup, demo path) as the system is built.

## Mock target app (`target_app/`)

A stand-in for a legacy core-banking teller application ("CoreLine Teller"). We were not given
access to a real bank system, so this app reproduces the properties that matter:

- **Hostile markup**: server-rendered pages, table layouts, the work area inside an `<iframe>`,
  no ids or test ids, meaningless field names (`q1`, `f3`), captions in neighbouring cells
  instead of `<label>`s, one action rendered as a clickable `<div>`, and errors returned as
  HTTP 200 pages.
- **Business outcomes**: member not found, validation errors (`ERR-10xx`, `ERR-22xx`),
  a restricted member (`ERR-3301`), and a read-only user denied access (`SEC-403`).
- **Injectable runtime faults**: blocking "System Notice" interstitials, latency,
  application errors (HTTP 500), session expiry (`SEC-440`), and a native `confirm()` dialog
  before an irreversible post.
- **Multiple tenants**: `acme` and `riverbend` run the same product with different labels,
  branding, version and column order. This is the stand-in for cross-tenant reuse.

All data is fictional (SSNs use the never-issued 900 range). Demo users are in
`target_app/seed/users.json`.

### Run it

```bash
pip install -r requirements.txt
python -m target_app --tenant acme --port 5001
```

Then open http://127.0.0.1:5001. A second tenant:

```bash
python -m target_app --tenant riverbend --port 5002
```

### Flows

1. **Look up a member's balance**: sign on → Member Inquiry → search by Member # → Select →
   read the *Regular Savings* balance.
2. **Open a sub-account**: member detail → Open Sub-Account → fill in the form → Continue →
   review screen → Confirm (irreversible).

### Fault injection (test harness only)

`/__control/*` endpoints are for tests and demos and are **not part of the app's UI**. The
automation's allowlist excludes them.

```bash
curl -X POST http://127.0.0.1:5001/__control/faults -H "Content-Type: application/json" -d "{\"interstitial_rate\": 1.0}"
```

| Key | Effect |
|---|---|
| `interstitial_rate` | probability a page shows a blocking System Notice overlay |
| `delay_ms` | latency added to every work page |
| `error_rate` | probability a page returns an Application Error (500) |
| `session_ttl_s` | idle seconds before the session expires |
| `confirm_dialog` | native confirm() before posting a new share |

Also: `POST /__control/expire-sessions` (expire every session now) and `POST /__control/reset`
(reset data and faults). The same settings are available as CLI flags (`--interstitial-rate`,
`--delay-ms`, `--error-rate`, `--session-ttl`, `--no-confirm-dialog`, `--seed`).

### Tests

```bash
python -m pytest tests
```
