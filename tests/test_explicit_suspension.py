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

    def test_repeated_generic_login_denial_never_marks_banned(self):
        session = Mock()
        with patch.dict(claimer.ACCOUNTS, {'main': {'password': 'secret'}}, clear=True), \
             patch.object(claimer, 'mark_banned') as mark:
            for result in (response(403, {'error': 'Forbidden'}),
                           response(200, {'error': 'invalid password'})):
                session.post.return_value = result
                for _ in range(8):
                    with self.assertRaises((requests.HTTPError, RuntimeError)):
                        claimer.login(session, 'main')
            mark.assert_not_called()
            self.assertIn('main', claimer.ACCOUNTS)

    def test_login_suspension_detected_before_http_error_or_json_error(self):
        for code in (200, 403):
            session = Mock()
            session.post.return_value = response(code, {'error': MESSAGE})
            with self.subTest(code=code), \
                 patch.dict(claimer.ACCOUNTS, {'main': {'password': 'secret'}}, clear=True), \
                 patch.object(claimer, 'mark_banned') as mark:
                with self.assertRaises(claimer.AccountSuspendedError):
                    claimer.login(session, 'main')
                mark.assert_called_once_with('main', MESSAGE)

    def test_api_401_with_explicit_suspension_does_not_relogin(self):
        session = Mock()
        session.post.return_value = response(401, {'message': MESSAGE})
        with patch.object(claimer, 'mark_banned') as mark, patch.object(claimer, 'login') as login:
            with self.assertRaises(claimer.AccountSuspendedError):
                claimer.api_post(session, 'state', {}, 'main')
            login.assert_not_called()
            mark.assert_called_once()

    def test_api_relogin_retry_response_is_also_checked(self):
        session = Mock()
        session.post.side_effect = [response(401, {'error': 'expired'}), response(403, {'error': MESSAGE})]
        with patch.object(claimer, 'login', return_value=session), patch.object(claimer, 'mark_banned') as mark:
            with self.assertRaises(claimer.AccountSuspendedError):
                claimer.api_post(session, 'state', {}, 'main')
            mark.assert_called_once()

    def test_generic_api_403_returned_as_error_not_a_ban(self):
        session = Mock()
        session.post.return_value = response(403, {'error': 'Forbidden'})
        with patch.object(claimer, 'mark_banned') as mark:
            result = claimer.api_post(session, 'state', {}, 'main')
            self.assertEqual(result.status_code, 403)
            mark.assert_not_called()

    def test_repeated_sweep_failures_never_delete_even_if_exception_mentions_suspension(self):
        session = Mock(cookies={})
        with patch.object(claimer, 'get_session', return_value=session), \
             patch.object(claimer, 'login', side_effect=RuntimeError('403 Forbidden account suspended')), \
             patch.object(claimer.time, 'sleep'), patch.object(claimer, 'log'), \
             patch.object(claimer, 'mark_banned') as mark:
            status = {}
            for _ in range(8):
                claimer.process_account({'username': 'main'}, status)
            mark.assert_not_called()
            self.assertIn('main', status)

    def test_disabled_detection_does_not_delete(self):
        with patch.object(claimer, 'BAN_DETECTION_ENABLED', False), patch.object(claimer, 'mark_banned') as mark:
            with self.assertRaises(claimer.AccountSuspendedError):
                claimer.check_account_suspension(response(403, {'error': MESSAGE}), 'main')
            mark.assert_not_called()


if __name__ == '__main__':
    unittest.main()
