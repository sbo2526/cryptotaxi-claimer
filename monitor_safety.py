"""Cross-replica ownership, request pacing and fail-closed monitoring policy."""

import hashlib
import math
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from runtime_config import monitoring_enabled, primary_username, auto_start_enabled


class SafetyStop(RuntimeError):
    """Stop the worker; no automatic restart or login loop.

    code is a fixed internal identifier, never a server message or secret.
    """
    def __init__(self, message, code='manual_review'):
        super().__init__(message)
        self.code = code


class RetryLater(RuntimeError):
    def __init__(self, seconds):
        super().__init__('Server requested a cooldown')
        self.seconds = seconds


def check_policy(user, endpoint):
    if not monitoring_enabled():
        raise SafetyStop('External monitoring is disabled')
    if not primary_username() or user != primary_username():
        raise SafetyStop('Only the explicitly configured primary account is permitted')
    if endpoint == 'trips/start' and auto_start_enabled():
        return
    if endpoint not in ('auth/login', 'state'):
        raise SafetyStop('This endpoint is disabled by the current account policy')


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


STARTUP_LOCK_WAIT_SECONDS = 120
LOCK_RECHECK_SECONDS = 10


class SharedMonitorLock:
    """PostgreSQL session advisory lock, shared by ALL replicas/projects.

    Missing configuration, an occupied lock or database connectivity loss blocks
    website access. Use the SAME database on every deployment for this account.
    A lost connection is never silently reconnected by this worker.
    """
    def __init__(self, user, connect=None, *, startup_wait=0, clock=None, sleep=None, on_wait=None):
        # Bounded, initial acquisition only. Never reconnect or retake a lost lock.
        if (isinstance(startup_wait, bool) or not isinstance(startup_wait, (int, float))
                or not 0 <= startup_wait <= STARTUP_LOCK_WAIT_SECONDS):
            raise SafetyStop('Invalid startup lock wait', 'invalid_config')
        self.startup_wait = startup_wait
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.on_wait = on_wait
        self.user = user
        self.connection = None
        self.connect = connect
        self.key = int.from_bytes(hashlib.sha256(('cryptotaxi-monitor:' + user.strip().casefold()).encode()).digest()[:8],
                                  byteorder='big', signed=True)

    def __enter__(self):
        url = os.environ.get('MONITOR_LOCK_DATABASE_URL', '')
        if not url or not self.user:
            raise SafetyStop('A shared monitor lock database and primary account are required', 'invalid_config')
        try:
            if self.connect is None:
                import psycopg
                self.connect = psycopg.connect
            self.connection = self.connect(url, autocommit=True, connect_timeout=10)
            self._acquire_initial_lock()
            self.connection.execute('''CREATE TABLE IF NOT EXISTS cryptotaxi_monitor_safety (
                monitor_key BIGINT PRIMARY KEY,
                suspended BOOLEAN NOT NULL DEFAULT FALSE,
                next_request_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )''')
            self.connection.execute('''INSERT INTO cryptotaxi_monitor_safety (monitor_key)
                VALUES (%s) ON CONFLICT (monitor_key) DO NOTHING''', (self.key,))
            if auto_start_enabled():
                self.connection.execute('''CREATE TABLE IF NOT EXISTS cryptotaxi_trip_starts (
                    monitor_key BIGINT NOT NULL,
                    car_key TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('pending', 'confirmed', 'observed')),
                    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at DOUBLE PRECISION,
                    ends_at DOUBLE PRECISION,
                    PRIMARY KEY (monitor_key, car_key)
                )''')
            self.check()
        except SafetyStop:
            self.close()
            raise
        except Exception:
            self.close()
            # Do not expose DSNs, passwords, hostnames or driver exception text.
            raise SafetyStop('Unable to obtain exclusive monitor ownership', 'database_error') from None
        except BaseException:
            self.close()  # SIGINT/SystemExit during startup must not leak a session.
            raise
        return self

    def _acquire_initial_lock(self):
        deadline = self.clock() + self.startup_wait
        # An attempt cap also prevents a busy loop if the injected clock stalls.
        attempts = 1 + math.ceil(self.startup_wait / LOCK_RECHECK_SECONDS)
        for attempt in range(attempts):
            row = self.connection.execute('SELECT pg_try_advisory_lock(%s)', (self.key,)).fetchone()
            if row and row[0] is True:
                return  # Stop querying: session advisory locks are reentrant.
            if not row or row[0] is not False:
                raise SafetyStop('Invalid ownership response', 'database_error')
            remaining = deadline - self.clock()
            if remaining <= 0 or attempt == attempts - 1:
                break
            if attempt == 0 and self.on_wait:
                self.on_wait()  # Local stage evidence only; no shared-state writes.
            self.sleep(min(LOCK_RECHECK_SECONDS, remaining))
        raise SafetyStop('Another monitor still owns this account; startup wait expired', 'already_running')

    def check(self):
        try:
            if self.connection is None or self.connection.closed:
                raise SafetyStop('Monitor ownership was lost', 'safety_unavailable')
            row = self.connection.execute(
                'SELECT suspended FROM cryptotaxi_monitor_safety WHERE monitor_key = %s',
                (self.key,),
            ).fetchone()
            if row and row[0] is True:
                raise SafetyStop('Account is persistently suspended', 'suspended')
            if not row or row[0] is not False:
                raise SafetyStop('Safety state is unavailable', 'safety_unavailable')
        except SafetyStop:
            raise
        except Exception:
            raise SafetyStop('Monitor ownership or safety state is unavailable; worker stopped', 'safety_unavailable') from None

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

    def reserve_trip_start(self, car_id):
        """Durable intent BEFORE sending; ambiguous/pending starts never replay.

        Only a previously observed successful trip may begin a new episode,
        after the caller has freshly verified the car is now idle/allowed.
        """
        self.check()
        try:
            row = self.connection.execute('''INSERT INTO cryptotaxi_trip_starts
                (monitor_key, car_key, phase) VALUES (%s, %s, 'pending')
                ON CONFLICT (monitor_key, car_key) DO UPDATE
                SET phase = 'pending', attempted_at = NOW(), started_at = NULL, ends_at = NULL
                WHERE cryptotaxi_trip_starts.phase = 'observed'
                RETURNING phase''', (self.key, str(car_id))).fetchone()
            if not row or row[0] != 'pending':
                raise SafetyStop('Unresolved trip start; manual review required', 'start_unknown')
        except SafetyStop:
            raise
        except Exception:
            raise SafetyStop('Unable to persist trip-start intent', 'start_unknown') from None

    def confirm_trip_start(self, car_id, started_at, ends_at):
        self.check()
        try:
            row = self.connection.execute('''UPDATE cryptotaxi_trip_starts
                SET phase = 'confirmed', started_at = %s, ends_at = %s
                WHERE monitor_key = %s AND car_key = %s AND phase = 'pending'
                RETURNING phase''', (started_at, ends_at, self.key, str(car_id))).fetchone()
            if not row or row[0] != 'confirmed':
                raise SafetyStop('Unable to confirm trip start', 'start_unknown')
        except SafetyStop:
            raise
        except Exception:
            raise SafetyStop('Unable to confirm trip start', 'start_unknown') from None

    def observe_trip_start(self, car_id, started_at, ends_at):
        self.check()
        try:
            row = self.connection.execute('''UPDATE cryptotaxi_trip_starts SET phase = 'observed'
                WHERE monitor_key = %s AND car_key = %s
                AND phase IN ('confirmed', 'observed') AND started_at = %s AND ends_at = %s
                RETURNING phase''', (self.key, str(car_id), started_at, ends_at)).fetchone()
            return bool(row and row[0] == 'observed')
        except Exception:
            raise SafetyStop('Unable to verify trip-start observation', 'start_unknown') from None

    def ensure_no_unresolved_starts(self):
        self.check()
        try:
            row = self.connection.execute('''SELECT car_key, phase,
                EXTRACT(EPOCH FROM (NOW() - attempted_at))
                FROM cryptotaxi_trip_starts
                WHERE monitor_key = %s AND phase IN ('pending', 'confirmed')
                ORDER BY attempted_at, car_key LIMIT 1''', (self.key,)).fetchone()
            if row:
                # Fixed vocabulary plus a bounded numeric age only. Never log
                # account/car identifiers, raw rows or driver exception text.
                phase = row[1] if row[1] in ('pending', 'confirmed') else 'unknown'
                try:
                    age = float(row[2])
                    age_text = str(int(age)) if math.isfinite(age) and 0 <= age <= 3155760000 else 'unknown'
                except (TypeError, ValueError, OverflowError):
                    age_text = 'unknown'
                raise SafetyStop('Unresolved trip-start journal requires manual review; '
                                 f'phase={phase}; age_seconds={age_text}', 'start_unknown')
        except SafetyStop:
            raise
        except Exception:
            raise SafetyStop('Unable to read trip-start journal', 'start_unknown') from None

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()  # Session advisory lock is released on disconnect.
            except Exception:
                pass
            self.connection = None

    def __exit__(self, *args):
        self.close()
