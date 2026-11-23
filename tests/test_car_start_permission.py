from contextlib import ExitStack
import copy
import json
import unittest
from unittest.mock import Mock, patch

import requests

import claimer
import dashboard


def car(car_id='own', **updates):
    value = {'id': car_id, 'name': car_id, 'canDrive': True, 'block': None,
             'trip': None, 'broken': False, 'quota': {'left': 5, 'bonus': 0}}
    value.update(updates)
    return value


def state(*own, rented=None):
    return {'myCars': list(own), 'rentedCars': rented or []}


def response(code=200, payload=None):
    value = requests.Response()
    value.status_code = code
    value._content = json.dumps(payload if payload is not None else {'trip': {'startedAt': 1000, 'endsAt': 61000}}).encode()
    value.url = 'https://example.test/api/trips/start'
    return value


class CarStartPermissionTests(unittest.TestCase):
    def sweep(self, snapshots, replies=None):
        session = Mock(cookies={'session': 'active'})
        session.post.side_effect = replies or [response()]
        status = {}
        with ExitStack() as stack:
            stack.enter_context(patch.object(claimer, 'get_session', return_value=session))
            stack.enter_context(patch.object(claimer, 'get_state', side_effect=snapshots))
            stack.enter_context(patch.object(claimer, 'fetch_missions', return_value=None))
            stack.enter_context(patch.object(claimer, 'fetch_referrals', return_value=None))
            stack.enter_context(patch.object(claimer, 'login', return_value=session))
            stack.enter_context(patch.object(claimer, 'make_session', return_value=session))
            stack.enter_context(patch.object(claimer.time, 'sleep'))
            stack.enter_context(patch.object(claimer, 'SESSIONS', {}))
            mark = stack.enter_context(patch.object(claimer, 'mark_banned'))
            claimer.process_account({'username': 'main'}, status)
            mark.assert_not_called()
        return session, status['main']

    def test_only_explicit_true_with_no_restrictions_is_allowed(self):
        for block in (None, {}, {'code': None, 'msg': ''}, False):
            self.assertEqual(claimer.car_start_block(car(block=block)), (None, None))
        for allowed in (False, None, 1, 'true'):
            with self.subTest(canDrive=allowed):
                self.assertEqual(claimer.car_start_block(car(canDrive=allowed))[0], 'permission')
        missing = car()
        del missing['canDrive']
        self.assertEqual(claimer.car_start_block(missing)[0], 'permission')

    def test_block_overrides_can_drive_and_preserves_message(self):
        value = car(block={'code': 'turn', 'msg': 'Ahora no es tu turno con este taxi.'})
        self.assertEqual(claimer.car_start_block(value), ('turn', value['block']['msg']))
        for block in ('wait', True, {'until': 100}, {'message': 'wait for turn'}):
            self.assertIsNotNone(claimer.car_start_block(car(block=block))[0])

    def test_broken_busy_and_exhausted_cars_are_not_allowed(self):
        for updates in ({'broken': True}, {'trip': {'endsAt': 100}},
                        {'quota': {'left': 0, 'bonus': 0}}):
            self.assertIsNotNone(claimer.car_start_block(car(**updates))[0])
        self.assertEqual(claimer.car_start_block(car(quota={'left': 0, 'bonus': 1})), (None, None))

    def test_blocked_car_is_skipped_without_errors_or_post(self):
        session, result = self.sweep([state(car(canDrive=False, block={'code': 'turn', 'msg': 'wait'}))])
        session.post.assert_not_called()
        self.assertEqual(result['last_result'], 'OK')
        self.assertEqual(result['errors_today'], 0)
        self.assertEqual(result['cars'][0]['block_msg'], 'wait')
        self.assertEqual(result['cars'][0]['status'], 'unavailable')

    def test_permission_is_refreshed_before_start(self):
        session, result = self.sweep([state(car()), state(car(canDrive=False))])
        session.post.assert_not_called()
        self.assertEqual(result['cars'][0]['block_code'], 'permission')
        self.assertEqual(result['errors_today'], 0)

    def test_starting_own_car_does_not_use_stale_permission_for_rental(self):
        both_ready = state(car(), rented=[car('rented')])
        after_start = state(car(trip={'endsAt': 61000}), rented=[
            car('rented', canDrive=False, block={'code': 'driving', 'msg': 'another car is on trip'})])
        session, result = self.sweep([both_ready, copy.deepcopy(both_ready), after_start])
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.post.call_args.kwargs['json'], {'carId': 'own'})
        rental = next(item for item in result['cars'] if item['id'] == 'rented')
        self.assertEqual(rental['block_code'], 'driving')
        self.assertEqual(result['trips_started_today'], 1)
        self.assertEqual(result['errors_today'], 0)

    def test_rental_can_start_when_own_car_has_no_permission(self):
        ready = state(car(canDrive=False), rented=[car('rented')])
        session, result = self.sweep([ready, copy.deepcopy(ready)])
        self.assertEqual(session.post.call_args.kwargs['json'], {'carId': 'rented'})
        self.assertEqual(result['trips_started_today'], 1)

    def test_retries_recheck_permission_instead_of_repeating_unauthorized_start(self):
        for failure in (response(401), response(429), requests.ConnectionError('temporary failure')):
            with self.subTest(failure=type(failure).__name__):
                session, result = self.sweep(
                    [state(car()), state(car()), state(car(canDrive=False))], [failure])
                self.assertEqual(session.post.call_count, 1)
                self.assertEqual(result['cars'][0]['status'], 'unavailable')
                self.assertEqual(result['errors_today'], 0)

    def test_server_rejection_after_refresh_is_not_retried_or_treated_as_ban(self):
        message = 'Ahora no es tu turno con este taxi.'
        for error in ({'message': message}, message):
            with self.subTest(error=error):
                session, result = self.sweep([state(car()), state(car())], [response(400, {'error': error})])
                session.post.assert_called_once()
                self.assertEqual(result['cars'][0]['block_msg'], message)
                self.assertEqual(result['cars'][0]['status'], 'unavailable')
                self.assertEqual(result['errors_today'], 0)

    def test_disappeared_car_is_not_started(self):
        session, result = self.sweep([state(car()), state()])
        session.post.assert_not_called()
        self.assertEqual(result['cars'][0]['block_code'], 'missing')

    def test_transport_cannot_blindly_retry_start_without_rechecking(self):
        with claimer.make_session() as session:
            self.assertEqual(session.get_adapter(f'{claimer.BASE}/trips/start').max_retries.total, 0)
            self.assertEqual(session.get_adapter(f'{claimer.BASE}/state').max_retries.total, 3)

    def test_failed_state_refresh_never_sends_start_post(self):
        session = Mock()
        with patch.object(claimer, 'get_state', side_effect=RuntimeError('state unavailable')):
            with self.assertRaises(RuntimeError):
                claimer.start_trip(session, 'own', 'main')
        session.post.assert_not_called()

    def test_car_is_reconsidered_on_next_sweep(self):
        blocked = state(car(canDrive=False))
        session, _ = self.sweep([blocked])
        session.post.assert_not_called()
        session, result = self.sweep([state(car()), state(car())])
        session.post.assert_called_once()
        self.assertEqual(result['trips_started_today'], 1)

    def test_dashboard_shows_escaped_restriction_instead_of_quota_only_label(self):
        html = dashboard.car_card({'status': 'unavailable', 'block_msg': '<b>wait for turn</b>'})
        self.assertIn('&lt;b&gt;wait for turn&lt;/b&gt;', html)
        self.assertNotIn('آماده برای استارت خودکار', html)


if __name__ == '__main__':
    unittest.main()
