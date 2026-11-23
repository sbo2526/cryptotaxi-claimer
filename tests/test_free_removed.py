import ast
from pathlib import Path
import unittest
from unittest.mock import patch

import dashboard


ROOT = Path(__file__).resolve().parents[1]


class FreeRemovedTests(unittest.TestCase):
    def test_dashboard_ignores_historical_free_stats(self):
        stale_status = {
            'existing-account': {
                'free': {'claimed': 3, 'total_earned': 0.5, 'links_found': 4},
                'cars': [],
            },
        }
        def load(path, default):
            if path == dashboard.STATUS_FILE:
                return stale_status
            if path == dashboard.ACCOUNTS_FILE:
                return [{'username': 'existing-account'}]
            return default

        with patch.object(dashboard, 'load_json', side_effect=load):
            page = dashboard.render()
        self.assertIn('existing-account', page)
        self.assertIn('ساخت خودکار اکانت‌ها', page)
        self.assertNotIn('FREE', page)
        self.assertFalse(hasattr(dashboard, 'free_status_section'))

    def test_claimer_has_no_free_worker_or_endpoints(self):
        tree = ast.parse((ROOT / 'claimer.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.Name)):
                name = node.name if isinstance(node, ast.FunctionDef) else node.id
                self.assertNotIn('free', name.lower())
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn('/free', node.value.lower())
                self.assertNotEqual(node.value, 'free-worker')

    def test_claimer_still_runs_sweeps_without_starting_ad_threads(self):
        import claimer
        with patch.object(claimer.sys, 'argv', ['claimer.py']), \
             patch.object(claimer, 'PROXY_ENABLED', False), \
             patch.object(claimer, 'start_auto_rent') as rent, \
             patch.object(claimer, 'run_sweep', side_effect=KeyboardInterrupt) as sweep, \
             patch.object(claimer.threading, 'Thread') as thread:
            with self.assertRaises(KeyboardInterrupt):
                claimer.main()
            sweep.assert_called_once()
            thread.assert_not_called()
            if claimer.AUTO_RENT_ENABLED:
                rent.assert_called_once()


if __name__ == '__main__':
    unittest.main()
