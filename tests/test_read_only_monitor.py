from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import claimer
import runtime_config
from monitor_safety import (SafetyStop, RetryLater, RequestPacer, SharedMonitorLock,
                            check_policy, retry_after_seconds)

ENV = {'PRIMARY_ACCOUNT': 'main', 'PRIMARY_ACCOUNT_PASSWORD': 'test-site-secret',
       'SITE_MONITORING_ENABLED': 'true', 'MONITOR_LOCK_DATABASE_URL': 'postgresql://test'}


def response(status=200, payload=None, headers=None):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(payload if payload is not None else {}).encode()
    value.headers.update(headers or {})
    value.url = 'https://example.test/api/state'
    return value


class MonitorSafetyTests(unittest.TestCase):
    def setUp(self):
        allowed = patch.object(runtime_config, 'ALLOWED_PRIMARY_ACCOUNT', 'main')
        allowed.start()
        self.addCleanup(allowed.stop)
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('STATUS_FILE', 'BANNED_ACCOUNTS_FILE'):
            setting = patch.object(claimer, name, str(self.root / (name + '.json')))
            setting.start()
            self.addCleanup(setting.stop)
        self.session = Mock()
        self.ownership = Mock()
        self.pacer = Mock()
        self.client = claimer.ReadOnlyMonitor('main', self.ownership, self.session, self.pacer)

    def test_every_mutation_endpoint_is_rejected_without_network(self):
        for endpoint in ('trips/start', 'market/rent', 'missions/claim', 'referrals/claim',
                         'auth/register', 'free/claim', '../auth/login', 'state?x=1'):
            client = claimer.ReadOnlyMonitor('main', self.ownership, self.session, self.pacer)
            with self.subTest(endpoint=endpoint), self.assertRaises(SafetyStop):
                client.request(endpoint, {})
        self.session.post.assert_not_called()

    def test_non_primary_and_disabled_monitoring_cannot_send(self):
        with self.assertRaises(SafetyStop):
            claimer.ReadOnlyMonitor('other', self.ownership, self.session, self.pacer).request('state', {})
        with patch.dict(os.environ, {'SITE_MONITORING_ENABLED': 'false'}), self.assertRaises(SafetyStop):
            self.client.request('state', {})
        self.session.post.assert_not_called()

    def test_login_payload_cannot_substitute_a_different_identity(self):
        with self.assertRaises(SafetyStop):
            self.client.request('auth/login', {'username': 'other', 'password': 'x'})
        self.session.post.assert_not_called()

    def test_missing_password_blocks_login(self):
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT_PASSWORD': ''}), self.assertRaises(SafetyStop):
            self.client.login()
        self.session.post.assert_not_called()

    def test_login_only_uses_primary_secret(self):
        self.session.post.return_value = response()
        self.client.login()
        self.assertEqual(self.session.post.call_args.kwargs['json'],
                         {'username': 'main', 'password': ENV['PRIMARY_ACCOUNT_PASSWORD']})
        self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])

    def test_only_whitelisted_state_fields_are_persisted(self):
        self.session.post.return_value = response(payload={
            'myCars': [], 'balances': {'usdt': 5}, 'token': 'private',
            'password': 'private', 'otherAccounts': [{'username': 'other'}],
        })
        self.client.poll()
        text = Path(claimer.STATUS_FILE).read_text()
        self.assertNotIn('private', text)
        self.assertNotIn('other', text)
        self.assertEqual(set(json.loads(text)), {'main', '_meta'})

    def test_persisted_snapshot_matches_dashboard_and_missing_balance_is_unknown(self):
        import dashboard
        for balance in ({}, {'usdt': 0}, {'usdt': 5}):
            with self.subTest(balance=balance):
                self.session.post.return_value = response(payload={
                    'myCars': [{'name': 'City Taxi', 'canDrive': True}],
                    'rentedCars': [], 'balances': balance,
                })
                self.client.poll()
                state = json.loads(Path(claimer.STATUS_FILE).read_text())
                self.assertEqual(state['main']['wallet']['usdt'], balance.get('usdt'))
                with patch.object(dashboard, 'banned_accounts_info', return_value=[]):
                    view = dashboard.dashboard_view(state)
                self.assertIsNotNone(view['sample'])
                panel = dashboard.account_data_section(view)
                self.assertIn('City Taxi', panel)
                self.assertIn('dir="ltr">' + ('—' if not balance else f'{balance["usdt"]:.4f}'), panel)

    def test_extra_trip_fields_survive_snapshot_and_dashboard_without_summing(self):
        import dashboard
        self.session.post.return_value = response(payload={
            'myCars': [{'name': 'Primary taxi', 'canDrive': True,
                        'quota': {'used': 10, 'limit': 10, 'bonus': 4}}],
            'rentedCars': [{'name': 'Rental taxi', 'canDrive': False,
                            'quota': {'used': 0, 'limit': 5, 'bonus': 0}}],
            'bonusTrips': 50,
        })
        self.client.poll()
        state = json.loads(Path(claimer.STATUS_FILE).read_text())
        cars = state['main']['cars']
        self.assertEqual(state['main']['bonus_trips'], 50)
        self.assertEqual(cars[0]['bonus'], 4)
        self.assertEqual(cars[0]['status'], 'idle')
        self.assertEqual(cars[1]['bonus'], 0)
        self.assertEqual(cars[1]['status'], 'unavailable')
        with patch.object(dashboard, 'banned_accounts_info', return_value=[]):
            panel = dashboard.account_data_section(dashboard.dashboard_view(state))
        self.assertIn('quota-count">10 از 10</span>', panel)
        self.assertIn('car-bonus-count">4</strong>', panel)
        self.assertIn('car-bonus-count">0</strong>', panel)
        self.assertIn('account-bonus-count">50</strong>', panel)
        self.assertNotIn('>54<', panel)
        self.assertNotIn('>60<', panel)

    def test_missing_extra_trip_and_quota_values_stay_unknown(self):
        self.session.post.return_value = response(payload={'myCars': [{'canDrive': True}]})
        self.client.poll()
        account = json.loads(Path(claimer.STATUS_FILE).read_text())['main']
        self.assertIsNone(account['bonus_trips'])
        for key in ('used', 'limit', 'bonus'):
            self.assertIsNone(account['cars'][0][key])
        self.assertEqual(account['cars'][0]['status'], 'idle')

    def test_429_and_retry_after_never_immediately_repeat(self):
        self.session.post.return_value = response(429, headers={'Retry-After': '1200'})
        with self.assertRaises(RetryLater) as result:
            self.client.poll()
        self.assertEqual(result.exception.seconds, 1200)
        self.pacer.cooldown.assert_called_once_with(1200)
        self.ownership.cooldown.assert_called_once_with(1200)
        self.session.post.assert_called_once()

    def test_503_obeys_cooldown_too(self):
        self.session.post.return_value = response(503)
        with self.assertRaises(RetryLater):
            self.client.poll()
        self.session.post.assert_called_once()

    def test_generic_denial_stops_but_does_not_archive_a_ban(self):
        for code in (401, 403):
            client = claimer.ReadOnlyMonitor('main', self.ownership, self.session, self.pacer)
            self.session.post.return_value = response(code, {'error': 'Forbidden'})
            with self.assertRaises(SafetyStop):
                client.poll()
            self.assertFalse(Path(claimer.BANNED_ACCOUNTS_FILE).exists())
            self.assertTrue(client.stopped)

    def test_explicit_suspension_stops_all_future_requests_and_persists_reason(self):
        self.session.post.return_value = response(403, {'error': 'Your account is suspended. Reason: Multi Account'})
        with self.assertRaises(SafetyStop):
            self.client.poll()
        self.assertTrue(self.client.stopped)
        self.ownership.suspend.assert_called_once()
        self.assertIn('Multi Account', Path(claimer.BANNED_ACCOUNTS_FILE).read_text())
        with self.assertRaises(SafetyStop):
            self.client.request('state', {})
        self.session.post.assert_called_once()
        restarted = claimer.ReadOnlyMonitor('main', self.ownership, self.session, self.pacer)
        with self.assertRaises(SafetyStop):
            restarted.login()
        self.session.post.assert_called_once()

    def test_network_failures_never_relogin_or_retry(self):
        self.session.post.side_effect = requests.ConnectionError('secret details')
        with self.assertRaises(SafetyStop) as result:
            self.client.poll()
        self.assertNotIn('secret details', str(result.exception))
        self.session.post.assert_called_once()

    def test_lost_lock_before_or_after_wait_prevents_request(self):
        for effects in ([SafetyStop('lost')], [None, SafetyStop('lost')]):
            self.ownership.check.side_effect = effects
            client = claimer.ReadOnlyMonitor('main', self.ownership, self.session, self.pacer)
            with self.assertRaises(SafetyStop):
                client.poll()
        self.session.post.assert_not_called()

    def test_redirects_are_not_followed(self):
        self.session.post.return_value = response(307)
        with self.assertRaises(SafetyStop):
            self.client.poll()
        self.session.post.assert_called_once()

    def test_corrupt_archive_is_fail_closed(self):
        Path(claimer.BANNED_ACCOUNTS_FILE).write_text('{invalid')
        with self.assertRaises(SafetyStop):
            self.client.poll()
        self.session.post.assert_not_called()

    def test_secrets_redacted_from_saved_reason(self):
        reason = 'Your account is suspended. ' + ENV['PRIMARY_ACCOUNT_PASSWORD']
        claimer.mark_suspended('main', reason)
        self.assertNotIn(ENV['PRIMARY_ACCOUNT_PASSWORD'], Path(claimer.BANNED_ACCOUNTS_FILE).read_text())

    def test_no_transport_retries_or_proxy_environment(self):
        with requests.Session() as session:
            client = claimer.ReadOnlyMonitor('main', self.ownership, session, self.pacer)
            self.assertFalse(session.trust_env)
            self.assertEqual(session.get_adapter(claimer.BASE).max_retries.total, 0)


class SharedLockTests(unittest.TestCase):
    def test_missing_shared_database_never_connects(self):
        connect = Mock()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SafetyStop):
            with SharedMonitorLock('main', connect):
                pass
        connect.assert_not_called()

    def test_one_owner_across_independent_instances_and_release_on_exit(self):
        owners = set()
        class Connection:
            closed = False
            key = None
            def execute(self, query, params=None):
                if 'pg_try_advisory_lock' in query:
                    key = params[0]
                    acquired = key not in owners
                    if acquired:
                        owners.add(key)
                        self.key = key
                    return Mock(fetchone=lambda: (acquired,))
                return Mock(fetchone=lambda: (False,))
            def close(self):
                if self.key is not None:
                    owners.discard(self.key)
                self.closed = True
        with patch.dict(os.environ, ENV, clear=True):
            with SharedMonitorLock('main', lambda *a, **k: Connection()) as owner:
                owner.check()
                with self.assertRaises(SafetyStop):
                    with SharedMonitorLock('main', lambda *a, **k: Connection()):
                        self.fail('duplicate owner')
            with SharedMonitorLock('main', lambda *a, **k: Connection()):
                pass
        self.assertEqual(owners, set())

    def test_database_failure_is_redacted(self):
        with patch.dict(os.environ, ENV, clear=True), self.assertRaises(SafetyStop) as result:
            with SharedMonitorLock('main', Mock(side_effect=RuntimeError('postgresql://password'))):
                pass
        self.assertNotIn('password', str(result.exception))

    def test_shared_cooldown_and_suspension_survive_new_lock_owners(self):
        shared = {'suspended': False, 'wait': 0}
        def connect(*args, **kwargs):
            connection = Mock(closed=False)
            def execute(query, params=None):
                if 'pg_try_advisory_lock' in query:
                    return Mock(fetchone=lambda: (True,))
                if 'SELECT suspended' in query:
                    return Mock(fetchone=lambda: (shared['suspended'],))
                if 'EXTRACT' in query:
                    return Mock(fetchone=lambda: (shared['wait'],))
                if 'suspended = TRUE' in query:
                    shared['suspended'] = True
                elif 'GREATEST(next_request_at' in query:
                    shared['wait'] = max(shared['wait'], params[0])
                elif "INTERVAL '60 seconds'" in query:
                    shared['wait'] = 60
                return Mock()
            connection.execute.side_effect = execute
            return connection
        with patch.dict(os.environ, ENV, clear=True):
            with SharedMonitorLock('main', connect) as first:
                first.before_request()
                first.cooldown(900)
            with SharedMonitorLock('main', connect) as second:
                with self.assertRaises(RetryLater) as delay:
                    second.before_request()
                self.assertEqual(delay.exception.seconds, 900)
                second.suspend()
            with self.assertRaises(SafetyStop):
                with SharedMonitorLock('main', connect):
                    self.fail('suspended primary must not restart')

    def test_lost_connection_fails_closed(self):
        owner = SharedMonitorLock('main')
        owner.connection = Mock(closed=True)
        with self.assertRaises(SafetyStop):
            owner.check()


class PacingTests(unittest.TestCase):
    def test_pacer_waits_without_sending_extra_requests(self):
        clock = [0.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        pacer = RequestPacer(clock=lambda: clock[0], sleep=sleep)
        pacer.wait()
        pacer.wait()
        self.assertEqual(sleeps, [60.0])
        pacer.cooldown(1200)
        pacer.wait()
        self.assertEqual(sleeps[-1], 1200.0)

    def test_retry_after_numeric_date_and_invalid(self):
        now = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('900'), 900)
        self.assertEqual(retry_after_seconds('Mon, 23 Nov 2026 01:00:00 GMT', now), 3600)
        for invalid in (None, '', 'nan', 'inf', '-1', 'not a date'):
            self.assertEqual(retry_after_seconds(invalid, now), 300)


if __name__ == '__main__':
    unittest.main()
