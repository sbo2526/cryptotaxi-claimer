"""Bounded deployment handover is not a worker restart or a lock takeover."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import claimer
import dashboard
import monitor_runtime
from monitor_safety import SafetyStop, SharedMonitorLock, STARTUP_LOCK_WAIT_SECONDS
from runtime_config import ALLOWED_PRIMARY_ACCOUNT


ENV = {'PRIMARY_ACCOUNT': ALLOWED_PRIMARY_ACCOUNT, 'PRIMARY_ACCOUNT_PASSWORD': 'fixture-only',
       'SITE_MONITORING_ENABLED': 'true', 'MONITOR_LOCK_DATABASE_URL': 'fixture-only'}


class StartupLockWaitTests(unittest.TestCase):
    def setUp(self):
        context = patch.dict(os.environ, ENV, clear=True)
        context.start()
        self.addCleanup(context.stop)
        self.now = 0
        self.sleeps = []
        self.connection = Mock(closed=False)
        self.connect = Mock(return_value=self.connection)
        self.waiting = Mock()
        self.queries = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def replies(self, locks, suspended=False):
        replies = iter(locks)
        def execute(query, params=None):
            self.queries.append(query)
            if 'pg_try_advisory_lock' in query:
                row = next(replies)
                if isinstance(row, Exception):
                    raise row
            elif 'SELECT suspended' in query:
                row = (suspended,)
            else:
                row = None
            return Mock(fetchone=lambda: row)
        self.connection.execute.side_effect = execute

    def owner(self, **changes):
        kwargs = dict(startup_wait=STARTUP_LOCK_WAIT_SECONDS, clock=lambda: self.now,
                      sleep=self.sleep, on_wait=self.waiting)
        kwargs.update(changes)
        return SharedMonitorLock(ALLOWED_PRIMARY_ACCOUNT, self.connect, **kwargs)

    def test_acquires_after_old_owner_releases_without_reconnect_or_prelock_writes(self):
        self.replies([(False,), (False,), (True,)])
        with self.owner() as owner:
            self.assertIs(owner.connection, self.connection)
            self.assertEqual(self.now, 20)
            self.assertTrue(all('pg_try_advisory_lock' in query for query in self.queries[:3]))
            self.assertIn('CREATE TABLE', self.queries[3])
        self.connect.assert_called_once()
        self.connection.close.assert_called_once()
        self.waiting.assert_called_once()
        self.assertEqual(self.sleeps, [10, 10])
        self.assertEqual(sum('pg_try_advisory_lock' in query for query in self.queries), 3)
        self.assertFalse(any('pg_terminate' in query or 'pg_advisory_unlock' in query for query in self.queries))

    def test_persistent_duplicate_stops_after_bounded_wait_without_shared_writes(self):
        self.replies([(False,)] * 13)
        with self.assertRaises(SafetyStop) as error:
            with self.owner():
                self.fail('No ownership')
        self.assertEqual(error.exception.code, 'already_running')
        self.assertEqual(sum(self.sleeps), 120)
        self.assertEqual(len(self.queries), 13)
        self.assertTrue(all('pg_try_advisory_lock' in query for query in self.queries))
        self.waiting.assert_called_once()
        self.connect.assert_called_once()
        self.connection.close.assert_called_once()

    def test_db_failure_during_wait_stops_immediately_and_does_not_reconnect(self):
        self.replies([(False,), RuntimeError('private DSN and password')])
        with self.assertRaises(SafetyStop) as error:
            with self.owner():
                self.fail('No ownership')
        self.assertEqual(error.exception.code, 'database_error')
        self.assertNotIn('private', str(error.exception))
        self.assertEqual(self.sleeps, [10])
        self.connect.assert_called_once()
        self.connection.close.assert_called_once()

    def test_immediate_acquisition_never_reacquires_reentrant_session_lock(self):
        self.replies([(True,)])
        with self.owner():
            pass
        self.waiting.assert_not_called()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(sum('pg_try_advisory_lock' in query for query in self.queries), 1)

    def test_default_nonworker_call_still_fails_immediately(self):
        self.replies([(False,)])
        with self.assertRaises(SafetyStop) as error:
            with self.owner(startup_wait=0):
                self.fail('No ownership')
        self.assertEqual(error.exception.code, 'already_running')
        self.assertEqual(self.sleeps, [])

    def test_interrupt_closes_connection_and_propagates(self):
        self.replies([(False,)])
        with self.assertRaises(KeyboardInterrupt):
            with self.owner(sleep=Mock(side_effect=KeyboardInterrupt)):
                self.fail('No ownership')
        self.connection.close.assert_called_once()

    def test_invalid_lock_result_is_not_treated_as_transient_contention(self):
        for row in (None, (), (1,), ('false',)):
            with self.subTest(row=row):
                self.replies([row])
                with self.assertRaises(SafetyStop) as error:
                    with self.owner():
                        self.fail('No ownership')
                self.assertEqual(error.exception.code, 'database_error')
        self.assertEqual(self.sleeps, [])

    def test_suspension_is_still_authoritative_after_handover(self):
        self.replies([(False,), (True,)], suspended=True)
        with self.assertRaises(SafetyStop) as error:
            with self.owner():
                self.fail('Suspended')
        self.assertEqual(error.exception.code, 'suspended')
        self.assertEqual(self.sleeps, [10])

    def test_wait_budget_cannot_be_unbounded_or_invalid(self):
        for value in (True, None, -1, 121, float('inf'), float('nan'), '120', 10**400):
            with self.subTest(value=value), self.assertRaises(SafetyStop) as error:
                self.owner(startup_wait=value)
            self.assertEqual(error.exception.code, 'invalid_config')
        self.connect.assert_not_called()

    def test_lost_ownership_after_acquisition_never_enters_startup_wait_again(self):
        self.replies([(True,)])
        with self.owner() as owner:
            self.connection.closed = True
            with self.assertRaises(SafetyStop) as error:
                owner.check()
            self.assertEqual(error.exception.code, 'safety_unavailable')
        self.assertEqual(sum('pg_try_advisory_lock' in query for query in self.queries), 1)
        self.assertEqual(self.sleeps, [])
        self.waiting.assert_not_called()
        self.connect.assert_called_once()

    def test_main_waits_before_creating_worker_and_reports_private_local_stage(self):
        self.replies([(False,), (True,)])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'runtime.json'
            def wait(seconds):
                worker.assert_not_called()
                self.assertEqual(monitor_runtime.read_report()['stage'], 'waiting_lock')
                with patch.object(dashboard, 'banned_accounts_info', return_value=[]):
                    view = dashboard.dashboard_view({})
                self.assertIsNone(dashboard.stop_code(view))
                self.assertIn('منتظر آزادشدن قفل', dashboard.status_section(view))
                self.assertEqual(dashboard.next_check_text(view)[0], 'زمانی ثبت نشده')
                self.sleep(seconds)
            def make_owner(user, **kwargs):
                self.assertEqual(kwargs['startup_wait'], 120)
                return SharedMonitorLock(user, self.connect, clock=lambda: self.now, sleep=wait, **kwargs)
            with patch.dict(os.environ, {'MONITOR_RUNTIME_FILE': str(path)}), \
                 patch.object(claimer, 'initialize_data'), \
                 patch.object(claimer, 'SharedMonitorLock', side_effect=make_owner), \
                 patch.object(claimer, 'ReadOnlyMonitor') as worker, \
                 patch('requests.sessions.Session.request', side_effect=AssertionError('No web request')):
                claimer.main()
                worker.assert_called_once()
                worker.return_value.run.assert_called_once()
        self.assertEqual(self.sleeps, [10])


if __name__ == '__main__':
    unittest.main()
