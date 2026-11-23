#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Taxi — Referral Faker (Path A: complete system, dormant until triggered)

This module creates "level 2" referral accounts to make the referral tree look
natural and avoid detection of fake referral inflation.

How it works:
1. Monitors the MAIN account's referral count via the /api/referrals endpoint
2. When the main account reaches TRIGGER_REFERRAL_COUNT (default 100),
   the faker activates
3. For each real referral account (an account referred by the main account):
   a. Login and fetch that account's own refCode from /api/referrals
   b. Create 2-8 fake accounts using THAT refCode (not the main account's)
   c. Staged over STAGING_DAYS days to look organic
4. Fake accounts are added to accounts.json — the claimer picks them up
   automatically and they start claiming (minimal activity = looks real)

The system is DORMANT until the trigger condition is met. It checks periodically.

Config (edit below):
- MAIN_ACCOUNT: username of the main account
- TRIGGER_REFERRAL_COUNT: referral count that activates the faker
- MIN_FAKE_PER_REFERRAL: minimum fake accounts per real referral (default 2)
- MAX_FAKE_PER_REFERRAL: maximum fake accounts per real referral (default 8)
- STAGING_DAYS: number of days to spread fake account creation (default 8)
- ENABLED: master toggle (default True — checks trigger, but won't act until met)

Usage:
    python3 referral_faker.py                          # monitor forever
    python3 referral_faker.py --check                  # one-time check
    python3 referral_faker.py --check --force          # force-check now
"""

from runtime_config import ACCOUNT_CREATION_ENABLED

import json
import os
import sys
import time
import random
import string
import logging
import hashlib
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Tehran timezone ──
TZ = ZoneInfo("Asia/Tehran")

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
REFERRAL_LOG = os.path.join(HERE, "referral_faker.log")
REFERRAL_STATUS_FILE = os.path.join(HERE, "referral_faker_status.json")

# ── Reuse shared config from account_creator ──
try:
    from account_creator import (
        BASE, MAIL_API, PASSWORD, MAIL_DOMAINS, PLATFORMS, SCREENS,
        COLOR_DEPTHS, DPRS, HARDWARE_CONCURRENCY, DEVICE_MEMORY,
        random_username, random_password_full, gen_device_header,
        load_accounts, save_accounts, count_accounts, existing_usernames,
        create_email, format_jalali_datetime,
    )
    _SHARED = True
except Exception:
    _SHARED = False
    # Fallback minimal versions if account_creator is not importable
    BASE = "https://thecryptotaxi.com/api"
    MAIL_API = "https://api.mail.tm"
    PASSWORD = "@MjM1465770852@"
    MAIL_DOMAINS = ["euploading.com"]
    def random_username():
        prefix = random.choice(["crypto", "taxi", "ride", "city", "cab", "uber"])
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 8)))
        return f"{prefix}{suffix}"
    def random_password_full():
        return "T" + "".join(random.choices(string.ascii_letters + string.digits, k=11))
    def gen_device_header():
        return hashlib.sha256(os.urandom(32)).hexdigest() + "." + hashlib.sha256(os.urandom(32)).hexdigest()
    def load_accounts():
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, list) else []
        except Exception:
            return []
    def save_accounts(a):
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(a, f, indent=2, ensure_ascii=False)
    def count_accounts():
        return len(load_accounts())
    def existing_usernames():
        return {a.get("username", "") for a in load_accounts()}
    def format_jalali_datetime(ts=None):
        if ts is None:
            ts = time.time()
        dt = datetime.fromtimestamp(ts, tz=TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    def create_email():
        domain = random.choice(MAIL_DOMAINS)
        username = random_username()
        email = f"{username}@{domain}"
        pw = random_password_full()
        try:
            r = requests.post(f"{MAIL_API}/accounts",
                              json={"address": email, "password": pw},
                              headers={"Content-Type": "application/json"},
                              timeout=30)
            if r.status_code in (200, 201):
                return email, pw
        except Exception:
            pass
        return None


# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════

ENABLED = True
MAIN_ACCOUNT = "bavafasobhan@gmail.com"
TRIGGER_REFERRAL_COUNT = 100
MIN_FAKE_PER_REFERRAL = 2
MAX_FAKE_PER_REFERRAL = 8
STAGING_DAYS = 8
CHECK_INTERVAL = 600  # check every 10 minutes


# ════════════════════════════════════════════════════════════════════════════
# Logging
# ════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(REFERRAL_LOG, encoding="utf-8")],
)
for _h in logging.getLogger().handlers:
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log = logging.getLogger("referral_faker")

try:
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_sh)
except Exception:
    pass


# ════════════════════════════════════════════════════════════════════════════
# Status
# ════════════════════════════════════════════════════════════════════════════

def init_status():
    try:
        with open(REFERRAL_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def update_status(**kwargs):
    try:
        s = {}
        try:
            with open(REFERRAL_STATUS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            s = {}
        s.update(kwargs)
        s["updated_at"] = time.time()
        s["updated_at_persian"] = format_jalali_datetime()
        tmp = REFERRAL_STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, REFERRAL_STATUS_FILE)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# API helpers
# ════════════════════════════════════════════════════════════════════════════

def api_login(email, password):
    """Login to thecryptotaxi.com and return a session (with cookies).
    The API sets a cookie __Host-ct_s on login — no bearer token."""
    headers = {"Content-Type": "application/json", "X-CT": "1", "X-CT-Lang": "es"}
    session = requests.Session()
    try:
        r = session.post(
            f"{BASE}/auth/login",
            json={"username": email, "password": password},
            headers=headers,
            timeout=30,
        )
        if r.status_code in (200, 201):
            data = r.json()
            if data.get("error"):
                return None
            # Check if session got the auth cookie
            if session.cookies.get("__Host-ct_s"):
                session.headers.update(headers)
                session._user = email
                session._password = password
                return session
    except Exception as e:
        log.warning("api_login error for %s: %s", email, e)
    return None


def api_post(session, endpoint, payload, user=""):
    """POST to API using the session's cookies. Returns parsed JSON or None."""
    if not session or not session.cookies.get("__Host-ct_s"):
        return None
    try:
        r = session.post(f"{BASE}/{endpoint}", json=payload, timeout=30)
        if r.status_code < 400:
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}
        return None
    except Exception as e:
        log.warning("api_post(%s) error for %s: %s", endpoint, user or getattr(session, "_user", ""), e)
        return None


def fetch_referral_count(main_email, main_password):
    """Login to main account and get total referral count.
    Returns (count, stats_dict) or (None, None) if failed."""
    session = api_login(main_email, main_password)
    if not session:
        return None, None
    data = api_post(session, "referrals", {}, main_email)
    if data is None:
        return None, None
    stats = data.get("stats", {})
    total = stats.get("total", 0)
    return total, stats


def get_account_refcode(email, password):
    """Login to an account and fetch its refCode.
    Returns the refCode string or None."""
    session = api_login(email, password)
    if not session:
        return None
    data = api_post(session, "referrals", {}, email)
    if data is None:
        return None
    return data.get("refCode", "")


def get_main_account_referral_names(main_email, main_password):
    """Get the list of referral account names for the main account.
    Returns list of (referral_name, email) tuples.

    Since all accounts in accounts.json were created with the main refCode,
    all of them (except the main account itself) are referrals.
    We cross-reference with the server's referral data for accuracy.
    """
    # Get all accounts from accounts.json
    accounts = load_accounts()
    main_username = MAIN_ACCOUNT

    # Filter: all accounts except the main account
    referral_accounts = []
    for a in accounts:
        u = a.get("username", "")
        if u == main_username:
            continue
        referral_accounts.append(a)

    # Also try to get referral names from the server
    session = api_login(main_email, main_password)
    server_names = set()
    if session:
        data = api_post(session, "referrals", {}, main_email)
        if data:
            # bonuses list contains referral usernames
            for bonus in data.get("bonuses", []):
                name = bonus.get("referral", "")
                if name:
                    server_names.add(name)
            # Also check if there's a list of referrals
            for key in ["referrals", "referred", "members", "list"]:
                lst = data.get(key)
                if isinstance(lst, list):
                    for item in lst:
                        if isinstance(item, dict):
                            name = item.get("username") or item.get("name") or item.get("referral")
                            if name:
                                server_names.add(name)
                        elif isinstance(item, str):
                            server_names.add(item)

    return referral_accounts, sorted(server_names)


# ════════════════════════════════════════════════════════════════════════════
# Fake account creation
# ════════════════════════════════════════════════════════════════════════════

def create_fake_account(ref_code, source_referral=""):
    """Create one fake account using the given refCode.
    Returns True on success.

    The fake account is a fully functional account:
    - Has its own email from mail.tm
    - Has a unique device fingerprint
    - Registered with the given refCode (the real referral's code)
    - Added to accounts.json — claimer picks it up automatically
    """
    # Generate unique device header
    device_header = gen_device_header()

    # Create disposable email
    email_result = create_email()
    if not email_result:
        log.info("No email available for fake account, skipping")
        return False

    email, email_pw = email_result
    username = email.split("@")[0]

    # Register with the real referral's refCode
    payload = {
        "username": username,
        "email": email,
        "password": PASSWORD,
        "refCode": ref_code.upper(),
        "acceptTerms": True,
        "lang": "es",
    }

    headers = {
        "Content-Type": "application/json",
        "X-CT": "1",
        "X-CT-Lang": "es",
        "X-CT-Device": device_header,
    }

    try:
        r = requests.post(f"{BASE}/auth/register", json=payload, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            # Add to accounts.json
            accounts = load_accounts()
            existing = existing_usernames()
            if email in existing:
                return True
            accounts.append({
                "username": email,
                "password": PASSWORD,
                "email": email,
                "email_password": email_pw,
                "email_domain": email.split("@")[-1],
                "referral_source": source_referral,
                "referral_ref_code": ref_code,
                "is_fake_referral": True,
            })
            save_accounts(accounts)
            log.info("✅ Fake referral account created: %s (refCode: %s, source: %s)",
                     email, ref_code, source_referral)
            return True
        elif r.status_code == 429:
            log.warning("Rate limited (429) on fake registration for %s — skipping", username)
            return False
        else:
            log.warning("Fake registration failed (%d) for %s: %s",
                        r.status_code, username, r.text[:200])
            return False
    except Exception as e:
        log.warning("Fake registration error for %s: %s", username, e)
        return False


def create_fakes_for_referral(referral_account, staging_day):
    """Create fake accounts for a single real referral account.

    Args:
        referral_account: dict with username, password, email
        staging_day: current day of staging (0 to STAGING_DAYS-1)

    Returns number of fake accounts created.
    """
    email = referral_account.get("email") or referral_account.get("username", "")
    password = referral_account.get("password", PASSWORD)
    name = referral_account.get("username", "")

    # Get this referral account's refCode
    ref_code = get_account_refcode(email, password)
    if not ref_code:
        log.warning("Could not get refCode for referral %s — skipping", name)
        return 0

    # Determine how many fakes to create on this staging day
    total_fakes = random.randint(MIN_FAKE_PER_REFERRAL, MAX_FAKE_PER_REFERRAL)
    fakes_per_day = total_fakes // STAGING_DAYS + (1 if random.random() < (total_fakes % STAGING_DAYS) / STAGING_DAYS else 0)

    # On the first staging day, create a few; on later days, create the rest
    if staging_day == 0:
        fakes_to_create = max(1, fakes_per_day)
    else:
        fakes_to_create = fakes_per_day

    log.info("Referral %s (refCode: %s) — creating %d fake(s) on day %d",
             name, ref_code, fakes_to_create, staging_day + 1)

    created = 0
    for _ in range(fakes_to_create):
        if create_fake_account(ref_code, source_referral=name):
            created += 1
        time.sleep(random.uniform(3, 8))  # small delay between fakes

    return created


# ════════════════════════════════════════════════════════════════════════════
# Main monitor / faker loop
# ════════════════════════════════════════════════════════════════════════════

def check_trigger():
    """Check if the main account has reached the trigger referral count.
    Returns (triggered, referral_count, stats)."""
    main_email = MAIN_ACCOUNT
    # The main account's password is in accounts.json
    accounts = load_accounts()
    main_password = None
    for a in accounts:
        if a.get("username") == MAIN_ACCOUNT:
            main_password = a.get("password", PASSWORD)
            break

    if not main_password:
        log.warning("Main account %s not found in accounts.json", MAIN_ACCOUNT)
        return False, 0, {}

    count, stats = fetch_referral_count(main_email, main_password)
    if count is None:
        return False, 0, {}

    triggered = count >= TRIGGER_REFERRAL_COUNT
    return triggered, count, stats


def run_check():
    """One-time check: if trigger is met, start the staging process."""
    if not ENABLED:
        return

    triggered, count, stats = check_trigger()

    status = init_status()
    status["enabled"] = ENABLED
    status["main_account"] = MAIN_ACCOUNT
    status["trigger_count"] = TRIGGER_REFERRAL_COUNT
    status["current_referral_count"] = count
    status["referral_stats"] = stats
    status["triggered"] = triggered
    status["check_time"] = time.time()
    status["check_time_persian"] = format_jalali_datetime()

    if not triggered:
        status["status"] = "dormant"
        status["phase_label"] = f"⏳ در انتظار {TRIGGER_REFERRAL_COUNT} رفرال اصلی — الان {count}"
        update_status(**status)
        log.info("DORMANT: main account has %d/%d referrals — not yet triggered",
                 count, TRIGGER_REFERRAL_COUNT)
        return

    # ── TRIGGERED: start creating fake referrals ──
    status["status"] = "active"
    status["phase_label"] = "🚀 ساخت ركال‌های فيك در تراز ۲"
    update_status(**status)

    log.info("=== TRIGGER MET! Main account has %d referrals — starting fake referral creation ===", count)

    # Get the list of real referral accounts
    referral_accounts, server_names = get_main_account_referral_names(MAIN_ACCOUNT, None)
    # Filter to accounts that have a password
    referral_accounts = [a for a in referral_accounts if a.get("password")]
    total_referrals = len(referral_accounts)

    status["total_real_referrals"] = total_referrals
    status["server_referral_names"] = server_names
    status["staging_days"] = STAGING_DAYS
    status["staging_day"] = 0
    status["total_fakes_created"] = 0
    update_status(**status)

    log.info("Found %d real referral accounts — staging over %d days",
             total_referrals, STAGING_DAYS)

    # Calculate which referrals to process on each staging day
    # Distribute referrals across staging days
    referrals_per_day = max(1, total_referrals // STAGING_DAYS)
    day_idx = 0
    day_referrals = []

    for i, acc in enumerate(referral_accounts):
        if i % referrals_per_day == 0 and day_referrals:
            # Start a new staging day
            day_idx += 1
            if day_idx >= STAGING_DAYS:
                break
            log.info("=== Staging day %d/%d ===", day_idx + 1, STAGING_DAYS)
            day_referrals = []
        day_referrals.append(acc)

    # Handle remaining
    if not day_referrals and referral_accounts:
        day_referrals = referral_accounts[:referrals_per_day]
        day_idx = 0

    # Process the first staging day
    status["staging_day"] = day_idx
    status["staging_day_label"] = f"روز {day_idx + 1}/{STAGING_DAYS}"
    update_status(**status)

    created = 0
    for acc in day_referrals:
        created += create_fakes_for_referral(acc, day_idx)

    status["total_fakes_created"] = created
    status["last_staging_day"] = day_idx
    status["phase_label"] = f"✅ روز {day_idx + 1}/{STAGING_DAYS} کامل شد — {created} ركال فيك ساخته شد"
    update_status(**status)

    log.info("Staging day %d complete — %d fake accounts created (total: %d)",
             day_idx + 1, created, created)


def main():
    if not ACCOUNT_CREATION_ENABLED:
        log.info("Automatic account creation is disabled")
        return

    args = sys.argv[1:]

    update_status(
        enabled=ENABLED,
        main_account=MAIN_ACCOUNT,
        trigger_count=TRIGGER_REFERRAL_COUNT,
        status="dormant",
        phase_label="⏳ سیستم ركال‌های فيك در حال لود — منتظر پاسخ كانالون",
        started_at=time.time(),
        started_at_persian=format_jalali_datetime(),
    )

    log.info("=== Referral Faker started ===")
    log.info("Main account: %s | Trigger: %d referrals", MAIN_ACCOUNT, TRIGGER_REFERRAL_COUNT)
    log.info("Config: %d-%d fakes per referral, %d staging days",
             MIN_FAKE_PER_REFERRAL, MAX_FAKE_PER_REFERRAL, STAGING_DAYS)
    log.info("Jalali time: %s", format_jalali_datetime())

    if "--check" in args:
        run_check()
        if "--force" in args or True:
            return

    # Continuous monitoring loop
    while True:
        try:
            run_check()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            update_status(status="stopped", phase_label="⛔ متوقف شد توسط کاربر")
            log.info("Referral Faker stopped by user")
            break
        except Exception as e:
            log.exception("Referral Faker error: %s", e)
            time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Referral Faker stopped")
    except Exception as e:
        log.exception("Referral Faker crashed: %s", e)
