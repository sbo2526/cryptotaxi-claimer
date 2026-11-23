from datetime import datetime, timedelta
import os
import unittest
from unittest.mock import patch

import requests

import dashboard
import runtime_config


NOW = datetime(2026, 11, 23, 11, 40, 5, tzinfo=dashboard.TZ)
PRIMARY = 'main-account'


def snapshot(when=NOW, **changes):
    sample = {'last_result': 'OK', 'last_run': when.strftime('%Y-%m-%d %H:%M:%S'),
              'wallet': {'usdt': 12.5}, 'cars': []}
    sample.update(changes)
    return {PRIMARY: sample}


class DashboardStateTests(unittest.TestCase):
    def setUp(self):
        allowed = patch.object(runtime_config, 'ALLOWED_PRIMARY_ACCOUNT', PRIMARY)
        allowed.start()
        self.addCleanup(allowed.stop)
        env = patch.dict(os.environ, {'PRIMARY_ACCOUNT': PRIMARY}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        archive = patch.object(dashboard, 'banned_accounts_info', return_value=[])
        archive.start()
        self.addCleanup(archive.stop)

    def enable(self):
        os.environ.update({'SITE_MONITORING_ENABLED': 'true',
                           'PRIMARY_ACCOUNT_PASSWORD': 'not-shown-site-secret',
                           'MONITOR_LOCK_DATABASE_URL': 'not-shown-database-url'})

    def test_empty_dashboard_has_no_fake_zero_metrics_or_creator_panel(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            page = dashboard.render()
        self.assertIn('اطلاعات ذخیره‌شده‌ای از اکانت در دسترس نیست.', page)
        self.assertIn('پایش خارجی خاموش است؛ هیچ عملیات خودکاری انجام نمی‌شود.', page)
        self.assertIn('پایش خاموش', page)
        self.assertNotIn('class="metrics"', page)
        self.assertNotIn('0.0000', page)
        self.assertNotIn('accountCreatorPanel', page)
        for obsolete in ('尚无', 'اولویت رنت فعال', 'dashboard زنده', 'سفرهای کلیم‌شده امروز', 'در حال استارت مجدد'):
            self.assertNotIn(obsolete, page)

    def test_missing_primary_is_distinct_from_zero_accounts(self):
        with patch.dict(os.environ, {}, clear=True):
            view = dashboard.dashboard_view({}, NOW)
            section = dashboard.status_section(view) + dashboard.technical_section(view)
        self.assertIn('تنظیم نشده', section)
        self.assertIn('PRIMARY_ACCOUNT', section)
        self.assertNotIn('تعداد اکانت‌ها', section)
        self.assertIsNone(view['sample'])

    def test_enabled_flag_is_not_reported_as_a_running_monitor(self):
        self.enable()
        view = dashboard.dashboard_view({}, NOW)
        self.assertEqual(view['label'], 'دریافت موفق ثبت نشده است')
        self.assertNotIn('پایش خارجی خاموش است', view['empty_note'])
        self.assertIsNone(view['received'])

    def test_enabled_but_incomplete_configuration_is_reported(self):
        os.environ['SITE_MONITORING_ENABLED'] = 'true'
        view = dashboard.dashboard_view({}, NOW)
        self.assertEqual(view['label'], 'پیکربندی پایش ناقص')
        self.assertIsNone(view['sample'])

    def test_valid_fresh_snapshot_shows_real_balance_and_cars(self):
        self.enable()
        state = snapshot(cars=[{'status': 'on_trip', 'name': 'Hackney', 'ends_at': 1800000000000},
                               {'status': 'idle', 'name': 'Other'}])
        view = dashboard.dashboard_view(state, NOW)
        panel = dashboard.account_data_section(view)
        self.assertEqual(view['label'], 'آخرین دریافت تازه است')
        self.assertIn('12.5000', panel)
        self.assertIn('Hackney', panel)
        self.assertIn('آمادهٔ حرکت', panel)
        self.assertNotIn('خطای امروز', panel)
        self.assertNotIn('کلیم', panel)

    def test_car_quota_and_extra_trips_are_separate_not_a_total(self):
        car = {'status': 'idle', 'name': 'Primary taxi', 'used': 10, 'limit': 10, 'bonus': 50}
        panel = dashboard.car_card(car)
        self.assertIn('quota-count">10 از 10</span>', panel)
        self.assertIn('car-bonus-count">50</strong>', panel)
        self.assertIn('آمادهٔ حرکت', panel)
        self.assertNotIn('60', panel)
        self.assertNotIn('تمام شده', panel)

    def test_account_extra_trips_are_not_copied_to_every_car(self):
        view = dashboard.dashboard_view(snapshot(bonus_trips=50, cars=[
            {'status': 'idle', 'used': 10, 'limit': 10, 'bonus': 2},
            {'status': 'unavailable', 'used': 0, 'limit': 5},
        ]), NOW)
        panel = dashboard.account_data_section(view)
        self.assertIn('account-bonus-count">50</strong>', panel)
        self.assertIn('car-bonus-count">2</strong>', panel)
        self.assertIn('car-bonus-count">نامشخص</strong>', panel)
        self.assertNotIn('car-bonus-count">50</strong>', panel)
        self.assertNotIn('>52<', panel)
        self.assertNotIn('>60<', panel)

    def test_unknown_trip_counts_are_not_fabricated_zero_or_fifty(self):
        for invalid in (None, -1, True, False, '50', '<script>bad</script>',
                        [], {}, 0.5, float('nan'), float('inf'), 10**400):
            with self.subTest(value=repr(invalid)[:60]):
                self.assertEqual(dashboard.trip_count_text(invalid), 'نامشخص')
        for valid in (0, 10, 50, 50.0):
            self.assertEqual(dashboard.trip_count_text(valid), str(int(valid)))
        panel = dashboard.car_card({'status': 'idle'})
        self.assertIn('car-bonus-count">نامشخص</strong>', panel)
        self.assertNotIn('>50<', panel)
        self.assertNotIn('>0<', panel)
        view = dashboard.dashboard_view(snapshot(), NOW)
        self.assertIn('account-bonus-count">نامشخص</strong>', dashboard.account_data_section(view))

    def test_zero_extras_are_displayed_without_overriding_reported_availability(self):
        panel = dashboard.car_card({'status': 'idle', 'used': 10, 'limit': 10, 'bonus': 0})
        self.assertIn('car-bonus-count">0</strong>', panel)
        self.assertIn('آمادهٔ حرکت', panel)
        blocked = dashboard.car_card({'status': 'unavailable', 'bonus': 50, 'block_msg': 'Not your turn'})
        self.assertIn('در دسترس نیست', blocked)
        self.assertIn('Not your turn', blocked)
        self.assertNotIn('آمادهٔ حرکت', blocked)

    def test_old_snapshot_remains_honestly_labeled_as_historical(self):
        self.enable()
        view = dashboard.dashboard_view(snapshot(NOW - timedelta(hours=1)), NOW)
        self.assertEqual(view['label'], 'اطلاعات قدیمی')
        self.assertIn('وضعیت فعلی سایت را تأیید نمی‌کند', dashboard.account_data_section(view))

    def test_disabled_monitor_can_show_stored_data_without_claiming_live_access(self):
        view = dashboard.dashboard_view(snapshot(), NOW)
        self.assertEqual(view['label'], 'پایش خاموش')
        self.assertIn('اطلاعات ذخیره‌شده نمایش داده می‌شود', dashboard.account_data_section(view))

    def test_last_success_does_not_change_when_page_is_refreshed(self):
        self.enable()
        first = dashboard.dashboard_view(snapshot(), NOW)
        later = dashboard.dashboard_view(snapshot(), NOW + timedelta(hours=1))
        self.assertEqual(first['received'], later['received'])
        self.assertIn('2 آذر 1405، 11:40:05', dashboard.status_section(first))
        self.assertIn('2 آذر 1405، 11:40:05', dashboard.status_section(later))
        self.assertNotIn('12:40:05', dashboard.status_section(later))

    def test_invalid_or_future_sample_does_not_turn_into_zero_metrics(self):
        variants = [snapshot(last_run='bad'), snapshot(last_run=None), snapshot(last_result='FAILED'),
                    snapshot(NOW + timedelta(hours=1)), snapshot(cars='bad'), snapshot(cars=[None]),
                    snapshot(cars=[{'status': []}]), {PRIMARY: []}, []]
        for state in variants:
            with self.subTest(state=state):
                view = dashboard.dashboard_view(state, NOW)
                self.assertIsNone(view['sample'])
                self.assertIsNone(view['received'])
                self.assertIn('id="emptyState"', dashboard.account_data_section(view))

    def test_unknown_balance_is_dash_but_real_zero_is_preserved(self):
        for value in (None, 'bad', True, float('nan'), float('inf'), -1):
            view = dashboard.dashboard_view(snapshot(wallet={'usdt': value}), NOW)
            panel = dashboard.account_data_section(view)
            self.assertIn('dir="ltr">—', panel)
            self.assertNotIn('0.0000', panel)
        view = dashboard.dashboard_view(snapshot(wallet={'usdt': 0}), NOW)
        self.assertIn('0.0000', dashboard.account_data_section(view))

    def test_other_account_data_never_supplies_last_success_or_balance(self):
        state = {'other': snapshot()[PRIMARY]}
        view = dashboard.dashboard_view(state, NOW)
        self.assertIsNone(view['sample'])
        self.assertIsNone(view['received'])

    def test_suspension_overrides_saved_successful_data(self):
        self.enable()
        with patch.object(dashboard, 'banned_accounts_info', return_value=[{'username': PRIMARY, 'confirmed': True}]):
            view = dashboard.dashboard_view(snapshot(), NOW)
        self.assertEqual(view['label'], 'متوقف: تعلیق ثبت‌شده')
        self.assertIsNone(view['sample'])
        self.assertIn('پایش این اکانت متوقف است', view['empty_note'])

    def test_rendering_does_not_make_any_external_request(self):
        with patch.object(requests.sessions.Session, 'request') as network:
            dashboard.render()
            network.assert_not_called()

    def test_primary_name_and_car_names_are_escaped(self):
        with patch.dict(os.environ, {'PRIMARY_ACCOUNT': '<script>identity</script>'}):
            view = dashboard.dashboard_view({}, NOW)
            self.assertNotIn('<script>identity</script>', dashboard.status_section(view))
        panel = dashboard.car_card({'name': '<script>taxi</script>', 'status': 'unavailable', 'block_msg': '<img src=x>'})
        self.assertNotIn('<script>taxi</script>', panel)
        self.assertNotIn('<img src=x>', panel)

    def test_history_follows_data_and_has_no_redundant_creator_section(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            page = dashboard.render()
        self.assertLess(page.index('class="overview"'), page.index('id="emptyState"'))
        self.assertLess(page.index('id="emptyState"'), page.index('id="bannedAccountsPanel"'))
        self.assertLess(page.index('id="bannedAccountsPanel"'), page.index('id="eventsPanel"'))

    def test_events_are_based_on_persisted_evidence_not_render_time(self):
        with patch.object(dashboard, 'dashboard_view', return_value=dashboard.dashboard_view(snapshot(), NOW)):
            lines = dashboard.event_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn('2 آذر 1405، 11:40:05', lines[0])
        self.assertIn('آخرین دریافت موفق', lines[0])

    def test_events_are_bounded_redacted_and_escaped(self):
        records = [{'username': f'<script>bad</script> secret-value {index}',
                    'detected_at': '2026-11-23 11:40:05', 'confirmed': True}
                   for index in range(40)]
        with patch.object(dashboard, 'dashboard_view', return_value={'received': None}), \
             patch.object(dashboard, 'banned_accounts_info', return_value=records), \
             patch.dict(os.environ, {'DASHBOARD_PASSWORD': 'secret-value'}):
            lines = dashboard.event_lines()
            html = dashboard.events_section()
        self.assertEqual(len(lines), 30)
        self.assertNotIn('secret-value', html)
        self.assertNotIn('<script>bad</script>', html)
        self.assertIn('[REDACTED]', html)
        self.assertIn('&lt;script&gt;', html)


if __name__ == '__main__':
    unittest.main()
