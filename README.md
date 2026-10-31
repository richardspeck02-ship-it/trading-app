# NIFTY 500 opening-range scanner

Filters stocks whose second **and** third five-minute candles remain inside the
first candle, including equal highs/lows (`[]`). Candle windows are 09:15–09:20,
09:20–09:25, and 09:25–09:30 Asia/Kolkata. Wicks count. This scanner reads data
and does not place orders.

## Run offline

Python 3.12 is supported. The full test suite requires the installed dependencies for login tests.

```sh
python -m unittest discover -s tests -v
python scanner.py --csv examples/candles.csv --date 2026-10-30
```

The example uses synthetic data: DEMO_MATCH qualifies and DEMO_REJECT does not.
CSV timestamps must include a timezone and represent candle **opening** times.
Results include matches and per-symbol status. Missing candles are incomplete,
never non-matches. Exit status is nonzero for incomplete data or request errors.
Weekends and requests before the session's 09:30 IST close are rejected; exchange
holidays are not inferred. If data publication is delayed, rerun after 09:30.

## Connect FYERS

Register a non-trading app through the official FYERS developer portal. The local
login callback is implemented in `login.py`; see the Windows steps below. Automatic
token refresh is not implemented. Never put credentials in Git or chat.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Set `FYERS_CLIENT_ID` and `FYERS_ACCESS_TOKEN` through your secure environment.
The scanner does not load `.env` files automatically. Provide a current, verified
NIFTY 500 constituent list in a text file, one FYERS symbol per line, such as
`NSE:RELIANCE-EQ`. No constituent list is bundled; the scanner checks every symbol
supplied but cannot establish index membership. Use date-appropriate membership
for historical sessions.

```sh
.venv/bin/python scanner.py --fyers --symbols /path/to/nifty500.txt --date YYYY-MM-DD
```

FYERS mode makes sequential five-minute history requests, with a delay between
symbols. A full universe takes several minutes. Confirm current API permissions,
usage limits, and symbol mappings with FYERS. Failed symbols are reported without
printing raw broker responses. Connected operation requires account credentials,
network access to FYERS, and a verified universe; it is not validated by the
offline example. Scheduled scans, a web interface, and trade execution are future work.

## Windows: local login and scanning

1. Extract the project ZIP into a folder, such as `Documents\trading-app`.
2. Open PowerShell in that folder. Run the commands below. Activation of the
   virtual environment is unnecessary, so no execution-policy change is needed.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scanner.py --csv examples/candles.csv --date 2026-10-30
```

If `py` is unavailable, use `python -m venv .venv` after confirming
`python --version` reports 3.12. `tzdata` supplies Asia/Kolkata timezone data on
Windows, where the operating system does not ship Python's IANA timezone database.

3. In FYERS' non-trading app form, try this **exact Redirect URL**:

   `http://127.0.0.1:8000/auth/fyers/callback`

   This is the callback our program implements, not a claim that FYERS' current
   registration form accepts HTTP loopback URLs. Its acceptance has not been
   verified. If the form rejects it, stop and report the validation message with
   secrets hidden. An HTTPS tunnel requires an explicit callback configuration
   change and is not implemented here. Do not substitute an unrelated website.
   Leave webhooks empty, review the terms, and create the app if the URL is accepted.

4. Test login:

```powershell
.\.venv\Scripts\python.exe login.py
```

Enter the App ID and app secret **in PowerShell on your PC**. Secret input is
hidden. The browser opens FYERS' official login page; enter your account login
details there. Keep PowerShell open. Following authorization, the callback returns
to the local listener, then PowerShell reports whether token exchange succeeded.
The listener binds only to 127.0.0.1 and stops after login or a ten-minute timeout.
The browser and this command must run on the same Windows PC.

5. To log in and immediately scan, supply your verified constituent file and
   session date (replace the example date with your desired completed session):

```powershell
.\.venv\Scripts\python.exe login.py --symbols .\nifty500.txt --date 2026-10-30
```

The symbol file contains one FYERS equity symbol per line. Login passes the token
to the scanner's process environment without printing it or storing it in a file.
The app secret is not forwarded to the scanner. Each new invocation requires login;
login-only mode discards the token when it exits. Avoid sharing callback URLs,
tokens, SDK logs, or credentials. Local callback and exchange tests use simulated
FYERS responses; actual account authorization remains unverified until you try it.

The authentication endpoint and payload follow the official FYERS 3.1.7 Python
SDK distributed on [PyPI](https://pypi.org/project/fyers-apiv3/3.1.7/).
