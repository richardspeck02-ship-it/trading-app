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
    return "match" if matches else "no_match", {
        "first_high": first.high, "first_low": first.low,
        "second_high": second.high, "second_low": second.low,
        "third_high": third.high, "third_low": third.low,
    }


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


def history_error(response):
    """Describe only whitelisted categories and numeric broker codes."""
    if not isinstance(response, dict):
        return "FYERS returned an unexpected response", False
    code = response.get("code")
    suffix = f" (FYERS code {code})" if type(code) is int and abs(code) <= 1000000 else ""
    message = response.get("message", "")
    message = message.lower() if isinstance(message, str) else ""
    limited = (type(code) is int and code in (429, -429)) or any(
        phrase in message for phrase in ("rate limit", "rate-limit", "too many requests", "request limit"))
    if limited:
        category = "FYERS reported a rate limit"
    elif "symbol" in message and any(word in message for word in ("invalid", "unknown", "not found")):
        category = "FYERS reported an invalid or unknown symbol"
    elif any(word in message for word in ("token", "unauthorized", "authentication")):
        category = "FYERS reported an authentication error"
    else:
        category = "FYERS history request rejected"
    return category + suffix, limited


class HistoryRateLimit(Exception):
    pass


def history_with_backoff(client, data, label):
    for attempt in range(3):
        response = client.history(data=data)
        if isinstance(response, dict) and response.get("s") == "ok":
            return response
        detail, limited = history_error(response)
        if not limited:
            return response
        if attempt == 2:
            raise HistoryRateLimit(detail)
        delay = 30 * (attempt + 1)
        progress(f"{label}: {detail}; pausing {delay}s before retry {attempt + 1}/2.")
        time.sleep(delay)


def opening_request(symbol, session):
    start = datetime.combine(session, STARTS[0], IST)
    # End before 09:30 so the 09:30 candle is not requested.
    end = datetime.combine(session, daytime(9, 30), IST) - timedelta(seconds=1)
    return {"symbol": symbol, "resolution": "5", "date_format": "0",
            "range_from": str(int(start.timestamp())),
            "range_to": str(int(end.timestamp())), "cont_flag": "1"}


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
    progress(f"Scanning {len(symbols)} symbols for {session.isoformat()}; "
             "only 09:15, 09:20 and 09:25 IST five-minute candles.")
    for index, symbol in enumerate(symbols, 1):
        request_started = time.monotonic()
        label = f"[{index}/{len(symbols)}] {symbol}"
        progress(f"{label}: fetching candles...")
        try:
            response = history_with_backoff(client, opening_request(symbol, session), label)
            if not isinstance(response, dict) or response.get("s") != "ok":
                # Avoid printing SDK responses, which can contain account information.
                detail, _ = history_error(response)
                errors[symbol] = detail
                progress(f"{label}: FAILED - {detail}.")
                continue
            candles = [Candle(datetime.fromtimestamp(c[0], IST), *(float(x) for x in c[1:5]))
                       for c in response["candles"]]
            grouped[symbol] = [c for c in candles if c.timestamp.date() == session
                               and c.timestamp.time() in STARTS]
            progress(f"{label}: received {len(grouped[symbol])} candles "
                     f"({time.monotonic() - request_started:.1f}s).")
        except HistoryRateLimit as exc:
            errors[symbol] = str(exc)
            for remaining in symbols[index:]:
                errors[remaining] = "Not fetched: scan stopped after repeated FYERS rate-limit responses"
            progress(f"{label}: {exc}. Stopping requests; "
                     f"{len(symbols) - index} remaining symbols not fetched.")
            break
        except Exception:
            errors[symbol] = "Could not fetch or parse FYERS candles"
            progress(f"{label}: FAILED - could not fetch or parse candles.")
        finally:
            progress(f"Completed {index}/{len(symbols)}; fetched {len(grouped)}; "
                     f"errors {len(errors)}; elapsed {time.monotonic() - started:.0f}s.")
            time.sleep(0.5)
    progress("Fetching complete. Checking the opening three candles...")
    return grouped, errors


def save_report(directory, report, grouped):
    """Write a distinct run folder, preserving earlier runs and failed coverage."""
    folder = Path(directory) / (report["session"] + "_" + datetime.now(IST).strftime("%Y%m%d_%H%M%S_%f"))
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (folder / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["status", "stock"])
        for status, symbols in summary_groups(report).items():
            writer.writerows((status, stock_name(s)) for s in symbols)
    (folder / "watchlist.txt").write_text(
        "".join(r["symbol"] + "\n" for r in report["matches"]), encoding="utf-8")
    retry = sorted(set(report["errors"]) | {r["symbol"] for r in report["results"]
                                           if r["status"] == "incomplete"})
    (folder / "failed-symbols.txt").write_text("".join(s + "\n" for s in retry), encoding="utf-8")
    with (folder / "matches.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ["symbol", "status", "first_high", "first_low", "second_high",
                  "second_low", "third_high", "third_low"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report["matches"])
    with (folder / "candles.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["symbol", "timestamp", "open", "high", "low", "close"])
        for symbol, candles in sorted(grouped.items()):
            for candle in candles:
                writer.writerow([symbol, candle.timestamp.isoformat(), candle.open,
                                 candle.high, candle.low, candle.close])
    progress(f"Saved results to {folder.resolve()}")
    progress("watchlist.txt contains local matching symbols; the FYERS account watchlist has not been changed.")
    if not report["complete"]:
        progress("PARTIAL RESULTS: failed or incomplete stocks are listed in failed-symbols.txt.")
    return folder


def stock_name(symbol):
    name = symbol.split(":", 1)[-1]
    if name.rsplit("-", 1)[-1] in ("EQ", "BE", "RR"):
        name = name.rsplit("-", 1)[0]
    return name


def summary_groups(report):
    groups = {"match": set(), "no-match": set(), "error": set(report["errors"])}
    for row in report["results"]:
        key = {"match": "match", "no_match": "no-match"}.get(row["status"], "error")
        groups[key].add(row["symbol"])
    for key in ("match", "no-match"):
        groups[key].difference_update(groups["error"])
    return {key: sorted(symbols) for key, symbols in groups.items()}


def print_summary(report):
    for status, symbols in summary_groups(report).items():
        print(f"\n{status.upper()}")
        print("\n".join(stock_name(s) for s in symbols) if symbols else "(none)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="Offline candles: symbol,timestamp,open,high,low,close")
    source.add_argument("--fyers", action="store_true", help="Read FYERS historical candles")
    source.add_argument("--report", help="Print a saved report.json as names and statuses; no API requests")
    parser.add_argument("--symbols", help="One FYERS symbol per line; required for FYERS mode")
    parser.add_argument("--date", type=date.fromisoformat, help="NSE session date YYYY-MM-DD")
    parser.add_argument("--output-dir", help="Save report, candles, matching symbols, and failed symbols in a new run folder")
    args = parser.parse_args(argv)
    if args.report:
        try:
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
            print_summary(report)
            return 0 if report.get("complete", False) else 1
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            parser.error("Could not read a valid scanner report.json")
    if args.date is None:
        parser.error("--date is required for a candle scan")
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
                 f"{len(errors)} errors.")
        complete = not errors and all(r["status"] != "incomplete" for r in rows)
        report = {"session": args.date.isoformat(), "timezone": "Asia/Kolkata",
                  "complete": complete,
                  "matches": [r for r in rows if r["status"] == "match"],
                  "results": rows, "errors": errors}
        if args.output_dir:
            save_report(args.output_dir, report, grouped)
        print_summary(report)
        return 0 if complete else 1
    except (ValueError, OSError, KeyError, ImportError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
