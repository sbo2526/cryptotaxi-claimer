from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

import claimer
import dashboard
from monitor_safety import RetryLater, SafetyStop


NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=claimer.TZ)
ENV = {'PRIMARY_ACCOUNT': 'main', 'PRIMARY_ACCOUNT_PASSWORD': 'test-only-site-secret',
       'SITE_MONITORING_ENABLED': 'true', 'MONITOR_LOCK_DATABASE_URL': 'postgresql://test-only'}


def trip(seconds):
    return {'status': 'on_trip', 'ends_at': (NOW.timestamp() + seconds) * 1000}


def response(code=200, payload=None, headers=None):
    result = requests.Response()
    result.status_code = code
    result._content = json.dumps({} if payload is None else payload).encode()
    result.headers.update(headers or {})
    return result


class ScheduleTests(unittest.TestCase):
    def plan(self, cars):
        return claimer.next_poll_plan(cars, now=NOW.timestamp())

    def test_nearest_of_owned_and_rented_trip_timers_determines_check(self):
        self.assertEqual(self.plan([trip(3600), trip(900), {'status': 'idle'}]), (915, 'trip_end'))

    def test_recent_and_expired_timers_never_create_a_tight_loop(self):
        for seconds in (-7200, -1, 0, 10, 200):
            with self.subTest(seconds=seconds):
                self.assertEqual(self.plan([trip(seconds)]), (300, 'trip_end'))

    def test_long_trip_has_bounded_periodic_check(self):
        self.assertEqual(self.plan([trip(86400)]), (21600, 'periodic_check'))

    def test_empty_or_idle_account_gets_less_frequent_check(self):
        for cars in ([], [{'status': 'idle'}], [{'status': 'unavailable'}]):
            self.assertEqual(self.plan(cars), (1800, 'no_active_trip'))

    def test_invalid_timers_never_become_immediate_checks(self):
        for end in (None, True, False, '1800000000000', [], {}, 0, -1,
                    NOW.timestamp(), float('nan'), float('inf'), 10**400):
            with self.subTest(end=repr(end)[:80]):
                self.assertEqual(self.plan([{'status': 'on_trip', 'ends_at': end}]), (1800, 'unknown_timer'))

    def test_unknown_timer_does_not_hide_an_earlier_known_trip(self):
        self.assertEqual(self.plan([trip(600), {'status': 'on_trip'}]), (615, 'trip_end'))
        self.assertEqual(self.plan([trip(10000), {'status': 'on_trip'}]), (1800, 'unknown_timer'))


class MonitorLoopTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module in (claimer, dashboard):
            for name in ('STATUS_FILE', 'BANNED_ACCOUNTS_FILE'):
                setting = patch.object(module, name, str(self.root / (name + '.json')))
                setting.start()
                self.addCleanup(setting.stop)
        self.session, self.owner, self.pacer = Mock(), Mock(), Mock()
        self.client = claimer.ReadOnlyMonitor('main', self.owner, self.session, self.pacer)

    def state(self):
        return json.loads(Path(claimer.STATUS_FILE).read_text())

    def endpoints(self):
        return [call.args[0].removeprefix(claimer.BASE + '/') for call in self.session.post.call_args_list]

    def test_one_login_same_session_and_saved_timer_schedule(self):
        self.session.post.side_effect = [response(), response(payload={
            'myCars': [], 'rentedCars': [{'name': 'Rental', 'trip': {'endsAt': (NOW.timestamp() + 1200) * 1000}}],
            'balances': {'usdt': 2},
        }), response(payload={'myCars': []})]
        observed = []
        def wait(seconds):
            state = self.state()
            observed.append((seconds, state['_meta']))
            self.assertNotIn(ENV['PRIMARY_ACCOUNT_PASSWORD'], json.dumps(state))
            self.assertIn('main', state)
            if len(observed) == 2:
                raise KeyboardInterrupt()
        with patch.object(claimer.time, 'time', return_value=NOW.timestamp()):
            with self.assertRaises(KeyboardInterrupt):
                self.client.run(sleep=wait)
        self.assertEqual(self.endpoints(), ['auth/login', 'state', 'state'])
        self.assertEqual([delay for delay, meta in observed], [1215, 1800])
        self.assertEqual(observed[0][1]['schedule_reason'], 'trip_end')
        self.assertEqual(observed[1][1]['schedule_reason'], 'no_active_trip')
        self.assertTrue(all(meta['monitor_status'] == 'waiting' for _, meta in observed))
        self.assertEqual(self.state()['_meta']['stop_code'], 'manual_stop')
        self.assertNotIn('next_check_at', self.state()['_meta'])
        self.assertTrue(self.client.stopped)
        self.session.close.assert_called()

    def test_real_session_reuses_cookie_without_persisting_it(self):
        seen = []
        class LocalSite(BaseHTTPRequestHandler):
            def do_POST(handler):
                handler.rfile.read(int(handler.headers.get('Content-Length', 0)))
                seen.append((handler.path, handler.headers.get('Cookie')))
                login = handler.path == '/api/auth/login'
                authenticated = handler.headers.get('Cookie') == 'session=test-only-cookie'
                body = json.dumps({} if login else {'myCars': []}).encode()
                handler.send_response(200 if login or authenticated else 401)
                if login:
                    handler.send_header('Set-Cookie', 'session=test-only-cookie; Path=/api; HttpOnly')
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            def log_message(handler, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), LocalSite)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with requests.Session() as session, patch.object(claimer, 'BASE', f'http://127.0.0.1:{server.server_port}/api'):
                client = claimer.ReadOnlyMonitor('main', self.owner, session, self.pacer)
                try:
                    client.login()
                    client.poll()
                    client.poll()
                    self.assertEqual(seen, [('/api/auth/login', None),
                                            ('/api/state', 'session=test-only-cookie'),
                                            ('/api/state', 'session=test-only-cookie')])
                    self.assertNotIn('test-only-cookie', json.dumps(self.state()))
                finally:
                    client.stop()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_second_login_is_blocked_even_when_called_directly(self):
        self.session.post.return_value = response()
        self.client.login()
        with self.assertRaises(SafetyStop):
            self.client.login()
        self.assertEqual(self.endpoints(), ['auth/login'])

    def test_session_rejection_stops_without_relogin_and_preserves_snapshot(self):
        self.session.post.side_effect = [response(), response(payload={'myCars': []}), response(401)]
        with self.assertRaises(SafetyStop) as error:
            self.client.run(sleep=Mock())
        self.assertEqual(error.exception.code, 'needs_login')
        self.assertEqual(self.endpoints(), ['auth/login', 'state', 'state'])
        state = self.state()
        self.assertEqual(state['main']['last_result'], 'OK')
        self.assertEqual(state['_meta']['stop_code'], 'needs_login')
        self.assertNotIn('next_check_at', state['_meta'])
        view = dashboard.dashboard_view(state)
        self.assertIsNotNone(view['sample'])
        self.assertIn('ورود دستی', view['note'])
        self.assertEqual(view['label'], 'پایش متوقف شده است')

    def test_denial_network_failure_and_redirect_stop_without_retry(self):
        for failure in (response(403), response(307), requests.ConnectionError('private details')):
            with self.subTest(failure=failure):
                session = Mock()
                session.post.side_effect = [response(), failure]
                client = claimer.ReadOnlyMonitor('main', self.owner, session, self.pacer)
                wait = Mock()
                with self.assertRaises(SafetyStop) as error:
                    client.run(sleep=wait)
                self.assertNotIn('private details', str(error.exception))
                self.assertEqual(session.post.call_count, 2)
                self.assertEqual(self.state()['_meta']['monitor_status'], 'stopped')
                wait.assert_not_called()

    def test_login_rate_limit_never_retries_but_persists_shared_cooldown(self):
        self.session.post.return_value = response(429, headers={'Retry-After': '1200'})
        wait = Mock()
        with self.assertRaises(SafetyStop) as error:
            self.client.run(sleep=wait)
        self.assertEqual(error.exception.code, 'rate_limited')
        self.assertEqual(self.endpoints(), ['auth/login'])
        self.owner.cooldown.assert_called_once_with(1200)
        wait.assert_not_called()
        self.assertEqual(self.state()['_meta']['stop_code'], 'rate_limited')

    def test_repeated_rate_limits_are_bounded_and_never_shorten_retry_after(self):
        self.session.post.side_effect = [response()] + [response(429, headers={'Retry-After': '1200'})] * 3
        waits = []
        def wait(seconds):
            waits.append(seconds)
            meta = self.state()['_meta']
            self.assertEqual(meta['monitor_status'], 'cooldown')
            self.assertIn('next_check_at', meta)
        with self.assertRaises(SafetyStop):
            self.client.run(sleep=wait)
        self.assertEqual(waits, [1200, 1200])
        self.assertEqual(self.endpoints(), ['auth/login'] + ['state'] * 3)
        self.assertEqual(self.owner.cooldown.call_count, 3)
        self.assertEqual(self.state()['_meta']['stop_code'], 'rate_limited')

    def test_success_resets_consecutive_cooldown_budget(self):
        self.session.post.side_effect = [response(), response(503), response(503),
                                        response(payload={'myCars': []}), response(503), response(503), response(503)]
        with self.assertRaises(SafetyStop):
            self.client.run(sleep=Mock())
        self.assertEqual(self.endpoints(), ['auth/login'] + ['state'] * 6)

    def test_shared_request_budget_can_only_delay_a_scheduled_read(self):
        self.session.post.side_effect = [response(), response(payload={'myCars': []}), response(401)]
        self.owner.before_request.side_effect = [None, RetryLater(900), None, None]
        sleeps = Mock()
        with self.assertRaises(SafetyStop):
            self.client.run(sleep=sleeps)
        self.assertEqual([call.args[0] for call in sleeps.call_args_list], [900, 1800])
        self.assertEqual(self.endpoints(), ['auth/login', 'state', 'state'])

    def test_malformed_state_stops_without_overwriting_good_sample(self):
        good = {'myCars': [], 'balances': {'usdt': 3}}
        for invalid in ({}, {'myCars': None}, {'myCars': {}}, {'myCars': [None]},
                        {'myCars': [{'trip': []}]}, {'myCars': [{'quota': 'bad'}]},
                        {'myCars': [], 'balances': []}, {'myCars': [], 'stats': None}):
            with self.subTest(invalid=invalid):
                session = Mock()
                session.post.side_effect = [response(), response(payload=good), response(payload=invalid)]
                client = claimer.ReadOnlyMonitor('main', self.owner, session, self.pacer)
                with self.assertRaises(SafetyStop):
                    client.run(sleep=Mock())
                state = self.state()
                self.assertEqual(state['main']['wallet']['usdt'], 3)
                self.assertEqual(state['_meta']['monitor_status'], 'stopped')
                self.assertEqual(session.post.call_count, 3)

    def test_explicit_suspension_removes_snapshot_and_never_retries(self):
        self.session.post.side_effect = [response(), response(payload={'myCars': []}),
                                        response(403, {'error': 'Your account is suspended. Reason: Multi Account'})]
        def shared_suspension():
            # Real DB ownership checks refuse further use once suspended,
            # including the best-effort final status write.
            self.owner.check.side_effect = SafetyStop('persistently suspended')
        self.owner.suspend.side_effect = shared_suspension
        with self.assertLogs('monitor', level='ERROR'), self.assertRaises(SafetyStop):
            self.client.run(sleep=Mock())
        self.assertNotIn('main', self.state())
        self.assertEqual(self.state()['_meta']['monitor_status'], 'suspended')
        self.assertNotIn('next_check_at', self.state()['_meta'])
        self.owner.suspend.assert_called_once()
        self.assertEqual(len(self.endpoints()), 3)

    def test_failed_schedule_persistence_stops_before_any_more_requests(self):
        self.session.post.side_effect = [response(), response(payload={'myCars': []})]
        real_publish = self.client.publish_activity
        def publish(status, *args, **kwargs):
            if status == 'waiting':
                raise OSError('private storage error')
            return real_publish(status, *args, **kwargs)
        with patch.object(self.client, 'publish_activity', side_effect=publish), self.assertRaises(SafetyStop):
            self.client.run(sleep=Mock())
        self.assertEqual(len(self.endpoints()), 2)
        self.assertEqual(self.state()['_meta']['monitor_status'], 'stopped')
        self.assertTrue(self.client.stopped)

    def test_lost_ownership_prevents_new_requests_or_reconnects(self):
        self.owner.check.side_effect = SafetyStop('lost ownership')
        with self.assertLogs('monitor', level='ERROR'), self.assertRaises(SafetyStop):
            self.client.run(sleep=Mock())
        self.session.post.assert_not_called()
        self.assertTrue(self.client.stopped)


class SavedActivityTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)
        archive = patch.object(dashboard, 'banned_accounts_info', return_value=[])
        archive.start()
        self.addCleanup(archive.stop)

    def status(self, **changes):
        meta = {'monitor_user': 'main', 'monitor_status': 'waiting',
                'updated_at': NOW.strftime('%Y-%m-%d %H:%M:%S'), 'schedule_reason': 'trip_end',
                'next_check_at': (NOW + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')}
        meta.update(changes)
        return {'_meta': meta}

    def test_future_schedule_is_not_a_claim_that_worker_is_running(self):
        view = dashboard.dashboard_view(self.status(), NOW)
        self.assertIn('بررسی بعدیِ برنامه‌ریزی‌شده', view['note'])
        self.assertIn('این زمان تضمین اجرا نیست', view['note'])
        self.assertIn('بر اساس پایان سفر', view['note'])
        self.assertIsNone(view['received'])

    def test_cooldown_is_shown_as_a_recorded_wait_not_a_healthy_running_worker(self):
        view = dashboard.dashboard_view(self.status(monitor_status='cooldown'), NOW)
        self.assertEqual(view['label'], 'مهلت انتظار ثبت شده است')
        self.assertEqual(view['tone'], 'warning')
        self.assertIn('پایان مهلت انتظار ثبت‌شده', view['note'])
        self.assertIn('این زمان تضمین اجرا نیست', view['note'])

    def test_passed_schedule_is_not_reported_as_an_upcoming_check(self):
        view = dashboard.dashboard_view(self.status(), NOW + timedelta(hours=2))
        self.assertIn('موعد بررسی ثبت‌شده گذشته است', view['note'])
        self.assertNotIn('بررسی بعدیِ برنامه‌ریزی‌شده', view['note'])

    def test_disabled_monitor_never_displays_an_old_schedule_as_active(self):
        with patch.dict(os.environ, {'SITE_MONITORING_ENABLED': 'false'}):
            view = dashboard.dashboard_view(self.status(), NOW)
        self.assertEqual(view['label'], 'پایش خاموش')
        self.assertNotIn('بررسی بعدیِ برنامه‌ریزی‌شده', view['note'])

    def test_foreign_or_invalid_metadata_is_ignored(self):
        for changes in ({'monitor_user': 'other'}, {'updated_at': 'bad'},
                        {'updated_at': (NOW + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')},
                        {'next_check_at': []}, {'monitor_status': 'invented'},
                        {'next_check_at': NOW.strftime('%Y-%m-%d %H:%M:%S')}):
            with self.subTest(changes=changes):
                view = dashboard.dashboard_view(self.status(**changes), NOW)
                self.assertIsNone(view['activity'])
                self.assertNotIn('بررسی بعدیِ برنامه‌ریزی‌شده', view['note'])

    def test_stop_codes_are_whitelisted_not_raw_server_or_local_error_text(self):
        view = dashboard.dashboard_view(self.status(monitor_status='stopped', stop_code='<script>private</script>'), NOW)
        self.assertEqual(view['label'], 'پایش متوقف شده است')
        self.assertNotIn('private', dashboard.status_section(view))
        self.assertIn('بررسی دستی', view['empty_note'])


if __name__ == '__main__':
    unittest.main()
