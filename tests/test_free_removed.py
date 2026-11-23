import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import dashboard
import runtime_config


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
            return default

        with patch.object(runtime_config, 'ALLOWED_PRIMARY_ACCOUNT', 'existing-account'), patch.dict(os.environ, {'PRIMARY_ACCOUNT': 'existing-account'}), patch.object(dashboard, 'load_json', side_effect=load):
            page = dashboard.render()
        self.assertIn('existing-account', page)
        self.assertIn('هنوز اطلاعاتی از اکانت دریافت نشده است.', page)
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



if __name__ == '__main__':
    unittest.main()
