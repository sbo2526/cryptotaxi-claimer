from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import claimer
import dashboard


class BannedCleanupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in (
            ('ACCOUNTS_FILE', []), ('STATUS_FILE', {'_meta': {'date': '2026-11-23'}}),
            ('BANNED_ACCOUNTS_FILE', {'banned': []}),
        ):
            path = str(self.root / (name + '.json'))
            self.write(path, value)
            for module in (claimer, dashboard):
                patcher = patch.object(module, name, path)
                patcher.start()
                self.addCleanup(patcher.stop)
        for name in ('ACCOUNTS', 'SESSIONS', 'CONSECUTIVE_FAILURES', 'LOGIN_FAILURE_COUNTS'):
            patcher = patch.object(claimer, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, value):
        Path(path).write_text(json.dumps(value))

    def accounts(self, *users):
        self.write(claimer.ACCOUNTS_FILE, [
            {'username': user, 'password': 'secret', 'email_password': 'mail-secret'}
            for user in users
        ])

    def test_mark_banned_deletes_credentials_live_state_and_session(self):
        self.accounts('bad', 'good')
        self.write(claimer.STATUS_FILE, {'bad': {'wallet': {}}, 'good': {}, '_meta': {}})
        claimer.ACCOUNTS['bad'] = {'password': 'secret'}
        claimer.SESSIONS['bad'] = object()
        claimer.mark_banned('bad', 'Your account is suspended. Reason: Multi Account')
        self.assertEqual([a['username'] for a in claimer.load_accounts()], ['good'])
        self.assertNotIn('bad', claimer.load_status())
        self.assertNotIn('bad', claimer.ACCOUNTS)
        self.assertNotIn('bad', claimer.SESSIONS)
        archive = claimer.load_banned_accounts()['banned']
        self.assertEqual(archive[0]['username'], 'bad')
        self.assertNotIn('password', archive[0])
        self.assertEqual(len(dashboard.active_accounts_info()), 1)
        self.assertEqual(dashboard.creator_status_info()['current'], 1)

    def test_old_archive_and_status_bans_are_purged(self):
        self.accounts('archived', 'flagged', 'good')
        self.write(claimer.BANNED_ACCOUNTS_FILE, {'banned': [{'username': 'archived', 'confirmed': True}]})
        self.write(claimer.STATUS_FILE, {
            'archived': {}, 'flagged': {'banned': True, 'ban_reason': 'Your account is suspended'},
            'good': {}, '_meta': {},
        })
        claimer.purge_banned_accounts()
        self.assertEqual([a['username'] for a in claimer.load_accounts()], ['good'])
        self.assertEqual(set(claimer.load_status()), {'good', '_meta'})
        self.assertEqual(claimer.banned_usernames(claimer.load_banned_accounts()), {'archived', 'flagged'})

    def test_empty_list_after_last_ban_is_supported(self):
        self.accounts('last')
        claimer.mark_banned('last', 'Your account is suspended')
        self.assertEqual(claimer.load_accounts(), [])
        with patch.object(claimer, 'process_account') as process, \
             patch.object(claimer, 'update_earnings'), patch.object(claimer, 'write_daily_report'):
            claimer.run_sweep(2)
            process.assert_not_called()
        self.assertEqual(dashboard.creator_status_info()['current'], 0)

    def test_parallel_bans_do_not_lose_removals_or_archive_records(self):
        users = [f'bad-{i}' for i in range(8)]
        self.accounts(*users, 'good')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda user: claimer.mark_banned(user, 'Your account is suspended'), users))
        self.assertEqual([a['username'] for a in claimer.load_accounts()], ['good'])
        self.assertEqual(claimer.banned_usernames(claimer.load_banned_accounts()), set(users))
        claimer.mark_banned(users[0], 'Your account is suspended')
        self.assertEqual(len(claimer.load_banned_accounts()['banned']), 8)

    def test_stale_sweep_cannot_restore_deleted_account(self):
        self.accounts('bad', 'good')
        def process(account, status):
            user = account['username']
            if user == 'bad':
                claimer.mark_banned(user, 'Your account is suspended')
            status[user] = {'last_result': 'FAILED' if user == 'bad' else 'OK'}
        with patch.object(claimer, 'process_account', side_effect=process), \
             patch.object(claimer, 'update_earnings'), patch.object(claimer, 'write_daily_report'):
            claimer.run_sweep(2)
        self.assertNotIn('bad', claimer.load_status())
        stale = {'bad': {'last_result': 'OK'}, 'good': {}, '_meta': {}}
        claimer.save_status(stale)
        self.assertNotIn('bad', claimer.load_status())

    def test_dashboard_cards_and_count_exclude_archived_flagged_and_deleted_users(self):
        self.accounts('archived', 'flagged', 'good')
        self.write(claimer.BANNED_ACCOUNTS_FILE, {'banned': [{'username': 'archived', 'confirmed': True}]})
        self.write(claimer.STATUS_FILE, {
            'archived': {}, 'flagged': {'banned': True, 'ban_reason': 'Your account is suspended'}, 'deleted': {}, 'good': {},
        })
        self.assertEqual([a['username'] for a in dashboard.active_accounts_info()], ['good'])
        page = dashboard.render()
        self.assertEqual(page.count('class="acc"'), 1)
        self.assertNotIn('deleted', page)
        self.assertEqual(dashboard.creator_status_info()['current'], 1)
        handler = object.__new__(dashboard.Handler)
        handler.path, handler.headers = '/count', {}
        with patch.object(handler, 'send_json') as response:
            handler.do_GET()
            response.assert_called_once_with({'ok': True, 'count': 1})

    def test_legacy_403_records_no_longer_delete_or_hide_active_account(self):
        self.accounts('main')
        self.write(claimer.BANNED_ACCOUNTS_FILE, {'banned': [{
            'username': 'main', 'reason': 'HTTP 403 Forbidden on login',
        }]})
        snapshot = {'main': {'banned': True, 'last_result': 'BAN',
                             'ban_reason': 'HTTP 403 Forbidden on login'}}
        self.write(claimer.STATUS_FILE, snapshot)
        claimer.purge_banned_accounts(snapshot)
        claimer.save_status(snapshot)
        self.assertEqual([a['username'] for a in claimer.load_accounts()], ['main'])
        self.assertIn('main', claimer.load_status())
        self.assertEqual(dashboard.creator_status_info()['current'], 1)
        self.assertFalse(dashboard.banned_accounts_info()[0]['confirmed'])
        self.assertIn('تأییدنشده', dashboard.banned_accounts_section())

    def test_generic_mark_request_is_refused_and_explicit_evidence_upgrades_legacy(self):
        self.accounts('main')
        self.write(claimer.BANNED_ACCOUNTS_FILE, {'banned': [{
            'username': 'main', 'reason': 'HTTP 403 Forbidden on login',
        }]})
        claimer.mark_banned('main', 'HTTP 403 Forbidden on login')
        self.assertEqual(len(claimer.load_accounts()), 1)
        reason = 'Your account is suspended. Reason: Multi Account'
        claimer.mark_banned('main', reason)
        self.assertEqual(claimer.load_accounts(), [])
        latest = claimer.load_banned_accounts()['banned'][-1]
        self.assertTrue(latest['confirmed'])
        self.assertEqual(latest['reason'], reason)
        self.assertTrue(dashboard.banned_accounts_info()[0]['confirmed'])

    def test_corrupt_archive_does_not_delete_credentials(self):
        self.accounts('good')
        Path(claimer.BANNED_ACCOUNTS_FILE).write_text('{invalid')
        with self.assertRaises(json.JSONDecodeError):
            claimer.purge_banned_accounts()
        self.assertEqual([a['username'] for a in claimer.load_accounts()], ['good'])


if __name__ == '__main__':
    unittest.main()
