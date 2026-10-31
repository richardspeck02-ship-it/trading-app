import hashlib
import io
import json
import socket
import ssl
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from login import LoginError, REDIRECT_URI, authorization_url, callback_code, exchange_code


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

    @patch("login.urllib.request.urlopen")
    def test_token_exchange_payload_and_timeout(self, request):
        request.return_value = io.BytesIO(json.dumps({"s": "ok", "access_token": "test-token"}).encode())
        self.assertEqual(exchange_code("id", "secret", "code"), "test-token")
        args, kwargs = request.call_args
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(json.loads(args[0].data), {
            "grant_type": "authorization_code",
            "appIdHash": hashlib.sha256(b"id:secret").hexdigest(), "code": "code",
        })

    @patch("login.urllib.request.urlopen")
    def test_broker_errors_do_not_expose_response(self, request):
        request.return_value = io.BytesIO(b'{"s":"error","message":"private-response"}')
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertNotIn("private-response", str(caught.exception))

    @patch("login.urllib.request.urlopen", side_effect=TimeoutError("private-response"))
    def test_network_failure_has_safe_error(self, request):
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertNotIn("private-response", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))

    @patch("login.urllib.request.urlopen")
    def test_http_failure_exposes_only_status_and_numeric_code(self, request):
        request.side_effect = HTTPError("https://example.invalid", 400, "private-reason", {},
                                        io.BytesIO(b'{"code":-999,"message":"private-response","access_token":"private-token"}'))
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("FYERS code -999", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    @patch("login.urllib.request.urlopen")
    def test_non_json_http_failure_is_reported(self, request):
        request.side_effect = HTTPError("https://example.invalid", 403, "private-reason", {},
                                        io.BytesIO(b'<html>private-response</html>'))
        with self.assertRaises(LoginError) as caught:
            exchange_code("id", "secret", "code")
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    @patch("login.urllib.request.urlopen")
    def test_connection_categories(self, request):
        for reason, expected in ((ssl.SSLCertVerificationError(1, "private"), "certificate"),
                                 (socket.gaierror(1, "private"), "DNS"),
                                 (TimeoutError("private"), "timed out")):
            with self.subTest(expected=expected):
                request.side_effect = URLError(reason)
                with self.assertRaises(LoginError) as caught:
                    exchange_code("id", "secret", "code")
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
