import os
from pathlib import Path
import unittest
from unittest.mock import patch

import claimer
import dashboard
import supervisor


class SafeDefaultsTests(unittest.TestCase):
    def test_default_supervisor_runs_only_dashboard(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(set(supervisor.service_commands()), {'dashboard'})
            with patch.object(claimer, 'SharedMonitorLock') as lock, patch.object(claimer, 'initialize_data'):
                claimer.main()
                lock.assert_not_called()

    def test_creator_implementations_removed(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / 'account_creator.py').exists())
        self.assertFalse((root / 'referral_faker.py').exists())
        self.assertFalse((root / 'mail_reader.py').exists())

    def test_creator_panel_cannot_be_reenabled(self):
        panel = dashboard.creator_status_section({'enabled': True, 'current': 1})
        self.assertIn('⛔ غیرفعال', panel)
        self.assertNotIn('creatorCountdown', panel)

    def test_only_primary_identity_can_appear(self):
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT': 'main'}, clear=True), \
             patch.object(dashboard, 'banned_accounts_info', return_value=[]):
            self.assertEqual(dashboard.active_accounts_info(), [{'username': 'main'}])
            self.assertEqual(dashboard.creator_status_info()['current'], 1)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dashboard.active_accounts_info(), [])


if __name__ == '__main__':
    unittest.main()
