from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import claimer
import dashboard
import monitor_runtime
from monitor_safety import SafetyStop, SharedMonitorLock
import runtime_config
import supervisor
import requests


PRIMARY = runtime_config.ALLOWED_PRIMARY_ACCOUNT
ENV = {'PRIMARY_ACCOUNT': PRIMARY, 'PRIMARY_ACCOUNT_PASSWORD': 'test-only-site-password',
       'SITE_MONITORING_ENABLED': 'true', 'MONITOR_LOCK_DATABASE_URL': 'postgresql://test-only-secret'}


class RuntimeReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'report.json'
        env = patch.dict(os.environ, dict(ENV, MONITOR_RUNTIME_FILE=str(self.path)), clear=True)
        env.start()
        self.addCleanup(env.stop)
        for module in (claimer, dashboard):
            for name in ('STATUS_FILE', 'BANNED_ACCOUNTS_FILE'):
                setting = patch.object(module, name, str(self.root / (name + '.json')))
                setting.start()
                self.addCleanup(setting.stop)

    def test_private_atomic_report_contains_only_whitelisted_fields(self):
        monitor_runtime.write_report('stopped', code='database_error')
        raw = self.path.read_text()
        data = json.loads(raw)
        self.assertEqual(set(data), {'user', 'stage', 'updated_at', 'code'})
        for value in ENV.values():
            if value != PRIMARY and value != 'true':
                self.assertNotIn(value, raw)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(monitor_runtime.read_report()['code'], 'database_error')
        self.assertEqual(list(self.root.glob('*.tmp')), [])
        monitor_runtime.write_report('stopped', code='postgresql://private-driver-text')
        self.assertNotIn('private-driver-text', self.path.read_text())
        self.assertEqual(monitor_runtime.read_report()['code'], 'manual_review')

    def test_safe_request_and_state_failure_codes_survive_runtime_and_presentation(self):
        for code in ('request_failed', 'state_invalid'):
            with self.subTest(code=code):
                monitor_runtime.write_report('stopped', code=code)
                self.assertEqual(monitor_runtime.read_report()['code'], code)
                view = dashboard.dashboard_view({})
                self.assertEqual(dashboard.stop_code(view), code)
                self.assertIn(code, dashboard.technical_section(view))
                self.assertIn(dashboard.STOP_MESSAGES[code], view['note'])

    def test_missing_corrupt_foreign_future_or_invalid_reports_are_ignored(self):
        self.assertIsNone(monitor_runtime.read_report())
        self.path.write_text('{bad')
        self.assertIsNone(monitor_runtime.read_report())
        now = datetime.now(monitor_runtime.TZ)
        report = {'user': PRIMARY, 'stage': 'starting', 'updated_at': now.strftime('%Y-%m-%d %H:%M:%S')}
        for value in ([], dict(report, user='someone-else'), dict(report, stage=[]),
                      dict(report, updated_at='bad'), dict(report, updated_at=None),
                      dict(report, updated_at=(now + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'))):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                self.assertIsNone(monitor_runtime.read_report())

    def test_no_supervisor_path_is_explicitly_unknown_not_fabricated(self):
        with patch.dict(os.environ, {'MONITOR_RUNTIME_FILE': ''}):
            self.assertFalse(monitor_runtime.write_report('starting'))
            self.assertIsNone(monitor_runtime.read_report())

    def test_report_write_failure_is_diagnostic_only(self):
        with self.assertLogs('monitor', level='ERROR'):
            self.assertFalse(monitor_runtime.write_report('starting', path=self.root / 'missing' / 'report.json'))
        self.assertIsNone(monitor_runtime.read_report())

    def test_exit_preserves_a_specific_failure_but_catches_unreported_death(self):
        monitor_runtime.write_report('stopped', code='already_running')
        original = self.path.read_bytes()
        monitor_runtime.record_exit()
        self.assertEqual(self.path.read_bytes(), original)
        monitor_runtime.write_report('waiting_first_read')
        monitor_runtime.record_exit()
        self.assertEqual(monitor_runtime.read_report()['code'], 'worker_exited')

    def test_prelock_failures_display_a_reason_without_account_state_writes_or_login(self):
        original = {'_meta': {}, PRIMARY: {'last_result': 'OK', 'cars': [], 'wallet': {'usdt': 3},
                                         'last_run': datetime.now(claimer.TZ).strftime('%Y-%m-%d %H:%M:%S')}}
        for code in ('database_error', 'already_running', 'safety_unavailable', 'suspended'):
            Path(claimer.STATUS_FILE).write_text(json.dumps(original))
            before = Path(claimer.STATUS_FILE).read_bytes()
            with self.subTest(code=code), patch.object(claimer, 'initialize_data'), \
                 patch.object(claimer, 'SharedMonitorLock', side_effect=SafetyStop('safe message', code)), \
                 patch.object(claimer, 'ReadOnlyMonitor') as worker, self.assertLogs('monitor', level='WARNING'):
                claimer.main()
            worker.assert_not_called()
            self.assertEqual(Path(claimer.STATUS_FILE).read_bytes(), before)
            report = monitor_runtime.read_report()
            self.assertEqual(report['code'], code)
            view = dashboard.dashboard_view(original)
            if code == 'suspended':
                self.assertEqual(view['label'], 'متوقف: تعلیق ثبت‌شده')
                self.assertIsNone(view['sample'])
                self.assertEqual(dashboard.active_accounts_info(), [])
            else:
                self.assertEqual(view['label'], 'پایش متوقف شده است')
                self.assertEqual(view['note'], dashboard.STOP_MESSAGES[code])
                self.assertIsNotNone(view['sample'])
            self.assertNotIn('بررسی بعدیِ برنامه‌ریزی‌شده', view['note'])

    def test_prelock_failure_with_empty_data_does_not_wait_for_first_receive(self):
        with patch.object(claimer, 'initialize_data'), \
             patch.object(claimer, 'SharedMonitorLock', side_effect=SafetyStop('safe message', 'database_error')), \
             patch.object(claimer, 'ReadOnlyMonitor') as worker, self.assertLogs('monitor', level='WARNING'):
            claimer.main()
        worker.assert_not_called()
        view = dashboard.dashboard_view({})
        self.assertEqual(view['label'], 'پایش متوقف شده است')
        self.assertIn('دیتابیس', view['empty_note'])
        self.assertIn('/app/data', view['empty_note'])
        self.assertNotIn('در انتظار اولین دریافت', dashboard.status_section(view))

    def test_missing_diagnostics_does_not_claim_worker_is_running(self):
        view = dashboard.dashboard_view({})
        self.assertEqual(view['label'], 'دریافت موفق ثبت نشده است')
        self.assertIn('وضعیت اجرای پایش تأیید نشده', view['note'])
        self.assertIn('در دادهٔ محلی ثبت نشده', dashboard.status_section(view))
        self.assertIn('اطلاعات ذخیره‌شده‌ای', dashboard.account_data_section(view))

    def test_initial_stage_becomes_unconfirmed_after_three_minutes_not_forever_waiting(self):
        for stage in dashboard.STARTUP_MESSAGES:
            monitor_runtime.write_report(stage)
            updated = monitor_runtime.read_report()['updated']
            early = dashboard.dashboard_view({}, now=updated + timedelta(seconds=90))
            late = dashboard.dashboard_view({}, now=updated + timedelta(minutes=10))
            self.assertEqual(early['label'], 'شروع پایش ثبت شده است')
            self.assertEqual(late['label'], 'شروع پایش نیازمند بررسی است')
            self.assertIn('ادامهٔ اجرا تأیید نمی‌شود', late['note'])
            self.assertEqual(monitor_runtime.read_report()['stage'], stage)

    def test_disabled_configuration_and_confirmed_bans_remain_authoritative(self):
        monitor_runtime.write_report('stopped', code='database_error')
        with patch.dict(os.environ, {'SITE_MONITORING_ENABLED': 'false'}):
            self.assertEqual(dashboard.dashboard_view({})['label'], 'پایش خاموش')
        with patch.object(dashboard, 'banned_accounts_info', return_value=[{'username': PRIMARY, 'confirmed': True}]):
            self.assertEqual(dashboard.dashboard_view({})['label'], 'متوقف: تعلیق ثبت‌شده')

    def test_replica_diagnostics_are_isolated_from_each_other_and_shared_status(self):
        other = self.root / 'other-report.json'
        monitor_runtime.write_report('waiting', path=self.path)
        monitor_runtime.write_report('stopped', code='already_running', path=other)
        self.assertEqual(monitor_runtime.read_report(path=self.path)['stage'], 'waiting')
        self.assertEqual(monitor_runtime.read_report(path=other)['code'], 'already_running')
        self.assertFalse(Path(claimer.STATUS_FILE).exists())

    def test_worker_reports_login_and_first_read_stages_without_extra_requests(self):
        session, owner, pacer = Mock(), Mock(), Mock()
        responses = []
        for payload in ({'token': 'test-only-unpersisted-login-token'}, {'myCars': []}):
            result = requests.Response()
            result.status_code = 200
            result._content = json.dumps(payload).encode()
            responses.append(result)
        session.post.side_effect = responses
        observed = []
        pacer.wait.side_effect = lambda: observed.append(monitor_runtime.read_report()['stage'])
        worker = claimer.ReadOnlyMonitor(PRIMARY, owner, session=session, pacer=pacer)
        def wait(seconds):
            self.assertEqual(monitor_runtime.read_report()['stage'], 'waiting')
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            worker.run(sleep=wait)
        self.assertEqual(observed, ['logging_in', 'waiting_first_read'])
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(monitor_runtime.read_report()['code'], 'manual_stop')
        self.assertNotIn('test-only-unpersisted-login-token', self.path.read_text())
        self.assertNotIn('test-only-unpersisted-login-token', Path(claimer.STATUS_FILE).read_text())

    def test_ownership_loss_still_records_a_safe_local_stop_without_shared_writes(self):
        owner, session = Mock(), Mock()
        owner.check.side_effect = SafetyStop('safety unavailable', 'safety_unavailable')
        worker = claimer.ReadOnlyMonitor(PRIMARY, owner, session=session, pacer=Mock())
        with self.assertLogs('monitor', level='ERROR'), self.assertRaises(SafetyStop):
            worker.run(sleep=Mock())
        self.assertEqual(monitor_runtime.read_report()['code'], 'safety_unavailable')
        self.assertFalse(Path(claimer.STATUS_FILE).exists())
        session.post.assert_not_called()

    def test_local_report_cannot_bypass_the_shared_database_gate(self):
        monitor_runtime.write_report('waiting')
        session = Mock()
        owner = Mock()
        owner.check.side_effect = SafetyStop('lost safety state', 'safety_unavailable')
        worker = claimer.ReadOnlyMonitor(PRIMARY, owner, session=session, pacer=Mock())
        with self.assertRaises(SafetyStop):
            worker.login()
        session.post.assert_not_called()


class LockFailureCodeTests(unittest.TestCase):
    def test_connection_failure_is_safe_and_has_specific_code(self):
        with patch.dict(os.environ, ENV, clear=True), self.assertRaises(SafetyStop) as result:
            with SharedMonitorLock(PRIMARY, connect=Mock(side_effect=RuntimeError('postgresql://private-driver-message'))):
                self.fail('must not obtain ownership')
        self.assertEqual(result.exception.code, 'database_error')
        self.assertNotIn('private-driver-message', str(result.exception))

    def test_occupied_lock_is_not_retried_or_classified_as_connection_failure(self):
        connection = Mock(closed=False)
        connection.execute.return_value.fetchone.return_value = (False,)
        connect = Mock(return_value=connection)
        with patch.dict(os.environ, ENV, clear=True), self.assertRaises(SafetyStop) as result:
            with SharedMonitorLock(PRIMARY, connect=connect):
                self.fail('occupied lock')
        self.assertEqual(result.exception.code, 'already_running')
        connect.assert_called_once()
        connection.execute.assert_called_once()
        connection.close.assert_called_once()

    def test_persistent_suspension_is_distinct_from_unavailable_safety_state(self):
        for row, code in (((True,), 'suspended'), (None, 'safety_unavailable')):
            connection = Mock(closed=False)
            def execute(query, params=None):
                return Mock(fetchone=lambda: (True,) if 'pg_try_advisory_lock' in query else row)
            connection.execute.side_effect = execute
            with patch.dict(os.environ, ENV, clear=True), self.assertRaises(SafetyStop) as result:
                with SharedMonitorLock(PRIMARY, connect=Mock(return_value=connection)):
                    self.fail('unsafe account')
            self.assertEqual(result.exception.code, code)
            connection.close.assert_called_once()


class SupervisorReportTests(unittest.TestCase):
    def test_child_diagnostics_path_is_shared_locally_fresh_per_run_and_exit_never_restarts_monitor(self):
        paths = []
        for specific_failure in (False, True):
            dashboard_process, monitor_process = Mock(), Mock()
            dashboard_process.poll.return_value = None
            monitor_process.poll.return_value = 0
            launches = []
            def launch(command, **kwargs):
                launches.append(kwargs)
                path = Path(kwargs['env']['MONITOR_RUNTIME_FILE'])
                if len(launches) == 2 and specific_failure:
                    monitor_runtime.write_report('stopped', code='database_error', path=path)
                return dashboard_process if len(launches) == 1 else monitor_process
            def wait(seconds):
                self.assertEqual(len(launches), 2)
                path = Path(launches[0]['env']['MONITOR_RUNTIME_FILE'])
                self.assertEqual(str(path), launches[1]['env']['MONITOR_RUNTIME_FILE'])
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                report = monitor_runtime.read_report(path=path)
                self.assertEqual(report['code'], 'database_error' if specific_failure else 'worker_exited')
                paths.append(path)
                raise KeyboardInterrupt()
            with patch.dict(os.environ, ENV, clear=True), patch.object(supervisor, 'initialize_data'), \
                 patch.object(supervisor.signal, 'signal'), patch.object(supervisor.subprocess, 'Popen', side_effect=launch) as popen, \
                 patch.object(supervisor.time, 'sleep', side_effect=wait), self.assertLogs('supervisor', level='WARNING'), \
                 self.assertRaises(KeyboardInterrupt):
                supervisor.main()
            self.assertEqual(popen.call_count, 2)
            self.assertFalse(paths[-1].exists())
            dashboard_process.terminate.assert_called_once()
        self.assertNotEqual(paths[0], paths[1])


if __name__ == '__main__':
    unittest.main()
