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
from monitor_safety import RetryLater, RequestPacer, SafetyStop, SharedMonitorLock, check_policy
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

    def test_rate_limit_recovery_starts_main_then_rental_and_saves_trip_timers(self):
        # Offline end-to-end loop: three rate-limited reads, recovery, then
        # verified starts over separate cycles. No restart/re-login or replay.
        after_main = self.state(own=[ready(trip=self.trip)], rentals=[ready(2)])
        self.session.post.side_effect = [
            response({}), response({}, 429), response({}, 429), response({}, 429),
            self.state(rentals=[ready(2)]), response({'trip': self.trip}), after_main,
            after_main, response({'trip': self.trip}),
            self.state(own=[ready(trip=self.trip)], rentals=[ready(2, trip=self.trip)]),
        ]
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            if len(waits) <= 3:
                self.assertEqual(self.calls().count('trips/start'), 0)
                self.assertEqual(json.loads(Path(claimer.STATUS_FILE).read_text())
                                 ['_meta']['monitor_status'], 'cooldown')
            if len(waits) == 5:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.client.run(sleep=sleep)
        self.assertEqual(waits[:4], [300, 300, 900, 300])
        self.assertEqual(self.calls().count('auth/login'), 1)
        starts = [call.kwargs['json']['carId'] for call in self.session.post.call_args_list
                  if call.args[0].endswith('/trips/start')]
        self.assertEqual(starts, [1, 2])
        for car_id in (1, 2):
            self.assertEqual(self.journal(car_id)['phase'], 'observed')
        saved = json.loads(Path(claimer.STATUS_FILE).read_text())[PRIMARY]['cars']
        self.assertTrue(all(car['ends_at'] == self.trip['endsAt'] for car in saved))
        self.assertTrue(all(car['status'] == 'on_trip' for car in saved))

    def test_rental_is_considered_only_when_no_owned_car_is_eligible(self):
        self.session.post.side_effect = [self.state(own=[ready(canDrive=False)], rentals=[ready(2)]),
                                         response({'trip': self.trip}),
                                         self.state(own=[ready(canDrive=False)], rentals=[ready(2, trip=self.trip)])]
        self.client.poll()
        self.assertEqual(self.session.post.call_args_list[1].kwargs['json'], {'carId': 2})
        self.assertIsNone(self.journal(1))

    def test_main_ordinary_trip_and_multiple_eligible_rentals_overlap_across_cycles(self):
        base, elapsed = time.time(), [0]
        main_trip = {'startedAt': base * 1000, 'endsAt': (base + 3600) * 1000}
        rental_trip = {'startedAt': (base + 300) * 1000, 'endsAt': (base + 3900) * 1000}
        second_rental_trip = {'startedAt': (base + 600) * 1000, 'endsAt': (base + 4200) * 1000}
        ordinary = {'used': 0, 'limit': 10, 'left': 10, 'bonus': 0}
        own_idle = ready(1, quota=ordinary)
        own_driving = ready(1, quota=ordinary, trip=main_trip)
        rental_idle = ready(2, quota=ordinary)
        rental_driving = ready(2, quota=ordinary, trip=rental_trip)
        second_idle = ready(4, quota=ordinary)
        second_driving = ready(4, quota=ordinary, trip=second_rental_trip)
        blocked = ready(3, canDrive=False, quota=ordinary, block={'code': 'not_your_turn'})
        after_main = self.state(own=[own_driving], rentals=[rental_idle, blocked, second_idle])
        after_first_rental = self.state(own=[own_driving], rentals=[rental_driving, blocked, second_idle])
        self.session.post.side_effect = [
            response({}), self.state(own=[own_idle], rentals=[rental_idle, blocked, second_idle]),
            response({'trip': main_trip}), after_main,
            after_main, response({'trip': rental_trip}), after_first_rental,
            after_first_rental, response({'trip': second_rental_trip}),
            self.state(own=[own_driving], rentals=[rental_driving, blocked, second_driving]),
        ]
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise KeyboardInterrupt()
            elapsed[0] += seconds
        with patch.object(claimer.time, 'time', side_effect=lambda: base + elapsed[0]):
            with self.assertRaises(KeyboardInterrupt):
                self.client.run(sleep=sleep)
        self.assertEqual(waits, [300, 300, 3015])
        starts = [call.kwargs['json']['carId'] for call in self.session.post.call_args_list
                  if call.args[0].endswith('/trips/start')]
        self.assertEqual(starts, [1, 2, 4])  # Main FIRST, then permitted rentals BEFORE it ends.
        self.assertEqual(self.calls().count('auth/login'), 1)
        self.assertEqual(self.pacer.wait.call_count, len(self.calls()))
        for car_id in (1, 2, 4):
            self.assertEqual(self.journal(car_id)['phase'], 'observed')
        self.assertIsNone(self.journal(3))
        cars = json.loads(Path(claimer.STATUS_FILE).read_text())[PRIMARY]['cars']
        self.assertEqual(sum(car['status'] == 'on_trip' for car in cars), 3)
        self.assertTrue(all(car['ends_at'] > (base + elapsed[0]) * 1000
                            for car in cars if car['status'] == 'on_trip'))

    def test_main_trip_does_not_override_rental_turn_or_capacity_restrictions(self):
        self.session.post.return_value = self.state(own=[ready(trip=self.trip)], rentals=[
            ready(2, canDrive=False, block={'code': 'not_your_turn'}),
            ready(3, quota={'left': 0, 'bonus': 0}),
        ])
        delay, reason = self.client.poll()
        self.assertEqual(reason, 'trip_end')
        self.assertGreater(delay, claimer.MIN_POLL_SECONDS)
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.db.rows, {})

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

    def test_active_owned_trip_is_not_restarted_and_eligible_rental_can_start(self):
        self.session.post.side_effect = [
            self.state(own=[ready(trip=self.trip)], rentals=[ready(2)]),
            response({'trip': self.trip}),
            self.state(own=[ready(trip=self.trip)], rentals=[ready(2, trip=self.trip)]),
        ]
        self.client.poll()
        starts = [call.kwargs['json'] for call in self.session.post.call_args_list
                  if call.args[0].endswith('/trips/start')]
        self.assertEqual(starts, [{'carId': 2}])

    def test_snapshot_explains_preflight_without_changing_eligibility(self):
        self.session.post.return_value = self.state(own=[ready()], rentals=[ready(2, canDrive=False)])
        self.client.poll(allow_start=False)
        cars = json.loads(Path(claimer.STATUS_FILE).read_text())[PRIMARY]['cars']
        self.assertEqual(cars[0]['start_check'], 'eligible')
        self.assertEqual(cars[1]['start_check'], 'not_permitted')
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.db.rows, {})

    def test_direct_mutation_without_preflight_intent_is_rejected(self):
        with self.assertRaises(SafetyStop):
            self.client.request('trips/start', {'carId': 1})
        self.session.post.assert_not_called()
        self.assertEqual(self.db.rows, {})

    def test_shared_budget_deferral_before_start_is_a_cooldown_not_a_stop(self):
        self.session.post.return_value = self.state()
        with patch.object(self.owner, 'before_request', side_effect=[None, RetryLater(0.25)]):
            with self.assertRaises(RetryLater) as error:
                self.client.poll()
        self.assertEqual(error.exception.seconds, 0.25)
        self.assertFalse(self.client.stopped)
        self.assertEqual(self.calls(), ['state'])
        self.assertEqual(self.db.rows, {})

    def test_verification_budget_deferral_preserves_unknown_outcome_and_journal(self):
        self.session.post.side_effect = [self.state(), response({'trip': self.trip})]
        with patch.object(self.owner, 'before_request', side_effect=[None, None, RetryLater(0.25)]):
            with self.assertRaises(SafetyStop) as error:
                self.client.poll()
        self.assertEqual(error.exception.code, 'start_unknown')
        self.assertEqual(self.calls(), ['state', 'trips/start'])
        self.assertEqual(self.journal()['phase'], 'confirmed')
        # A new process cannot start another taxi while the outcome is unobserved.
        with self.assertRaises(SafetyStop) as unresolved:
            self.owner.ensure_no_unresolved_starts()
        self.assertEqual(unresolved.exception.code, 'start_unknown')

    def test_db_reservation_latency_does_not_create_perpetual_budget_deferrals(self):
        clock, shared_next = [0.0], [0.0]
        reservations = []
        def sleep(seconds):
            clock[0] += seconds
        def reserve_budget():
            if clock[0] < shared_next[0]:
                raise RetryLater(shared_next[0] - clock[0])
            clock[0] += 0.02  # DB latency after the local timer was armed.
            shared_next[0] = clock[0] + 60.0
            reservations.append(clock[0])
        self.client.pacer = RequestPacer(clock=lambda: clock[0], sleep=sleep)
        self.session.post.side_effect = [self.state(), response({'trip': self.trip}),
                                         self.state(own=[ready(trip=self.trip)])]
        with patch.object(self.owner, 'before_request', side_effect=reserve_budget), \
             patch.object(claimer.time, 'monotonic', side_effect=lambda: clock[0]):
            self.client.poll()
        self.assertEqual(self.calls(), ['state', 'trips/start', 'state'])
        self.assertEqual(self.journal()['phase'], 'observed')
        self.assertEqual(len(reservations), 3)
        for earlier, later in zip(reservations, reservations[1:]):
            self.assertGreaterEqual(later - earlier, 60)

    def test_pre_send_cooldown_recovers_with_fresh_state_not_blind_start_retry(self):
        self.session.post.side_effect = [response({}), self.state(), self.state(),
                                         response({'trip': self.trip}),
                                         self.state(own=[ready(trip=self.trip)])]
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                raise KeyboardInterrupt()
        # login, state, deferred start (NO send), fresh state, start, verify
        with patch.object(self.owner, 'before_request', side_effect=[None, None, RetryLater(0.25), None, None, None]):
            with self.assertRaises(KeyboardInterrupt):
                self.client.run(sleep=sleep)
        self.assertEqual(waits[0], 0.25)
        self.assertEqual(self.calls(), ['auth/login', 'state', 'state', 'trips/start', 'state'])
        self.assertEqual(self.journal()['phase'], 'observed')

    def test_failed_post_start_state_read_cannot_be_labelled_as_plain_state_failure(self):
        self.session.post.side_effect = [self.state(), response({'trip': self.trip}), response({})]
        with self.assertRaises(SafetyStop) as error:
            self.client.poll()
        self.assertEqual(error.exception.code, 'start_unknown')
        self.assertEqual(self.journal()['phase'], 'confirmed')
        self.assertEqual(self.calls(), ['state', 'trips/start', 'state'])

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
