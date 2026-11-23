from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests

import claimer
import dashboard
from monitor_safety import SafetyStop, SharedMonitorLock, check_policy
from runtime_config import ALLOWED_PRIMARY_ACCOUNT
from trip_rules import start_block


PRIMARY = ALLOWED_PRIMARY_ACCOUNT
ENV = {'PRIMARY_ACCOUNT': PRIMARY, 'PRIMARY_ACCOUNT_PASSWORD': 'test-only-password',
       'SITE_MONITORING_ENABLED': 'true', 'AUTO_START_TRIPS_ENABLED': 'true'}


def ready(car_id=1, **changes):
    car = {'id': car_id, 'name': 'Taxi', 'trip': None, 'canDrive': True,
           'quota': {'used': 10, 'limit': 10, 'left': 0, 'bonus': 120}}
    car.update(changes)
    return car


def response(payload, code=200):
    result = requests.Response()
    result.status_code = code
    result._content = json.dumps(payload).encode()
    return result


class JournalConnection:
    """Model the persistent SQL phase transitions across independent clients."""
    closed = False

    def __init__(self):
        self.rows = {}
        self.cooldowns = []

    def execute(self, query, params=None):
        row = None
        if 'SELECT suspended' in query:
            row = (False,)
        elif 'EXTRACT' in query:
            row = (0,)
        elif 'GREATEST(next_request_at' in query:
            self.cooldowns.append(params[0])
        elif 'SELECT car_key FROM cryptotaxi_trip_starts' in query:
            row = next(((car_key,) for (key, car_key), value in self.rows.items()
                        if key == params[0] and value['phase'] in ('pending', 'confirmed')), None)
        elif 'INSERT INTO cryptotaxi_trip_starts' in query:
            key = (params[0], params[1])
            old = self.rows.get(key)
            if old is None or old['phase'] == 'observed':
                self.rows[key] = {'phase': 'pending'}
                row = ('pending',)
        elif "SET phase = 'confirmed'" in query:
            key = (params[2], params[3])
            old = self.rows.get(key)
            if old and old['phase'] == 'pending':
                old.update(phase='confirmed', started=params[0], ends=params[1])
                row = ('confirmed',)
        elif "SET phase = 'observed'" in query:
            old = self.rows.get((params[0], params[1]))
            if (old and old['phase'] in ('confirmed', 'observed')
                    and old['started'] == params[2] and old['ends'] == params[3]):
                old['phase'] = 'observed'
                row = ('observed',)
        return Mock(fetchone=lambda: row)


class TripRuleTests(unittest.TestCase):
    def test_real_bonus_extends_ten_of_ten_without_inventing_remaining_quota(self):
        self.assertIsNone(start_block(ready(), time.time() * 1000))
        self.assertIsNone(start_block(ready(quota={'left': 2}), time.time() * 1000))
        self.assertIsNotNone(start_block(ready(quota={'used': 0, 'limit': 10}), time.time() * 1000))

    def test_explicit_turn_permission_and_valid_capacity_are_mandatory(self):
        for changes in ({'canDrive': False}, {'canDrive': 'true'}, {'broken': True},
                        {'trip': {}}, {'trip': {'endsAt': 1}}, {'block': {'code': 'not_your_turn'}},
                        {'id': None}, {'id': True}, {'quota': None},
                        {'quota': {'left': 0, 'bonus': 0}}, {'quota': {'left': '5'}},
                        {'quota': {'bonus': True}}, {'quota': {'bonus': float('nan')}},
                        {'quota': {'bonus': -5}}, {'quota': {'bonus': 0.5}},
                        {'quota': {'bonus': 5, 'bonusExpiresAt': 1}}):
            with self.subTest(changes=changes):
                self.assertIsNotNone(start_block(ready(**changes), time.time() * 1000))


class AutoStartTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for module in (claimer, dashboard):
            for name in ('STATUS_FILE', 'BANNED_ACCOUNTS_FILE'):
                setting = patch.object(module, name, str(root / (name + '.json')))
                setting.start()
                self.addCleanup(setting.stop)
        self.db = JournalConnection()
        self.owner = SharedMonitorLock(PRIMARY)
        self.owner.connection = self.db
        self.session, self.pacer = Mock(), Mock()
        self.client = claimer.ReadOnlyMonitor(PRIMARY, self.owner, self.session, self.pacer)
        now = time.time() * 1000
        self.trip = {'startedAt': now - 1000, 'endsAt': now + 3600000}

    def state(self, own=None, rentals=None):
        return response({'myCars': [ready()] if own is None else own,
                         'rentedCars': [] if rentals is None else rentals})

    def journal(self, car_id=1):
        return self.db.rows.get((self.owner.key, str(car_id)))

    def calls(self):
        return [call.args[0].removeprefix(claimer.BASE + '/') for call in self.session.post.call_args_list]

    def test_one_owned_start_persisted_before_send_then_verified_in_same_session(self):
        responses = iter([self.state(rentals=[ready(2)]), response({'trip': self.trip}),
                          self.state(own=[ready(trip=self.trip)], rentals=[ready(2)])])
        def send(url, **kwargs):
            if url.endswith('/trips/start'):
                self.assertEqual(self.journal()['phase'], 'pending')
                self.assertEqual(kwargs['json'], {'carId': 1})
                self.assertFalse(kwargs['allow_redirects'])
            return next(responses)
        self.session.post.side_effect = send
        plan = self.client.poll()
        self.assertEqual(self.calls(), ['state', 'trips/start', 'state'])
        self.assertEqual(plan, (300, 'ready_trip'))
        self.assertEqual(self.journal()['phase'], 'observed')
        self.assertIsNone(self.journal(2))
        snapshot = json.loads(Path(claimer.STATUS_FILE).read_text())
        self.assertEqual(snapshot[PRIMARY]['cars'][0]['status'], 'on_trip')
        self.assertEqual(self.pacer.wait.call_count, 3)

    def test_rental_is_considered_only_when_no_owned_car_is_eligible(self):
        self.session.post.side_effect = [self.state(own=[ready(canDrive=False)], rentals=[ready(2)]),
                                         response({'trip': self.trip}),
                                         self.state(own=[ready(canDrive=False)], rentals=[ready(2, trip=self.trip)])]
        self.client.poll()
        self.assertEqual(self.session.post.call_args_list[1].kwargs['json'], {'carId': 2})
        self.assertIsNone(self.journal(1))

    def test_disabled_default_remains_read_only_even_when_ready(self):
        with patch.dict(os.environ, {'AUTO_START_TRIPS_ENABLED': ''}):
            self.session.post.return_value = self.state()
            self.client.poll()
            with self.assertRaises(SafetyStop):
                check_policy(PRIMARY, 'trips/start')
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.db.rows, {})

    def test_no_permission_or_capacity_means_no_mutation(self):
        self.session.post.return_value = self.state(own=[ready(canDrive=False), ready(2, quota={'left': 0, 'bonus': 0})])
        self.client.poll()
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.db.rows, {})

    def test_direct_mutation_without_preflight_intent_is_rejected(self):
        with self.assertRaises(SafetyStop):
            self.client.request('trips/start', {'carId': 1})
        self.session.post.assert_not_called()
        self.assertEqual(self.db.rows, {})

    def test_stale_preflight_after_pacing_cannot_reserve_or_send(self):
        self.session.post.return_value = self.state()
        def wait():
            if self.pacer.wait.call_count == 2:
                self.client._state_received -= 91
        self.pacer.wait.side_effect = wait
        with self.assertRaises(SafetyStop) as error:
            self.client.poll()
        self.assertEqual(error.exception.code, 'start_blocked')
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.db.rows, {})

    def test_slow_journal_reservation_cannot_dispatch_a_stale_start(self):
        self.session.post.return_value = self.state()
        reserve = self.owner.reserve_trip_start
        def slow_reserve(car_id):
            reserve(car_id)
            self.client._state_received -= 91
        with patch.object(self.owner, 'reserve_trip_start', side_effect=slow_reserve), self.assertRaises(SafetyStop):
            self.client.poll()
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.journal()['phase'], 'pending')

    def test_confirm_persistence_failure_never_replays_sent_start(self):
        self.session.post.side_effect = [self.state(), response({'trip': self.trip})]
        with patch.object(self.owner, 'confirm_trip_start', side_effect=SafetyStop('storage error', 'start_unknown')):
            with self.assertRaises(SafetyStop):
                self.client.poll()
        self.assertEqual(self.calls(), ['state', 'trips/start'])
        self.assertEqual(self.journal()['phase'], 'pending')

    def test_policy_is_rechecked_after_pacing_before_mutation(self):
        self.session.post.return_value = self.state()
        def wait():
            if self.pacer.wait.call_count == 2:
                os.environ['AUTO_START_TRIPS_ENABLED'] = 'false'
        self.pacer.wait.side_effect = wait
        with self.assertRaises(SafetyStop):
            self.client.poll()
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.db.rows, {})

    def test_ambiguous_outcomes_are_durable_and_never_replayed_after_restart(self):
        failures = [requests.Timeout('private server details'), response({}), response({'error': 'rejected'}, 400),
                    response({}, 429), response({}, 503), response({}, 401), response({}, 403), response({}, 307)]
        for failure in failures:
            with self.subTest(failure=str(failure)):
                self.db.rows.clear()
                self.db.cooldowns.clear()
                session = Mock()
                session.post.side_effect = [self.state(), failure]
                client = claimer.ReadOnlyMonitor(PRIMARY, self.owner, session, Mock())
                with self.assertRaises(SafetyStop) as error:
                    client.poll()
                self.assertNotIn('private server details', str(error.exception))
                self.assertEqual(session.post.call_count, 2)
                self.assertEqual(self.journal()['phase'], 'pending')
                if isinstance(failure, requests.Response) and failure.status_code in (429, 503):
                    self.assertEqual(self.db.cooldowns, [300])
                restarted_session = Mock()
                restarted_session.post.return_value = self.state()
                restarted = claimer.ReadOnlyMonitor(PRIMARY, self.owner, restarted_session, Mock())
                with self.assertRaises(SafetyStop) as blocked:
                    restarted.poll()
                self.assertEqual(blocked.exception.code, 'start_unknown')
                self.assertEqual(restarted_session.post.call_count, 1)

    def test_confirmed_response_without_observation_remains_blocked(self):
        self.session.post.side_effect = [self.state(), response({'trip': self.trip}), self.state()]
        with self.assertRaises(SafetyStop) as error:
            self.client.poll()
        self.assertEqual(error.exception.code, 'start_unknown')
        self.assertEqual(self.journal()['phase'], 'confirmed')
        self.session.post.side_effect = None
        self.session.post.return_value = self.state()
        restarted = claimer.ReadOnlyMonitor(PRIMARY, self.owner, self.session, Mock())
        with self.assertRaises(SafetyStop):
            restarted.poll()
        self.assertEqual(self.calls().count('trips/start'), 1)

    def test_new_episode_only_after_success_is_observed_and_server_reports_idle_again(self):
        self.session.post.side_effect = [self.state(), response({'trip': self.trip}),
                                         self.state(own=[ready(trip=self.trip)])]
        self.client.poll()
        self.assertEqual(self.journal()['phase'], 'observed')
        later = time.time() + 7200
        next_trip = {'startedAt': later * 1000, 'endsAt': (later + 3600) * 1000}
        self.session.post.side_effect = [self.state(), response({'trip': next_trip}),
                                         self.state(own=[ready(trip=next_trip)])]
        with patch.object(claimer.time, 'time', return_value=later):
            self.client.poll()
        self.assertEqual(self.calls().count('trips/start'), 2)
        self.assertEqual(self.journal()['phase'], 'observed')

    def test_unresolved_old_start_stops_other_starts_even_if_original_car_is_on_trip(self):
        self.db.rows[(self.owner.key, '1')] = {'phase': 'pending'}
        self.session.post.return_value = self.state(own=[ready(trip=self.trip)], rentals=[ready(2)])
        with self.assertRaises(SafetyStop) as error:
            self.client.poll()
        self.assertEqual(error.exception.code, 'start_unknown')
        self.assertEqual(self.calls(), ['state'])
        self.assertIsNone(self.journal(2))

    def test_auto_run_reuses_the_one_login_session(self):
        self.session.post.side_effect = [response({}), self.state(), response({'trip': self.trip}),
                                         self.state(own=[ready(trip=self.trip)])]
        with self.assertRaises(KeyboardInterrupt):
            self.client.run(sleep=Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(self.calls(), ['auth/login', 'state', 'trips/start', 'state'])
        self.assertEqual(self.journal()['phase'], 'observed')
        self.assertTrue(self.client.stopped)

    def test_mutation_intent_persistence_failure_never_sends(self):
        self.session.post.return_value = self.state()
        with patch.object(self.owner, 'reserve_trip_start', side_effect=SafetyStop('journal unavailable', 'start_unknown')):
            with self.assertRaises(SafetyStop):
                self.client.poll()
        self.assertEqual(self.calls(), ['state'])

    def test_opt_in_never_enables_claim_rent_registration_or_other_identity(self):
        for endpoint in ('missions/claim', 'referrals/claim', 'market/rent', 'auth/register'):
            with self.subTest(endpoint=endpoint), self.assertRaises(SafetyStop):
                check_policy(PRIMARY, endpoint)
        with self.assertRaises(SafetyStop):
            check_policy('other@example.test', 'trips/start')
        with patch.dict(os.environ, {'SITE_MONITORING_ENABLED': 'false'}), self.assertRaises(SafetyStop):
            check_policy(PRIMARY, 'trips/start')

    def test_dashboard_describes_configured_auto_start_without_claiming_worker_liveness(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            page = dashboard.render()
        self.assertIn('استارت خودکار در تنظیمات فعال است', page)
        self.assertNotIn('حالت امن · فقط‌خواندنی', page)
        self.assertIn('کلیم و رنت غیرفعال', page)
        self.assertIn('نامشخص', dashboard.trip_count_text(None))


if __name__ == '__main__':
    unittest.main()
