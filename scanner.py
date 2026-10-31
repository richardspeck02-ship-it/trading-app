"""Scan the first three NSE five-minute candles. All times are Asia/Kolkata."""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as daytime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
STARTS = (daytime(9, 15), daytime(9, 20), daytime(9, 25))


def progress(message):
    stamp = datetime.now(IST).strftime("%H:%M:%S IST")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self):
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Candle timestamps must include a timezone")
        if not all(math.isfinite(x) and x > 0 for x in
                   (self.open, self.high, self.low, self.close)):
            raise ValueError("OHLC prices must be finite and positive")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Invalid OHLC range")


def evaluate(candles, session, now):
    """Return match/no_match/incomplete/pending; never use an open candle."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must include a timezone")
    if now < datetime.combine(session, daytime(9, 30), IST):
        return "pending", None
    selected = {}
    for candle in candles:
        local = candle.timestamp.astimezone(IST)
        if local.date() == session and local.time() in STARTS:
            if local.time() in selected:
                raise ValueError("Duplicate opening candle")
            selected[local.time()] = candle
    if len(selected) != 3:
        return "incomplete", None
    first, second, third = (selected[t] for t in STARTS)
    matches = all(c.low >= first.low and c.high <= first.high for c in (second, third))
    return "match" if matches else "no_match", {"first_high": first.high, "first_low": first.low}


def load_csv(path):
    grouped = {}
    with open(path, newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            symbol = row["symbol"].strip()
            if not symbol:
                raise ValueError("Empty symbol")
            candle = Candle(datetime.fromisoformat(row["timestamp"]),
                            *(float(row[k]) for k in ("open", "high", "low", "close")))
            grouped.setdefault(symbol, []).append(candle)
    if not grouped:
        raise ValueError("Candle CSV is empty")
    return grouped


def load_symbols(path):
    symbols = [line.strip() for line in Path(path).read_text().splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Symbol list must be nonempty and contain no duplicates")
    if any(not re.fullmatch(r"NSE:[A-Z0-9&-]+-(EQ|BE|RR)", s) for s in symbols):
        raise ValueError("Use FYERS NSE symbols with EQ, BE, or RR series, e.g. NSE:RELIANCE-EQ")
    return symbols


def fetch_fyers(symbols, session):
    # Import only in connected mode so offline scanning has no dependencies.
    progress("Loading FYERS client...")
    from fyers_apiv3 import fyersModel

    client_id = os.environ.get("FYERS_CLIENT_ID")
    token = os.environ.get("FYERS_ACCESS_TOKEN")
    if not client_id or not token:
        raise ValueError("Set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN securely before connected scanning")
    progress("Initializing FYERS client...")
    client = fyersModel.FyersModel(client_id=client_id, token=token, is_async=False, log_path="")
    grouped, errors = {}, {}
    started = time.monotonic()
    progress(f"Scanning {len(symbols)} symbols for {session.isoformat()}; five-minute candles.")
    for index, symbol in enumerate(symbols, 1):
        request_started = time.monotonic()
        label = f"[{index}/{len(symbols)}] {symbol}"
        progress(f"{label}: fetching candles...")
        try:
            response = client.history(data={
                "symbol": symbol, "resolution": "5", "date_format": "1",
                "range_from": session.isoformat(), "range_to": session.isoformat(), "cont_flag": "1",
            })
            if response.get("s") != "ok":
                # Avoid printing SDK responses, which can contain account information.
                errors[symbol] = "FYERS history request failed"
                progress(f"{label}: FAILED - FYERS history request rejected.")
                continue
            grouped[symbol] = [Candle(datetime.fromtimestamp(c[0], IST),
                                      *(float(x) for x in c[1:5]))
                               for c in response["candles"]]
            progress(f"{label}: received {len(grouped[symbol])} candles "
                     f"({time.monotonic() - request_started:.1f}s).")
        except Exception:
            errors[symbol] = "Could not fetch or parse FYERS candles"
            progress(f"{label}: FAILED - could not fetch or parse candles.")
        finally:
            progress(f"Completed {index}/{len(symbols)}; fetched {len(grouped)}; "
                     f"errors {len(errors)}; elapsed {time.monotonic() - started:.0f}s.")
            time.sleep(0.5)
    progress("Fetching complete. Checking the opening three candles...")
    return grouped, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="Offline candles: symbol,timestamp,open,high,low,close")
    source.add_argument("--fyers", action="store_true", help="Read FYERS historical candles")
    parser.add_argument("--symbols", help="One FYERS symbol per line; required for FYERS mode")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="NSE session date YYYY-MM-DD")
    args = parser.parse_args(argv)
    now = datetime.now(IST)
    if args.date.weekday() >= 5:
        parser.error("Choose a weekday NSE session (exchange holidays are not inferred)")
    if now < datetime.combine(args.date, daytime(9, 30), IST):
        parser.error("Wait until 09:30 IST on the session date so all three candles are closed")
    try:
        if args.fyers:
            if not args.symbols:
                parser.error("--fyers requires --symbols with current NIFTY 500 constituents")
            grouped, errors = fetch_fyers(load_symbols(args.symbols), args.date)
        else:
            progress(f"Reading candle CSV for {args.date.isoformat()}...")
            grouped, errors = load_csv(args.csv), {}
        rows = []
        for symbol, candles in sorted(grouped.items()):
            try:
                status, levels = evaluate(candles, args.date, now)
                rows.append({"symbol": symbol, "status": status, **(levels or {})})
                if status == "match":
                    progress(f"MATCH: {symbol}")
                elif status == "incomplete":
                    progress(f"INCOMPLETE: {symbol} - missing opening candle data.")
            except ValueError:
                errors[symbol] = "Invalid or duplicate candle data"
                progress(f"FAILED: {symbol} - invalid or duplicate candle data.")
        progress(f"Scan complete: {sum(r['status'] == 'match' for r in rows)} matches; "
                 f"{sum(r['status'] == 'incomplete' for r in rows)} incomplete; "
                 f"{len(errors)} errors. Printing JSON results.")
        print(json.dumps({"session": args.date.isoformat(), "timezone": "Asia/Kolkata",
                          "matches": [r for r in rows if r["status"] == "match"],
                          "results": rows, "errors": errors}, indent=2))
        return 1 if errors or any(r["status"] == "incomplete" for r in rows) else 0
    except (ValueError, OSError, KeyError, ImportError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
