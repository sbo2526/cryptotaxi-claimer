# Crypto Taxi — protected, opt-in read-only monitor

## Safety defaults

- **All website traffic is OFF by default.** The supervisor starts only the dashboard.
- Registration, fake referrals, disposable-email creation, proxy rotation, trip
  starts, rentals and reward claims have been removed. There is no environment
  switch to re-enable mutation endpoints.
- The only external endpoints implemented are `auth/login` and `state`. Reading
  a website/API may also require permission: obtain that permission before opt-in.
- Only `PRIMARY_ACCOUNT`, explicitly set in the environment, may authenticate.
  No account credentials are loaded from JSON or selected from old status data.
- The monitor never automatically restarts after suspension, access denial,
  network failure, malformed state or loss of lock ownership.
- Shared PostgreSQL ownership is required BEFORE any external request. All
  deployments for the primary account must use the **same database**.
- No hidden transport retries, redirects, proxy switching or repeated login.
  Requests are paced at least 60 seconds apart; state polling is every 5 minutes
  after each completed poll. These are conservative defaults, NOT a guarantee of
  site approval or immunity from suspension.
- 429/503 respect numeric or HTTP-date `Retry-After` with at least a 5-minute
  cooldown. The same request is not immediately reissued. A rate-limited login
  stops for manual review. Generic 401/403 stop the worker, but are NOT recorded
  as an account ban. Explicit suspension stops the client immediately, closes the
  session and records a persistent suspension marker in the shared database and
  local archive. Future starts refuse it, even after a container is replaced.
  The request budget and server cooldown are also stored in the shared database.

## Railway setup (secrets go in Variables, NEVER chat or Git)

### Required for the dashboard

| Variable | Purpose |
|---|---|
| `DASHBOARD_USER` | Your private dashboard login name |
| `DASHBOARD_PASSWORD` | Unique randomly generated password, at least 16 characters |
| `PRIMARY_ACCOUNT` | Exact username/email of your ONE primary website account |
| `DATA_DIR` | Writable runtime directory, default `/app/data` in Docker |

The dashboard responds with **503 until its login is configured**; this is
intentional, rather than exposing account information publicly. Use HTTPS for
Basic authentication. `/healthz` is an unauthenticated static liveness endpoint;
set Railway's healthcheck path to `/healthz` if a healthcheck is configured. It
reveals no account, credential or monitor state.

Attach a Railway volume at `/app/data` (writable by UID 10001) if you need state
and suspension markers to survive redeployments. Nothing in the repository's
old root-level JSON files is automatically imported into the new application.

### Dashboard display

The top row separates monitoring configuration, the selected primary identity,
and the **last successful saved receive** (Tehran time). Refreshing the page does
not change that timestamp. Enabling the opt-in flag alone is not proof that a
monitor process is running; samples older than ten minutes are labeled stale.

Without a valid primary snapshot, the dashboard shows an explicit Persian empty
state instead of zero balance/trip cards. Wallet and car information appear only
from a valid saved snapshot; unknown balance is a dash, not a fabricated zero.
The creator panel and old reward/claim counters have been removed. Suspension
history and local persisted events are collapsed by default; the browser remembers
each panel's open/closed state when storage is available. Events summarize saved
receives and confirmed suspension records, not a complete execution log. Runtime
logs remain in Railway. Rendering and the 30-second page refresh never make site
requests or enable monitoring.

### Optional read-only monitoring — ONLY after API permission is confirmed

| Variable | Purpose |
|---|---|
| `SITE_MONITORING_ENABLED` | Must be exactly `true` (case-insensitive); absent means OFF |
| `PRIMARY_ACCOUNT_PASSWORD` | Website password, from Railway's secret Variables |
| `MONITOR_LOCK_DATABASE_URL` | PostgreSQL connection URL shared by every deployment for this account |

The database user needs permission to create/use the small
`cryptotaxi_monitor_safety` table (hashed monitor key, suspension flag and next
request time; no account passwords). Use a direct PostgreSQL connection or a
**session-mode** pooler; transaction-mode poolers cannot safely support session
advisory locks. Configure TLS for a remote
public database connection. A lock/configuration failure blocks website access;
the monitor never runs unlocked. Losing the database connection stops it before
its next website request; it never reconnects silently. An already in-flight
read cannot be recalled. This lock does not control older app versions or
clients outside this application.

Do **not** enable monitoring on another project with a different lock database.
Use Railway's UI to stop/delete the spare service and set the retained service
to one replica. Also stop old local/VPS/systemd processes and scheduled jobs.
Code changes alone cannot delete Railway services or stop old binaries elsewhere.
The new release makes NO site calls on any upgraded service unless explicitly
opted in, even if two dashboards are deployed.

After a manual stop/failure, investigate first. If appropriate, manually restart
or redeploy the ONE retained service. Do not remove a confirmed suspension marker
or restart a suspended account unless support has resolved it.

## Credential migration and rotation

- `accounts.json`, email configuration, proxy lists and runtime JSON are no longer
  tracked or copied into Docker. Legacy local files are left in place, ignored;
  the new runtime **does not read them**. There is no bulk-account import.
- The Docker image uses an explicit source allowlist, excludes `.git`, `.env`,
  data and backups, and runs as a non-root user.
- Removing tracked files **does not remove them from Git history**, prior images,
  old deployment artifacts, clones or backups. Rotate the website password,
  exposed email passwords and any reused credentials. Revoke active sessions
  where the provider supports it. This must be done in the provider's own UI.
- Coordinated history cleanup is a separate administrative operation; this
  change does not rewrite Git history or force-push other branches.
- No passwords, cookies, tokens, raw login payloads or DSNs are persisted by the
  monitor. Only whitelisted state fields and redacted suspension reasons are saved.

## Running locally

Use environment variables from your own secret store, then run:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python supervisor.py
```

The supervisor stays in the foreground and terminates its children on shutdown.
Do not run a second supervisor. The legacy auto-install scripts are disabled;
they previously copied credentials and enabled additional unattended services.
The dashboard binds `0.0.0.0` using Railway's `PORT` (default 8080).

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use mocks/temporary state and local HTTP only; they do not access account
APIs. Shared-lock unit tests simulate PostgreSQL ownership; a production shared
PostgreSQL connection and Railway replica/volume configuration must still be
verified by the deployment administrator. Automated trading/claiming code and its
old execution tests have been replaced with read-only policy/security tests.
