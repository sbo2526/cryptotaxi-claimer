import json
import unittest
from unittest.mock import Mock, patch

import requests

import claimer
from ban_detection import is_ban_error, suspension_reason


MESSAGE = ('Your account is suspended.Reason: Multi Account.'
           'If you think this is a mistake, contact Support on Telegram (@CryptoTaxiSupport).')


def response(status, payload=None, text=None):
    result = requests.Response()
    result.status_code = status
    result.url = 'https://example.test/api/auth/login'
    result._content = (text if text is not None else json.dumps(payload)).encode()
    result.encoding = 'utf-8'
    return result


class ExplicitSuspensionTests(unittest.TestCase):
    def test_generic_errors_never_prove_suspension(self):
        for text in ('Forbidden', 'Access denied', 'blocked', 'suspended',
                     'invalid password', 'HTTP 403 on login', '12 consecutive failures',
                     'Your account is not suspended', 'If your account is suspended, contact support',
                     'IP blocked', 'Cloudflare challenge', 'Multi Account'):
            for status in (200, 401, 403, 429, 500):
                with self.subTest(text=text, status=status):
                    self.assertFalse(is_ban_error(status_code=status, error_text=text))
        self.assertFalse(is_ban_error(status_code=403))

    def test_account_specific_messages_and_codes_are_recognized(self):
        for text in (MESSAGE, 'Your account has been banned', 'Account suspended',
                     'ACCOUNT_SUSPENDED', 'account_banned', 'Your account is temporarily suspended'):
            with self.subTest(text=text):
                self.assertTrue(is_ban_error(error_text=text))

    def test_actual_json_and_html_body_preserve_reason(self):
        for result in (
            response(403, {'error': {'message': MESSAGE}}),
            response(200, {'error': {'code': 'ACCOUNT_SUSPENDED', 'reason': 'Multi Account'}}),
            response(403, text='<h1>Your account is suspended.</h1><p>Reason: <b>Multi Account</b></p>'),
        ):
            with self.subTest(status=result.status_code):
                self.assertIn('Multi Account', suspension_reason(result))
        self.assertIsNone(suspension_reason(response(403, text='<h1>Forbidden</h1>')))
        self.assertIsNone(suspension_reason(response(200, {'profile': {'bio': MESSAGE}})))



if __name__ == '__main__':
    unittest.main()
