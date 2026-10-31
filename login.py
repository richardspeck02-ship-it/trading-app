"""Local FYERS authorization. Credentials stay in memory for this run only."""

import argparse
import getpass
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.parse
import requests
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

# Endpoints and token payload match FYERS' official Python SDK 3.1.7.
AUTH_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
REDIRECT_URI = "http://127.0.0.1:8000/auth/fyers/callback"
CALLBACK_PATH = "/auth/fyers/callback"


class LoginError(Exception):
    pass


def authorization_url(client_id, state):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "response_type": "code", "state": state,
    })


def callback_code(target, expected_state):
    parts = urllib.parse.urlsplit(target)
    if parts.path != CALLBACK_PATH or parts.scheme or parts.netloc:
        raise LoginError("Unexpected callback path")
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    state = query.get("state", [])
    if len(state) != 1 or not secrets.compare_digest(state[0], expected_state):
        raise LoginError("Login state did not match. Restart login and try again.")
    if "error" in query or query.get("s", ["ok"]) != ["ok"]:
        raise LoginError("FYERS did not authorize this login. Restart and try again.")
    code = query.get("auth_code", [])
    if len(code) != 1 or not code[0].strip():
        raise LoginError("The callback did not contain a single authorization code.")
    return code[0]


def make_handler(state):
    class CallbackHandler(BaseHTTPRequestHandler):
        # Never log a callback URL: its query contains a temporary credential.
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get("Host") != "127.0.0.1:8000":
                self.respond(400, "Unexpected callback host.")
                return
            if urllib.parse.urlsplit(self.path).path != CALLBACK_PATH:
                self.respond(404, "Not found.")
                return
            if self.server.auth_code is not None:
                self.respond(409, "This login callback has already been used.")
                return
            try:
                code = callback_code(self.path, state)
            except LoginError as exc:
                self.respond(400, str(exc))
                return
            self.server.auth_code = code
            self.respond(200, "Authorization received. Return to PowerShell to see the login result. You can close this tab.")

        def respond(self, status, message):
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


def exchange_code(client_id, secret, code):
    payload = {
        "grant_type": "authorization_code",
        "appIdHash": hashlib.sha256(f"{client_id}:{secret}".encode()).hexdigest(),
        "code": code,
    }
    try:
        # Use the same requests/JSON transport as FYERS' official SDK.
        # Keep TLS verification enabled and prevent code-bearing redirects.
        response = requests.post(TOKEN_URL, json=payload, timeout=30, allow_redirects=False)
    except requests.exceptions.SSLError:
        raise LoginError("Token exchange failed: TLS certificate verification failed. Check Windows date/time and certificate trust; keep verification enabled.") from None
    except requests.exceptions.Timeout:
        raise LoginError("Token exchange failed: connection timed out. Check connectivity and retry a fresh login.") from None
    except requests.exceptions.RequestException:
        raise LoginError("Token exchange failed: network connection error. Check connectivity, firewall, and proxy settings, then retry.") from None
    with response:
        try:
            result = response.json()
        except ValueError:
            result = None
        if not 200 <= response.status_code < 300:
            kind = "JSON" if result is not None else "non-JSON"
            raise LoginError(f"Token exchange rejected: HTTP {response.status_code}{broker_code(result)}; {kind} response. "
                             "A refusal without a FYERS code does not establish incorrect credentials. "
                             "If it persists, contact FYERS support with this status and the attempt time.")
        if result is None:
            raise LoginError("Token exchange received an invalid JSON response. Check for a network sign-in page or filtering proxy.")
    if not isinstance(result, dict) or result.get("s") != "ok":
        raise LoginError(f"FYERS rejected the token exchange{broker_code(result)}. "
                         "Check app credentials and the registered redirect URL.")
    token = result.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise LoginError("FYERS returned no access token. Restart login.")
    return token


def broker_code(result):
    # Display only a numeric diagnostic, never arbitrary broker text or tokens.
    value = result.get("code") if isinstance(result, dict) else None
    if type(value) is int and -1000000 <= value <= 1000000:
        return f" (FYERS code {value})"
    return ""


def login(client_id, secret):
    state = secrets.token_urlsafe(32)
    try:
        server = HTTPServer(("127.0.0.1", 8000), make_handler(state))
    except OSError:
        raise LoginError("Cannot start callback listener. Close any other app using local port 8000 and retry.") from None
    with server:
        server.auth_code = None
        server.timeout = 1
        url = authorization_url(client_id, state)
        print("Complete login in the FYERS browser page. Waiting up to 10 minutes.")
        print("If the browser does not open, paste this login link into your browser:")
        print(url)
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
        deadline = time.monotonic() + 600
        while server.auth_code is None and time.monotonic() < deadline:
            server.handle_request()
        if server.auth_code is None:
            raise LoginError("Login timed out. Confirm FYERS accepts the registered redirect URL and try again.")
        return exchange_code(client_id, secret, server.auth_code)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="After login, scan the supplied FYERS symbol file")
    parser.add_argument("--date", type=date.fromisoformat, help="Scan session YYYY-MM-DD")
    parser.add_argument("--live", action="store_true", help="Open a read-only live browser scanner for today's session")
    parser.add_argument("--output-dir", default="results", help="Save scan files under this directory (default: results)")
    args = parser.parse_args(argv)
    if args.live and args.date:
        parser.error("--live uses today's market session; omit --date")
    if args.live and not args.symbols:
        args.symbols = "nifty500.txt"
    if not args.live and bool(args.symbols) != bool(args.date):
        parser.error("Provide --symbols and --date together, or omit both to test login")
    if args.symbols and not Path(args.symbols).is_file():
        parser.error("Symbol file not found")
    try:
        client_id = os.environ.get("FYERS_CLIENT_ID") or input("FYERS App ID: ").strip()
        secret = os.environ.get("FYERS_APP_SECRET") or getpass.getpass("FYERS app secret (hidden): ")
        if not client_id or not secret:
            raise LoginError("App ID and app secret are required.")
        token = login(client_id, secret)
        print("Login succeeded. No token or app secret has been saved to disk.")
        if args.symbols:
            child_env = os.environ.copy()
            child_env.pop("FYERS_APP_SECRET", None)
            child_env["FYERS_CLIENT_ID"] = client_id
            child_env["FYERS_ACCESS_TOKEN"] = token
            if args.live:
                command = [sys.executable, str(Path(__file__).with_name("live_app.py")), "--symbols", args.symbols]
            else:
                command = [sys.executable, str(Path(__file__).with_name("scanner.py")),
                           "--fyers", "--symbols", args.symbols, "--date", args.date.isoformat(),
                           "--output-dir", args.output_dir]
            return subprocess.run(command, env=child_env, check=False).returncode
        print("To login and scan, run again with --symbols <file> --date YYYY-MM-DD.")
        return 0
    except (LoginError, EOFError) as exc:
        print(str(exc) or "Login input was interrupted.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
