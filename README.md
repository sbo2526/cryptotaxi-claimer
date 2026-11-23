# Crypto Taxi — primary-account monitor with a direct-access dashboard

## Safety defaults

- **All website traffic is OFF by default.** The supervisor starts only the dashboard.
- Registration, fake referrals, disposable-email creation, proxy rotation and
  reward claims remain removed. New rentals have only an inactive local preview;
  market reads, rental execution and payments remain unavailable.
- Monitoring alone permits only `auth/login` and `state`. The separately opted-in
  `AUTO_START_TRIPS_ENABLED=true` adds only the guarded `trips/start` path. Reading
  or automating a website/API may require permission: obtain that permission
  before enabling either mode.
- The owner-selected sole permitted identity is `bavafasobhan@gmail.com`.
  `PRIMARY_ACCOUNT` must explicitly match that identity (surrounding whitespace
  is trimmed; the identity otherwise matches exactly). An unset or different
  value selects **no account** and prevents login, even when monitoring is opted
  in. Changing Variables alone cannot enable another identity. The public identity
  is pinned in `runtime_config.py`; passwords remain exclusively in Variables.
  No account credentials are loaded from JSON or selected from old status data.
- The monitor never automatically restarts after suspension, access denial,
  network failure, malformed state or loss of lock ownership.
- Shared PostgreSQL ownership is required BEFORE any external request. All
  deployments for the primary account must use the **same database**.
  During initial worker startup only, an occupied lock is rechecked every ten
  seconds on the same connection for up to 120 seconds, so an overlapping old
  Railway deployment can shut down normally. No login, site request or shared
  status write occurs while waiting. There is no forced unlock, process kill,
  database reconnect or automatic worker restart. A still-occupied lock stops
  with `already_running`; connection errors and invalid results stop immediately.
  Once acquired, a lost lock is never reacquired by that worker.
- No hidden transport retries, redirects, proxy switching or repeated login.
  Each worker makes **at most one login attempt** and reuses the same in-memory
  `requests.Session` (including server cookies) for all reads. Session expiry does
  not trigger an automatic login. Sessions are not saved to disk: an explicitly
  started new worker needs a new login, subject to the shared request budget.
- Requests remain paced at least 60 seconds apart. After a successful shared
  database budget reservation, the local pacer's next slot is aligned to that
  completed reservation (never moved earlier). This prevents database/IPC latency
  from creating a small residual budget delay on every subsequent start or read.
  A pre-send start-budget deferral follows the existing bounded cooldown path and
  fetches fresh state before reconsidering a start; it is not a generic failure.
  A deferred or failed verification **after** a start was sent instead stops with
  `start_unknown` and retains the durable journal; no mutation is replayed.
  Subsequent state reads use
  the nearest saved trip end plus 15 seconds, with a minimum 5-minute gap and a
  maximum 6-hour periodic check. With no active trips or an invalid/unknown timer,
  the fallback is 30 minutes (or an earlier known trip). Only numeric millisecond
  timestamps are used. Expired timers cause a delayed **state read**, never an
  assumed completion or automatic claim. When trip starts are separately enabled,
  only a freshly verified eligible idle car may start. Timers in the dashboard
  count down locally without site requests. These are conservative application
  defaults, NOT the site's published limits, permission or a guarantee against bans.
- 429/503 respect numeric or HTTP-date `Retry-After` with at least a 5-minute
  cooldown, also persisted in the shared database. The worker makes at most two
  delayed read retries and stops on the third consecutive cooldown; a successful
  read resets that counter. The same request is not immediately reissued. A
  deferred/rate-limited login stops without retry. Generic 401/403 stop the worker
  for manual session/access review, but are NOT recorded as an account ban.
  Network failures, redirects, unexpected API errors and malformed state stop
  without retry or re-login; a previous good snapshot is retained as historical
  data. Explicit suspension stops the client immediately, closes the
  session and records a persistent suspension marker in the shared database and
  local archive. Future starts refuse it, even after a container is replaced.
  The request budget and server cooldown are also stored in the shared database.

## Railway setup (secrets go in Variables, NEVER chat or Git)

### Dashboard access — public by owner request

The dashboard now opens directly by URL **without a username or password by
default**. `/`, `/index.html` and `/count` are readable without authentication.
**Anyone who knows or discovers the URL can view the displayed account identity,
balance, taxis, diagnostics and history.** The address is not a secret access key.
The page explicitly labels public mode, keeps `Cache-Control: no-store`, and sends
`X-Robots-Tag: noindex, nofollow` and `Referrer-Policy: no-referrer`; these discourage
indexing/link leakage but do not make public information private.

| Variable | Purpose |
|---|---|
| `DASHBOARD_PUBLIC` | Defaults to `true` for direct access. Set `false` to restore private Basic authentication. |
| `DASHBOARD_USER` | Required only in private mode: dashboard login name |
| `DASHBOARD_PASSWORD` | Required only in private mode: unique password of at least 16 characters |
| `PRIMARY_ACCOUNT` | Must be exactly `bavafasobhan@gmail.com`; any other identity is rejected |
| `DATA_DIR` | Writable runtime directory, default `/app/data` in Docker |

Existing `DASHBOARD_USER`/`DASHBOARD_PASSWORD` values do not protect the dashboard
while public mode is selected (including the new default). To restore protection,
set `DASHBOARD_PUBLIC=false` with valid credentials and apply/redeploy. Invalid or
empty explicit public-mode values fall back to private mode, not public access.
In private mode, missing/weak credentials return **503** and absent/invalid Basic
auth returns **401**. Use HTTPS for Basic authentication.

This only changes dashboard viewing: all POSTs and game-action routes remain
unavailable, new rentals stay disabled, and game credentials, monitoring opt-in,
shared ownership and trip-start policy are unchanged. Public viewing cannot enable
monitoring or start a taxi. `/healthz` remains an unauthenticated static liveness
endpoint with no account, credential or monitor state; use it for Railway healthchecks.

Attach a Railway volume at `/app/data` (writable by UID 10001) if you need state
and suspension markers to survive redeployments. Nothing in the repository's
old root-level JSON files is automatically imported into the new application.

Selecting the sole permitted identity does not delete accounts on the website or
edit Railway Variables/volumes on your behalf. Remove obsolete secondary-account
secrets in Railway's UI if any remain, and set `PRIMARY_ACCOUNT` to the value above.
Do not paste passwords into chat or source. Historical suspension records are
retained as safety evidence, not as an active account list; this identity
restriction does not erase a suspension marker or allow a suspended account to run.

### Dashboard display

The dashboard uses a default dark palette (including cards, tables, disclosures,
status messages and native browser controls), without JavaScript/theme storage or
external assets. Persian text prefers the **locally installed IRANSans** regular
and bold faces through an Arabic-only `@font-face` definition; Latin identifiers
keep their existing fonts. Labels use larger sizes for readability. **No IRANSans
font binary is included**: devices without it fall back to Tahoma/Arial. To ensure
the exact face on every phone, a font file licensed for web embedding must first
be supplied and integrated; naming a font in CSS alone cannot install it remotely.
There are no font-CDN requests and no change to authentication or monitoring.

The Persian RTL overview starts with a plain-language status and suggested next
step. Three summary cards separate configured automatic starts, the **last
successful saved receive** (Tehran time), and the planned next check. The primary
identity stays in the header. Refreshing the page does not change the receive
timestamp. Enabling the opt-in flag alone is not proof that a monitor process is
running; recorded stops take precedence over a fresh sample, and samples older
than ten minutes are labeled stale.

Wallet, fleet count, recorded trips and reported readiness precede individual taxi
cards, with personal taxis first. All/personal/rented filters run only in the
browser and retain their selection across refreshes when storage is available.
Each card explicitly distinguishes a recorded started trip from a car without an
active trip in the last receive. Recorded trips have a large HH:MM:SS countdown,
updated every second, with a server-rendered fallback and an explicit awaiting-new-
data message after expiry (not an automatic claim of trip completion). A globally
unknown start outcome is not mislabeled as not started. The worker saves a safe
`start_check` reason from the same eligibility rule it uses before starting; idle
cards explain missing permission/capacity, unknown old data, disabled automation,
stale samples or a recorded service stop rather than promising a start. Personal
cars retain priority; existing eligible rentals can also start **while the main
car is on an ordinary or extra trip**. An active main trip is not an account-wide
start lock. Multiple existing rentals may overlap with it, at most one new start
per cycle with the existing pacing and a fresh state/confirmation for every start.
The worker does not wait for the main trip to end when a rental is eligible; the
next eligible car is considered after at least five minutes. This means overlapping
trips, not simultaneous HTTP requests. Turn/permission/capacity restrictions and
unresolved-journal stops still apply. Renting new cars remains disabled.
Each card keeps quota details and the original site block message in a disclosure;
the known Spanish not-your-turn message also has a Persian summary. Technical
status and configuration guidance live in a separate collapsed panel, opened by
the help links. No dashboard button starts a trip or changes service settings.
The layout adapts to phones without external fonts, images or script dependencies.

Without a valid primary snapshot, the dashboard shows an explicit Persian empty
state instead of zero balance/trip cards. Wallet and car information appear only
from a valid saved snapshot; unknown balance is a dash, not a fabricated zero.
Quota usage/limit, each car's reported extra-trip count (`quota.bonus`) and the
account-wide extra-trip count (`bonusTrips`) are displayed separately. Missing or
invalid counts are unknown, not zero. They are not added together or interpreted
as remaining trips: the API's relationship between these counters is not verified.
Reaching a reported quota limit alone does not prove that all extra trips are
exhausted. Car availability still follows the reported `canDrive`, trip and block
state, never a locally calculated quota total. Old snapshots that previously
stored default zeros cannot recover missing values until a new successful read.
The creator panel and old reward/claim counters have been removed. Suspension
history and local persisted events are collapsed by default; the browser remembers
each panel's open/closed state when storage is available. Events summarize saved
receives, recorded stops and confirmed suspension records, not a complete
execution log. The monitor also records its planned next check or cooldown end,
scoped to the selected primary account, and a fixed safe reason when it stops.
A saved future plan is **not a heartbeat or proof that the worker is alive**;
overdue plans are explicitly labeled unconfirmed. Runtime logs remain in Railway.
Rendering and the 30-second page refresh never make site requests or enable
monitoring.

### When the dashboard has no saved receive

Enabling the flag and supplying a database URL do not prove the worker started.
A failed database connection or an occupied advisory lock can stop the worker
**before** it has ownership to publish shared account status. The supervisor now
provides its dashboard and worker with a fresh private local diagnostics file
(`MONITOR_RUNTIME_FILE`, managed internally; do not set it in Railway Variables).
The path is unique to that supervisor run, outside the shared volume, so a
rejected replica cannot overwrite the owner's shared status or another replica's
diagnostics. Only fixed stage/error identifiers, the primary identity and a stage
timestamp are recorded; never driver messages, URLs, credentials or server bodies.

The dashboard distinguishes database setup/connection errors, an occupied lock,
unavailable safety state, login/session stops and an unreported worker exit.
Safe `request_failed` and `state_invalid` codes distinguish request/response failures
from invalid state or local persistence failures, without storing raw exceptions.
The technical panel exposes the fixed stop code. An older `manual_review` stop is
not enough to prove no start was sent: the saved pre-start sample can still show
no active trip. Check the game's current trip state and Runtime Logs before any
manual intervention; never delete or reset the trip-start journal to bypass it.
A startup-stage report older than three minutes is explicitly unconfirmed and
requires inspection of **Runtime/Deploy Logs**, rather than showing an endless
first-receive wait. The supervisor records an exit without restarting the monitor
and preserves any more specific worker-reported cause. A supervisor/container
crash or unwritable diagnostics directory can still prevent a report; missing
reports are shown as unknown, not evidence of a running worker. These reports are
observability only: no website retry, re-login, lock bypass or database reconnect
is authorized by these reports. The initial `waiting_lock` stage distinguishes a
bounded deployment handover wait (up to two minutes) from a final `already_running`
stop. An occupied lock can be an old deployment still shutting down or a genuinely
active other service; its holder is not identified by this message. If contention
persists beyond the startup window, keep the intended Railway service at one
replica and stop the redundant deployment/service or local worker after identifying
it. Do not force-release the database lock or restart repeatedly. The app cannot
change Railway replicas, volumes or other projects on your behalf.

A previously visible balance disappearing after a deployment is different from a
slow first read. Check that the service has a persistent volume mounted at
`/app/data`, `DATA_DIR` points there, and UID 10001 can write to it. Without persistent
storage the saved snapshots can be lost when Railway replaces a container. The
application cannot provision/mount a Railway volume or recover lost snapshots;
an empty local state alone does not establish why the data is absent. The empty
state and missing receive time now refer to **local stored evidence**, not a claim
that this account has never successfully received information anywhere.

### Optional automatic trip starts — separate explicit opt-in

The owner requested **automatic starts only**, not restoration of mission claims,
referral claims, rentals or fake-account/referral automation. The start endpoint,
`{carId}` payload and eligibility fields were recovered from the earlier project
commit `a0460df`; historical code is not proof of today's API contract or site
permission. No live website mutation was performed during development.

Set `AUTO_START_TRIPS_ENABLED=true` on the one retained Railway service **only if
site rules permit automation**, after read-only monitoring works. It defaults to
off; without `SITE_MONITORING_ENABLED=true` no website action is permitted at all.
To disable future starts, remove/set the start flag to `false` and apply/redeploy;
an already in-flight operation cannot be recalled. Do not repeatedly redeploy or
restart while investigating an uncertain result. Dashboard labels describe the
configuration, not a guarantee that a worker is running.

The worker reuses the same session and shared owner/budget for every request:

1. Read and validate current state. Prefer eligible owned cars, then rented cars.
   A car needs an ID, explicit `canDrive=true`, no block/broken/active trip and
   positive reported `quota.left` or unexpired `quota.bonus`. Never infer capacity
   from `limit-used` or copy the account bonus pool to every car.
2. Start at most **one car per cycle**. Recheck policy/ownership/suspension and
   eligibility after pacing; the state response may be at most 90 seconds old
   at dispatch. No known unsupported endpoint probing or HTTP retries.
3. Persist an intent in the same PostgreSQL database **before** the single POST.
   The DB user also needs create/use permission for `cryptotaxi_trip_starts`.
4. Validate the returned trip's start/end timestamps, then read state again with
   the same request pacing and observe that exact trip. Only then is this episode
   eligible to be replaced once the server later reports the car idle/allowed.
5. If another eligible car remains, check again after at least five minutes;
   otherwise use the nearest trip-end schedule. This is not a burst-start loop.

The journal is keyed by hashed account identity and car ID. Pending or confirmed
but unobserved intents survive process restarts and loss of the local volume.
Timeouts, malformed replies, denied starts and unverified outcomes stop the worker
and **are not automatically replayed**, including 429/503 on a start; server
cooldowns are still persisted. A read-side cooldown before dispatch may postpone
work and cause a new state read, but never reissues a sent start. Confirmed
suspension still stops all operations. Do not delete journal rows to force retries:
inspect actual game state and logs first. This is conservative duplicate
prevention, not an exactly-once API guarantee or a guarantee against bans.

The browser remains a local-data viewer: page refreshes/GETs/POSTs do not dispatch
starts, and unsupported mutation routes remain disabled. No site credentials or
sessions are recovered from Git history. Mission/referral `claim` and `market/rent`
remain unavailable even with the trip-start flag on.

### New-rental workspace — restored preview, deliberately inactive

The **مدیریت رنت** panel is separate from using already-rented taxis for trips.
It shows the count in the selected primary account's last valid local snapshot,
the old application's two-rental cap, and theoretical additional slots. Unknown
account data stays unknown; this arithmetic is not permission to rent. Existing
rentals above the cap (for example four) are preserved, not cancelled or deleted.
The old cap is **not** a confirmed game limit or a new restriction on driving
already-rented taxis. Auto-start settings do not enable new rentals.

`rental_rules.py` reconstructs the old rules from `claimer.py` at `a0460df`:
count existing rentals, cap additional drafts at two minus that count, and retain
listing order. Historically `market` returned `listings` and `market/rent` accepted
`{listingId}`; those historical shapes are not a verified current API contract.
The pure function accepts optional offline listings for tests/future review,
whitelists display fields, rejects malformed/duplicate IDs, and **always** returns
`enabled=False` and `requires_review=True`. It does not rank profitability or
invent prices, free offers, contract duration, shifts or daily-trip allowances.
The dashboard supplies no listings, so it honestly says the market is not checked,
not that the market is empty. No fake market file or sample offers are deployed.

There is no rent worker, network client, executable rent payload, activation route,
or browser handler. `market` and `market/rent` remain denied by the central request
policy; all dashboard POSTs still return 405. Old-style `AUTO_RENT_ENABLED=true`
or other environment variables cannot activate it. Restoring execution in a
future phase requires separate authorization, verified prices/contract terms and
API permission, an explicit spending policy, shared-session/lock pacing and a
durable no-replay journal for ambiguous rental/payment outcomes. None is enabled
by this preview. No credentials, sessions, multi-account loops or automatic retries
were recovered from the historical implementation.

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
old execution tests have been replaced with policy/security and guarded-start
tests. Start tests simulate the durable SQL state machine and HTTP replies; real
PostgreSQL schema integration and the current game API contract still require
operator validation.
