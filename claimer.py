#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The Crypto Taxi — auto claimer (parallel multi-account, live monitor)

Core behaviour
--------------
- Each account is monitored in PARALLEL on a short poll interval
  (default 45 s). Whenever an account's car is idle (canDrive=true),
  a trip is started immediately — so a finished trip is restarted
  automatically within seconds, without waiting for a long cycle.
- Session self-healing: each account keeps its own HTTP session; a
  401 (expired session) triggers an automatic re-login and retry.
- Status of every car (on-trip / idle / unavailable) plus live
  trip-end timestamps is written to status.json for the dashboard.
- Per-account daily counters + daily markdown report.

Usage:
  python3 claimer.py                 # monitor forever, default poll 45s
  python3 claimer.py --poll 60       # poll every 60 seconds
  python3 claimer.py --once          # one sweep over all accounts, then exit
  python3 claimer.py --once --workers 3
"""

import json
import os
import sys
import time
import random
import logging
import threading
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Import mail reader for fetching verification codes
try:
    from mail_reader import get_manager as _get_mail_manager, MailManager as _MailManager
    _mail_manager = _get_mail_manager()
except Exception:
    _mail_manager = None

# Fix the program clock to Tehran timezone (Asia/Tehran, UTC+03:30)
TZ = ZoneInfo("Asia/Tehran")

BASE = "https://thecryptotaxi.com/api"
HEADERS = {
    "Content-Type": "application/json",
    "X-CT": "1",
    "X-CT-Lang": "es",
}

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
STATUS_FILE = os.path.join(HERE, "status.json")
EARNINGS_FILE = os.path.join(HERE, "earnings.json")
REPORT_DIR = os.path.join(HERE, "reports")
LOG_FILE = os.path.join(HERE, "claimer.log")
PROXIES_FILE = os.path.join(HERE, "proxies.txt")
PROXY_STATE_FILE = os.path.join(HERE, "proxy_state.json")
DEFAULT_POLL = 60          # seconds between monitor sweeps
DEFAULT_WORKERS = 5
RECENT_ERRORS_LIMIT = 15   # how many recent errors to keep per account

# ---- Proxy rotation config ----
PROXY_ENABLED = True          # master toggle — set False to disable all proxying
PROXY_ROTATE_PER_ACCOUNT = True  # each account gets a stable proxy; False = rotate per request
PROXY_HEALTH_CHECK_INTERVAL = 600  # seconds between health checks (10 min)
PROXY_DEAD_COOLDOWN = 300     # seconds before a dead proxy is re-tested
PROXY_HEALTH_CHECK_URL = "https://thecryptotaxi.com/"  # URL used to verify a proxy works

class _TehranFormatter(logging.Formatter):
    """Custom formatter that shows Tehran-local Gregorian datetime in log lines."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_TehranFormatter())
log = logging.getLogger("claimer")
# Also log to stdout, but guard against Windows console encoding issues
# (Persian characters may crash the StreamHandler on non-UTF-8 Windows consoles).
try:
    _sh = logging.StreamHandler(sys.stdout)
    log.addHandler(_sh)
except Exception:
    pass

stats_lock = threading.Lock()
sessions_lock = threading.Lock()
SESSIONS = {}   # username -> requests.Session
ACCOUNTS = {}   # username -> {username, password}
PROXY_MANAGER = None  # set in main() — ProxyManager instance


# --------------------------------------------------------------------------- #
# Proxy Manager — rotation, health checking, dead-proxy cooldown
# --------------------------------------------------------------------------- #
class ProxyManager:
    """
    Manages a list of proxies and rotates them for requests.

    - Reads proxies from proxies.txt (one per line, format: scheme://host:port)
    - Supports user:pass@host:port format
    - Health-checks proxies periodically and skips dead ones
    - Dead proxies go into cooldown before being re-tested
    - Falls back to no-proxy (direct) if no healthy proxies are available

    proxies.txt format:
        http://1.2.3.4:8080
        https://user:pass@5.6.7.8:9090
        socks5://9.10.11.12:1080
    """

    def __init__(self, proxies_file=PROXIES_FILE, state_file=PROXY_STATE_FILE):
        self.proxies_file = proxies_file
        self.state_file = state_file
        self.all_proxies = []        # list of proxy dicts: {"url": str, "dead": bool, "dead_until": float}
        self.healthy_proxies = []    # list of proxy URLs currently healthy
        self._index = 0              # rotation counter
        self._lock = threading.Lock()
        self._last_health_check = 0.0
        self._load_state()
        self._load_proxies()
        if self.all_proxies:
            self._refresh_healthy()
            log.info("Proxy Manager: %d proxy(ies) loaded, %d healthy",
                     len(self.all_proxies), len(self.healthy_proxies))
        else:
            log.info("Proxy Manager: no proxies loaded — running direct (no proxy)")

    def _load_proxies(self):
        """Read proxy list from file."""
        try:
            with open(self.proxies_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            proxies = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Normalize: ensure scheme:// prefix
                if "://" not in line:
                    line = "http://" + line
                proxies.append({
                    "url": line,
                    "dead": False,
                    "dead_until": 0.0,
                })
            self.all_proxies = proxies
        except FileNotFoundError:
            log.info("Proxy Manager: proxies.txt not found — running direct")
            self.all_proxies = []
        except Exception as e:
            log.warning("Proxy Manager: error loading proxies.txt: %s", e)
            self.all_proxies = []

    def _load_state(self):
        """Load dead-proxy state from disk."""
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            dead_map = state.get("dead", {})
            for proxy in self.all_proxies:
                url = proxy["url"]
                if url in dead_map:
                    proxy["dead"] = True
                    proxy["dead_until"] = dead_map[url]
        except Exception:
            pass  # no state file yet — that's fine

    def _save_state(self):
        """Persist dead-proxy state to disk."""
        try:
            dead_map = {}
            for proxy in self.all_proxies:
                if proxy["dead"]:
                    dead_map[proxy["url"]] = proxy["dead_until"]
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"dead": dead_map}, f, indent=2)
        except Exception:
            pass

    def _refresh_healthy(self):
        """Build the healthy list from all proxies, respecting cooldown."""
        now = time.time()
        self.healthy_proxies = []
        for proxy in self.all_proxies:
            if proxy["dead"] and proxy["dead_until"] > now:
                continue  # still in cooldown
            self.healthy_proxies.append(proxy["url"])
        # Auto-clear expired dead flags
        for proxy in self.all_proxies:
            if proxy["dead"] and proxy["dead_until"] <= now:
                proxy["dead"] = False

    def get_proxy(self, account_username=None):
        """
        Return a proxy dict for requests, or None for direct connection.
        If PROXY_ROTATE_PER_ACCOUNT is True and an account is given,
        the same proxy is returned for that account on each call (stable).
        Otherwise, the proxy is rotated on each call.
        """
        if not PROXY_ENABLED:
            return None

        with self._lock:
            # Periodic health check
            if time.time() - self._last_health_check > PROXY_HEALTH_CHECK_INTERVAL:
                self._health_check_async()
                self._last_health_check = time.time()

            if not self.healthy_proxies:
                return None  # no healthy proxies — direct

            if PROXY_ROTATE_PER_ACCOUNT and account_username:
                # Stable per-account proxy: hash the username to pick a proxy
                idx = hash(account_username) % len(self.healthy_proxies)
                url = self.healthy_proxies[idx]
            else:
                # Rotate on each call
                url = self.healthy_proxies[self._index % len(self.healthy_proxies)]
                self._index += 1

            return {"http": url, "https": url}

    def report_dead(self, proxy_url):
        """Mark a proxy as dead (called on connection errors)."""
        with self._lock:
            for proxy in self.all_proxies:
                if proxy["url"] == proxy_url:
                    if not proxy["dead"]:
                        log.warning("Proxy Manager: marking proxy as dead: %s (cooldown %ds)",
                                    proxy_url, PROXY_DEAD_COOLDOWN)
                        proxy["dead"] = True
                        proxy["dead_until"] = time.time() + PROXY_DEAD_COOLDOWN
                        self._save_state()
                    self._refresh_healthy()
                    break

    def _health_check_async(self):
        """
        Health-check all proxies that are in cooldown (re-test them).
        Runs in the calling thread but is non-blocking for the request path
        because we only test proxies that were previously dead.
        """
        now = time.time()
        to_test = []
        for proxy in self.all_proxies:
            if proxy["dead"] and proxy["dead_until"] <= now:
                to_test.append(proxy)

        if not to_test:
            return

        log.info("Proxy Manager: health-checking %d proxy(ies)...", len(to_test))
        for proxy in to_test:
            url = proxy["url"]
            try:
                r = requests.get(
                    PROXY_HEALTH_CHECK_URL,
                    proxies={"http": url, "https": url},
                    timeout=10,
                )
                if r.status_code < 500:
                    # Proxy is alive — clear dead flag
                    if proxy["dead"]:
                        log.info("Proxy Manager: proxy %s is alive again — re-enabled", url)
                        proxy["dead"] = False
                        proxy["dead_until"] = 0.0
                else:
                    # Still dead — extend cooldown
                    proxy["dead_until"] = now + PROXY_DEAD_COOLDOWN
            except Exception:
                # Still dead
                proxy["dead_until"] = now + PROXY_DEAD_COOLDOWN

        self._refresh_healthy()
        self._save_state()
        log.info("Proxy Manager: health check done — %d healthy out of %d",
                 len(self.healthy_proxies), len(self.all_proxies))

    def get_stats(self):
        """Return proxy stats for dashboard display."""
        with self._lock:
            now = time.time()
            dead_count = sum(1 for p in self.all_proxies if p["dead"] and p["dead_until"] > now)
            return {
                "total": len(self.all_proxies),
                "healthy": len(self.healthy_proxies),
                "dead": dead_count,
                "enabled": PROXY_ENABLED,
                "rotate_per_account": PROXY_ROTATE_PER_ACCOUNT,
            }

    def reload(self):
        """Reload the proxy list from file (hot-reload)."""
        self._load_proxies()
        self._refresh_healthy()
        log.info("Proxy Manager: proxy list reloaded — %d total, %d healthy",
                 len(self.all_proxies), len(self.healthy_proxies))


RENT_MARKET_INTERVAL = 300   # seconds between market checks (5 minutes)
AUTO_RENT_ENABLED = True     # set to False if API not available
RENT_ENDPOINT = "market/rent" # endpoint for renting a car from a listing
MAX_RENTED_PER_ACCOUNT = 2   # maximum rented cars per account — never exceed this

# ---- FREE section: visit ads, wait, claim USDT ----
FREE_ENABLED = True           # master toggle for the FREE worker
FREE_INTERVAL = 120           # seconds between FREE checks (2 minutes)
FREE_MAX_WAIT = 180           # max seconds to wait for an ad timer (3 min)
FREE_POLL_INTERVAL = 5        # seconds between polls while waiting for ad timer

# ---- BAN detection + auto-remove config ----
BAN_DETECTION_ENABLED = True      # master toggle
BAN_CONSECUTIVE_FAILURES = 5      # consecutive sweep failures before BAN
BAN_403_AUTO_REMOVE = True        # auto-remove on HTTP 403
BAN_LOGIN_FAILURE_THRESHOLD = 3   # consecutive login failures before BAN
BAN_ERROR_KEYWORDS = [            # server error keywords that indicate BAN
    "banned", "bloqueado", "bloqueada", "suspended", "suspendido",
    "blocked", "blocked_user", "cuenta bloqueada", "account blocked",
    "acceso denegado", "forbidden", "no autorizado",
]
BANNED_ACCOUNTS_FILE = os.path.join(HERE, "banned_accounts.json")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def today_str():
    return datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# BAN Detection + Auto-Remove
# --------------------------------------------------------------------------- #
def load_banned_accounts():
    """Load the banned accounts list from disk."""
    try:
        with open(BANNED_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"banned": []}


def save_banned_accounts(data):
    """Persist banned accounts list to disk."""
    try:
        with open(BANNED_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("Failed to save banned_accounts.json: %s", e)


# --------------------------------------------------------------------------- #
# Email / Verification Code Reader
# --------------------------------------------------------------------------- #
def get_account_email_info(user):
    """Get email and email_password for an account from accounts.json.
    Returns (email, email_pw) or (None, None) if not available."""
    accounts = load_accounts()
    for a in accounts:
        if a.get("username") == user:
            email = a.get("email") or a.get("username", "")
            email_pw = a.get("email_password")
            if email_pw:
                return email, email_pw
            # Legacy accounts without stored email password
            return email, None
    return None, None


def fetch_verification_code(user, lookback_minutes=30):
    """Fetch the latest verification code for an account's email.
    Returns the code string or None."""
    if _mail_manager is None:
        log.warning("MailManager not available — cannot fetch verification code for %s", user)
        return None
    email, email_pw = get_account_email_info(user)
    if not email or not email_pw:
        log.warning("No email_password stored for %s — cannot fetch verification code", user)
        return None
    code = _mail_manager.get_verification_code(email, email_pw, lookback_minutes=lookback_minutes)
    if code:
        log.info("✅ Verification code for %s (%s): %s", user, email, code)
    return code


def fetch_account_messages(user, limit=10):
    """Fetch recent messages for an account's email.
    Returns list of message dicts or empty list."""
    if _mail_manager is None:
        return []
    email, email_pw = get_account_email_info(user)
    if not email or not email_pw:
        return []
    return _mail_manager.fetch_messages(email, email_pw, limit=limit)


def check_all_emails_for_codes(accounts=None):
    """Check all accounts' emails for new verification codes.
    Returns dict of {username: code} for accounts with codes found."""
    if _mail_manager is None:
        return {}
    if accounts is None:
        accounts = load_accounts()
    results = {}
    for a in accounts:
        user = a.get("username", "")
        email_pw = a.get("email_password")
        if not email_pw:
            continue
        email = a.get("email") or user
        code = _mail_manager.get_verification_code(email, email_pw, lookback_minutes=15)
        if code:
            results[user] = code
            log.info("✅ Code found for %s: %s", user, code)
    return results


def get_email_status_summary():
    """Get summary of email access status for all accounts.
    Returns dict with counts and per-account details."""
    accounts = load_accounts()
    total = len(accounts)
    with_email_pw = 0
    without_email_pw = 0
    details = []
    for a in accounts:
        user = a.get("username", "")
        has_pw = bool(a.get("email_password"))
        email = a.get("email") or user
        if has_pw:
            with_email_pw += 1
        else:
            without_email_pw += 1
        details.append({
            "username": user,
            "email": email,
            "has_email_password": has_pw,
            "can_read_email": has_pw and _mail_manager is not None,
        })
    return {
        "total_accounts": total,
        "with_email_password": with_email_pw,
        "without_email_password": without_email_pw,
        "mail_manager_available": _mail_manager is not None,
        "details": details,
    }


def is_ban_error(status_code=None, error_text=None):
    """
    Detect whether an HTTP response or error message indicates a BAN.

    Returns True if the response is a BAN signal.
    """
    if status_code is not None:
        # 403 = Forbidden — definitive BAN signal
        if status_code == 403:
            return True
        # 401 persistent = credentials blocked (checked separately by caller)

    if error_text:
        text_lower = error_text.lower()
        for keyword in BAN_ERROR_KEYWORDS:
            if keyword in text_lower:
                return True
    return False


def mark_banned(user, reason, consecutive_failures=0):
    """
    Mark an account as banned:
    1. Remove from accounts.json
    2. Add to banned_accounts.json with reason and timestamp
    3. Clean up the session
    4. Log clearly
    """
    timestamp = datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
    log.warning("[%s] 🚫 BAN DETECTED — removing account: %s", user, reason)

    # Remove from accounts.json
    try:
        accounts = load_accounts()
        original_count = len(accounts)
        accounts = [a for a in accounts if a.get("username") != user]
        if len(accounts) < original_count:
            save_accounts(accounts)
            log.info("[%s] removed from accounts.json (now %d accounts)", user, len(accounts))
    except Exception as e:
        log.error("[%s] failed to remove from accounts.json: %s", user, e)

    # Add to banned_accounts.json
    try:
        banned = load_banned_accounts()
        banned["banned"].append({
            "username": user,
            "reason": reason,
            "consecutive_failures": consecutive_failures,
            "detected_at": timestamp,
        })
        save_banned_accounts(banned)
        log.info("[%s] added to banned_accounts.json (total banned: %d)",
                 user, len(banned["banned"]))
    except Exception as e:
        log.error("[%s] failed to save banned_accounts.json: %s", user, e)

    # Clean up session
    with sessions_lock:
        if user in SESSIONS:
            del SESSIONS[user]
            log.info("[%s] session cleared", user)

    # Update status.json to mark BAN
    try:
        status = load_status()
        if user in status:
            status[user]["last_result"] = "BAN"
            status[user]["banned"] = True
            status[user]["banned_at"] = timestamp
            status[user]["ban_reason"] = reason
            save_status(status)
    except Exception:
        pass


# Track consecutive failures per account (in-memory + status.json)
CONSECUTIVE_FAILURES = {}  # username -> int
LOGIN_FAILURE_COUNTS = {}  # username -> int


def load_accounts():
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError("accounts.json must be a non-empty JSON list of {username, password} objects")
    out = []
    for acc in data:
        if not acc.get("username") or not acc.get("password"):
            raise ValueError(f"account missing username/password: {acc}")
        out.append(acc)
    return out


def load_status():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_status(status):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Revenue Tracker: persistent earnings history
# --------------------------------------------------------------------------- #
def load_earnings():
    if os.path.exists(EARNINGS_FILE):
        try:
            with open(EARNINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_earnings(earnings):
    with open(EARNINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(earnings, f, ensure_ascii=False, indent=2)


def update_earnings(status):
    """Update earnings.json with today's earnings from all accounts."""
    today = today_str()
    earnings = load_earnings()

    # Ensure structure
    earnings.setdefault("daily", {})
    earnings.setdefault("weekly", {})
    earnings.setdefault("monthly", {})
    earnings.setdefault("all_time", {"total_usdt": 0.0, "total_trips": 0, "updated": ""})

    accounts = {k: v for k, v in status.items() if k != "_meta" and isinstance(v, dict)}

    # Calculate today's totals — use earnings_today (actual trip earnings),
    # NOT wallet balance (which is the running total, not today's earning).
    today_usdt = 0.0
    today_trips = 0
    acc_breakdown = {}
    for user, acc in accounts.items():
        earned_today = acc.get("earnings_today", 0) or 0
        trips_today = acc.get("trips_started_today", 0) or 0
        wallet_usdt = acc.get("wallet", {}).get("usdt", 0) or 0
        today_usdt += earned_today
        today_trips += trips_today

        # Per-account breakdown — store today's earning, not wallet balance
        acc_breakdown[user] = {
            "usdt": earned_today,
            "trips": trips_today,
            "wallet_usdt": wallet_usdt,
        }

    # Set (not accumulate) today's totals — update_earnings runs every sweep,
    # so += would multiply the values by the number of sweeps per day.
    earnings["daily"][today] = {
        "usdt": today_usdt,
        "trips": today_trips,
        "accounts": acc_breakdown,
    }

    # Weekly (current ISO week: YYYY-Www)
    week_key = datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-W%W")
    earnings["weekly"].setdefault(week_key, {"usdt": 0.0, "trips": 0, "days": {}})
    earnings["weekly"][week_key]["usdt"] = today_usdt
    earnings["weekly"][week_key]["trips"] = today_trips
    earnings["weekly"][week_key]["days"][today] = {"usdt": today_usdt, "trips": today_trips}

    # Monthly (YYYY-MM)
    month_key = datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m")
    earnings["monthly"].setdefault(month_key, {"usdt": 0.0, "trips": 0})
    earnings["monthly"][month_key]["usdt"] = today_usdt
    earnings["monthly"][month_key]["trips"] = today_trips

    # All-time: sum from API stats
    total_usdt = 0.0
    total_trips = 0
    for acc in accounts.values():
        s = acc.get("stats", {})
        total_usdt += s.get("usdt_earned", 0) or 0
        total_trips += s.get("trips", 0) or 0

    earnings["all_time"]["total_usdt"] = total_usdt
    earnings["all_time"]["total_trips"] = total_trips
    earnings["all_time"]["updated"] = datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d %H:%M:%S")

    # Prune old data (keep 90 days daily, 12 weeks weekly, 24 months monthly)
    def prune_old(d, days=90):
        cutoff = datetime.fromtimestamp(time.time() - days * 86400, tz=TZ).strftime("%Y-%m-%d")
        return {k: v for k, v in d.items() if k >= cutoff}

    def prune_weeks(d, weeks=12):
        cutoff = datetime.fromtimestamp(time.time() - weeks * 7 * 86400, tz=TZ).strftime("%Y-W%W")
        return {k: v for k, v in d.items() if k >= cutoff}

    def prune_months(d, months=24):
        cutoff = datetime.fromtimestamp(time.time() - months * 30 * 86400, tz=TZ).strftime("%Y-%m")
        return {k: v for k, v in d.items() if k >= cutoff}

    earnings["daily"] = prune_old(earnings.get("daily", {}))
    earnings["weekly"] = prune_weeks(earnings.get("weekly", {}))
    earnings["monthly"] = prune_months(earnings.get("monthly", {}))

    save_earnings(earnings)

    # Log summary
    log.info("Revenue Tracker: today=%.4f USDT | week=%.4f | month=%.4f | all_time=%.4f",
             today_usdt,
             earnings["weekly"].get(week_key, {}).get("usdt", 0),
             earnings["monthly"].get(month_key, {}).get("usdt", 0),
             total_usdt)


def make_session(proxy=None):
    """Session with automatic retries on transient connection/SSL/429/5xx errors.
    Optionally configured with a proxy dict {"http": url, "https": url}."""
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    if proxy:
        s.proxies.update(proxy)
    return s


def get_session(user):
    """Get (or create) a session for a user. Assigns a proxy per account."""
    with sessions_lock:
        if user not in SESSIONS:
            proxy = None
            if PROXY_MANAGER:
                proxy = PROXY_MANAGER.get_proxy(account_username=user)
            SESSIONS[user] = make_session(proxy=proxy)
            if proxy:
                log.info("[%s] session created with proxy: %s", user, proxy["https"])
            else:
                log.info("[%s] session created (direct, no proxy)", user)
        return SESSIONS[user]


def _report_proxy_error(session, user, exc):
    """If the session uses a proxy and a connection error occurred, report it dead."""
    if not PROXY_MANAGER or not session.proxies:
        return
    proxy_url = session.proxies.get("https") or session.proxies.get("http")
    if proxy_url:
        PROXY_MANAGER.report_dead(proxy_url)
        log.warning("[%s] proxy error on %s: %s — proxy reported dead, will retry direct on next login",
                    user, proxy_url, short_err(exc) if 'short_err' in dir() else str(exc))


def login(session, user):
    """
    Log in (in place). Returns the session. Raises on failure.
    Tracks consecutive login failures and detects BAN.
    """
    acc = ACCOUNTS.get(user)
    if not acc:
        raise RuntimeError(f"account {user} not found in accounts.json")
    try:
        r = session.post(f"{BASE}/auth/login", headers=HEADERS,
                         json={"username": user, "password": acc["password"]}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            error_msg = str(data.get("error", {}))
            # Check for BAN keywords in login error
            if is_ban_error(error_text=error_msg):
                LOGIN_FAILURE_COUNTS[user] = LOGIN_FAILURE_COUNTS.get(user, 0) + 1
                if BAN_DETECTION_ENABLED and LOGIN_FAILURE_COUNTS[user] >= BAN_LOGIN_FAILURE_THRESHOLD:
                    mark_banned(user, f"login rejected: {error_msg}",
                                consecutive_failures=LOGIN_FAILURE_COUNTS[user])
                raise RuntimeError(f"login error: {error_msg}")
            # Non-BAN login error — track but don't ban
            LOGIN_FAILURE_COUNTS[user] = LOGIN_FAILURE_COUNTS.get(user, 0) + 1
            raise RuntimeError(f"login error: {error_msg}")
        # Success — reset login failure count
        LOGIN_FAILURE_COUNTS[user] = 0
        log.info("[%s] logged in OK", user)
        return session
    except requests.exceptions.HTTPError as e:
        # 403 = BAN
        if e.response is not None and e.response.status_code == 403:
            if BAN_DETECTION_ENABLED and BAN_403_AUTO_REMOVE:
                mark_banned(user, f"HTTP 403 Forbidden on login",
                            consecutive_failures=BAN_LOGIN_FAILURE_THRESHOLD)
        raise
    except requests.exceptions.ConnectionError as e:
        _report_proxy_error(session, user, e)
        raise


def api_post(session, endpoint, payload, user):
    """POST with session self-healing: on 401 re-login once and retry.
    On proxy connection error: report proxy dead, recreate session direct, retry once."""
    try:
        r = session.post(f"{BASE}/{endpoint}", headers=HEADERS, json=payload, timeout=30)
        if r.status_code == 401:
            log.warning("[%s] session expired (401) on %s -> re-logging in and retrying", user, endpoint)
            session = login(session, user)
            r = session.post(f"{BASE}/{endpoint}", headers=HEADERS, json=payload, timeout=30)
        return r
    except requests.exceptions.ConnectionError as e:
        # Proxy might be dead — report it and retry with a fresh direct session
        _report_proxy_error(session, user, e)
        log.warning("[%s] connection error on %s: %s -> recreating session (direct) and retrying once",
                    user, endpoint, e)
        # Recreate session without proxy and retry
        with sessions_lock:
            old_session = SESSIONS.pop(user, None)
        session = make_session(proxy=None)  # direct, no proxy
        SESSIONS[user] = session
        session = login(session, user)
        return session.post(f"{BASE}/{endpoint}", headers=HEADERS, json=payload, timeout=30)


def get_state(session, user):
    r = api_post(session, "state", {}, user)
    r.raise_for_status()
    return r.json()


def start_trip(session, car_id, user):
    return api_post(session, "trips/start", {"carId": car_id}, user)


# --------------------------------------------------------------------------- #
# Mission Center: fetch missions state and auto-claim completed rewards
# --------------------------------------------------------------------------- #
def fetch_missions(session, user):
    """Fetch the Mission Center state. Returns parsed data or None on failure."""
    try:
        r = api_post(session, "missions", {}, user)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("[%s] missions fetch failed: %s", user, short_err(e))
        return None


def claim_mission(session, user, mission_id):
    """Claim a mission reward. Returns parsed data (with error) or None."""
    try:
        r = api_post(session, "missions/claim", {"id": mission_id}, user)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("[%s] mission claim failed for %s: %s", user, mission_id, short_err(e))
        return None


# --------------------------------------------------------------------------- #
# Auto-Rent: monitor market for available cars and auto-rent them
# --------------------------------------------------------------------------- #
def check_market(session, user):
    """Check market for available car listings to rent. Returns list of listings."""
    try:
        r = api_post(session, "market", {}, user)
        r.raise_for_status()
        data = r.json()
        listings = data.get("listings", []) or []
        return listings
    except Exception as e:
        log.warning("[%s] market check failed: %s", user, short_err(e))
        return []


def rent_car_from_listing(session, user, listing):
    """Attempt to rent a car from a market listing."""
    listing_id = listing.get("id")
    name = listing.get("name", listing_id)
    try:
        r = api_post(session, RENT_ENDPOINT, {"listingId": listing_id}, user)
        if r.status_code == 404:
            log.info("[%s] rent endpoint not found (404) — listing '%s' skipped", user, name)
            return False
        if r.status_code == 400:
            err = r.json().get("error", {}).get("message", "400")
            log.warning("[%s] rent rejected for '%s': %s", user, name, err)
            return False
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            log.warning("[%s] rent failed for '%s': %s", user, name, data["error"])
            return False
        contract = data.get("contract", {})
        log.info("[%s] RENTED car: %s | contract: %s | shift: %s | dailyTrips: %s",
                 user, name, contract.get("id"), contract.get("shift"), contract.get("dailyTrips"))
        return True
    except Exception as e:
        log.warning("[%s] rent error for '%s': %s", user, name, short_err(e))
        return False


def _auto_rent_worker():
    """
    Background worker: checks market every RENT_MARKET_INTERVAL and rents
    available listings — but never exceeds MAX_RENTED_PER_ACCOUNT per account.

    Strategy:
    - For each account, count current rented cars from status.json
    - Only rent for accounts with < MAX_RENTED_PER_ACCOUNT rented cars
    - When an account hits the max, skip it and move to the next account
    - Accounts already above the max (e.g. legacy accounts with 4) are skipped
      — they keep their existing rented cars but no new ones are added
    """
    while True:
        try:
            accounts = load_accounts()
            status = load_status()
            for acc in accounts:
                user = acc["username"]
                # Count current rented cars for this account
                acc_status = status.get(user, {})
                cars = acc_status.get("cars", [])
                rented_count = sum(1 for c in cars if c.get("rented"))

                # Skip if already at or above the max
                if rented_count >= MAX_RENTED_PER_ACCOUNT:
                    log.debug("[%s] skipped — already has %d rented car(s) (max %d)",
                              user, rented_count, MAX_RENTED_PER_ACCOUNT)
                    continue

                # Account has room for more — check market
                session = get_session(user)
                if not session.cookies:
                    continue

                listings = check_market(session, user)
                if not listings:
                    log.debug("[%s] market empty — no cars to rent", user)
                    continue

                # Only rent enough to reach the max
                slots = MAX_RENTED_PER_ACCOUNT - rented_count
                log.info("[%s] market: %d listing(s) found, %d slot(s) available — attempting auto-rent",
                         user, len(listings), slots)
                rented = 0
                for listing in listings:
                    if rented >= slots:
                        break  # reached the max for this account
                    if rent_car_from_listing(session, user, listing):
                        rented += 1
                        time.sleep(3)  # delay between rent attempts
                if rented:
                    log.info("[%s] auto-rent: %d car(s) rented (now %d total, max %d)",
                             user, rented, rented_count + rented, MAX_RENTED_PER_ACCOUNT)
                else:
                    log.info("[%s] auto-rent: no cars rented (endpoint may need adjustment)", user)
        except Exception as e:
            log.warning("auto-rent worker error: %s", e)
        time.sleep(RENT_MARKET_INTERVAL)


def start_auto_rent():
    t = threading.Thread(target=_auto_rent_worker, daemon=True, name="auto-rent")
    t.start()
    log.info("auto-rent worker started (checking market every %ds)", RENT_MARKET_INTERVAL)
    return t


# --------------------------------------------------------------------------- #
# FREE section: visit ads, wait for timer, claim USDT
# --------------------------------------------------------------------------- #
def fetch_free_links(session, user):
    """Fetch available FREE ads/links from the server.
    Returns parsed JSON with links list, or None on failure."""
    try:
        r = session.post(f"{BASE}/free", headers=HEADERS, json={}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return None
        return data
    except Exception as e:
        log.warning("[%s] free fetch failed: %s", user, short_err(e))
        return None


def free_start(session, user, link_id):
    """Start a FREE ad visit. Server begins the timer.
    Returns the pending data (with endsAt) or None."""
    try:
        r = session.post(f"{BASE}/free/start", headers=HEADERS,
                         json={"linkId": link_id}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return None
        return data
    except Exception as e:
        log.warning("[%s] free/start failed for link %s: %s", user, link_id, short_err(e))
        return None


def free_claim(session, user, visit_id):
    """Claim the reward for a completed FREE ad visit.
    Returns parsed JSON (with reward) or None."""
    try:
        r = session.post(f"{BASE}/free/claim", headers=HEADERS,
                         json={"visitId": visit_id}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return None
        return data
    except Exception as e:
        log.warning("[%s] free/claim failed for visit %s: %s", user, visit_id, short_err(e))
        return None


def process_free_for_account(session, user, status):
    """Process FREE ads for a single account.
    1. Fetch available links
    2. For each unvisited link, start visit + wait for timer + claim
    Returns dict with stats: links_found, started, claimed, earned."""
    stats = {"links_found": 0, "started": 0, "claimed": 0, "earned": 0.0}

    data = fetch_free_links(session, user)
    if not data:
        return stats

    links = data.get("links", [])
    stats["links_found"] = len(links)
    earned_total = data.get("earned", 0)
    stats["earned_total"] = earned_total

    pending = data.get("pending")
    # If there's an active pending visit, check if it's done and claim it
    if pending and pending.get("endsAt"):
        ends_at = pending["endsAt"] / 1000  # ms to s
        now = time.time()
        if now >= ends_at:
            # Timer is done — claim it
            res = free_claim(session, user, pending.get("visitId"))
            if res and res.get("reward"):
                reward = res.get("reward", 0)
                stats["claimed"] += 1
                stats["earned"] += reward
                log.info("[%s] FREE claim: +%.4f USDT (total earned: %.4f)", user, reward, earned_total + reward)
            else:
                log.debug("[%s] FREE: pending timer done but claim failed/empty", user)
        else:
            # Still waiting — skip for now
            remaining = ends_at - now
            log.debug("[%s] FREE: waiting %.0fs for ad timer", user, remaining)
            return stats
    else:
        # No pending — try to start a new visit on an available link
        available = [l for l in links if not l.get("done")]
        if available:
            # Pick a random available link
            link = random.choice(available)
            link_id = link.get("id")
            if link_id:
                res = free_start(session, user, link_id)
                if res and res.get("endsAt"):
                    stats["started"] += 1
                    log.info("[%s] FREE: started visit on '%s' — waiting %.0fs (reward: %.4f USDT)",
                             user, link.get("title", "ad"), (res["endsAt"]/1000 - time.time()),
                             link.get("reward", 0))

    return stats


def _free_worker():
    """Background worker: periodically processes FREE ads for all accounts.
    - Checks every FREE_INTERVAL seconds
    - For each account: fetch links, check pending timers, claim when done
    - Starts new visits on available ads when no pending
    """
    while True:
        try:
            if not FREE_ENABLED:
                time.sleep(FREE_INTERVAL)
                continue

            accounts = load_accounts()
            status = load_status()
            total_claimed = 0
            total_earned = 0.0

            for acc in accounts:
                user = acc["username"]
                session = get_session(user)
                if not session.cookies:
                    continue

                try:
                    stats = process_free_for_account(session, user, status)
                    if stats["claimed"] > 0:
                        total_claimed += stats["claimed"]
                        total_earned += stats["earned"]
                    # Update status.json with free stats
                    acc_status = status.setdefault(user, {})
                    free_data = acc_status.setdefault("free", {})
                    free_data["last_run"] = datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
                    free_data["links_found"] = stats.get("links_found", 0)
                    free_data["started"] = stats.get("started", 0)
                    free_data["claimed"] = stats.get("claimed", 0)
                    free_data["earned"] = stats.get("earned", 0)
                    free_data["total_earned"] = free_data.get("total_earned", 0) + stats.get("earned", 0)
                except Exception as e:
                    log.warning("[%s] free worker error: %s", user, short_err(e))

            # Persist status so the dashboard sees the FREE data
            save_status(status)

            if total_claimed > 0:
                log.info("FREE worker: %d claim(s), %.4f USDT earned this cycle",
                         total_claimed, total_earned)
        except Exception as e:
            log.warning("free worker error: %s", e)
        time.sleep(FREE_INTERVAL)


def start_free_worker():
    t = threading.Thread(target=_free_worker, daemon=True, name="free-worker")
    t.start()
    log.info("FREE worker started (checking every %ds)", FREE_INTERVAL)
    return t


# --------------------------------------------------------------------------- #
# Referral Center: fetch referral state and auto-claim extra trips
# --------------------------------------------------------------------------- #
def fetch_referrals(session, user):
    """Fetch the Referral Center state. Returns parsed data or None on failure."""
    try:
        r = api_post(session, "referrals", {}, user)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("[%s] referrals fetch failed: %s", user, short_err(e))
        return None


def claim_referral_trips(session, user, car_id):
    """Claim referral extra trips into a car. Returns parsed data (with error) or None."""
    try:
        r = api_post(session, "referrals/claim", {"carId": car_id}, user)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("[%s] referral claim failed for car %s: %s", user, car_id, short_err(e))
        return None


# --------------------------------------------------------------------------- #
# stats / reporting
# --------------------------------------------------------------------------- #
def reset_daily_if_needed(status):
    today = today_str()
    if status.get("_meta", {}).get("date") != today:
        status["_meta"] = {"date": today}
        for acc in status.values():
            if isinstance(acc, dict) and "username" in acc:
                # archive today's earnings into the per-account history before reset
                hist = list(acc.get("earnings_history", []))
                today_earn = acc.get("earnings_today", 0) or 0
                if today_earn:
                    hist.append({"date": status["_meta"]["date"], "earned": today_earn})
                # keep last 90 days of history
                acc["earnings_history"] = hist[-90:]
                acc["trips_started_today"] = 0
                acc["checks_today"] = 0
                acc["errors_today"] = 0
                acc["earnings_today"] = 0.0


def write_daily_report(status):
    today = today_str()
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"report-{today}.md")
    accounts = {k: v for k, v in status.items() if k != "_meta" and isinstance(v, dict)}
    total_trips_today = sum(a.get("trips_started_today", 0) for a in accounts.values())
    total_checks_today = sum(a.get("checks_today", 0) for a in accounts.values())
    total_missions_today = sum(a.get("missions_claimed_today", 0) for a in accounts.values())
    total_referrals_today = sum(a.get("referrals_claimed_today", 0) for a in accounts.values())
    lines = [
        f"# Daily Report — {today}",
        "",
        f"_Generated: {datetime.fromtimestamp(time.time(), tz=TZ).strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
        f"**Summary:** {len(accounts)} account(s) · {total_trips_today} trips started today · "
        f"{total_checks_today} monitor checks today · **{total_missions_today} mission(s) claimed** · "
        f"**{total_referrals_today} referral trip(s) claimed**",
        "",
        "| Account | Checks today | Trips today | Missions claimed | Referral trips | Total trips (all days) | Errors today | Last run | Status |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for user, a in accounts.items():
        lines.append(
            "| {u} | {c} | {t} | {m} | {r} | {tt} | {e} | {lr} | {st} |".format(
                u=user,
                c=a.get("checks_today", 0),
                t=a.get("trips_started_today", 0),
                m=a.get("missions_claimed_today", 0),
                r=a.get("referrals_claimed_today", 0),
                tt=a.get("trips_started_total", 0),
                e=a.get("errors_today", 0),
                lr=a.get("last_run", "-"),
                st=a.get("last_result", "-"),
            )
        )
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("daily report written: %s", path)
    return path


# --------------------------------------------------------------------------- #
# one monitor sweep for one account
# --------------------------------------------------------------------------- #
def short_err(e):
    """Readable one-line error message."""
    s = str(e)
    if "reset by peer" in s or "Connection aborted" in s:
        return "Connection reset by peer (104) — خطای اتصال موقت"
    return f"{type(e).__name__}: {s}"


SESSION_NOTED = {}  # username -> True once "session active, no login" was logged


def process_account(acc, status):
    user = acc["username"]
    # Stagger: the parallel workers would otherwise all hit the server at
    # the exact same instant. A small random lead time (0.5-3s) spreads the
    # requests so the pattern looks natural and lowers 429 / rate-limit risk.
    time.sleep(random.uniform(0.5, 3.0))
    trips_started = 0
    errors_today = 0
    earned_today = 0.0   # USDT earned today (from trips started in this sweep)
    earned_total = 0.0   # USDT earned today across all starts this sweep
    recent = list(status.get(user, {}).get("recent_errors", []))

    def record(msg, why=None, level="error"):
        nonlocal errors_today
        if level == "error":
            errors_today += 1
        recent.append({
            "t": datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "e": msg,
            "why": why or msg,
            "level": level,
        })
        if len(recent) > RECENT_ERRORS_LIMIT:
            recent.pop(0)

    def do_sweep():
        nonlocal trips_started, errors_today, earned_today, earned_total
        session = get_session(user)
        # Reuse the active session cookie. Log in ONLY when there is no cookie
        # yet (fresh session / after restart) or when the server rejects us
        # with 401 (self-heal in api_post). No per-sweep logins -> no suspicious
        # repeated logins, no ban risk.
        if not session.cookies:
            session = login(session, user)
            SESSION_NOTED[user] = False
        elif not SESSION_NOTED.get(user):
            log.info("[%s] session active — reusing cookie, no login", user)
            SESSION_NOTED[user] = True
        state = get_state(session, user)
        # PRIORITY: own car FIRST, then rented cars. The own (main) car is
        # started before rented cars; when the own car's trips and extra/bonus
        # trips are exhausted, the sweep naturally moves on to rented cars.
        rented_cars = state.get("rentedCars", [])
        own_cars = state.get("myCars", [])
        cars = [(c, True) for c in rented_cars] + [(c, False) for c in own_cars]
        now = int(time.time() * 1000)

        car_infos = []
        ready = []
        for car, is_rented in cars:
            car_id = car.get("id")
            name = car.get("name", str(car_id))
            plate = car.get("plate", "")
            quota = car.get("quota", {})
            if car.get("trip"):
                status_code = "on_trip"
                ends_at = car["trip"].get("endsAt", 0)
                started_at = car["trip"].get("startedAt", 0)
                minutes_left = round((ends_at - now) / 60000)
                car_infos.append({
                    "id": car_id, "name": name, "plate": plate, "status": "on_trip",
                    "ends_at": ends_at, "started_at": started_at, "minutes_left": minutes_left,
                    "rented": is_rented,
                    "used": quota.get("used", 0), "limit": quota.get("limit", 0),
                    "bonus": quota.get("bonus", 0), "bonus_expires_at": quota.get("bonusExpiresAt"),
                })
            elif (not car.get("broken")
                  and car.get("trip") is None
                  and (quota.get("left", 0) > 0 or quota.get("bonus", 0) > 0)):
                # A car is "ready" to start a trip if it has quota remaining
                # (or bonus trips), is not broken, and is not already on a trip.
                # We do NOT rely on canDrive: the server sets canDrive=False
                # on the own car when a rented car is already on a trip
                # (block.code="driving"), but the server MAY accept a second
                # concurrent trip (rented + own) in some cases (different
                # model, stale flag, etc.). We attempt the start and let the
                # server validate — if it rejects with 400, we skip gracefully.
                status_code = "idle"
                car_infos.append({
                    "id": car_id, "name": name, "plate": plate, "status": "idle",
                    "ends_at": None, "minutes_left": 0, "rented": is_rented,
                    "used": quota.get("used", 0), "limit": quota.get("limit", 0),
                    "bonus": quota.get("bonus", 0), "bonus_expires_at": quota.get("bonusExpiresAt"),
                })
                ready.append((car, is_rented))
            else:
                status_code = "unavailable"
                # Capture the block reason so the dashboard can show WHY
                block = car.get("block", {})
                block_code = block.get("code", "") if block else ""
                block_msg = block.get("msg", "") if block else ""
                car_infos.append({
                    "id": car_id, "name": name, "plate": plate, "status": "unavailable",
                    "ends_at": None, "minutes_left": 0, "rented": is_rented,
                    "used": quota.get("used", 0), "limit": quota.get("limit", 0),
                    "bonus": quota.get("bonus", 0), "bonus_expires_at": quota.get("bonusExpiresAt"),
                    "block_code": block_code, "block_msg": block_msg,
                })

        # Priority: own car FIRST, then rented cars.
        # Use the own (main) car first. When the own car finishes its trip
        # AND its extra/bonus trips are exhausted, move on to rented cars.
        # The server MAY accept both (own + rented) or reject the second one
        # with a 400 — we attempt both independently, each 400 is logged and
        # skipped without affecting the other car's start attempt.
        own_ready = [c for c, is_r in ready if not is_r]
        rented_ready = [c for c, is_r in ready if is_r]
        to_start = []
        if own_ready:
            to_start.append(own_ready[0])
        if rented_ready:
            to_start.append(rented_ready[0])
        if not to_start:
            log.info("[%s] no idle car to start (all on trip / quota full / shift)", user)
        else:
            log.info("[%s] attempting to start up to 2 cars (own first, then rented): %s",
                     user, [c.get("name", c.get("id")) for c in to_start])
            for car in to_start:
                car_id = car.get("id")
                name = car.get("name", car_id)
                try:
                    r = start_trip(session, car_id, user)
                    if r.status_code == 429:
                        log.warning("[%s] rate limited (429) on %s; waiting 10s and retrying once", user, name)
                        time.sleep(10)
                        r = start_trip(session, car_id, user)
                        if r.status_code == 429:
                            log.warning("[%s] still rate limited on %s; skipping", user, name)
                            record(f"rate limited (429) on {name}",
                                   why="سرور درخواست‌های زیاد را محدود کرد (rate limit) — پس از ۱۰ ثانیه attempt مجدد نیز متوقف شد",
                                   level="error")
                            continue
                    if r.status_code == 401:
                        session = login(session, user)
                        r = start_trip(session, car_id, user)
                    if r.status_code == 400:
                        err_msg = r.json().get("error", {}).get("message", "400 Bad Request")
                        record(f"start rejected (400) for {name}",
                               why=f"سرور استارت را رد کرد: {err_msg}", level="warning")
                        continue
                    r.raise_for_status()
                    data = r.json()
                    if data.get("error"):
                        log.error("[%s] start FAILED for %s (%s): %s", user, name, car_id, data["error"])
                        record(f"start failed: {name} — {data['error']}",
                               why="سرور درخواست استارت را با خطا رد کرد")
                    else:
                        trip = data.get("trip", {})
                        mins = round((trip.get("endsAt", 0) - trip.get("startedAt", 0)) / 60000) if trip else "?"
                        log.info("[%s] STARTED trip for %s (%s), ~%s min", user, name, car_id, mins)
                        trips_started += 1
                        earning = trip.get("earning", 0) or 0
                        if earning:
                            earned_today += earning
                            earned_total += earning
                        # reflect the new trip immediately so the dashboard shows
                        # the live countdown right away (no waiting for next sweep)
                        for cinfo in car_infos:
                            if cinfo.get("id") == car_id:
                                cinfo["status"] = "on_trip"
                                cinfo["ends_at"] = trip.get("endsAt", 0)
                                cinfo["started_at"] = trip.get("startedAt", 0)
                                cinfo["minutes_left"] = round((cinfo["ends_at"] - now) / 60000)
                        time.sleep(2)
                except requests.exceptions.ConnectionError as e:
                    msg = short_err(e)
                    log.warning("[%s] connection error starting %s (%s): %s -> retrying once in 10s",
                                user, name, car_id, msg)
                    time.sleep(10)
                    try:
                        r = start_trip(session, car_id, user)
                        r.raise_for_status()
                        data = r.json()
                        if data.get("error"):
                            record(f"start failed after retry: {name} — {data['error']}",
                                   why="اتصال به سرور قطع شد و attempt مجدد نیز با خطا سرور مواجه شد")
                        else:
                            record(f"connection error while starting {name}",
                                   why="اتصال به سرور در میانه قطع شد (Connection reset by peer) — پس از ۱۰ ثانیه attempt مجدد موفق شد",
                                   level="warning")
                            trip = data.get("trip", {})
                            log.info("[%s] STARTED trip for %s (%s) after retry, ~%s min",
                                     user, name, car_id,
                                     round((trip.get("endsAt", 0) - trip.get("startedAt", 0)) / 60000) if trip else "?")
                            trips_started += 1
                            for cinfo in car_infos:
                                if cinfo.get("id") == car_id:
                                    cinfo["status"] = "on_trip"
                                    cinfo["ends_at"] = trip.get("endsAt", 0)
                                    cinfo["started_at"] = trip.get("startedAt", 0)
                                    cinfo["minutes_left"] = round((cinfo["ends_at"] - now) / 60000)
                    except Exception as e2:
                        log.error("[%s] start FAILED for %s (%s) after retry: %s", user, name, car_id, e2)
                        record(f"start failed after retry: {name} — {short_err(e2)}",
                               why="اتصال به سرور قطع شد و attempt مجدد نیز موفق نشد")
                except Exception as e:
                    log.error("[%s] start FAILED for %s (%s): %s", user, name, car_id, e)
                    record(f"start failed: {name} — {short_err(e)}",
                           why="خطای نامشخص در درخواست استارت")

        # --- Mission Center: fetch missions and auto-claim completed rewards.
        # Missions that are done, not yet claimed, and have a reward get
        # claimed automatically. Video / telegram missions need manual user
        # interaction (submitting a link / joining a channel) — never
        # auto-claimed.
        missions_claimed = 0
        missions_payload = None
        missions_data = fetch_missions(session, user)
        if missions_data is not None:
            claims = []
            for m in list(missions_data.get("daily", [])) + list(missions_data.get("achievements", [])):
                if (m.get("done") and not m.get("claimed") and m.get("reward")
                        and m.get("kind") not in ("video", "telegram")):
                    res = claim_mission(session, user, m.get("id"))
                    if res and not res.get("error"):
                        missions_claimed += 1
                        reward = res.get("reward", {})
                        log.info("[%s] mission CLAIMED: %s -> %s %s",
                                 user, m.get("id"), reward.get("amount"), reward.get("cur"))
                        claims.append({"id": m.get("id"), "reward": reward,
                                       "t": datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d %H:%M:%S")})
                    else:
                        log.warning("[%s] mission claim rejected: %s", user, m.get("id"))
            missions_payload = {
                "daily": missions_data.get("daily", []),
                "achievements": missions_data.get("achievements", []),
                "resetsAt": missions_data.get("resetsAt", 0),
                "claims": claims,
            }

        # --- Referral Center: claim pending extra trips into the reward car.
        # When referrals complete their 10/10 daily service, the site grants
        # bonus trips (pending) that must be claimed into a car; those trips
        # then extend the car's daily quota beyond the normal 10. We claim
        # them automatically so the main account keeps driving.
        referrals_claimed = 0
        referrals_payload = None
        referrals_data = fetch_referrals(session, user)
        if referrals_data is not None and not referrals_data.get("error"):
            pending = referrals_data.get("pending") or {}
            pending_trips = pending.get("trips", 0) or 0
            # pick the reward car: prefer the site's suggested rewardCar,
            # otherwise the first claimable own car.
            reward_car = referrals_data.get("rewardCar") or {}
            car_id = reward_car.get("id")
            if not car_id:
                for c in cars:
                    if c.get("isMine") and not c.get("broken") and c.get("canDrive"):
                        car_id = c.get("id")
                        break
            if pending_trips > 0 and car_id:
                res = claim_referral_trips(session, user, car_id)
                if res and not res.get("error"):
                    referrals_claimed = res.get("trips", pending_trips)
                    log.info("[%s] referral trips CLAIMED: %s extra trips -> car %s (%s)",
                             user, referrals_claimed, car_id,
                             reward_car.get("name", ""))
                else:
                    log.warning("[%s] referral claim rejected: %s", user, res)
            referrals_payload = {
                "pending": referrals_data.get("pending", {}),
                "rewardCar": reward_car,
                "stats": referrals_data.get("stats", {}),
                "bonuses": referrals_data.get("bonuses", []),
                "depositPct": referrals_data.get("depositPct", 0),
                "refCode": referrals_data.get("refCode", ""),
            }

        with stats_lock:
            reset_daily_if_needed(status)
            acc_stat = status.setdefault(user, {"username": user})
            acc_stat["last_run"] = datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
            acc_stat["last_result"] = "OK" if errors_today == 0 else f"{errors_today} error(s)"
            acc_stat["trips_started_total"] = acc_stat.get("trips_started_total", 0) + trips_started
            acc_stat["trips_started_today"] = acc_stat.get("trips_started_today", 0) + trips_started
            acc_stat["checks_today"] = acc_stat.get("checks_today", 0) + 1
            acc_stat["errors_today"] = acc_stat.get("errors_today", 0) + errors_today
            acc_stat["recent_errors"] = recent[:]
            acc_stat["cars"] = car_infos
            # Account-level features from the site's state (wallet / bonus trips / lifetime stats).
            balances = state.get("balances", {}) or {}
            acc_stat["wallet"] = {
                "usdt": balances.get("usdt", 0),
                "cash": balances.get("efectivo", 0),
            }
            acc_stat["bonus_trips"] = state.get("bonusTrips", 0)
            acc_stat["stats"] = {
                "trips": (state.get("stats", {}) or {}).get("trips", 0),
                "usdt_earned": (state.get("stats", {}) or {}).get("usdtEarned", 0),
            }
            acc_stat["missions"] = missions_payload or acc_stat.get("missions", {})
            acc_stat["missions_claimed_today"] = acc_stat.get("missions_claimed_today", 0) + missions_claimed
            acc_stat["missions_claimed_total"] = acc_stat.get("missions_claimed_total", 0) + missions_claimed
            acc_stat["referrals"] = referrals_payload or acc_stat.get("referrals", {})
            acc_stat["referrals_claimed_today"] = acc_stat.get("referrals_claimed_today", 0) + referrals_claimed
            acc_stat["referrals_claimed_total"] = acc_stat.get("referrals_claimed_total", 0) + referrals_claimed
            acc_stat["earnings_today"] = earned_today
            acc_stat["earnings_total"] = acc_stat.get("earnings_total", 0) + earned_total
        log.info("[%s] sweep: %d car(s) on trip, %d idle -> %d started, %d mission(s) claimed, %d referral trip(s) claimed, %d error(s)",
                 user, sum(1 for c in car_infos if c["status"] == "on_trip"), len(ready), trips_started, missions_claimed, referrals_claimed, errors_today)

    # run the sweep with one retry on transient connection errors
    sweep_success = False
    try:
        try:
            do_sweep()
            sweep_success = True
        except requests.exceptions.ConnectionError as e:
            msg = short_err(e)
            log.warning("[%s] connection error: %s -> retrying sweep once in 10s", user, msg)
            time.sleep(10)
            do_sweep()
            sweep_success = True
            record("connection error during sweep",
                   why="اتصال به سرور در میانه قطع شد (Connection reset by peer) — پس از ۱۰ ثانیه sweep مجدد موفق شد",
                   level="warning")
    except Exception as e:
        msg = short_err(e)
        log.error("[%s] sweep FAILED: %s", user, msg)
        record(f"sweep failed: {msg}",
               why="سرور پاسخ صحیحی به درخواست state نداد — ممکن است موقتی باشد")
        with stats_lock:
            reset_daily_if_needed(status)
            acc_stat = status.setdefault(user, {"username": user})
            acc_stat["last_run"] = datetime.fromtimestamp(time.time(), tz=TZ).strftime("%Y-%m-%d %H:%M:%S")
            acc_stat["last_result"] = f"FAILED: {msg}"
            acc_stat["errors_today"] = acc_stat.get("errors_today", 0) + 1
            acc_stat["recent_errors"] = recent[:]

    # --- BAN detection: track consecutive sweep failures ---
    if sweep_success:
        # Reset consecutive failure counter on success
        CONSECUTIVE_FAILURES[user] = 0
    else:
        CONSECUTIVE_FAILURES[user] = CONSECUTIVE_FAILURES.get(user, 0) + 1
        failures = CONSECUTIVE_FAILURES[user]
        log.warning("[%s] consecutive sweep failures: %d/%d",
                    user, failures, BAN_CONSECUTIVE_FAILURES)

        if BAN_DETECTION_ENABLED and failures >= BAN_CONSECUTIVE_FAILURES:
            # Also check if the error text contains BAN keywords
            ban_keyword_match = any(
                kw in msg.lower() for kw in BAN_ERROR_KEYWORDS
            )
            if ban_keyword_match:
                mark_banned(user, f"sweep error contains BAN keyword: {msg}",
                            consecutive_failures=failures)
            else:
                mark_banned(user, f"{failures} consecutive sweep failures",
                            consecutive_failures=failures)
        # Check for 403 in the error message
        elif "403" in msg and BAN_DETECTION_ENABLED and BAN_403_AUTO_REMOVE:
            mark_banned(user, f"HTTP 403 detected in sweep: {msg}",
                        consecutive_failures=failures)


# --------------------------------------------------------------------------- #
# sweeps
# --------------------------------------------------------------------------- #
def run_sweep(workers):
    accounts = load_accounts()

    # Load banned accounts list and skip them
    banned_list = set()
    if BAN_DETECTION_ENABLED:
        banned_data = load_banned_accounts()
        banned_list = {b["username"] for b in banned_data.get("banned", [])}
        if banned_list:
            # Also remove any banned accounts that somehow remain in accounts.json
            accounts = [a for a in accounts if a["username"] not in banned_list]
            if len(accounts) < len(load_accounts()):
                log.info("sweep: %d banned account(s) skipped/removed from processing",
                         len(load_accounts()) - len(accounts))

    with sessions_lock:
        ACCOUNTS.clear()
        for a in accounts:
            ACCOUNTS[a["username"]] = a
    status = load_status()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda acc: process_account(acc, status), accounts))
    with stats_lock:
        # Add proxy stats to meta for dashboard display
        if PROXY_MANAGER:
            status.setdefault("_meta", {})["proxy"] = PROXY_MANAGER.get_stats()
        save_status(status)
    update_earnings(status)
    write_daily_report(status)


def main():
    poll = DEFAULT_POLL
    workers = DEFAULT_WORKERS
    args = sys.argv[1:]
    if "--poll" in args:
        i = args.index("--poll")
        poll = int(args[i + 1])
    if "--workers" in args:
        i = args.index("--workers")
        workers = int(args[i + 1])
    once = "--once" in args

    # Initialize Proxy Manager (if enabled)
    global PROXY_MANAGER
    if PROXY_ENABLED:
        PROXY_MANAGER = ProxyManager()

    if once:
        run_sweep(workers)
        return

    log.info("Live monitor started. Poll: every %ds. Parallel accounts: up to %d. Press Ctrl+C to stop.",
             poll, workers)
    if AUTO_RENT_ENABLED:
        start_auto_rent()
    if FREE_ENABLED:
        start_free_worker()
    while True:
        try:
            run_sweep(workers)
        except Exception as e:
            log.exception("sweep crashed: %s", e)
        # JITTER: spread sweeps out with a random offset around the poll
        # interval (±20%). Requests no longer hit the server at perfectly
        # predictable times — looks more natural and lowers the risk of
        # rate limiting (429) / pattern detection.
        interval = poll + random.uniform(-poll * 0.2, poll * 0.2)
        slept = 0
        while slept < interval:
            step = min(30, interval - slept)
            time.sleep(step)
            slept += step


if __name__ == "__main__":
    main()
