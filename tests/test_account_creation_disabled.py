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
                return [{"username": "first"}, {"username": "second"}]
            return {'status': 'running', 'next_cycle_at': 9999999999}

        with patch.object(dashboard, 'load_json', side_effect=load):
            info = dashboard.creator_status_info()
            self.assertFalse(info['running'])
            self.assertEqual(info['current'], 2)
            self.assertIsNone(info['next_cycle_ts'])
            self.assertIn('غیرفعال', dashboard.creator_status_section(info))
            self.assertIn('غیرفعال', dashboard.referral_faker_status_section())

    def test_disabled_panel_only_shows_existing_account_count(self):
        panel = dashboard.creator_status_section({
            'enabled': False, 'current': 54, 'target': 100,
            'running': True, 'next_cycle_ts': 9999999999,
            'last_action': '尚无',
        })
        self.assertIn('⛔ غیرفعال', panel)
        self.assertIn('تعداد اکانت‌های موجود', panel)
        self.assertIn('<b>54</b>', panel)
        for obsolete in ('尚无', 'creatorCountdown', 'creatorFill',
                         'هدف نهایی', 'باقی مانده', 'خطاها',
                         'interval', 'زمان راه‌اندازی', 'اطلاعات چرخه',
                         '🚀 شروع شد', 'آخرین اکانت ساخته‌شده'):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, panel)

    def test_disabled_panel_handles_empty_accounts(self):
        panel = dashboard.creator_status_section({'enabled': False, 'current': 0})
        self.assertIn('<b>0</b>', panel)
        self.assertNotIn('creatorCountdown', panel)

    def test_enabled_panel_retains_cycle_details(self):
        panel = dashboard.creator_status_section({'running': True, 'current': 54})
        self.assertIn('creatorCountdown', panel)
        self.assertIn('در حال اجرا', panel)

    def test_standalone_workers_exit_without_network_requests(self):
        import account_creator
        import referral_faker
        with patch('requests.sessions.Session.request') as request:
            account_creator.main()
            referral_faker.main()
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
