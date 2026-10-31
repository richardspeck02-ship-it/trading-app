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

from scanner import Candle, IST, evaluate, history_error, load_symbols, opening_request, progress


class LiveState:
    def __init__(self, symbols, session, demo=False):
        self.symbols = symbols
        self.session = session
        self.demo = demo
        self.lock = threading.RLock()
        self.records = {s: {"price": None, "updated": None, "trade_time": None, "bars": {}}
                        for s in symbols}
        self.official = {}
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
            if not 0 <= minute < 15:
                return
            bucket = minute // 5
            bar = record["bars"].get(bucket)
            if bar is None:
                record["bars"][bucket] = [price, price, price, price]
            else:
                bar[1] = max(bar[1], price)
                bar[2] = min(bar[2], price)
                bar[3] = price

    def preview(self, record, now):
        bars = record["bars"]
        if now < datetime.combine(self.session, daytime(9, 20), IST) or not all(i in bars for i in (0, 1)):
            return "Waiting for opening candles"
        first = bars[0]
        if any(bars[i][2] < first[2] or bars[i][1] > first[1] for i in (1, 2) if i in bars):
            return "Outside range (provisional)"
        if 2 not in bars:
            return "Inside so far (2 candles, provisional)"
        return "Candidate (provisional)" if now >= datetime.combine(self.session, daytime(9, 30), IST) else "Inside so far (3rd forming, provisional)"

    def apply_history(self, symbol, candles, now):
        status, levels = evaluate(candles, self.session, now)
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
                    status = {"match": "Confirmed match", "no_match": "Confirmed outside range",
                              "incomplete": "Incomplete historical candles", "error": "Verification failed"}.get(official["status"], "Waiting")
                    levels = official.get("levels")
                elif record.get("subscription_error"):
                    status = "Subscription error"
                if levels is None and 0 in record["bars"]:
                    levels = {"first_high": record["bars"][0][1], "first_low": record["bars"][0][2]}
                stale = record["updated"] is None or (now - datetime.fromisoformat(record["updated"])).total_seconds() > 20
                rows.append({"symbol": symbol, "price": record["price"], "updated": record["updated"],
                             "stale": stale, "status": status, "levels": levels,
                             "error": official.get("error") if official else record.get("subscription_error")})
            return {"session": self.session.isoformat(), "time": now.isoformat(), "demo": self.demo,
                    "feed": self.feed, "notice": self.notice, "total": len(rows),
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
                         "second_high", "second_low", "third_high", "third_low"])
        for r in rows:
            writer.writerow([self.session.isoformat(), r["symbol"], r["price"], r["updated"],
                             *(r["levels"].get(k) for k in ("first_high", "first_low", "second_high", "second_low", "third_high", "third_low"))])
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
        if now < datetime.combine(state.session, daytime(9, 30, 3), IST) or now.weekday() >= 5:
            continue
        with state.lock:
            remaining = [s for s in state.symbols if s not in state.official]
            if not remaining:
                return
            remaining.sort(key=lambda s: "Candidate" not in state.preview(state.records[s], now))
            symbol = remaining[0]
        progress(f"Confirming opening candles: {symbol}")
        try:
            with requests.get("https://api-t1.fyers.in/data/history",
                              params=opening_request(symbol, state.session),
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
                if response.status_code != 200 or not isinstance(data, dict) or data.get("s") != "ok":
                    raise ValueError(detail)
                candles = [Candle(datetime.fromtimestamp(c[0], IST), *(float(v) for v in c[1:5])) for c in data["candles"]]
                state.apply_history(symbol, candles, now)
        except Exception:
            with state.lock:
                state.official[symbol] = {"status": "error", "error": "Could not verify opening candles; no match exported."}
        # One worker; the next request starts at least a second after this finishes.


def connect_feed(state, stop, client_id, token):
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
               for minute, high, low in ((15, 110, 90), (20, 110, 95), (25, 105, 90))]
    state.apply_history("NSE:DEMO-EQ", candles, datetime.combine(today, daytime(9, 31), IST))
    state.records["NSE:DEMO-EQ"].update(price=102.5, updated=datetime.now(IST).isoformat())
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="nifty500.txt")
    parser.add_argument("--demo", action="store_true", help="Synthetic demo, without broker credentials")
    args = parser.parse_args(argv)
    client_id, token = os.environ.get("FYERS_CLIENT_ID"), os.environ.get("FYERS_ACCESS_TOKEN")
    if not args.demo and (not client_id or not token):
        parser.error("Run login.py --live --symbols nifty500.txt for a real session")
    state = demo_state() if args.demo else LiveState(load_symbols(args.symbols), datetime.now(IST).date())
    if not args.demo:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            state.notice = "Weekend: no regular NSE session is expected today. Demo mode is available; start a real session before 09:15 IST on a trading day."
        elif now >= datetime.combine(state.session, daytime(9, 15), IST):
            state.notice = "Started after the opening bell: early ticks may be missing. Historical reconciliation starts after 09:30; complete coverage may take several minutes."
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
