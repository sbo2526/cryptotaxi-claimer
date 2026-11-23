import os
import unittest
from unittest.mock import Mock, patch

import claimer
import dashboard
from monitor_safety import SafetyStop, check_policy
import runtime_config


SELECTED_ACCOUNT = 'bavafasobhan@gmail.com'


class SelectedPrimaryAccountTests(unittest.TestCase):
    """Exercise the actual owner selection, not the fake identities in fixtures."""

    def test_only_the_owner_selected_identity_is_allowed(self):
        self.assertEqual(runtime_config.ALLOWED_PRIMARY_ACCOUNT, SELECTED_ACCOUNT)
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT': SELECTED_ACCOUNT}, clear=True):
            self.assertEqual(runtime_config.primary_username(), SELECTED_ACCOUNT)
            with patch.object(dashboard, 'banned_accounts_info', return_value=[]):
                self.assertEqual(dashboard.active_accounts_info(), [{'username': SELECTED_ACCOUNT}])

    def test_old_other_and_missing_primary_values_cannot_select_an_account(self):
        for configured in ('', 'other-account', 'other@example.test', 'BAVAFASOBHAN@gmail.com'):
            with self.subTest(configured=configured), patch.dict(os.environ, {'PRIMARY_ACCOUNT': configured}, clear=True):
                self.assertEqual(runtime_config.primary_username(), '')
                self.assertEqual(dashboard.active_accounts_info(), [])

    def test_surrounding_whitespace_does_not_change_the_selected_identity(self):
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT': '  ' + SELECTED_ACCOUNT + '\n'}, clear=True):
            self.assertEqual(runtime_config.primary_username(), SELECTED_ACCOUNT)

    def test_environment_cannot_override_owner_selection_or_send_other_login(self):
        env = {'PRIMARY_ACCOUNT': 'other@example.test', 'SITE_MONITORING_ENABLED': 'true',
               'PRIMARY_ACCOUNT_PASSWORD': 'test-only-not-a-real-password'}
        with patch.dict(os.environ, env, clear=True):
            for user in (env['PRIMARY_ACCOUNT'], SELECTED_ACCOUNT):
                with self.subTest(user=user), self.assertRaises(SafetyStop):
                    check_policy(user, 'auth/login')
                session = Mock()
                client = claimer.ReadOnlyMonitor(user, Mock(), session=session, pacer=Mock())
                with self.assertRaises(SafetyStop):
                    client.login()
                session.post.assert_not_called()

    def test_incorrect_primary_configuration_stops_main_before_database_or_site(self):
        env = {'PRIMARY_ACCOUNT': 'other@example.test', 'SITE_MONITORING_ENABLED': 'true',
               'PRIMARY_ACCOUNT_PASSWORD': 'test-only-not-a-real-password'}
        with patch.dict(os.environ, env, clear=True), patch.object(claimer, 'initialize_data'), \
             patch.object(claimer, 'SharedMonitorLock') as database, \
             patch.object(claimer, 'ReadOnlyMonitor') as monitor, \
             self.assertLogs('monitor', level='ERROR'):
            claimer.main()
        database.assert_not_called()
        monitor.assert_not_called()

    def test_old_status_data_never_becomes_an_active_secondary_account(self):
        status = {'other@example.test': {'last_result': 'OK', 'last_run': '2026-11-23 10:00:00',
                                         'cars': [], 'wallet': {'usdt': 100}}}
        for configured in (SELECTED_ACCOUNT, 'other@example.test', ''):
            with self.subTest(configured=configured), patch.dict(os.environ, {'PRIMARY_ACCOUNT': configured}, clear=True), \
                 patch.object(dashboard, 'banned_accounts_info', return_value=[]):
                view = dashboard.dashboard_view(status)
                self.assertIsNone(view['sample'])
                self.assertIsNone(view['received'])
                panel = dashboard.status_section(view) + dashboard.technical_section(view)
                self.assertIn(SELECTED_ACCOUNT, panel)
                self.assertNotIn('other@example.test', panel)

    def test_owner_selection_does_not_enable_external_monitoring(self):
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT': SELECTED_ACCOUNT}, clear=True):
            self.assertFalse(runtime_config.monitoring_enabled())
            with self.assertRaises(SafetyStop):
                check_policy(SELECTED_ACCOUNT, 'state')

    def test_explicit_monitor_opt_in_only_permits_selected_read_only_endpoints(self):
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT': SELECTED_ACCOUNT, 'SITE_MONITORING_ENABLED': 'true'}, clear=True):
            for endpoint in ('auth/login', 'state'):
                check_policy(SELECTED_ACCOUNT, endpoint)
            for endpoint in ('trips/start', 'auth/register', 'market/rent'):
                with self.subTest(endpoint=endpoint), self.assertRaises(SafetyStop):
                    check_policy(SELECTED_ACCOUNT, endpoint)

    def test_selected_identity_does_not_bypass_a_confirmed_suspension(self):
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT': SELECTED_ACCOUNT}, clear=True), \
             patch.object(dashboard, 'banned_accounts_info', return_value=[
                 {'username': SELECTED_ACCOUNT, 'confirmed': True}]):
            self.assertEqual(dashboard.active_accounts_info(), [])
            self.assertTrue(dashboard.dashboard_view({})['suspended'])


if __name__ == '__main__':
    unittest.main()
