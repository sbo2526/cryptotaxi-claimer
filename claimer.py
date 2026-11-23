#!/usr/bin/env python3
"""Opt-in, single-account, read-only monitor. No start/rent/claim/registration.

External access is OFF by default. Enabling it requires API permission from the
site, a primary account configured in environment variables and exclusive
ownership in a shared PostgreSQL database. This does not promise ban immunity.
"""

import json
import logging
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

from ban_detection import is_ban_error, is_confirmed_ban_record, suspension_reason
from monitor_safety import (SafetyStop, RetryLater, RequestPacer, SharedMonitorLock,
                            check_policy, retry_after_seconds)
from runtime_config import DATA_DIR, initialize_data, monitoring_enabled, primary_username

BASE = 'https://thecryptotaxi.com/api'
TZ = ZoneInfo('Asia/Tehran')
STATUS_FILE = str(DATA_DIR / 'status.json')
BANNED_ACCOUNTS_FILE = str(DATA_DIR / 'banned_accounts.json')
log = logging.getLogger('monitor')
state_lock = threading.RLock()

# Conservative request reduction, not anti-detection timing or site permission.
MIN_POLL_SECONDS = 300
IDLE_POLL_SECONDS = 1800
MAX_POLL_SECONDS = 21600
TRIP_SETTLE_SECONDS = 15
MAX_CONSECUTIVE_COOLDOWNS = 3


def next_poll_plan(cars, now=None):
    """Nearest saved trip end (milliseconds), bounded to 5 min .. 6 hours.

    Unknown/no active timers fall back to 30 minutes. An expired timer still
    requires a state read; it never proves completion or triggers a mutation.
    Do not randomize timing to imitate users. Shared pacing/cooldowns can only
    postpone this plan, never be bypassed by it.
    """
    now = time.time() if now is None else now
    delays = []
    for car in cars:
        if car['status'] != 'on_trip':
            continue
        end = car.get('ends_at')
        try:
            valid = (isinstance(end, (int, float)) and not isinstance(end, bool)
                     and math.isfinite(end) and 946684800000 <= end <= 253402300799000)
        except OverflowError:
            valid = False
        if valid:
            delays.append((max(MIN_POLL_SECONDS, end / 1000 - now + TRIP_SETTLE_SECONDS), 'trip_end'))
        else:
            delays.append((IDLE_POLL_SECONDS, 'unknown_timer'))
    delay, reason = min(delays, key=lambda plan: plan[0]) if delays else (IDLE_POLL_SECONDS, 'no_active_trip')
    if delay > MAX_POLL_SECONDS:
        return MAX_POLL_SECONDS, 'periodic_check'
    return math.ceil(delay), reason


def atomic_save_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    # Corrupt state must fail closed, not silently forget a suspension.


def load_banned_accounts():
    data = read_json(BANNED_ACCOUNTS_FILE, {'banned': []})
    if not isinstance(data, dict) or not isinstance(data.get('banned'), list):
        raise SafetyStop('Invalid suspension archive; manual review required')
    return data


def is_suspended(user):
    return any(record.get('username') == user and is_confirmed_ban_record(record)
               for record in load_banned_accounts()['banned'] if isinstance(record, dict))


def redact(message):
    value = str(message)
    for key in ('PRIMARY_ACCOUNT_PASSWORD', 'DASHBOARD_PASSWORD', 'MONITOR_LOCK_DATABASE_URL'):
        secret = os.environ.get(key, '')
        if secret:
            value = value.replace(secret, '[REDACTED]')
    return value[:4000]


def mark_suspended(user, reason):
    if user != primary_username() or not is_ban_error(error_text=reason):
        raise SafetyStop('Unconfirmed suspension or non-primary account')
    with state_lock:
        archive = load_banned_accounts()
        archive['banned'].append({
            'username': user, 'reason': redact(reason), 'confirmed': True,
            'detected_at': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        atomic_save_json(BANNED_ACCOUNTS_FILE, archive)
        status = read_json(STATUS_FILE, {'_meta': {}})
        status.pop(user, None)
        status['_meta'] = {'monitor_user': user, 'monitor_status': 'suspended',
                           'updated_at': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),
                           'stop_code': 'suspended'}
        atomic_save_json(STATUS_FILE, status)


def car_snapshot(car, rented=False):
    trip = car.get('trip') or {}
    block = car.get('block') or {}
    quota = car.get('quota') or {}
    if trip:
        status = 'on_trip'
    elif car.get('canDrive') is True and not block and not car.get('broken'):
        status = 'idle'
    else:
        status = 'unavailable'
    return {
        'id': car.get('id'), 'name': car.get('name', 'تاکسی'), 'plate': car.get('plate', ''),
        'status': status, 'rented': rented,
        'ends_at': trip.get('endsAt'), 'started_at': trip.get('startedAt', 0),
        # Missing counts are unknown, not zero; quota and extra trips are
        # independent reported fields, not a computed eligibility decision.
        'used': quota.get('used'), 'limit': quota.get('limit'),
        'bonus': quota.get('bonus'),
        'block_code': block.get('code', '') if isinstance(block, dict) else 'blocked',
        'block_msg': ((block.get('msg') or block.get('message') or '')
                      if isinstance(block, dict) else str(block)),
    }


class ReadOnlyMonitor:
    def __init__(self, user, ownership, session=None, pacer=None):
        self.user, self.ownership = user, ownership
        self.session = session or requests.Session()
        # No invisible transport retries for login, reads or writes.
        self.session.mount('https://', HTTPAdapter(max_retries=0))
        self.session.trust_env = False  # No proxy rotation or automatic direct fallback.
        self.pacer = pacer or RequestPacer()
        self.stopped = False
        self.login_attempted = False

    def stop(self):
        self.stopped = True
        self.session.close()

    def request(self, endpoint, payload):
        # There is no public arbitrary URL or mutation endpoint path.
        try:
            check_policy(self.user, endpoint)
            if endpoint == 'auth/login':
                if payload.get('username') != self.user:
                    raise SafetyStop('Login identity must match the primary account')
                if self.login_attempted:
                    raise SafetyStop('Login already attempted; manual restart required', 'needs_login')
                # Even a failed/rate-limited login is never retried in this run.
                self.login_attempted = True
            if self.stopped or is_suspended(self.user):
                raise SafetyStop('Account monitoring is stopped')
            self.ownership.check()
            self.pacer.wait()
            # Recheck ownership/policy after any wait, BEFORE touching the site.
            check_policy(self.user, endpoint)
            self.ownership.check()
            if self.stopped or is_suspended(self.user):
                raise SafetyStop('Account monitoring is stopped')
            self.ownership.before_request()  # Budget survives process/service restarts.
            result = self.session.post(
                f'{BASE}/{endpoint}', json=payload, timeout=30, allow_redirects=False,
                headers={'Content-Type': 'application/json', 'X-CT': '1', 'X-CT-Lang': 'es'},
            )
            reason = suspension_reason(result)
            if reason:
                self.stopped = True  # Even if archive persistence fails, never continue this worker.
                self.ownership.suspend()  # Shared persistent marker, including across redeployments.
                mark_suspended(self.user, reason)
                raise SafetyStop('The server explicitly suspended the primary account', 'suspended')
            if result.status_code in (429, 503):
                delay = retry_after_seconds(result.headers.get('Retry-After'))
                self.ownership.cooldown(delay)
                self.pacer.cooldown(delay)
                raise RetryLater(delay)  # No immediate reissue, login or IP change.
            if result.status_code == 401:
                raise SafetyStop('Session rejected; manual login review required', 'needs_login')
            if result.status_code == 403:
                raise SafetyStop('Access denied; manual review required (not classified as a ban)', 'access_denied')
            if 300 <= result.status_code < 400:
                raise SafetyStop('Unexpected redirect; manual review required')
            result.raise_for_status()
            data = result.json()
            if not isinstance(data, dict) or data.get('error'):
                raise SafetyStop('Unexpected API error; manual review required')
            return data
        except SafetyStop:
            self.stop()
            raise
        except RetryLater:
            raise
        except Exception:
            self.stop()
            # No driver/HTTP exception bodies, URLs with tokens or credentials in logs.
            raise SafetyStop('Monitoring failed; no automatic retry, manual review required') from None

    def login(self):
        password = os.environ.get('PRIMARY_ACCOUNT_PASSWORD', '')
        if not password:
            self.stop()
            raise SafetyStop('PRIMARY_ACCOUNT_PASSWORD is required')
        return self.request('auth/login', {'username': self.user, 'password': password})

    def poll(self):
        state = self.request('state', {})
        try:
            # Reject login-shaped, partial or malformed responses, rather than
            # overwriting a good snapshot with fabricated empty account data.
            if not any(key in state for key in ('myCars', 'rentedCars')):
                raise ValueError('Missing car collections')
            cars = []
            for key, rented in (('myCars', False), ('rentedCars', True)):
                collection = state.get(key, [])
                if not isinstance(collection, list):
                    raise ValueError('Invalid car collection')
                for car in collection:
                    if not isinstance(car, dict) or any(
                        car.get(field) is not None and not isinstance(car[field], dict)
                        for field in ('trip', 'quota')
                    ):
                        raise ValueError('Invalid car record')
                    cars.append(car_snapshot(car, rented))
            balances, stats = state.get('balances', {}), state.get('stats', {})
            if not isinstance(balances, dict) or not isinstance(stats, dict):
                raise ValueError('Invalid account data')
            delay, reason = next_poll_plan(cars)
            timestamp = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
            # Whitelist fields; no session/cookie/token/password persistence.
            snapshot = {
                '_meta': {'monitor_status': 'read_only', 'monitor_user': self.user, 'date': timestamp[:10]},
                self.user: {'last_run': timestamp, 'last_result': 'OK', 'cars': cars,
                            'wallet': {'usdt': balances.get('usdt'), 'cash': balances.get('efectivo')},
                            'bonus_trips': state.get('bonusTrips'),
                            'stats': {'trips': stats.get('trips', 0), 'usdt_earned': stats.get('usdtEarned', 0)}},
            }
            with state_lock:
                self.ownership.check()
                if self.stopped or is_suspended(self.user):
                    raise SafetyStop('Suspension prevents publishing an active snapshot', 'suspended')
                atomic_save_json(STATUS_FILE, snapshot)
            return delay, reason
        except SafetyStop:
            self.stop()
            raise
        except Exception:
            self.stop()
            raise SafetyStop('Invalid state or persistence failure; manual review required') from None

    def publish_activity(self, status, seconds=None, reason=None, code=None):
        """Primary-scoped local evidence, not a heartbeat or a running guarantee."""
        self.ownership.check()
        with state_lock:
            state = read_json(STATUS_FILE, {'_meta': {}})
            if not isinstance(state, dict):
                raise SafetyStop('Invalid local status; manual review required')
            now = datetime.now(TZ)
            meta = {'monitor_user': self.user, 'monitor_status': status,
                    'updated_at': now.strftime('%Y-%m-%d %H:%M:%S')}
            if seconds is not None:
                meta['next_check_at'] = (now + timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')
            if reason:
                meta['schedule_reason'] = reason
            if code:
                meta['stop_code'] = code
            # Keep an explicit suspension authoritative, even on final cleanup.
            if is_suspended(self.user):
                meta = {'monitor_user': self.user, 'monitor_status': 'suspended',
                        'updated_at': meta['updated_at'], 'stop_code': 'suspended'}
            state['_meta'] = meta
            atomic_save_json(STATUS_FILE, state)

    def run(self, sleep=None):
        """One login attempt, same Session for all reads, bounded cooldowns."""
        sleep = time.sleep if sleep is None else sleep
        stop_code = 'manual_review'
        try:
            self.publish_activity('starting')
            self.login()
            cooldowns = 0
            while True:
                try:
                    delay, reason = self.poll()
                except RetryLater as cooldown:
                    cooldowns += 1
                    if cooldowns >= MAX_CONSECUTIVE_COOLDOWNS:
                        raise SafetyStop('Repeated cooldowns; worker stopped for manual review', 'rate_limited')
                    self.publish_activity('cooldown', seconds=cooldown.seconds)
                    sleep(cooldown.seconds)
                else:
                    cooldowns = 0
                    self.publish_activity('waiting', seconds=delay, reason=reason)
                    sleep(delay)
        except RetryLater:
            # Only the one startup login can reach here: never retry it.
            stop_code = 'rate_limited'
            raise SafetyStop('Login deferred or rate-limited; manual review required', stop_code) from None
        except SafetyStop as error:
            stop_code = error.code
            raise
        except KeyboardInterrupt:
            stop_code = 'manual_stop'
            raise
        except Exception:
            raise SafetyStop('Worker failed safely; manual review required') from None
        finally:
            self.stop()
            try:
                self.publish_activity('stopped', code=stop_code)
            except Exception:
                # Ownership/state may be unavailable. Do not reconnect or mask
                # the original error with driver details or a misleading state.
                log.error('Unable to record worker stop; inspect Railway logs.')


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    initialize_data()
    if not monitoring_enabled():
        log.info('External monitoring disabled. Automatic actions have been removed.')
        return
    user = primary_username()
    if not user or not os.environ.get('PRIMARY_ACCOUNT_PASSWORD'):
        log.error('Configure the primary account in Railway Variables; worker remains stopped.')
        return
    try:
        with SharedMonitorLock(user) as ownership:
            monitor = ReadOnlyMonitor(user, ownership)
            monitor.run()
    except SafetyStop as error:
        log.warning('%s', error)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.error('Worker stopped safely; inspect configuration without sharing secrets.')


if __name__ == '__main__':
    main()
