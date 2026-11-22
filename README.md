# Crypto Taxi — Multi-Account Claimer

Automated multi-account claimer for [thecryptotaxi.com](https://thecryptotaxi.com).

## What it does

| Service | Description |
|---|---|
| **Claimer** | Periodically sweeps all accounts — starts trips, claims missions, claims referral trips, and runs the FREE ad visitor |
| **Account Creator** | Creates new accounts every ~5 minutes, up to 100 (auto-shuts at target) |
| **Dashboard** | Web dashboard on port 8080 with live stats, countdown timers, and per-account tables |
| **Referral Faker** | Dormant until main account reaches 100 real referrals, then creates 2-8 level-2 fake accounts per referral |
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
- `claimer.py` — main claimer with FREE worker
- `account_creator.py` — background account creator
- `dashboard.py` — web dashboard (port 8080)
- `referral_faker.py` — referral faker (dormant by default)
- `mail_reader.py` — mail.tm verification code reader
- `ctl.sh` — control script (start/stop/restart/status)
- `Dockerfile` — container config for Railway/Docker
