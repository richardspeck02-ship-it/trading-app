import hashlib
import json
import unittest
import requests
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from login import LoginError, REDIRECT_URI, authorization_url, callback_code, exchange_code


def response(status, body):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    return result


class LoginTests(unittest.TestCase):
    def test_login_url_uses_exact_redirect_and_state(self):
        parts = urlsplit(authorization_url("test-id", "random-state"))
        self.assertEqual(parts.netloc, "api-t1.fyers.in")
        query = parse_qs(parts.query)
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["state"], ["random-state"])
        self.assertEqual(query["response_type"], ["code"])

    def test_callback_returns_auth_code(self):
        self.assertEqual(callback_code(
            "/auth/fyers/callback?s=ok&state=expected&auth_code=test-code", "expected"), "test-code")

    def test_rejects_invalid_callbacks(self):
        for target in (
            "/wrong?state=expected&auth_code=code",
            "/auth/fyers/callback?auth_code=code",
            "/auth/fyers/callback?state=wrong&auth_code=code",
            "/auth/fyers/callback?state=expected&state=expected&auth_code=code",
            "/auth/fyers/callback?state=expected&auth_code=one&auth_code=two",
            "/auth/fyers/callback?state=expected&auth_code=",
            "/auth/fyers/callback?state=expected&s=error&auth_code=code",
            "/auth/fyers/callback?state=expected&error=denied",
        ):
            with self.subTest(target=target), self.assertRaises(LoginError):
                callback_code(target, "expected")

    @patch("login.requests.post")
    def test_token_exchange_payload_and_timeout(self, request):
        request.return_value = response(200, json.dumps({"s": "ok", "access_token": "test-token"}))
        self.assertEqual(exchange_code("id", "secret", "code"), "test-token")
        args, kwargs = request.call_args
        self.assertEqual(kwargs["timeout"], 30)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("verify", kwargs)
        self.assertEqual(kwargs["json"], {
            "grant_type": "authorization_code",
            "appIdHash": hashlib.sha256(b"id:secret").hexdigest(), "code": "code",
        })

    @patch("login.requests.post")
    def test_broker_errors_do_not_expose_response(self, request):
        request.return_value = response(200, '{"s":"error","message":"private-response"}')
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertNotIn("private-response", str(caught.exception))

    @patch("login.requests.post", side_effect=requests.exceptions.Timeout("private-response"))
    def test_network_failure_has_safe_error(self, request):
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertNotIn("private-response", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))

    @patch("login.requests.post")
    def test_http_failure_exposes_only_status_and_numeric_code(self, request):
        request.return_value = response(400, '{"code":-999,"message":"private-response","access_token":"private-token"}')
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("FYERS code -999", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    @patch("login.requests.post")
    def test_non_json_http_failure_is_reported(self, request):
        request.return_value = response(403, '<html>private-response</html>')
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    @patch("login.requests.post")
    def test_connection_categories(self, request):
        for reason, expected in ((requests.exceptions.SSLError("private"), "certificate"),
                                 (requests.exceptions.ConnectionError("private"), "connection error"),
                                 (requests.exceptions.Timeout("private"), "timed out")):
            with self.subTest(expected=expected):
                request.side_effect = reason
                with self.assertRaises(LoginError) as caught:
                    exchange_code("id", "secret", "code")
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
