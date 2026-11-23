import unittest
from unittest.mock import patch

import dashboard
import supervisor


class AccountCreationDisabledTests(unittest.TestCase):
    def test_supervisor_never_launches_or_restarts_creation_workers(self):
        manager = supervisor.Supervisor()
        for key in ("account_creator", "referral_faker"):
            service = manager.services[key]
            with self.subTest(service=key), patch('supervisor.subprocess.Popen') as launch:
                service.start()
                manager._check_service(service)
                launch.assert_not_called()
                self.assertFalse(service.status()['enabled'])
                self.assertFalse(service.status()['auto_restart'])
        for key in ('claimer', 'dashboard'):
            self.assertTrue(manager.services[key].config.get('enabled', True))

    def test_stale_status_does_not_show_creator_as_active(self):
        def load(path, default):
            if path == dashboard.ACCOUNTS_FILE:
                return [{}, {}]
            return {'status': 'running', 'next_cycle_at': 9999999999}

        with patch.object(dashboard, 'load_json', side_effect=load):
            info = dashboard.creator_status_info()
            self.assertFalse(info['running'])
            self.assertEqual(info['current'], 2)
            self.assertIsNone(info['next_cycle_ts'])
            self.assertIn('غیرفعال', dashboard.creator_status_section(info))
            self.assertIn('غیرفعال', dashboard.referral_faker_status_section())

    def test_standalone_workers_exit_without_network_requests(self):
        import account_creator
        import referral_faker
        with patch('requests.sessions.Session.request') as request:
            account_creator.main()
            referral_faker.main()
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
