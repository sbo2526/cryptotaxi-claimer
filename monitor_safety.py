"""Cross-replica ownership, request pacing and fail-closed monitoring policy."""

import hashlib
import math
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from runtime_config import monitoring_enabled, primary_username


class SafetyStop(RuntimeError):
    """Stop the worker; no automatic restart or login loop."""


class RetryLater(RuntimeError):
    def __init__(self, seconds):
        super().__init__('Server requested a cooldown')
        self.seconds = seconds


def check_policy(user, endpoint):
    if not monitoring_enabled():
        raise SafetyStop('External monitoring is disabled')
    if not primary_username() or user != primary_username():
        raise SafetyStop('Only the explicitly configured primary account is permitted')
    if endpoint not in ('auth/login', 'state'):
        raise SafetyStop('This endpoint is disabled in read-only mode')


def retry_after_seconds(value, now=None):
    """Never shorten a server-supplied Retry-After, including HTTP dates."""
    now = now or datetime.now(timezone.utc)
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return 300.0
        return max(300.0, seconds)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(300.0, (date - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 300.0


class RequestPacer:
    def __init__(self, interval=60.0, clock=time.monotonic, sleep=time.sleep):
        self.interval = max(60.0, interval)
        self.clock, self.sleep = clock, sleep
        self.next_allowed = 0.0

    def wait(self):
        while self.clock() < self.next_allowed:
            self.sleep(self.next_allowed - self.clock())
        self.next_allowed = self.clock() + self.interval

    def cooldown(self, seconds):
        self.next_allowed = max(self.next_allowed, self.clock() + seconds)


class SharedMonitorLock:
    """PostgreSQL session advisory lock, shared by ALL replicas/projects.

    Missing configuration, an occupied lock or database connectivity loss blocks
    website access. Use the SAME database on every deployment for this account.
    A lost connection is never silently reconnected by this worker.
    """
    def __init__(self, user, connect=None):
        self.user = user
        self.connection = None
        self.connect = connect
        self.key = int.from_bytes(hashlib.sha256(('cryptotaxi-monitor:' + user.strip().casefold()).encode()).digest()[:8],
                                  byteorder='big', signed=True)

    def __enter__(self):
        url = os.environ.get('MONITOR_LOCK_DATABASE_URL', '')
        if not url or not self.user:
            raise SafetyStop('A shared monitor lock database and primary account are required')
        try:
            if self.connect is None:
                import psycopg
                self.connect = psycopg.connect
            self.connection = self.connect(url, autocommit=True, connect_timeout=10)
            result = self.connection.execute('SELECT pg_try_advisory_lock(%s)', (self.key,)).fetchone()
            if not result or result[0] is not True:
                raise SafetyStop('Another monitor already owns this account')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS cryptotaxi_monitor_safety (
                monitor_key BIGINT PRIMARY KEY,
                suspended BOOLEAN NOT NULL DEFAULT FALSE,
                next_request_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )''')
            self.connection.execute('''INSERT INTO cryptotaxi_monitor_safety (monitor_key)
                VALUES (%s) ON CONFLICT (monitor_key) DO NOTHING''', (self.key,))
            self.check()
        except Exception:
            self.close()
            # Do not expose DSNs, passwords, hostnames or driver exception text.
            raise SafetyStop('Unable to obtain exclusive monitor ownership') from None
        return self

    def check(self):
        try:
            if self.connection is None or self.connection.closed:
                raise SafetyStop('Monitor ownership was lost')
            row = self.connection.execute(
                'SELECT suspended FROM cryptotaxi_monitor_safety WHERE monitor_key = %s',
                (self.key,),
            ).fetchone()
            if not row or row[0] is not False:
                raise SafetyStop('Account is persistently suspended')
        except Exception:
            raise SafetyStop('Monitor ownership or safety state is unavailable; worker stopped') from None

    def before_request(self):
        self.check()
        try:
            remaining = self.connection.execute('''SELECT GREATEST(0,
                EXTRACT(EPOCH FROM (next_request_at - NOW())))
                FROM cryptotaxi_monitor_safety WHERE monitor_key = %s''', (self.key,)).fetchone()
            if not remaining:
                raise SafetyStop('Missing shared pacing state')
            if float(remaining[0]) > 0:
                raise RetryLater(float(remaining[0]))
            self.connection.execute('''UPDATE cryptotaxi_monitor_safety
                SET next_request_at = NOW() + INTERVAL '60 seconds' WHERE monitor_key = %s''', (self.key,))
        except RetryLater:
            raise
        except Exception:
            raise SafetyStop('Unable to reserve shared request budget') from None

    def cooldown(self, seconds):
        self.check()
        try:
            self.connection.execute('''UPDATE cryptotaxi_monitor_safety SET next_request_at =
                GREATEST(next_request_at, NOW() + (%s * INTERVAL '1 second'))
                WHERE monitor_key = %s''', (seconds, self.key))
        except Exception:
            raise SafetyStop('Unable to persist server cooldown') from None

    def suspend(self):
        self.check()
        try:
            self.connection.execute('''UPDATE cryptotaxi_monitor_safety SET suspended = TRUE
                WHERE monitor_key = %s''', (self.key,))
        except Exception:
            raise SafetyStop('Unable to persist suspension; monitor stopped') from None

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()  # Session advisory lock is released on disconnect.
            except Exception:
                pass
            self.connection = None

    def __exit__(self, *args):
        self.close()
