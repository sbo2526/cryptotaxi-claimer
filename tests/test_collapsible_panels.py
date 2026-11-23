from html.parser import HTMLParser
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

import dashboard


class PanelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.panels = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': dict(attrs), 'children': []}
        if self.stack:
            self.stack[-1]['children'].append(node)
        if tag == 'details' and 'data-status-panel' in node['attrs']:
            self.panels.append(node)
        if tag not in ('meta', 'link', 'input', 'br', 'hr', 'img'):
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1]['tag'] == tag:
            self.stack.pop()


class CollapsiblePanelTests(unittest.TestCase):
    def assert_native_panel(self, html, panel_id):
        parser = PanelParser()
        parser.feed(html)
        self.assertEqual(len(parser.panels), 1)
        panel = parser.panels[0]
        self.assertEqual(panel['attrs']['id'], panel_id)
        self.assertNotIn('open', panel['attrs'])
        self.assertEqual([child['tag'] for child in panel['children']], ['summary', 'div'])
        self.assertIn('panel-body', panel['children'][1]['attrs']['class'])
        self.assertIn('chevron', html)
        self.assertEqual(parser.stack, [])

    def test_events_is_native_disclosure(self):
        with patch.object(dashboard, 'event_lines', return_value=[]):
            html = dashboard.events_section()
        self.assert_native_panel(html, 'eventsPanel')
        self.assertIn('رویدادی برای نمایش وجود ندارد.', html)

    def test_banned_accounts_is_native_disclosure(self):
        with patch.object(dashboard, 'banned_accounts_info', return_value=[]):
            html = dashboard.banned_accounts_section()
        self.assert_native_panel(html, 'bannedAccountsPanel')
        self.assertIn('بدون سابقه', html)

    def test_full_dashboard_has_three_unique_disclosures_and_no_email_access_text(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            html = dashboard.render()
        parser = PanelParser()
        parser.feed(html)
        self.assertEqual([p['attrs']['id'] for p in parser.panels],
                         ['technicalPanel', 'bannedAccountsPanel', 'eventsPanel'])
        self.assertNotIn('دسترسی به ایمیل‌های اکانت‌ها', html)
        self.assertNotIn('email-status', html)

    @unittest.skipUnless(shutil.which('node'), 'Node required for JS behavior test')
    def test_trip_timer_does_not_promise_claim_or_restart(self):
        harness = '''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const timers = [
  {dataset: {ends: '125000'}, textContent: ''},
  {dataset: {ends: '1'}, textContent: ''},
  {dataset: {ends: 'bad'}, textContent: 'unchanged'},
  {dataset: {ends: '7321000'}, textContent: ''}
];
vm.runInNewContext(SCRIPT, {
  document: {querySelectorAll: selector => selector.startsWith('.trip-timer') ? timers : []},
  Date: {now: () => 0 + 60000}, setInterval() {}
});
assert.equal(timers[0].textContent, 'زمان باقی‌ماندهٔ ثبت‌شده: 00:01:05');
assert.equal(timers[1].textContent, 'زمان ثبت‌شدهٔ سفر به پایان رسیده؛ منتظر دریافت بعدی');
assert.equal(timers[2].textContent, 'unchanged');
assert.equal(timers[3].textContent, 'زمان باقی‌ماندهٔ ثبت‌شده: 02:01:01');
vm.runInNewContext(SCRIPT, {
  document: {querySelectorAll: selector => selector.startsWith('.trip-timer') ? timers : []},
  Date: {now: () => 61000}, setInterval() {}
});
assert.equal(timers[0].textContent, 'زمان باقی‌ماندهٔ ثبت‌شده: 00:01:04');
'''.replace('SCRIPT', json.dumps(dashboard.PAGE_JS))
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)

    @unittest.skipUnless(shutil.which('node'), 'Node required for JS behavior test')
    def test_each_panel_restores_and_saves_state_and_tolerates_blocked_storage(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            html = dashboard.render()
        start = html.index('  // Keep each account panel')
        end = html.index('  // End disclosure persistence', start)
        script = html[start:end]
        harness = '''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = SCRIPT;
function panel(id) {
  return {id, open: false, listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }};
}
const first = panel('eventsPanel');
const second = panel('bannedAccountsPanel');
const stored = new Map([['status_panel_open_eventsPanel', '1']]);
const context = {
  document: { querySelectorAll: () => [first, second] },
  localStorage: {getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value)}
};
vm.runInNewContext(code, context);
assert.equal(first.open, true);
assert.equal(second.open, false);
first.open = false; first.listeners.toggle();
second.open = true; second.listeners.toggle();
assert.equal(stored.get('status_panel_open_eventsPanel'), '0');
assert.equal(stored.get('status_panel_open_bannedAccountsPanel'), '1');
const reloaded = [panel(first.id), panel(second.id)];
context.document.querySelectorAll = () => reloaded;
vm.runInNewContext(code, context);
assert.equal(reloaded[0].open, false);
assert.equal(reloaded[1].open, true);
context.localStorage = {getItem() {throw Error('blocked');}, setItem() {throw Error('blocked');}};
vm.runInNewContext(code, context);
reloaded[0].open = true;
reloaded[0].listeners.toggle();
assert.equal(reloaded[0].open, true);
'''.replace('SCRIPT', json.dumps(script))
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
