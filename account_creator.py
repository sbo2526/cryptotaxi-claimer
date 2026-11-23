#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Taxi — Account Creator (professional, safe, background)

Creates accounts on thecryptotaxi.com up to a target count (default 100).
Runs in the background alongside claimer.py. Claimer hot-reloads
accounts.json every sweep, so new accounts start claiming automatically.

Safety measures:
- Jitter on the 5-min interval (±90s) so requests don't look robotic
- Unique device fingerprints per account (different platform/screen/tz)
- Random username generation (alphanumeric, 8-12 chars)
- Multiple mail.tm domains (rotated) for disposable emails
- Error handling: failed registrations are logged and skipped
- Rate-limit aware: on 429, waits longer before next attempt
- Auto-shutdown when target count is reached
- Claimer is never interrupted — this script only writes accounts.json

Usage:
  python3 account_creator.py              # default: 100 accounts, 5-min interval
  python3 account_creator.py --target 50 --interval 300
"""

from runtime_config import ACCOUNT_CREATION_ENABLED

import json
import os
import sys
import time
import random
import hashlib
import string
import logging
import datetime
import requests
from zoneinfo import ZoneInfo

# Fix the program clock to Tehran timezone (Asia/Tehran, UTC+03:30)
TZ = ZoneInfo("Asia/Tehran")

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
CREATOR_LOG = os.path.join(HERE, "creator.log")
CREATOR_STATUS_FILE = os.path.join(HERE, "creator_status.json")

BASE = "https://thecryptotaxi.com/api"
MAIL_API = "https://api.mail.tm"
PASSWORD = "@MjM1465770852@"
REF_CODE = "YK5Q22TW"

# mail.tm domains — only domains that actually exist on mail.tm.
# Usernames are random and unique, so one domain is enough for uniqueness.
MAIL_DOMAINS = [
    "euploading.com",
]

# Platform presets for device fingerprint variation
PLATFORMS = [
    {"platform": "Linux", "lang": "es-ES", "languages": "es-ES,es", "tz": "America/Mexico_City"},
    {"platform": "Linux", "lang": "es-ES", "languages": "es-ES,es", "tz": "Europe/Madrid"},
    {"platform": "Windows", "lang": "es-ES", "languages": "es-ES,es", "tz": "America/Bogota"},
    {"platform": "Macintosh", "lang": "es-ES", "languages": "es-ES,es", "tz": "America/Argentina/Buenos_Aires"},
    {"platform": "Linux", "lang": "es-MX", "languages": "es-MX,es-ES,es", "tz": "America/Mexico_City"},
    {"platform": "Windows", "lang": "es-MX", "languages": "es-MX,es-ES,es", "tz": "America/Sao_Paulo"},
]

SCREENS = [(1920, 1080), (1366, 768), (1440, 900), (1536, 864), (2560, 1440)]
COLOR_DEPTHS = [24, 30]
DPRS = [1, 1.25, 1.5]
HARDWARE_CONCURRENCY = [4, 8, 16]
DEVICE_MEMORY = [4, 8, 16]

# ------------------------------------------------------------------------- #
# Logging — Tehran timezone, Jalali format
# ------------------------------------------------------------------------- #
class _TehranFormatter(logging.Formatter):
    """Custom formatter that shows Tehran-local Jalali datetime in log lines."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=TZ)
        jy, jm, jd = gregorian_to_jalaali(dt.year, dt.month, dt.day)
        month_name = PERSIAN_MONTHS[jm - 1]
        day_name = PERSIAN_DAYS[(dt.weekday() + 1) % 7]
        return to_persian_digits(
            f"{jy}/{month_name}/{jd} {day_name} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(CREATOR_LOG, encoding="utf-8")],
)
# Apply the Tehran formatter to all handlers
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_TehranFormatter())
log = logging.getLogger("creator")

# Also try stdout, guarded against Windows encoding issues
try:
    _sh = logging.StreamHandler(sys.stdout)
    log.addHandler(_sh)
except Exception:
    pass


# ------------------------------------------------------------------------- #
# Jalali (Solar Hijri) calendar conversion
# ------------------------------------------------------------------------- #
# Gregorian → Jalali using the accurate 33-year algorithm.
# Based on the well-known Jalaali calendar math (Borkowski / Jalaali-JS algorithm).

def _g2d(gy, gm, gd):
    """Convert Gregorian date to days since 0001-01-01 (proleptic Gregorian)."""
    d = gy * 365 + (gy + (gm - 14) // 12) // 4 - (gy + (gm - 14) // 12) // 100 \
        + (gy + (gm - 14) // 12) // 400 + (153 * (gm + (12 - ((gm - 14) // 12 + 1)) % 12 + 3) + 2) // 5 + gd - 428
    # The above formula can be simplified; use a cleaner standard one instead:
    # Standard Gregorian ordinal calculation
    a = (14 - gm) // 12
    y = gy + 4800 - a
    m = gm + 12 * a - 3
    return gd + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def _d2g(days):
    """Convert days since 0001-01-01 to Gregorian date (y, m, d)."""
    # days is the Julian Day Number offset; convert to Gregorian
    a = days + 32044
    b = (4 * a + 3) // 146097
    c = a - (146097 * b) // 4
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    m = (5 * e + 2) // 153
    day = e - (153 * m + 2) // 5 + 1
    month = m + 3 - 12 * (m // 10)
    year = 100 * b + d - 4800 + m // 10
    return year, month, day


def _jalaali_leap(jy):
    """Return True if jy is a leap year in the Jalaali calendar."""
    return ((jy - (jy // 33) * 33)) in (1, 5, 9, 13, 17, 22, 26, 30)


def _j2d(jy, jm, jd):
    """Convert Jalaali date to days since 0001-01-01 Jalaali (epoch)."""
    if jm <= 6:
        return (jy - 1) * 365 + (jy - 1) // 33 * 8 + (jm - 1) * 31 + (jd - 1)
    else:
        leap = 1 if _jalaali_leap(jy) else 0
        return (jy - 1) * 365 + (jy - 1) // 33 * 8 + 186 + (jm - 7) * 30 + (jd - 1) + leap


def _d2j(days):
    """Convert days since Jalaali epoch to Jalaali date (y, m, d)."""
    jy = (days * 33 + 4) // 12899 + 1
    while _j2d(jy, 1, 1) > days:
        jy -= 1
    while _j2d(jy + 1, 1, 1) <= days:
        jy += 1
    leap = 1 if _jalaali_leap(jy) else 0
    m = 1
    for m_len in [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29 + leap]:
        if days - _j2d(jy, m, 1) < m_len:
            break
        m += 1
    jd = days - _j2d(jy, m, 1) + 1
    return jy, m, jd


def gregorian_to_jalaali(gy, gm, gd):
    """Convert a Gregorian date to Jalaali (Solar Hijri) date."""
    jdn = _g2d(gy, gm, gd)
    # Jalaali epoch: 622-03-22 Gregorian = Julian Day 1948220
    jdn -= 1948220
    return _d2j(jdn)


def jalaali_to_gregorian(jy, jm, jd):
    """Convert a Jalaali date to Gregorian date."""
    jdn = _j2d(jy, jm, jd) + 1948220
    return _d2g(jdn)


# Persian month names (Jalali)
PERSIAN_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

PERSIAN_DAYS = [
    "شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه",
]

# Convert ASCII digits to Persian digits
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian_digits(text):
    """Convert ASCII digits in text to Persian digits."""
    return text.translate(_FA_DIGITS)


def format_jalali_datetime(ts=None):
    """Format a Unix timestamp (or now) as a Persian Jalali datetime string
    in Tehran timezone."""
    if ts is None:
        ts = time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=TZ)
    jy, jm, jd = gregorian_to_jalaali(dt.year, dt.month, dt.day)
    day_name = PERSIAN_DAYS[(dt.weekday() + 1) % 7]
    month_name = PERSIAN_MONTHS[jm - 1]
    h, mi, s = dt.hour, dt.minute, dt.second
    return to_persian_digits(
        f"{jy}/{month_name}/{jd} {day_name} {h:02d}:{mi:02d}:{s:02d}"
    )


def format_jalali_time(ts=None):
    """Format a Unix timestamp as Persian Jalali time (HH:MM:SS) in Tehran tz."""
    if ts is None:
        ts = time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=TZ)
    h, mi, s = dt.hour, dt.minute, dt.second
    return to_persian_digits(f"{h:02d}:{mi:02d}:{s:02d}")


def format_jalali_date(ts=None):
    """Format a Unix timestamp as Persian Jalali date in Tehran timezone."""
    if ts is None:
        ts = time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=TZ)
    jy, jm, jd = gregorian_to_jalaali(dt.year, dt.month, dt.day)
    month_name = PERSIAN_MONTHS[jm - 1]
    return to_persian_digits(f"{jy}/{month_name}/{jd}")


# ------------------------------------------------------------------------- #
# Creator status writer (live countdown + phase)
# ------------------------------------------------------------------------- #
import threading
_status_lock = threading.Lock()


def init_status():
    """Initialize or load the creator status file."""
    try:
        with open(CREATOR_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def update_creator_status(**kwargs):
    """Update the creator status JSON atomically (thread-safe)."""
    with _status_lock:
        try:
            status = {}
            try:
                with open(CREATOR_STATUS_FILE, "r", encoding="utf-8") as f:
                    status = json.load(f)
            except Exception:
                status = {}
            status.update(kwargs)
            status["updated_at"] = time.time()
            status["updated_at_persian"] = format_jalali_datetime()
            tmp = CREATOR_STATUS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CREATOR_STATUS_FILE)
        except Exception:
            pass


# ------------------------------------------------------------------------- #
# Helpers
# ------------------------------------------------------------------------- #
def random_hex(n):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def random_username():
    """Generate a valid username: 3-18 chars, letters/numbers/underscore/dot."""
    prefix = random.choice(["crypto", "taxi", "ride", "city", "cab", "uber"])
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 8)))
    return f"{prefix}{suffix}"


def random_password():
    """Generate a valid password: >=8 chars, upper + lower + number."""
    return random_password_full()


def random_password_full():
    upper = random.choice(string.ascii_uppercase)
    lower = random.choice(string.ascii_lowercase)
    digit = random.choice(string.digits)
    rest = "".join(random.choices(string.ascii_letters + string.digits, k=random.randint(5, 12)))
    pw = upper + lower + digit + rest
    return pw


def gen_device_header():
    """Generate a unique device header matching device.js format: {deviceId}.{fingerprint}"""
    device_id = random_hex(32)

    platform = random.choice(PLATFORMS)
    width, height = random.choice(SCREENS)
    color_depth = random.choice(COLOR_DEPTHS)
    dpr = random.choice(DPRS)
    hardware_conc = random.choice(HARDWARE_CONCURRENCY)
    device_mem = random.choice(DEVICE_MEMORY)

    parts = [
        platform["platform"],
        platform["lang"],
        platform["languages"],
        platform["tz"],
        str(width),
        str(height),
        str(color_depth),
        str(dpr),
        str(hardware_conc),
        str(device_mem),
        "0",  # maxTouchPoints
        "",   # webglSignal (can't compute server-side)
        "",   # canvasSignal (can't compute server-side)
    ]
    fp = hashlib.sha256("§".join(parts).encode()).hexdigest()
    return f"{device_id}.{fp}"


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)


def count_accounts():
    accounts = load_accounts()
    # Count unique usernames
    seen = set()
    for a in accounts:
        u = a.get("username", "")
        if u:
            seen.add(u)
    return len(seen)


def existing_usernames():
    accounts = load_accounts()
    seen = set()
    for a in accounts:
        u = a.get("username", "")
        if u:
            seen.add(u)
    return seen


# ------------------------------------------------------------------------- #
# Email creation (mail.tm)
# ------------------------------------------------------------------------- #
def create_email():
    """Create a disposable email via mail.tm. Returns (email, password) or None."""
    domain = random.choice(MAIL_DOMAINS)
    username = random_username()
    email = f"{username}@{domain}"
    email_pw = random_password_full()

    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(
            f"{MAIL_API}/accounts",
            json={"address": email, "password": email_pw},
            headers=headers,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            return email, email_pw
        else:
            log.warning("Email creation failed (%d) for %s: %s", resp.status_code, email, resp.text[:200])
            return None
    except Exception as e:
        log.warning("Email creation error for %s: %s", email, e)
        return None


# ------------------------------------------------------------------------- #
# Registration
# ------------------------------------------------------------------------- #
def register_account(email, password, device_header):
    """Register a new account on thecryptotaxi.com. Returns True on success."""
    username = email.split("@")[0]

    payload = {
        "username": username,
        "email": email,
        "password": password,
        "refCode": REF_CODE,
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
        resp = requests.post(
            f"{BASE}/auth/register",
            json=payload,
            headers=headers,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            log.info("✅ Registered: %s (%s)", username, email)
            return True
        elif resp.status_code == 429:
            log.warning("Rate limited (429) on registration for %s — waiting 30s", username)
            time.sleep(30)
            return False
        else:
            log.warning("Registration failed (%d) for %s: %s", resp.status_code, username, resp.text[:200])
            return False
    except Exception as e:
        log.warning("Registration error for %s: %s", username, e)
        return False


# ------------------------------------------------------------------------- #
# Single account creation cycle
# ------------------------------------------------------------------------- #
def create_one_account():
    """Attempt to create one new account. Returns True if successful."""
    cycle_start = time.time()

    # Phase: preparing device header
    update_creator_status(
        phase="preparing_device",
        phase_label="در حال ساخت device header و ایمیل",
        cycle_start=cycle_start,
        cycle_start_persian=format_jalali_datetime(cycle_start),
        cycle_elapsed=0,
    )

    # Generate unique device header
    device_header = gen_device_header()

    # Phase: creating email
    email_start = time.time()
    update_creator_status(
        phase="creating_email",
        phase_label="در حال ساخت ایمیل ره‌گیر",
        email_start=email_start,
        email_elapsed=0,
    )
    email_result = create_email()
    email_elapsed = time.time() - email_start
    update_creator_status(email_elapsed=email_elapsed)

    if not email_result:
        log.info("No email available, skipping this cycle")
        update_creator_status(
            phase="email_failed",
            phase_label="ساخت ایمیل شکست خورد — دور بعدی",
            cycle_elapsed=time.time() - cycle_start,
        )
        return False

    email, email_pw = email_result

    # Phase: registering on the site
    reg_start = time.time()
    update_creator_status(
        phase="registering",
        phase_label=f"در حال ثبت‌نام {email} در سرور",
        email=email,
        register_start=reg_start,
        register_elapsed=0,
    )
    success = register_account(email, PASSWORD, device_header)
    reg_elapsed = time.time() - reg_start
    update_creator_status(register_elapsed=reg_elapsed)

    if not success:
        update_creator_status(
            phase="register_failed",
            phase_label="ثبت‌نام شکست خورد — دور بعدی",
            email=email,
            cycle_elapsed=time.time() - cycle_start,
        )
        return False

    # Phase: saving to accounts.json
    save_start = time.time()
    update_creator_status(
        phase="saving_account",
        phase_label="در حال ذخیره اکانت در فهرستی",
        save_start=save_start,
        save_elapsed=0,
    )
    accounts = load_accounts()
    existing = existing_usernames()
    if email in existing:
        log.info("Account %s already exists, skipping", email)
        update_creator_status(
            phase="already_exists",
            phase_label=f"اکانت {email} از قبل وجود داشت",
            cycle_elapsed=time.time() - cycle_start,
        )
        return True

    accounts.append({
        "username": email,
        "password": PASSWORD,
        "email": email,
        "email_password": email_pw,
        "email_domain": email.split("@")[-1],
    })
    save_accounts(accounts)
    save_elapsed = time.time() - save_start
    total_elapsed = time.time() - cycle_start
    update_creator_status(
        phase="completed",
        phase_label=f"✅ اکانت {email} با موفقیت ساخته شد",
        last_created_email=email,
        last_created_at=time.time(),
        last_created_persian=format_jalali_datetime(),
        save_elapsed=save_elapsed,
        cycle_elapsed=total_elapsed,
        accounts_created=count_accounts(),
    )
    log.info("📝 Added to accounts.json: %s (total: %d)", email, count_accounts())
    return True


# ------------------------------------------------------------------------- #
# Main loop
# ------------------------------------------------------------------------- #
def countdown_sleep(sleep_time, target_ts, target, current):
    """
    Sleep for `sleep_time` seconds, but update creator_status.json every second
    with a live countdown so the dashboard shows exactly how many seconds remain
    until the next creation cycle starts.
    """
    start = time.time()
    remaining = sleep_time
    while remaining > 0:
        now = time.time()
        remaining = target_ts - now
        if remaining <= 0:
            break
        remaining = max(0, int(remaining))
        update_creator_status(
            phase="countdown",
            phase_label=f"⏳ در انتظار دور بعدی — {remaining} ثانیه مانده",
            countdown_seconds=remaining,
            next_cycle_at=target_ts,
            next_cycle_persian=format_jalali_datetime(target_ts),
            target=target,
            accounts_created=count_accounts(),
            remaining_to_target=max(0, target - count_accounts()),
            progress_percent=round(count_accounts() / target * 100, 1) if target > 0 else 0,
            current_accounts=count_accounts(),
            last_phase="countdown",
        )
        time.sleep(min(1, remaining))
    # Final tick when cycle is about to start
    update_creator_status(
        phase="cycle_starting",
        phase_label="▶ دور ساخت اکانت شروع شد",
        countdown_seconds=0,
        next_cycle_at=target_ts,
        next_cycle_persian=format_jalali_datetime(target_ts),
        target=target,
        accounts_created=count_accounts(),
        remaining_to_target=max(0, target - count_accounts()),
        progress_percent=round(count_accounts() / target * 100, 1) if target > 0 else 0,
        current_accounts=count_accounts(),
    )


def main():
    if not ACCOUNT_CREATION_ENABLED:
        log.info("Automatic account creation is disabled")
        return

    target = 100
    interval = 300  # 5 minutes

    args = sys.argv[1:]
    if "--target" in args:
        target = int(args[args.index("--target") + 1])
    if "--interval" in args:
        interval = int(args[args.index("--interval") + 1])

    start_time = time.time()

    # Initialize status
    update_creator_status(
        status="running",
        phase="init",
        phase_label="ابتداء سازنده اکانت",
        target=target,
        interval=interval,
        accounts_created=count_accounts(),
        remaining_to_target=max(0, target - count_accounts()),
        progress_percent=round(count_accounts() / target * 100, 1) if target > 0 else 0,
        started_at=start_time,
        started_at_persian=format_jalali_datetime(start_time),
        next_cycle_at=None,
        next_cycle_persian="—",
        countdown_seconds=None,
        errors=0,
        total_cycles=0,
        total_success=0,
        total_failed=0,
        last_created_email=None,
        last_created_at=None,
        last_created_persian=None,
        last_error=None,
    )

    log.info("=== Account Creator started ===")
    log.info("Target: %d accounts | Interval: %ds | Ref code: %s", target, interval, REF_CODE)
    log.info("Jalali time: %s", format_jalali_datetime())

    current = count_accounts()
    log.info("Current accounts in accounts.json: %d", current)

    if current >= target:
        log.info("Already at target (%d). Creator shutting down.", current)
        update_creator_status(
            status="shutdown",
            phase="done",
            phase_label=f"✅ هدف رسیده — {current}/{target} اکانت",
            shutdown_at=time.time(),
            shutdown_persian=format_jalali_datetime(),
        )
        return

    consecutive_failures = 0
    cycle_count = 0
    total_success = 0
    total_failed = 0

    while True:
        current = count_accounts()
        if current >= target:
            log.info("=== Target reached (%d accounts). Creator shutting down. ===", current)
            update_creator_status(
                status="shutdown",
                phase="done",
                phase_label=f"✅ هدف رسیده — {current}/{target} اکانت",
                shutdown_at=time.time(),
                shutdown_persian=format_jalali_datetime(),
                accounts_created=current,
                remaining_to_target=0,
                progress_percent=100.0,
            )
            return

        cycle_count += 1
        cycle_start = time.time()

        log.info("--- Cycle: %d/%d accounts | %s ---", current, target, format_jalali_datetime())

        update_creator_status(
            phase="cycle_starting",
            phase_label=f"▶ دور #{cycle_count} — {current}/{target} اکانت",
            cycle_number=cycle_count,
            cycle_start=cycle_start,
            cycle_start_persian=format_jalali_datetime(cycle_start),
            countdown_seconds=0,
            next_cycle_at=None,
            next_cycle_persian="—",
            accounts_created=current,
            remaining_to_target=max(0, target - current),
            progress_percent=round(current / target * 100, 1) if target > 0 else 0,
            current_accounts=current,
        )

        success = create_one_account()

        if success:
            consecutive_failures = 0
            total_success += 1
            current = count_accounts()
            log.info("Cycle complete: %d/%d accounts", current, target)
        else:
            total_failed += 1
            consecutive_failures += 1
            last_err = "Registration or email creation failed"
            log.info("Cycle failed (consecutive failures: %d)", consecutive_failures)
            update_creator_status(
                errors=total_failed,
                last_error=last_err,
                last_error_persian=format_jalali_datetime(),
            )

            if consecutive_failures >= 5:
                log.warning("5 consecutive failures — waiting 2 minutes before next attempt")
                update_creator_status(
                    phase="cooling_down",
                    phase_label="⚠️ ۵ شکست پیاپی — ۲ دقیقه انتظار",
                )
                time.sleep(120)
                consecutive_failures = 0

        # Update totals
        update_creator_status(
            total_cycles=cycle_count,
            total_success=total_success,
            total_failed=total_failed,
        )

        # Jitter: add ±90s to the interval to look less robotic
        jitter = random.uniform(-90, 90)
        sleep_time = max(60, interval + jitter)

        # Calculate the exact target timestamp for the next cycle
        next_cycle_ts = time.time() + sleep_time

        log.info("Sleeping %ds until next cycle (next at %s)",
                 sleep_time, format_jalali_datetime(next_cycle_ts))

        # Live countdown: update status every second
        countdown_sleep(sleep_time, next_cycle_ts, target, count_accounts())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        update_creator_status(
            status="stopped",
            phase="stopped",
            phase_label="⛔ سازنده اکانت متوقف شد توسط کاربر",
            stopped_at=time.time(),
            stopped_persian=format_jalali_datetime(),
        )
        log.info("Account Creator stopped by user")
    except Exception as e:
        update_creator_status(
            status="crashed",
            phase="error",
            phase_label=f"💥 خطا: {e}",
            error=str(e),
            crashed_at=time.time(),
            crashed_persian=format_jalali_datetime(),
        )
        log.exception("Account Creator crashed: %s", e)
