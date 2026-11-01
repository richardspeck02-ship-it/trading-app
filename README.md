# Opening-range stock scanner

The only pattern rule is that the **second fifteen-minute candle stays inside
(or equal to) the first candle's high and low**, including wicks. Candle windows
are 09:15–09:30 and 09:30–09:45 Asia/Kolkata. There is no condition on the first
candle's close or the previous session's range. No third candle is required.
This scanner reads data and does not place orders.

## Run offline

Python 3.12 is supported. The full test suite requires the installed dependencies for login tests.

```sh
python -m unittest discover -s tests -v
python scanner.py --csv examples/candles.csv --date 2026-10-30
```

The example uses synthetic data: DEMO_MATCH qualifies and DEMO_REJECT does not.
Fifteen-minute CSV timestamps must include a timezone and represent candle
**opening** times. Requests before 09:45 IST on the session date are rejected.
Missing either opening candle is incomplete, never a non-match; incomplete data
or request errors return a nonzero exit status. Closed days (weekends and exchange
holidays) do not supply these opening bars and therefore cannot produce matches.
Special sessions need the same 09:15/09:30 starts to qualify. If publication is
delayed, rerun after 09:45.

### Offline CSV format

CSV columns are `symbol,timestamp,open,high,low,close,interval`. Use `15` for
fifteen-minute bars. No daily stock or index reference data is needed. Daily (`D`)
rows in older saved files are ignored. Five-minute rows (including legacy rows
with no `interval` column, which default to `5`) are not treated as fifteen-minute
bars; fetch fresh fifteen-minute history instead.

New reports identify `interval: "15"`, `candle_count: 2` and `pattern: "inside_only"`.
Saved `candles.csv` files retain opening bars and intervals for offline reruns.
Reports and match CSV exports include the first/second highs and lows and the
first close for inspection; the close is not a filter. Reading an old
`report.json` only prints its saved results, so rerun the candle data to evaluate
an old scan using the current rule.

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
`NSE:RELIANCE-EQ`. A user-supplied snapshot is bundled as `nifty500.txt` (see below); the scanner
checks every symbol supplied but cannot establish index membership. Use date-appropriate membership
for historical sessions.

## Live browser scanner (read-only)

Update the project and install the streaming SDK compatibility dependency:

```powershell
git pull
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
```

Try the browser interface now with synthetic prices (no FYERS login required):

```powershell
.\.venv312\Scripts\python.exe live_app.py --demo
```

For a real session, stop demo mode with Ctrl+C, then run before **09:15 IST** on a
trading day:

```powershell
.\.venv312\Scripts\python.exe login.py --live --symbols .\nifty100.txt
```

Complete the normal FYERS login. The dashboard opens at `http://127.0.0.1:8765`
on your Windows PC. Keep PowerShell open. No trading app or order-placement
permission is needed. There is no execution code in this feature.

Live mode is limited to 100 symbols for the user's Standard account. The SDK's
5,000-symbol implementation capacity does not establish account entitlement.
The uploaded NIFTY 500 list remains available for historical scans; live mode
rejects lists over 100 before login or subscription, without truncating them.
Other active subscriptions may consume part of the account-wide allowance.
The page shows the selected universe and actual received-price coverage.

- **Prices:** streamed from FYERS; the page polls the local server once per second.
- **Provisional patterns:** apply only the inside-candle rule. Built from prices
  that have a valid last-trade
  timestamp, using the first two 15-minute windows. Ticks may be incomplete,
  coalesced, late, or missed during disconnects; these are previews, not official OHLC.
- **Confirmed matches:** after 09:45:03 IST, one paced worker requests the official
  opening candles, prioritizing streaming candidates. It then reconciles the rest
  of the universe to find matches that missing ticks may have hidden. One request
  finishes before the worker waits at least one second before the next. This is a
  conservative pacing choice, not verification of current FYERS quotas. There is
  no guarantee every stock is confirmed by 09:50.
- **Missing/late data:** late starts need historical backfill. Errors and incomplete
  candles are shown explicitly; choose “All stocks” to inspect them. A rate-limit
  response stops confirmation requests rather than hammering the API. Streaming
  previews remain separate. Restart later to retry failed confirmations.
- **Exports:** browser buttons download confirmed matches as CSV or FYERS-style
  symbol text. Unconfirmed, rejected, and incomplete stocks are excluded. This is
  a custom reference export; compatibility with FYERS' watchlist-import format is
  unverified. It does not modify a FYERS account watchlist.

Stream-built candidates can change on confirmation. After confirmation, the
opening pattern is fixed for the session while prices continue updating. Prices
without updates for 20 seconds are marked stale (including inactive stocks).
The app handles only the current session; restart for a new trading date.
Weekends/holidays do not produce a normal live session. Demo data is clearly
labelled. Automated local tests and the synthetic dashboard can run without
broker access; real-market streaming remains unverified until an authenticated
market-session run. SDK diagnostic logs may contain market/account details;
do not share those logs or commit them.

`setuptools==80.9.0` retains `pkg_resources`, which the pinned FYERS streaming SDK
imports. Newer releases that remove it cannot be substituted without updating
the SDK integration. Its upstream deprecation warning is expected.

```sh
.venv/bin/python scanner.py --fyers --symbols /path/to/nifty500.txt --date YYYY-MM-DD
```

FYERS mode makes sequential fifteen-minute history requests, with a delay between
symbols. A full universe takes several minutes. Confirm current API permissions,
usage limits, and symbol mappings with FYERS. Failed symbols are reported without
printing raw broker responses. Connected operation requires account credentials,
network access to FYERS, and a verified universe; it is not validated by the
offline example. Scheduled scans, a web interface, and trade execution are future work.

### Progress logs

The scanner prints timestamped progress immediately to PowerShell: client startup,
the current symbol and total count, received candle counts, request failures,
elapsed time, matching stocks, and the final summary. The SDK's current history
call has no request timeout: if it stalls, the last `fetching candles...` line
identifies the pending symbol. Logs do not imply a stalled request has finished.
Progress goes to stderr; stdout shows MATCH, NO-MATCH, and ERROR groups with stock
names only. Detailed JSON is saved in the run folder. No tokens,
app secrets, raw broker responses, or exception contents are printed by these logs.
Existing runs must finish or be restarted to use updated code.

History rejections now include numeric FYERS error codes when present and safe
categories rather than raw response text. Generic failures are not assumed to be
rate limits. Explicit rate-limit responses receive at most two retries, after
30 and 60 seconds. If still limited, requests stop and remaining symbols are
reported as not fetched. These delays are a retry policy, not a claim about
FYERS' published quotas. Successful symbols remain available in the final output.

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

## Historical pattern scan and watchlist output

Requests now use epoch bounds from 09:15:00 through 09:44:59 IST, requesting only
the fifteen-minute candle starts at 09:15 and 09:30. Any extra candles returned are filtered
out. This requires one opening-history request per stock, plus any rate-limit retries.
The inclusive inside-candle rule applies to historical sessions. This is a
historical pattern scan; returns or profit cannot be calculated without entry,
exit, sizing, and execution rules.

For the last trading session before Saturday 2026-10-31, use the session
2026-10-30, for which earlier user runs received market data:

```powershell
git pull
.\.venv312\Scripts\python.exe login.py --symbols .\nifty500.txt --date 2026-10-30
```

After login the scanner creates a distinct timestamped folder under `results`:

- `report.json`: matches, every evaluated status, errors, and a `complete` flag.
- `matches.csv`: matching stocks and the high/low of each of the two fifteen-minute candles.
- `watchlist.txt`: one matching FYERS symbol per line, for reference.
- `candles.csv`: fetched opening candles, usable for offline reruns.
- `failed-symbols.txt`: failed/incomplete stocks for a targeted retry.

The terminal prints the exact output folder. Results from failed/incomplete runs
are marked partial; those stocks are never treated as non-matches. A new run does
not replace prior files. To retry only failed stocks, use that run's
`failed-symbols.txt` as `--symbols` (only if it is nonempty). To rerun calculations
without consuming API calls:

```powershell
.\.venv312\Scripts\python.exe scanner.py --csv .\results\YOUR_RUN_FOLDER\candles.csv --date 2026-10-30
```

An offline rerun covers only the symbols present in that candle file; refer to the
original report for missing coverage. An empty candle file cannot validate a scan.
Historical index membership is not independently established by the uploaded list.

**FYERS watchlist limitation:** the pinned official SDK has no watchlist creation
or modification method. Current online API documentation could not be checked
here (HTTP 403), and no supported watchlist-write endpoint has been verified.
The program does not change your FYERS account watchlist. `watchlist.txt` is a
local list, not a verified FYERS import format. Use the results to add stocks
through FYERS' UI; its import capability/format needs checking before automating
that step. No browser session cookies or undocumented account endpoints are used.


## Uploaded constituent snapshot

`nifty500.txt` was converted from the user-supplied `ind_nifty500list (1).csv`
on 2026-10-31. The upload contains 501 unique entries: 496 EQ, two BE, and
three RR. All are retained using `NSE:<Symbol>-<Series>`, including punctuation.
The filename is the index name; it is not an assertion of exactly 500 rows.
The CSV does not identify an effective membership date. This is a supplied
snapshot, not an automatically refreshed or independently verified index list.
FYERS' public symbol-master download returned HTTP 403 during preparation,
so these mappings have not been verified against that master. History failures
remain explicit errors; no entries were silently dropped or converted to EQ.
The loader accepts EQ, BE, and RR series. Earlier instructions to supply only
EQ symbols are examples for ordinary equities, not a requirement to remove
other series from this uploaded snapshot.

Use a date-appropriate constituent list for historical scans. For a first
connectivity check with this supplied snapshot:

```powershell
.\.venv312\Scripts\python.exe login.py --symbols .\nifty500.txt --date 2026-10-30
```

This scans the previous weekday and does not establish historical index membership.


## Prepare the NIFTY 100 universe

The default live symbol file is `nifty100.txt`, converted from the user-uploaded
`ind_nifty100list.csv` on 2026-10-31. The supplied CSV contains exactly 100 unique
EQ-series symbols; all 100 were retained. No DUMMY placeholder is present.
The CSV has no effective membership date; this is a supplied snapshot, not an
automatically refreshed or independently verified point-in-time universe.
FYERS subscription validity still requires the authenticated runtime check.

Download the constituent CSV from the official
[NIFTY 100 page](https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-100).
For a future refresh, either upload that CSV in chat for conversion, or convert
it locally to a new output filename (the bundled file is not overwritten):

```powershell
.\.venv312\Scripts\python.exe universe.py ".\ind_nifty100list.csv" --output nifty100-refreshed.txt
.\.venv312\Scripts\python.exe login.py --live
```

The bundled list is ready to use with `login.py --live`; no conversion is needed
for this snapshot. To use a refreshed file, pass `--symbols nifty100-refreshed.txt`.
Adjust the CSV filename to match your download. The importer preserves symbols
and series, rejects duplicates or lists over 100, and refuses to overwrite an
existing output. It does not independently verify index membership or FYERS symbol
availability; those depend on the official input and broker responses. A file
with more than 100 entries requires a deliberate selection within the account
limit; no rows are silently discarded. Starting other broker streams can reduce
the allowance available to this scanner.


## Names-only scan summaries

Historical scans now print three groups: **MATCH**, **NO-MATCH**, and **ERROR**,
containing stock names without exchange/series prefixes or candle prices.
Incomplete candle data belongs in ERROR, never NO-MATCH. New saved runs include
`summary.csv` with just `status,stock`; detailed candle calculations remain in
`report.json`, `matches.csv`, and `candles.csv` for audit.

An existing report can be displayed without broker login or new API requests:

```powershell
.\.venv312\Scripts\python.exe scanner.py --report .\results\YOUR_RUN_FOLDER\report.json
```

This works with reports generated before the summary update too. Live browser
prices and its exports are unaffected by this terminal-summary change.
