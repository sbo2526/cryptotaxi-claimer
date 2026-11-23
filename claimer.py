#!/usr/bin/env python3
"""Opt-in, single-account, read-only monitor. No start/rent/claim/registration.

External access is OFF by default. Enabling it requires API permission from the
site, a primary account configured in environment variables and exclusive
ownership in a shared PostgreSQL database. This does not promise ban immunity.
"""

import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime
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
        status.setdefault('_meta', {})['monitor_status'] = 'suspended'
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
        'used': quota.get('used', 0), 'limit': quota.get('limit', 0),
        'bonus': quota.get('bonus', 0),
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

    def stop(self):
        self.stopped = True
        self.session.close()

    def request(self, endpoint, payload):
        # There is no public arbitrary URL or mutation endpoint path.
        try:
            check_policy(self.user, endpoint)
            if endpoint == 'auth/login' and payload.get('username') != self.user:
                raise SafetyStop('Login identity must match the primary account')
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
                raise SafetyStop('The server explicitly suspended the primary account')
            if result.status_code in (429, 503):
                delay = retry_after_seconds(result.headers.get('Retry-After'))
                self.ownership.cooldown(delay)
                self.pacer.cooldown(delay)
                raise RetryLater(delay)  # No immediate reissue, login or IP change.
            if result.status_code in (401, 403):
                raise SafetyStop('Access denied; manual review required (not classified as a ban)')
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
        cars = [car_snapshot(car, False) for car in (state.get('myCars') or [])]
        cars += [car_snapshot(car, True) for car in (state.get('rentedCars') or [])]
        timestamp = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        # Whitelist fields; never persist credentials, cookies, tokens or raw API payloads.
        snapshot = {
            '_meta': {'monitor_status': 'read_only', 'date': timestamp[:10]},
            self.user: {'last_run': timestamp, 'last_result': 'OK', 'cars': cars,
                        'wallet': {'usdt': (state.get('balances') or {}).get('usdt', 0),
                                   'cash': (state.get('balances') or {}).get('efectivo', 0)},
                        'bonus_trips': state.get('bonusTrips', 0),
                        'stats': {'trips': (state.get('stats') or {}).get('trips', 0),
                                  'usdt_earned': (state.get('stats') or {}).get('usdtEarned', 0)}},
        }
        with state_lock:
            if self.stopped or is_suspended(self.user):
                self.stop()
                raise SafetyStop('Suspension prevents publishing an active snapshot')
            atomic_save_json(STATUS_FILE, snapshot)


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
            try:
                monitor.login()
                while True:
                    try:
                        monitor.poll()
                    except RetryLater as cooldown:
                        time.sleep(cooldown.seconds)
                    else:
                        time.sleep(300)
            finally:
                monitor.stop()
    except RetryLater:
        # A rate-limited login is not retried unattended.
        log.warning('Login rate-limited; worker stopped for manual review.')
    except SafetyStop as error:
        log.warning('%s', error)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.error('Worker stopped safely; inspect configuration without sharing secrets.')


if __name__ == '__main__':
    main()
