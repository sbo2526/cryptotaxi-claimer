# Crypto Taxi — Multi-Account Claimer

Automated multi-account claimer for [thecryptotaxi.com](https://thecryptotaxi.com).

## What it does

| Service | Description |
|---|---|
| **Claimer** | Periodically sweeps all accounts — starts trips, claims missions, and claims referral trips |
| **Account Creator** | Disabled — automatic account creation is turned off |
| **Dashboard** | Web dashboard on port 8080 with live stats, countdown timers, and per-account tables |
| **Referral Faker** | Disabled — automatic referral account creation is turned off |
| **Supervisor** | Monitors all services, auto-restarts on crash, rotates logs |

## Quick Start (Railway / Docker)

```bash
# Deploy on Railway — just connect this GitHub repo
# Railway detects Dockerfile and deploys automatically

# Or build locally:
docker build -t cryptotaxi .
docker run -d -p 8080:8080 cryptotaxi
```

## Files

- `supervisor.py` — process manager / watchdog
- `claimer.py` — main claimer
- `account_creator.py` — background account creator
- `dashboard.py` — web dashboard (port 8080)
- `referral_faker.py` — referral faker (dormant by default)
- `mail_reader.py` — mail.tm verification code reader
- `ctl.sh` — control script (start/stop/restart/status)
- `Dockerfile` — container config for Railway/Docker

## Account creation disabled

`ACCOUNT_CREATION_ENABLED = False` in `runtime_config.py` prevents the supervisor
from starting or restarting both account-creation workers. Their standalone entry
points also exit without making requests. The dashboard shows them as disabled,
even if old status files exist. Existing accounts and the claimer are unchanged.

This takes effect on Railway only after the change is merged into `main` and
successfully deployed. Editing this working branch does not stop existing production workers.

The FREE ad visitor feature has been removed from both the dashboard and the
claimer. No background worker visits or claims ad links. Historical runtime data
is left intact; it does not re-enable this feature.

## Banned account cleanup

Recorded bans are reconciled at the beginning and end of every sweep. Banned
accounts are removed from `accounts.json` (including stored credentials), live
`status.json`, and in-memory account/session caches. The ban archive retains only
its existing audit records; it is not a list of active accounts. Newly detected
bans are archived before deletion. Concurrent detections use serialized, atomic
JSON writes, and an empty active account list is supported.

The dashboard uses the same filtered active list for cards, its total, the creator
panel count, and `/count`. Deleted/status-only accounts are not reintroduced by
stale status data. The page detects count changes every 10 seconds.

This does not delete accounts on the external website. Automatic deletion requires
an explicit account suspension/ban message or an account-specific error code in
the actual login/API response, including error JSON returned with HTTP 200. The
message and reason (for example, Multi Account) are retained in the audit archive.
Generic 401/403, Forbidden, access denied, proxy/network errors, and repeated
failures alone never authorize deletion.

Legacy archive/status entries based only on 403 or failure counts are retained as
unconfirmed history but no longer remove or hide an existing active account.
Previously deleted credentials are not reconstructed or restored automatically.
