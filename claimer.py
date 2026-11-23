#!/usr/bin/env python3
"""Opt-in primary-account monitor with separately opted-in automatic trip starts.
No rent/claim/registration or automatic re-login.

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
                            check_policy, retry_after_seconds, STARTUP_LOCK_WAIT_SECONDS)
from monitor_runtime import write_report
from runtime_config import DATA_DIR, initialize_data, monitoring_enabled, primary_username, auto_start_enabled
from trip_rules import start_block, valid_car_id, timestamp_ms

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
        # Explain the exact preflight decision in the UI, not canDrive alone.
        'start_check': start_block(car, time.time() * 1000) or 'eligible',
        'ends_at': trip.get('endsAt'), 'started_at': trip.get('startedAt', 0),
        # Missing counts are unknown, not zero; quota and extra trips are
        # independent reported fields, not a computed eligibility decision.
        'used': quota.get('used'), 'limit': quota.get('limit'),
        'bonus': quota.get('bonus'), 'left': quota.get('left'),
        'bonus_expires_at': quota.get('bonusExpiresAt'),
        'block_code': block.get('code', '') if isinstance(block, dict) else 'blocked',
        'block_msg': ((block.get('msg') or block.get('message') or '')
                      if isinstance(block, dict) else str(block)),
    }


class ReadOnlyMonitor:
    """Legacy class name; trip mutation still requires its own explicit opt-in."""
    def __init__(self, user, ownership, session=None, pacer=None):
        self.user, self.ownership = user, ownership
        self.session = session or requests.Session()
        # No invisible transport retries for login, reads or writes.
        self.session.mount('https://', HTTPAdapter(max_retries=0))
        self.session.trust_env = False  # No proxy rotation or automatic direct fallback.
        self.pacer = pacer or RequestPacer()
        self.stopped = False
        self.login_attempted = False
        self._start_intent = None
        self._raw_cars = []
        self._state_received = None
        self._observed_starts = set()

    def stop(self):
        self.stopped = True
        self.session.close()

    def request(self, endpoint, payload):
        # Only the dedicated validated start path can authorize a mutation.
        mutation_started = False
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
            # The DB reservation happens AFTER pacer.wait() starts its timer.
            # Align the next local slot with the completed shared reservation;
            # otherwise DB/IPC latency can cause a tiny residual on every cycle.
            self.pacer.cooldown(60.0)
            if endpoint == 'trips/start':
                intent = self._start_intent
                if (not intent or not isinstance(payload, dict) or not valid_car_id(payload.get('carId'))
                        or payload != {'carId': intent['car']['id']}
                        or self._state_received is None
                        or not 0 <= time.monotonic() - self._state_received <= 90
                        or start_block(intent['car'], time.time() * 1000) is not None):
                    raise SafetyStop('Trip start requires fresh explicit driving permission', 'start_blocked')
                self.ownership.reserve_trip_start(payload['carId'])
                mutation_started = True  # Persisted BEFORE the one possible send.
                # Reserving the durable intent may itself block on storage.
                # Recheck immediately before dispatch, without retrying a POST.
                check_policy(self.user, endpoint)
                self.ownership.check()
                if (self.stopped or is_suspended(self.user)
                        or not 0 <= time.monotonic() - self._state_received <= 90
                        or start_block(intent['car'], time.time() * 1000) is not None):
                    raise SafetyStop('Start preflight expired before dispatch', 'start_unknown')
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
                if mutation_started:
                    raise SafetyStop('Trip-start outcome needs manual review; cooldown persisted', 'start_unknown')
                raise RetryLater(delay)  # No immediate reissue, login or IP change.
            if result.status_code == 401:
                raise SafetyStop('Session rejected; manual login review required', 'needs_login')
            if result.status_code == 403:
                raise SafetyStop('Access denied; manual review required (not classified as a ban)', 'access_denied')
            if 300 <= result.status_code < 400:
                raise SafetyStop('Unexpected redirect; manual review required', 'request_failed')
            result.raise_for_status()
            data = result.json()
            if not isinstance(data, dict) or data.get('error'):
                raise SafetyStop('Unexpected API error; manual review required', 'request_failed')
            if mutation_started:
                trip = data.get('trip')
                now_ms = time.time() * 1000
                if (not isinstance(trip, dict) or not timestamp_ms(trip.get('startedAt'))
                        or not timestamp_ms(trip.get('endsAt'))
                        or trip['startedAt'] > now_ms + 30000
                        or trip['endsAt'] <= max(trip['startedAt'], now_ms)):
                    raise SafetyStop('Trip start was not verifiably confirmed', 'start_unknown')
                self.ownership.confirm_trip_start(payload['carId'], trip['startedAt'], trip['endsAt'])
            return data
        except SafetyStop as error:
            self.stop()
            if mutation_started and error.code not in ('suspended', 'needs_login', 'access_denied', 'start_unknown'):
                raise SafetyStop('Trip-start outcome needs manual review; never replay automatically', 'start_unknown') from None
            raise
        except RetryLater:
            if mutation_started:
                self.stop()
                raise SafetyStop('Trip-start outcome needs manual review', 'start_unknown') from None
            raise
        except Exception:
            self.stop()
            # No driver/HTTP exception bodies, URLs with tokens or credentials in logs.
            code = 'start_unknown' if mutation_started else 'request_failed'
            raise SafetyStop('Monitoring failed; no automatic retry, manual review required', code) from None

    def login(self):
        password = os.environ.get('PRIMARY_ACCOUNT_PASSWORD', '')
        if not password:
            self.stop()
            raise SafetyStop('PRIMARY_ACCOUNT_PASSWORD is required')
        return self.request('auth/login', {'username': self.user, 'password': password})

    def start_trip(self, car_id):
        car = next((car for car in self._raw_cars if car.get('id') == car_id), None)
        if (not valid_car_id(car_id) or not auto_start_enabled() or not car or start_block(car, time.time() * 1000) is not None):
            raise SafetyStop('Car is not explicitly eligible for trip start', 'start_blocked')
        self._start_intent = {'car': car}
        try:
            return self.request('trips/start', {'carId': car_id})
        finally:
            self._start_intent = None

    def poll(self, allow_start=True):
        state = self.request('state', {})
        received_at = time.monotonic()
        try:
            # Reject login-shaped, partial or malformed responses, rather than
            # overwriting a good snapshot with fabricated empty account data.
            if not any(key in state for key in ('myCars', 'rentedCars')):
                raise ValueError('Missing car collections')
            cars, raw_cars = [], []
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
                    raw_cars.append(car)
                    cars.append(car_snapshot(car, rented))
            balances, stats = state.get('balances', {}), state.get('stats', {})
            if not isinstance(balances, dict) or not isinstance(stats, dict):
                raise ValueError('Invalid account data')
            delay, reason = next_poll_plan(cars)
            timestamp = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
            # Whitelist fields; no session/cookie/token/password persistence.
            snapshot = {
                '_meta': {'monitor_status': 'trip_starts' if auto_start_enabled() else 'read_only', 'monitor_user': self.user, 'date': timestamp[:10]},
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
            self._raw_cars, self._state_received = raw_cars, received_at
            self._observed_starts = set()
            if auto_start_enabled():
                ids = [str(car['id']) for car in raw_cars if valid_car_id(car.get('id'))]
                if len(ids) != len(set(ids)):
                    raise SafetyStop('Ambiguous duplicate car identities', 'start_blocked')
                for car in raw_cars:
                    trip = car.get('trip')
                    if (valid_car_id(car.get('id')) and isinstance(trip, dict)
                            and timestamp_ms(trip.get('startedAt')) and timestamp_ms(trip.get('endsAt'))):
                        if self.ownership.observe_trip_start(car['id'], trip['startedAt'], trip['endsAt']):
                            self._observed_starts.add(str(car['id']))
                self.ownership.ensure_no_unresolved_starts()
                ready = [car for car in raw_cars if start_block(car, time.time() * 1000) is None]
                if allow_start and ready:
                    # myCars precede rentedCars; at most ONE start per cycle.
                    car_id = ready[0]['id']
                    self.start_trip(car_id)
                    try:
                        plan = self.poll(allow_start=False)  # Same session, paced state verification.
                    except RetryLater:
                        raise SafetyStop('Start sent but observation deferred; never replay automatically', 'start_unknown') from None
                    except SafetyStop as error:
                        if error.code in ('suspended', 'needs_login', 'access_denied', 'start_unknown'):
                            raise
                        raise SafetyStop('Start sent but observation failed; never replay automatically', 'start_unknown') from None
                    if str(car_id) not in self._observed_starts:
                        raise SafetyStop('Started trip not observed in fresh state', 'start_unknown')
                    return plan
                if ready and delay > MIN_POLL_SECONDS:
                    delay, reason = MIN_POLL_SECONDS, 'ready_trip'
            return delay, reason
        except RetryLater:
            # A start was NOT sent (post-send deferrals are converted above).
            # Run's bounded cooldown handler must receive this control signal.
            # It will fetch fresh state before considering any future start.
            raise
        except SafetyStop:
            self.stop()
            raise
        except Exception:
            self.stop()
            raise SafetyStop('Invalid state or persistence failure; manual review required', 'state_invalid') from None

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
            write_report('logging_in')
            self.login()
            write_report('waiting_first_read')
            cooldowns = 0
            while True:
                try:
                    delay, reason = self.poll()
                except RetryLater as cooldown:
                    cooldowns += 1
                    if cooldowns >= MAX_CONSECUTIVE_COOLDOWNS:
                        raise SafetyStop('Repeated cooldowns; worker stopped for manual review', 'rate_limited')
                    self.publish_activity('cooldown', seconds=cooldown.seconds)
                    write_report('cooldown')
                    sleep(cooldown.seconds)
                else:
                    cooldowns = 0
                    self.publish_activity('waiting', seconds=delay, reason=reason)
                    write_report('waiting')
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
            write_report('stopped', code=stop_code)


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    initialize_data()
    if not monitoring_enabled():
        log.info('External monitoring disabled. No website actions will run.')
        return
    write_report('starting')
    user = primary_username()
    if not user or not os.environ.get('PRIMARY_ACCOUNT_PASSWORD'):
        log.error('Configure the primary account in Railway Variables; worker remains stopped.')
        write_report('stopped', code='invalid_config')
        return
    try:
        write_report('acquiring_lock')
        with SharedMonitorLock(user, startup_wait=STARTUP_LOCK_WAIT_SECONDS,
                               on_wait=lambda: write_report('waiting_lock')) as ownership:
            monitor = ReadOnlyMonitor(user, ownership)
            monitor.run()
    except SafetyStop as error:
        write_report('stopped', code=error.code)
        log.warning('%s', error)
    except KeyboardInterrupt:
        write_report('stopped', code='manual_stop')
    except Exception:
        write_report('stopped', code='manual_review')
        log.error('Worker stopped safely; inspect configuration without sharing secrets.')


if __name__ == '__main__':
    main()
