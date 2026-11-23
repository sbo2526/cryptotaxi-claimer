import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import dashboard


class BannedAccountsPanelTests(unittest.TestCase):
    def sources(self, archive=None, status=None):
        def load(path, default):
            if path == dashboard.BANNED_ACCOUNTS_FILE:
                return archive if archive is not None else default
            if path == dashboard.STATUS_FILE:
                return status if status is not None else default
            return default
        return patch.object(dashboard, 'load_json', side_effect=load)

    def test_removed_accounts_remain_visible_with_reason_and_time(self):
        with self.sources({'banned': [{
            'username': 'removed-account', 'reason': 'HTTP 403 Forbidden on login',
            'detected_at': '2026-11-23 10:00:00',
        }]}):
            page = dashboard.render()
        for expected in ('اکانت‌های بن‌شده', '0 اکانت بن‌شده', 'removed-account',
                         'HTTP 403 Forbidden on login', '2026-11-23 10:00:00'):
            self.assertIn(expected, page)
        self.assertNotIn('دسترسی به ایمیل‌های اکانت‌ها', page)
        self.assertNotIn('email-status', page)

    def test_deduplicates_archive_and_merges_explicit_status_flags(self):
        archive = {'banned': [
            {'username': 'duplicate', 'reason': 'older', 'detected_at': '2026-11-20 10:00:00'},
            {'username': 'duplicate', 'reason': 'latest', 'detected_at': '2026-11-22 10:00:00'},
        ]}
        status = {
            '_meta': {'last_result': 'BAN'},
            'duplicate': {'banned': True, 'ban_reason': 'status reason'},
            'status-only': {'banned': True, 'ban_reason': 'blocked', 'banned_at': '2026-11-23 10:00:00'},
            'legacy': {'last_result': 'BAN'},
            'login-error': {'last_result': 'ERROR', 'last_error': '403 login failed'},
        }
        with self.sources(archive, status):
            records = dashboard.banned_accounts_info()
        self.assertEqual([r['username'] for r in records], ['status-only', 'duplicate', 'legacy'])
        self.assertEqual(records[1]['reason'], 'latest')

    def test_untrusted_record_fields_are_escaped(self):
        payload = '<script>alert(1)</script>'
        with self.sources({'banned': [{
            'username': payload, 'reason': payload, 'detected_at': payload,
        }]}):
            panel = dashboard.banned_accounts_section()
        self.assertNotIn(payload, panel)
        self.assertEqual(panel.count('&lt;script&gt;alert(1)&lt;/script&gt;'), 3)

    def test_missing_details_use_persian_placeholders(self):
        with self.sources({'banned': [{'username': 'no-details'}]}):
            panel = dashboard.banned_accounts_section()
        self.assertIn('دلیل ثبت نشده', panel)
        self.assertIn('زمان ثبت نشده', panel)
        self.assertNotIn('尚无', panel)

    def test_malformed_record_shapes_are_ignored(self):
        for archive in ([], {'banned': None}, {'banned': 'bad'},
                        {'banned': [None, 'bad', {}, {'username': []}, {'username': ''}]}):
            with self.subTest(archive=archive), self.sources(archive, []):
                panel = dashboard.banned_accounts_section()
                self.assertIn('0 اکانت بن‌شده', panel)
                self.assertIn('هیچ اکانت بن‌شده‌ای ثبت نشده است.', panel)

    def test_missing_and_invalid_json_files_show_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'banned.json'
            status = Path(directory) / 'status.json'
            with patch.object(dashboard, 'BANNED_ACCOUNTS_FILE', str(archive)), \
                 patch.object(dashboard, 'STATUS_FILE', str(status)):
                self.assertEqual(dashboard.banned_accounts_info(), [])
                archive.write_text('{invalid')
                status.write_text('{invalid')
                self.assertEqual(dashboard.banned_accounts_info(), [])


if __name__ == '__main__':
    unittest.main()
