"""Read-only local streaming scanner and browser dashboard."""

import argparse
import csv
from datetime import datetime, time as daytime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import threading
import time
import webbrowser

import requests

from scanner import (Candle, INTERVAL, IST, OPENING_END, SESSION_INDEX, daily_request, evaluate, history_error,
                     opening_request, parse_history, previous_candle, previous_session, progress)
from universe import load_live_symbols, validate_live_symbols


class LiveState:
    def __init__(self, symbols, session, demo=False, universe="Custom watchlist"):
        self.symbols = symbols
        self.session = session
        self.demo = demo
        self.universe = universe
        self.lock = threading.RLock()
        self.records = {s: {"symbol": s, "price": None, "updated": None, "trade_time": None, "bars": {}}
                        for s in symbols}
        self.official = {}
        self.previous_session = None
        self.previous = {}
        self.feed = "Starting"
        self.notice = "Start before 09:15 IST for opening-candle previews. Stream-built patterns are provisional."
        self.started = datetime.now(IST)
        self.history_paused = False
        self.stream_errors = []
        self.subscription_acks = 0

    def tick(self, message, received=None):
        if not isinstance(message, dict) or message.get("symbol") not in self.records:
            return
        received = received or datetime.now(IST)
        try:
            price = float(message["ltp"])
            if not math.isfinite(price) or price <= 0:
                return
        except (KeyError, TypeError, ValueError):
            return
        try:
            trade = datetime.fromtimestamp(float(message["last_traded_time"]), IST)
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            trade = None
        with self.lock:
            record = self.records[message["symbol"]]
            if trade and record["trade_time"] and trade < record["trade_time"]:
                return
            record["price"] = price
            record["updated"] = received.isoformat()
            self.feed = "Receiving prices"
            # Arrival/feed times can include stale trade prices. Require the actual
            # last-trade timestamp before building a candle from this message.
            if trade is None:
                return
            if trade.date() != self.session or trade > received + timedelta(seconds=5):
                return
            record["trade_time"] = trade
            minute = (trade.hour * 60 + trade.minute) - (9 * 60 + 15)
            if not 0 <= minute < 30:
                return
            bucket = minute // 15
            bar = record["bars"].get(bucket)
            if bar is None:
                record["bars"][bucket] = [price, price, price, price]
            else:
                bar[1] = max(bar[1], price)
                bar[2] = min(bar[2], price)
                bar[3] = price

    def preview(self, record, now):
        bars = record["bars"]
        if now < datetime.combine(self.session, daytime(9, 30), IST) or not all(i in bars for i in (0, 1)):
            return "Waiting for opening candles"
        first = bars[0]
        prior = self.previous.get(record["symbol"])
        if prior is None:
            return "Waiting for previous-session range"
        if not (first[3] > prior.high or first[3] < prior.low):
            return "No opening breakout (provisional)"
        second = bars[1]
        if second[2] < first[2] or second[1] > first[1]:
            return "Outside range (provisional)"
        return ("Candidate (provisional)" if now >= datetime.combine(self.session, OPENING_END, IST)
                else "Inside so far (2nd forming, provisional)")

    def apply_history(self, symbol, candles, now):
        status, levels = evaluate(candles, self.session, now, self.previous_session)
        with self.lock:
            self.official[symbol] = {"status": status, "levels": levels}

    def snapshot(self, now=None):
        now = now or datetime.now(IST)
        with self.lock:
            rows = []
            for symbol, record in self.records.items():
                official = self.official.get(symbol)
                status = self.preview(record, now)
                levels = None
                if official:
                    status = {"match": "Confirmed match", "no_match": "Confirmed no match",
                              "incomplete": "Incomplete historical candles", "error": "Verification failed"}.get(official["status"], "Waiting")
                    levels = official.get("levels")
                elif record.get("subscription_error"):
                    status = "Subscription error"
                if levels is None and 0 in record["bars"]:
                    levels = {"first_high": record["bars"][0][1], "first_low": record["bars"][0][2],
                              "first_close": record["bars"][0][3]}
                    if symbol in self.previous:
                        levels.update(previous_high=self.previous[symbol].high,
                                      previous_low=self.previous[symbol].low,
                                      previous_session=self.previous_session.isoformat() if self.previous_session else None)
                stale = record["updated"] is None or (now - datetime.fromisoformat(record["updated"])).total_seconds() > 20
                rows.append({"symbol": symbol, "price": record["price"], "updated": record["updated"],
                             "stale": stale, "status": status, "levels": levels,
                             "error": official.get("error") if official else record.get("subscription_error")})
            return {"session": self.session.isoformat(), "time": now.isoformat(), "demo": self.demo,
                    "interval": INTERVAL, "candle_count": 2,
                    "previous_session": self.previous_session.isoformat() if self.previous_session else None,
                    "feed": self.feed, "notice": self.notice, "universe": self.universe, "total": len(rows),
                    "seen": sum(r["price"] is not None for r in rows),
                    "verified": sum(o["status"] in ("match", "no_match") for o in self.official.values()),
                    "confirmed": sum(r["status"] == "Confirmed match" for r in rows),
                    "subscription_acks": self.subscription_acks,
                    "stream_errors": list(self.stream_errors), "rows": rows}


    def export(self, kind):
        rows = [r for r in self.snapshot()["rows"] if r["status"] == "Confirmed match"]
        if kind == "txt":
            return "".join(r["symbol"] + "\n" for r in rows).encode()
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["session", "symbol", "last_price", "price_updated", "first_high", "first_low",
                         "second_high", "second_low",
                         "first_close", "previous_session", "previous_high", "previous_low"])
        for r in rows:
            writer.writerow([self.session.isoformat(), r["symbol"], r["price"], r["updated"],
                             *(r["levels"].get(k) for k in ("first_high", "first_low", "second_high", "second_low", "first_close", "previous_session", "previous_high", "previous_low"))])
        return stream.getvalue().encode("utf-8-sig")


def stream_error_details(message, symbols):
    """Expose SDK categories/codes and known symbols, never raw response text."""
    invalid = []
    code = message.get("code") if isinstance(message, dict) else None
    text = message.get("message", "") if isinstance(message, dict) else str(message)
    text = text.lower() if isinstance(text, str) else ""
    if isinstance(message, dict) and isinstance(message.get("invalid_symbols"), list):
        invalid = [s for s in message["invalid_symbols"] if isinstance(s, str) and s in symbols]
    if invalid:
        detail = f"Invalid subscription symbols ({len(invalid)}): " + ", ".join(invalid[:10])
    elif code == 11001 or "authentication failed" in text or "valid token" in text or "token is expired" in text:
        detail = "Stream authentication failed or token expired"
    elif code == 11011 or "subscription failed" in text:
        detail = "FYERS rejected a subscription"
    elif "rate limit" in text or "too many requests" in text or "429" in text:
        detail = "Rate limit reported during stream setup"
    elif "5000" in text or "symbol limit" in text:
        detail = "Stream symbol limit reported"
    elif "handshake" in text or "403" in text:
        detail = "WebSocket handshake or access refused"
    elif "certificate" in text or "ssl" in text:
        detail = "WebSocket TLS/certificate error"
    elif isinstance(message, dict) and message.get("type") == "sub":
        detail = "Symbol conversion or subscription setup failed"
    else:
        detail = "Stream transport or SDK error"
    if type(code) is int and abs(code) <= 1000000:
        detail += f" (FYERS code {code})"
    return detail, invalid


def verify_worker(state, stop, client_id, token):
    """Prioritize streaming candidates, then reconcile missing/noncandidate stocks."""
    while not stop.wait(1):
        now = datetime.now(IST)
        if now.date() != state.session:
            with state.lock:
                state.notice = "Session ended. Restart the app for the new trading date."
            return
        with state.lock:
            remaining = [s for s in state.symbols if s not in state.official]
            if not remaining:
                return
            remaining.sort(key=lambda s: "Candidate" not in state.preview(state.records[s], now))
            symbol = remaining[0]
        try:
            # Each iteration performs at most one request; stop.wait(1) above
            # keeps daily-reference and opening requests under the same pacing.
            if state.previous_session is None:
                request = daily_request(SESSION_INDEX, state.session)
                phase = "reference"
            elif symbol not in state.previous:
                request = daily_request(symbol, state.session, state.previous_session)
                phase = "daily"
            elif now < datetime.combine(state.session, daytime(9, 45, 3), IST):
                # Load all previous ranges before waiting for opening bars.
                pending = [s for s in remaining if s not in state.previous]
                if not pending:
                    continue
                symbol = pending[0]
                request = daily_request(symbol, state.session, state.previous_session)
                phase = "daily"
            else:
                request = opening_request(symbol, state.session)
                phase = "opening"
            progress(f"Checking {phase} candles: {symbol}")
            with requests.get("https://api-t1.fyers.in/data/history",
                              params=request,
                              headers={"Authorization": f"{client_id}:{token}", "Content-Type": "application/json", "version": "3"},
                              timeout=15, allow_redirects=False) as response:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                detail, limited = history_error(data)
                if response.status_code == 429 or limited:
                    with state.lock:
                        state.history_paused = True
                        state.notice = "FYERS rate limit: confirmation paused. Streaming previews remain provisional. Restart later to retry."
                    progress("FYERS rate limit: stopping confirmation requests.")
                    return
                if response.status_code != 200:
                    raise ValueError(detail)
                candles = parse_history(data, INTERVAL if phase == "opening" else "D")
                if phase == "reference":
                    previous = previous_session(candles, state.session)
                    if previous is None:
                        raise ValueError("Previous exchange session unavailable")
                    with state.lock:
                        state.previous_session = previous
                elif phase == "daily":
                    prior = previous_candle(candles, state.previous_session)
                    with state.lock:
                        if prior is None:
                            state.official[symbol] = {"status": "incomplete", "error": "Previous-session daily candle missing"}
                        else:
                            state.previous[symbol] = prior
                else:
                    state.apply_history(symbol, [state.previous[symbol]] + candles, now)
        except Exception:
            with state.lock:
                if state.previous_session is None:
                    state.notice = "Previous exchange session could not be verified. No matches exported; restart to retry."
                    for item in state.symbols:
                        state.official[item] = {"status": "error", "error": "Previous exchange session unavailable"}
                    return
                state.official[symbol] = {"status": "error", "error": "Could not verify opening or previous-session candles; no match exported."}


def connect_feed(state, stop, client_id, token):
    validate_live_symbols(state.symbols)
    from fyers_apiv3.FyersWebsocket.data_ws import FyersDataSocket

    def on_open():
        with state.lock:
            state.feed = "Subscribing; waiting for prices"
        for start in range(0, len(state.symbols), 100):
            if stop.is_set():
                return
            progress(f"Requesting stream batch {start // 100 + 1}: symbols {start + 1}-{min(start + 100, len(state.symbols))}/{len(state.symbols)}")
            socket.subscribe(symbols=state.symbols[start:start + 100], data_type="SymbolUpdate")
            if stop.wait(2):
                return
        progress("Subscription requests sent. Check actual received-symbol coverage in the dashboard.")

    def on_message(message):
        if isinstance(message, dict) and message.get("s") == "ok" and message.get("type") == "sub":
            with state.lock:
                state.subscription_acks += 1
            progress("FYERS acknowledged a subscription request.")
        if isinstance(message, list):
            for row in message:
                state.tick(row)
        else:
            state.tick(message)

    def on_error(message):
        detail, invalid = stream_error_details(message, state.records)
        with state.lock:
            state.feed = "Stream error; some subscriptions or prices may be unavailable"
            prefix = "Weekend: no regular NSE session expected. " if state.session.weekday() >= 5 else ""
            state.notice = prefix + detail + ". Choose All stocks to inspect coverage."
            state.stream_errors.append({"time": datetime.now(IST).isoformat(), "detail": detail})
            state.stream_errors[:] = state.stream_errors[-10:]
            for symbol in invalid:
                state.records[symbol]["subscription_error"] = detail
        progress(detail)

    def on_close(_):
        with state.lock:
            state.feed = "Disconnected"
            state.notice = "Stream disconnected. Wait for reconnect; missing ticks require candle confirmation."

    socket = FyersDataSocket(access_token=f"{client_id}:{token}", log_path="", litemode=False,
                             write_to_file=True, reconnect=True, reconnect_retry=5,
                             on_message=on_message, on_connect=on_open, on_error=on_error, on_close=on_close)
    socket.connect()
    return socket


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get("Host") != "127.0.0.1:8765":
                self.send_error(400)
                return
            if self.path == "/":
                self.respond(Path(__file__).with_name("dashboard.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.respond(json.dumps(state.snapshot()).encode(), "application/json")
            elif self.path in ("/matches.csv", "/watchlist.txt"):
                kind = "csv" if self.path.endswith("csv") else "txt"
                self.respond(state.export(kind), "text/csv" if kind == "csv" else "text/plain",
                             f"matches-{state.session}.{kind}")
            else:
                self.send_error(404)

        def respond(self, body, content_type, filename=None):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)
    return Handler


def demo_state():
    today = datetime.now(IST).date()
    state = LiveState(["NSE:DEMO-EQ", "NSE:OUTSIDE-EQ", "NSE:WAITING-EQ"], today, demo=True)
    state.feed = "DEMO — synthetic prices, no FYERS connection"
    state.notice = "DEMO ONLY. Real scans need FYERS login and a trading session."
    candles = [Candle(datetime.combine(today, daytime(9, minute), IST), 100, high, low, 100)
               for minute, high, low in ((15, 110, 90), (30, 110, 90))]
    state.previous_session = today - timedelta(days=1)
    prior = Candle(datetime.combine(state.previous_session, daytime(0), IST), 95, 99, 90, 95, interval="D")
    state.previous["NSE:DEMO-EQ"] = prior
    state.apply_history("NSE:DEMO-EQ", [prior] + candles, datetime.combine(today, daytime(9, 46), IST))
    state.records["NSE:DEMO-EQ"].update(price=102.5, updated=datetime.now(IST).isoformat())
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="nifty100.txt")
    parser.add_argument("--demo", action="store_true", help="Synthetic demo, without broker credentials")
    args = parser.parse_args(argv)
    client_id, token = os.environ.get("FYERS_CLIENT_ID"), os.environ.get("FYERS_ACCESS_TOKEN")
    if not args.demo and (not client_id or not token):
        parser.error("Run login.py --live --symbols nifty100.txt for a real session")
    if args.demo:
        state = demo_state()
        state.universe = "Synthetic demo"
    else:
        try:
            symbols = load_live_symbols(args.symbols)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        label = "NIFTY 100" if Path(args.symbols).stem.lower() == "nifty100" else "Custom watchlist"
        state = LiveState(symbols, datetime.now(IST).date(), universe=label)
    if not args.demo:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            state.notice = "Weekend: no regular NSE session is expected today. Demo mode is available; start a real session before 09:15 IST on a trading day."
        elif now >= datetime.combine(state.session, daytime(9, 15), IST):
            state.notice = "Started after the opening bell: early ticks may be missing. Historical reconciliation starts after 09:45; complete coverage may take several minutes."
    stop, socket = threading.Event(), None
    server = ThreadingHTTPServer(("127.0.0.1", 8765), handler_for(state))
    try:
        if not args.demo:
            # Start in a daemon thread so subscription setup cannot block the dashboard.
            def start_feed():
                nonlocal socket
                try:
                    socket = connect_feed(state, stop, client_id, token)
                except Exception:
                    with state.lock:
                        state.feed = "Could not start streaming; check installation and FYERS access"
                    progress("Stream startup failed; credentials and raw exception omitted.")
            threading.Thread(target=start_feed, daemon=True).start()
            threading.Thread(target=verify_worker, args=(state, stop, client_id, token), daemon=True).start()
        print("Dashboard: http://127.0.0.1:8765 — keep this terminal open. Ctrl+C stops it.", flush=True)
        webbrowser.open("http://127.0.0.1:8765")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if socket:
            socket.close_connection()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
