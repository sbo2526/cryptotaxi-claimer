import base64
from http.server import HTTPServer
import os
import threading
import unittest
from unittest.mock import patch

import requests

import dashboard
import runtime_config


class DashboardAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), dashboard.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        allowed = patch.object(runtime_config, 'ALLOWED_PRIMARY_ACCOUNT', 'main')
        allowed.start()
        self.addCleanup(allowed.stop)
        env = patch.dict(os.environ, {'DASHBOARD_USER': 'owner',
                                     'DASHBOARD_PASSWORD': 'test-dashboard-pass-123',
                                     'PRIMARY_ACCOUNT': 'main'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)

    def test_all_data_routes_require_authentication(self):
        for path in ('/', '/count', '/creator-status', '/clear'):
            result = self.session.get(self.url + path, timeout=5)
            self.assertEqual(result.status_code, 401)
            self.assertNotIn('main', result.text)
            self.assertIn('Basic', result.headers['WWW-Authenticate'])

    def test_missing_or_weak_configuration_never_exposes_data(self):
        for password in ('', 'short'):
            with patch.dict(os.environ, {'DASHBOARD_PASSWORD': password}):
                result = self.session.get(self.url + '/', timeout=5)
                self.assertEqual(result.status_code, 503)
                self.assertNotIn('main', result.text)

    def test_authenticated_counts_and_page(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            result = self.session.get(self.url + '/count', auth=('owner', 'test-dashboard-pass-123'), timeout=5)
            self.assertEqual(result.json(), {'ok': True, 'count': 1})
            result = self.session.get(self.url + '/', auth=('owner', 'test-dashboard-pass-123'), timeout=5)
            self.assertEqual(result.status_code, 200)
            self.assertIn('حالت امن', result.text)
            self.assertNotIn('test-dashboard-pass-123', result.text)
            self.assertEqual(result.headers['Cache-Control'], 'no-store')

    def test_health_is_static_and_does_not_reveal_account_or_configuration(self):
        result = self.session.get(self.url + '/healthz', timeout=5)
        self.assertEqual(result.json(), {'ok': True})

    def test_mutating_routes_are_disabled_even_with_auth(self):
        auth = ('owner', 'test-dashboard-pass-123')
        self.assertEqual(self.session.post(self.url + '/clear', auth=auth, json={}, timeout=5).status_code, 405)
        self.assertEqual(self.session.get(self.url + '/clear', auth=auth, timeout=5).status_code, 404)

    def test_new_rental_ui_does_not_add_executable_market_or_rent_routes(self):
        auth = ('owner', 'test-dashboard-pass-123')
        for path in ('/rent', '/market', '/market/rent', '/rentals/enable'):
            with self.subTest(path=path):
                self.assertEqual(self.session.get(self.url + path, timeout=5).status_code, 401)
                self.assertEqual(self.session.get(self.url + path, auth=auth, timeout=5).status_code, 404)
                self.assertEqual(self.session.post(self.url + path, auth=auth, json={'listingId': 1}, timeout=5).status_code, 405)

    def test_invalid_authorization_does_not_crash(self):
        for value in ('Basic not-base64', 'Bearer bad', 'Basic ' + base64.b64encode(b'no-colon').decode()):
            result = self.session.get(self.url + '/', headers={'Authorization': value}, timeout=5)
            self.assertEqual(result.status_code, 401)


if __name__ == '__main__':
    unittest.main()
