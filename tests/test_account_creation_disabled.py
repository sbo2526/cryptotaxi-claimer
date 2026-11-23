import os
from pathlib import Path
import unittest
from unittest.mock import patch

import claimer
import dashboard
import supervisor
import runtime_config


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

    def test_creator_panel_is_not_part_of_read_only_dashboard(self):
        self.assertNotIn('accountCreatorPanel', dashboard.render())
        self.assertFalse(hasattr(dashboard, 'creator_status_section'))

    def test_only_primary_identity_can_appear(self):
        with patch.object(runtime_config, 'ALLOWED_PRIMARY_ACCOUNT', 'main'), \
             patch.dict(os.environ, {'PRIMARY_ACCOUNT': 'main'}, clear=True), \
             patch.object(dashboard, 'banned_accounts_info', return_value=[]):
            self.assertEqual(dashboard.active_accounts_info(), [{'username': 'main'}])
            self.assertEqual(len(dashboard.active_accounts_info()), 1)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dashboard.active_accounts_info(), [])


if __name__ == '__main__':
    unittest.main()
