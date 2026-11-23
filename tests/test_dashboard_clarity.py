"""Presentation regressions: simple summaries must not overstate worker evidence."""
from datetime import datetime, timedelta
import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

import dashboard


NOW = datetime(2026, 11, 23, 12, 30, tzinfo=dashboard.TZ)
USER = dashboard.ALLOWED_PRIMARY_ACCOUNT
NODE = shutil.which('node')


class DashboardClarityTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch.dict(os.environ, {
                'PRIMARY_ACCOUNT': USER, 'SITE_MONITORING_ENABLED': 'true',
                'AUTO_START_TRIPS_ENABLED': 'true', 'PRIMARY_ACCOUNT_PASSWORD': 'fixture-only',
                'MONITOR_LOCK_DATABASE_URL': 'fixture-only',
            }, clear=True),
            patch.object(dashboard, 'read_report', return_value=None),
            patch.object(dashboard, 'banned_accounts_info', return_value=[]),
        ):
            context.start()
            self.addCleanup(context.stop)

    def view(self, cars=None, minutes_ago=0):
        sample = {'last_result': 'OK',
                  'last_run': (NOW - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S'),
                  'cars': cars or [], 'wallet': {'usdt': 1.25}, 'bonus_trips': 120}
        return dashboard.dashboard_view({USER: sample}, NOW)

    def test_stopped_summary_outranks_fresh_sample_and_enabled_auto_start(self):
        view = self.view()
        view['runtime'] = {'stage': 'stopped', 'code': 'start_unknown'}
        html = dashboard.status_section(view)
        self.assertIn('نتیجهٔ استارت روشن نیست', html)
        self.assertIn('تنظیم روشن؛ اجرا متوقف', html)
        self.assertNotIn('اطلاعات حساب دریافت شده است', html)
        self.assertIn('12:30:00', html)  # Historic receive remains visible, not liveness.
        self.assertIn('زمانی ثبت نشده', html)

    def test_new_startup_supersedes_old_saved_stop_without_claiming_success(self):
        view = self.view()
        view['activity'] = {'status': 'stopped', 'message': dashboard.STOP_MESSAGES['manual_stop']}
        view['runtime'] = {'stage': 'logging_in', 'code': '', 'updated': NOW}
        self.assertIsNone(dashboard.stop_code(view))
        title = dashboard.simple_status(view)[0]
        self.assertEqual(title, 'شروع دریافت ثبت شده است')
        self.assertNotIn('دریافت شده', title)

    def test_schedule_uses_captured_clock_and_marks_overdue_or_stopped(self):
        view = self.view()
        view['activity'] = {'status': 'waiting', 'next_check': NOW + timedelta(minutes=15)}
        time, note = dashboard.next_check_text(view)
        self.assertEqual(time, '12:45:00')
        self.assertIn('2 آذر 1405', note)
        self.assertIn('نه تضمین اجرا', note)
        view['now'] = NOW + timedelta(minutes=16)
        self.assertEqual(dashboard.next_check_text(view)[0], 'موعد بررسی گذشته است')
        view['runtime'] = {'stage': 'stopped', 'code': 'database_error'}
        self.assertEqual(dashboard.next_check_text(view)[0], 'زمانی ثبت نشده')

    def test_incomplete_configuration_is_not_presented_as_ready(self):
        del os.environ['PRIMARY_ACCOUNT_PASSWORD']
        view = self.view()
        self.assertIn('تنظیم روشن؛ پیکربندی ناقص', dashboard.status_section(view))
        self.assertEqual(dashboard.simple_status(view)[0], 'تنظیمات حساب کامل نیست')

    def test_owned_first_filters_and_separate_bonus_without_derived_quota(self):
        view = self.view([
            {'name': 'rental-fixture', 'rented': True, 'status': 'unavailable', 'bonus': 0},
            {'name': 'personal-fixture', 'rented': False, 'status': 'idle',
             'used': 10, 'limit': 10, 'bonus': 120},
        ])
        html = dashboard.account_data_section(view)
        self.assertLess(html.index('personal-fixture'), html.index('rental-fixture'))
        for kind in ('all', 'owned', 'rented'):
            self.assertIn(f'data-filter="{kind}"', html)
        self.assertEqual(html.count('account-bonus-count'), 1)
        self.assertEqual(html.count('car-bonus-count'), 2)
        self.assertIn('سفر عادی باقی‌مانده</span><strong>نامشخص', html)
        self.assertNotIn('>240<', html)

    def test_known_turn_message_is_translated_original_is_escaped_in_details(self):
        html = dashboard.car_card({'status': 'unavailable', 'rented': True,
                                   'block_msg': 'Ahora no es tu turno con este taxi.'})
        self.assertIn('نوبت شما نیست', html)
        self.assertIn('Ahora no es tu turno con este taxi.', html)
        other = dashboard.car_card({'status': 'unavailable', 'name': '<script>bad()</script>',
                                    'block_msg': '<img src=x onerror=bad()>'})
        self.assertNotIn('<script>', other)
        self.assertNotIn('<img', other)
        self.assertIn('&lt;img', other)

    def test_stale_data_is_clearly_labelled_not_a_live_garage(self):
        view = self.view(minutes_ago=11)
        self.assertEqual(dashboard.simple_status(view)[0], 'اطلاعات ذخیره‌شده را می‌بینید')
        self.assertIn('اطلاعات قدیمی است', dashboard.account_data_section(view))

    def test_empty_and_populated_page_need_no_network_and_hide_technical_details(self):
        for view in (self.view(), dashboard.dashboard_view({}, NOW)):
            with self.subTest(sample=view['sample']), \
                 patch.object(dashboard, 'dashboard_view', return_value=view), \
                 patch.object(dashboard, 'load_json', side_effect=lambda path, default: default), \
                 patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
                page = dashboard.render()
            self.assertIn('lang="fa" dir="rtl"', page)
            self.assertIn('id="mainContent"', page)
            self.assertIn('href="#technicalPanel"', page)
            self.assertIn('id="technicalPanel" class="history-panel" data-status-panel>', page)
            self.assertNotIn('<form', page)
            self.assertNotIn('fetch(', page)
            self.assertLess(page.index('id="overview"'), page.index('id="technicalPanel"'))

    @unittest.skipUnless(shutil.which('node'), 'Node required for browser logic test')
    def test_filter_persistence_empty_results_invalid_values_and_blocked_storage(self):
        harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
function run(saved, blocked) {
  let stored;
  const buttons = ['all', 'owned', 'rented'].map(kind => ({
    dataset: {filter: kind}, listeners: {},
    setAttribute(k,v) { this[k]=v; }, addEventListener(k,fn) { this.listeners[k]=fn; }
  }));
  const cards = [{dataset:{carKind:'owned'}, hidden:false}];
  const empty = [{hidden:true}];
  const panel = {open:false};
  const link = {addEventListener(k,fn) { this[k]=fn; }};
  vm.runInNewContext(SCRIPT, {
    document:{querySelectorAll(selector) {
      return ({'[data-filter]': buttons, '[data-car-kind]':cards,
        '[data-filter-empty]':empty, 'a[href="#technicalPanel"]':[link],
        '#technicalPanel':[panel]})[selector] || [];
    }},
    localStorage:{getItem() {if(blocked)throw Error('blocked'); return saved;},
      setItem(k,v) {if(blocked)throw Error('blocked'); stored=v;}},
    setInterval() {}, Date
  });
  assert.equal(cards[0].hidden, false); // Invalid filter resets to all.
  buttons[2].listeners.click();
  assert.equal(cards[0].hidden, true);
  assert.equal(empty[0].hidden, false);
  assert.equal(buttons[2]['aria-pressed'], 'true');
  if (!blocked) assert.equal(stored,'rented');
  buttons[0].listeners.click();
  assert.equal(cards[0].hidden,false);
  assert.equal(empty[0].hidden,true);
  link.click(); assert.equal(panel.open,true);
}
run('invalid',false); run(null,true);
'''.replace('SCRIPT', json.dumps(dashboard.PAGE_JS))
        subprocess.run([NODE, '-e', harness], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
