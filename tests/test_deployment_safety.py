import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import runtime_config
import supervisor


ROOT = Path(__file__).resolve().parents[1]


class DeploymentSafetyTests(unittest.TestCase):
    def test_no_credential_or_runtime_json_is_tracked(self):
        paths = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
        for forbidden in ('accounts.json', 'email_account.json', 'status.json',
                          'banned_accounts.json', 'earnings.json', 'proxies.txt'):
            self.assertNotIn(forbidden, paths)

    def test_container_has_explicit_allowlist_and_runs_non_root(self):
        source = (ROOT / 'Dockerfile').read_text()
        self.assertNotIn('COPY .', source)
        self.assertIn('USER app', source)
        self.assertNotIn('accounts.json', source)
        rules = (ROOT / '.dockerignore').read_text().splitlines()
        self.assertEqual(rules[0], '**')
        self.assertNotIn('!.git', rules)
        self.assertNotIn('!data', rules)
        self.assertIn('healthcheckPath = "/healthz"', (ROOT / 'railway.toml').read_text())

    def test_initialize_does_not_copy_credentials_or_overwrite_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime_config, 'DATA_DIR', Path(directory)):
            runtime_config.initialize_data()
            path = Path(directory) / 'banned_accounts.json'
            path.write_text('{"banned": [{"confirmed": true}]}')
            runtime_config.initialize_data()
            self.assertIn('true', path.read_text())
            self.assertFalse((Path(directory) / 'accounts.json').exists())

    def test_failed_monitor_is_not_restarted_by_supervisor(self):
        dashboard_process = Mock()
        dashboard_process.poll.return_value = None
        monitor_process = Mock()
        monitor_process.poll.return_value = 0
        with patch.dict(os.environ, {'SITE_MONITORING_ENABLED': 'true'}, clear=True), \
             patch.object(supervisor, 'initialize_data'), \
             patch.object(supervisor.signal, 'signal'), \
             patch.object(supervisor.subprocess, 'Popen', side_effect=[dashboard_process, monitor_process]) as launch, \
             patch.object(supervisor.time, 'sleep', side_effect=[None, None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.main()
            self.assertEqual(launch.call_count, 2)
        dashboard_process.terminate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
