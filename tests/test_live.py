from contextlib import redirect_stderr
from datetime import date, datetime, time as daytime
import io
import json
import unittest
from unittest.mock import Mock, patch

from live_app import LiveState, connect_feed, demo_state, stream_error_details, verify_worker
from scanner import Candle, IST


class LiveTests(unittest.TestCase):
    session = date(2026, 10, 30)

    def setUp(self):
        self.state = LiveState(["NSE:ABB-EQ", "NSE:ACC-EQ"], self.session)
        self.state.previous_session = date(2026, 10, 29)
        self.prior = Candle(datetime(2026, 10, 29, tzinfo=IST), 95, 99, 90, 95, "D")
        self.state.previous = dict.fromkeys(self.state.symbols, self.prior)

    def tick(self, minute, second, price):
        stamp = datetime.combine(self.session, daytime(9, minute, second), IST)
        self.state.tick({"symbol": "NSE:ABB-EQ", "ltp": price, "last_traded_time": stamp.timestamp()}, stamp)

    def candles(self, outside=False):
        return [self.prior] + [Candle(datetime.combine(self.session, daytime(9, minute), IST), 100, high, low, 100)
                for minute, high, low in ((15, 110, 90), (30, 111 if outside else 110, 90))]

    def test_inclusive_candidate_is_never_exported_before_confirmation(self):
        for minute in (15, 30):
            self.tick(minute, 0, 100)
            self.tick(minute, 1, 110)
            self.tick(minute, 2, 90)
            self.tick(minute, 3, 100)
        snapshot = self.state.snapshot(datetime(2026, 10, 30, 9, 45, tzinfo=IST))
        self.assertEqual(snapshot["rows"][0]["status"], "Candidate (provisional)")
        self.assertEqual(self.state.export("txt"), b"")

    def test_history_can_reject_stream_candidate(self):
        self.state.apply_history("NSE:ABB-EQ", self.candles(True), datetime(2026, 10, 30, 9, 46, tzinfo=IST))
        self.assertEqual(self.state.snapshot()["rows"][0]["status"], "Confirmed no match")
        self.assertEqual(self.state.export("txt"), b"")

    def test_only_confirmed_matches_export(self):
        self.state.apply_history("NSE:ABB-EQ", self.candles(), datetime(2026, 10, 30, 9, 46, tzinfo=IST))
        self.assertEqual(self.state.export("txt"), b"NSE:ABB-EQ\n")
        self.assertIn("NSE:ABB-EQ", self.state.export("csv").decode("utf-8-sig"))
        self.assertNotIn("NSE:ACC-EQ", self.state.export("csv").decode("utf-8-sig"))
        self.assertNotIn("third_high", self.state.export("csv").decode("utf-8-sig"))
        self.assertEqual(self.state.snapshot()["interval"], "15")
        self.assertEqual(self.state.snapshot()["candle_count"], 2)

    def test_prices_without_trade_timestamps_do_not_build_candles(self):
        self.state.tick({"symbol": "NSE:ABB-EQ", "ltp": 100}, datetime(2026, 10, 30, 9, 15, tzinfo=IST))
        self.assertEqual(self.state.records["NSE:ABB-EQ"]["bars"], {})
        self.assertEqual(self.state.snapshot()["seen"], 1)

    def test_invalid_ticks_and_old_sessions_do_not_build_candles(self):
        for message in (None, {"symbol": "UNKNOWN", "ltp": 1},
                        {"symbol": "NSE:ABB-EQ", "ltp": float("nan")},
                        {"symbol": "NSE:ABB-EQ", "ltp": 100, "last_traded_time": "bad"}):
            self.state.tick(message)
        self.assertEqual(self.state.records["NSE:ABB-EQ"]["bars"], {})

    def test_missing_candles_are_not_exported(self):
        self.state.apply_history("NSE:ABB-EQ", self.candles()[:2], datetime(2026, 10, 30, 9, 46, tzinfo=IST))
        self.assertEqual(self.state.snapshot()["rows"][0]["status"], "Incomplete historical candles")
        self.assertEqual(self.state.export("txt"), b"")

    def test_stale_prices_and_json_serialization(self):
        self.tick(15, 0, 100)
        data = self.state.snapshot(datetime(2026, 10, 30, 9, 16, tzinfo=IST))
        self.assertTrue(data["rows"][0]["stale"])
        json.dumps(data, allow_nan=False)

    def test_out_of_order_trade_cannot_replace_newer_price(self):
        self.tick(20, 0, 105)
        self.tick(15, 0, 100)
        self.assertEqual(self.state.records["NSE:ABB-EQ"]["price"], 105)

    @patch("live_app.datetime")
    @patch("live_app.requests.get")
    def test_confirmation_prioritizes_streaming_candidates(self, request, clock):
        now = datetime(2026, 10, 30, 9, 46, tzinfo=IST)
        clock.now.return_value = now
        clock.combine = datetime.combine
        clock.fromtimestamp = datetime.fromtimestamp
        self.state.records["NSE:ACC-EQ"]["bars"] = {0: [100, 110, 90, 100],
                                                    1: [100, 105, 95, 100]}
        response = request.return_value.__enter__.return_value
        response.status_code = 200
        response.json.return_value = {"s": "ok", "candles": [
            [c.timestamp.timestamp(), c.open, c.high, c.low, c.close] for c in self.candles()[1:]]}
        stop = Mock()
        stop.wait.side_effect = [False, True]
        with redirect_stderr(io.StringIO()):
            verify_worker(self.state, stop, "private-client", "private-token")
        self.assertEqual(request.call_args.kwargs["params"]["symbol"], "NSE:ACC-EQ")
        self.assertEqual(self.state.official["NSE:ACC-EQ"]["status"], "match")
        self.assertNotIn("NSE:ABB-EQ", self.state.official)

    @patch("live_app.datetime")
    @patch("live_app.requests.get")
    def test_verification_rate_limit_stops_without_publishing_match(self, request, clock):
        clock.now.return_value = datetime(2026, 10, 30, 9, 46, tzinfo=IST)
        clock.combine = datetime.combine
        request.return_value.__enter__.return_value.status_code = 429
        request.return_value.__enter__.return_value.json.return_value = {"s": "error", "code": 429, "message": "private"}
        stop = Mock()
        stop.wait.return_value = False
        with redirect_stderr(io.StringIO()):
            verify_worker(self.state, stop, "private-client", "private-token")
        self.assertTrue(self.state.history_paused)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.state.export("txt"), b"")
        self.assertNotIn("private", json.dumps(self.state.snapshot()))

    def test_demo_is_explicitly_labelled(self):
        data = demo_state().snapshot()
        self.assertTrue(data["demo"])
        self.assertIn("DEMO", data["feed"])
        self.assertEqual(data["confirmed"], 1)

    def test_stream_error_exposes_safe_code_and_known_symbols_only(self):
        detail, invalid = stream_error_details({"code": -300, "type": "sub", "message": "private-token",
                                               "invalid_symbols": ["NSE:ABB-EQ", "private-secret"]}, self.state.records)
        self.assertIn("FYERS code -300", detail)
        self.assertEqual(invalid, ["NSE:ABB-EQ"])
        self.assertNotIn("private", detail)

    def test_stream_error_categories_do_not_assume_weekend_is_cause(self):
        for message, expected in (({"code": 11011, "message": "private"}, "rejected a subscription"),
                                  ({"code": -300, "type": "sub", "message": "private"}, "conversion"),
                                  ("Handshake status 403 private-token", "handshake"),
                                  (RuntimeError("private-token"), "SDK error")):
            with self.subTest(expected=expected):
                detail, _ = stream_error_details(message, self.state.records)
                self.assertIn(expected, detail)
                self.assertNotIn("private", detail)

    @patch("fyers_apiv3.FyersWebsocket.data_ws.FyersDataSocket")
    def test_subscription_batches_and_error_callback(self, factory):
        state = LiveState([f"NSE:TEST{i}-EQ" for i in range(100)], self.session)
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        factory.return_value.connect.side_effect = lambda: factory.call_args.kwargs["on_connect"]()
        logs = io.StringIO()
        with redirect_stderr(logs):
            connect_feed(state, stop, "private-client", "private-token")
            factory.call_args.kwargs["on_error"]({"code": 11011, "message": "private-response"})
            factory.call_args.kwargs["on_message"]({"s": "ok", "type": "sub"})
        batches = factory.return_value.subscribe.call_args_list
        self.assertEqual([len(c.kwargs["symbols"]) for c in batches], [100])
        self.assertEqual([c.args[0] for c in stop.wait.call_args_list], [2])
        self.assertEqual(state.snapshot()["subscription_acks"], 1)
        self.assertIn("FYERS code 11011", state.snapshot()["stream_errors"][0]["detail"])
        self.assertNotIn("private", logs.getvalue())

    def test_oversized_universe_rejected_before_connecting(self):
        state = LiveState([f"NSE:TEST{i}-EQ" for i in range(101)], self.session)
        with self.assertRaisesRegex(ValueError, "at most 100"):
            connect_feed(state, Mock(), "private-client", "private-token")

    def test_stream_aggregates_full_fifteen_minutes_and_stops_at_0945(self):
        self.tick(14, 59, 999)
        self.assertEqual(self.state.records["NSE:ABB-EQ"]["bars"], {})
        for minute, second, price in [(15, 0, 100), (19, 59, 98), (20, 0, 110),
                                      (25, 0, 90), (29, 59, 105), (30, 0, 104),
                                      (35, 0, 110), (40, 0, 90), (44, 59, 100)]:
            self.tick(minute, second, price)
        bars = self.state.records["NSE:ABB-EQ"]["bars"]
        self.assertEqual(bars, {0: [100, 110, 90, 105], 1: [104, 110, 90, 100]})
        self.tick(45, 0, 500)
        self.assertEqual(bars, {0: [100, 110, 90, 105], 1: [104, 110, 90, 100]})
        record = self.state.records["NSE:ABB-EQ"]
        self.assertEqual(self.state.preview(record, datetime(2026, 10, 30, 9, 44, 59, tzinfo=IST)),
                         "Inside so far (2nd forming, provisional)")
        self.assertEqual(self.state.preview(record, datetime(2026, 10, 30, 9, 45, tzinfo=IST)),
                         "Candidate (provisional)")
        self.assertEqual(self.state.export("txt"), b"")

    @patch("live_app.datetime")
    @patch("live_app.requests.get")
    def test_worker_waits_until_094503_for_official_opening_bars(self, request, clock):
        clock.combine = datetime.combine
        clock.fromtimestamp = datetime.fromtimestamp
        response = request.return_value.__enter__.return_value
        response.status_code = 200
        response.json.return_value = {"s": "ok", "candles": [
            [c.timestamp.timestamp(), c.open, c.high, c.low, c.close] for c in self.candles()[1:]]}
        for minute, second, calls in [(44, 59, 0), (45, 2, 0), (45, 3, 1)]:
            with self.subTest(minute=minute, second=second):
                request.reset_mock()
                clock.now.return_value = datetime(2026, 10, 30, 9, minute, second, tzinfo=IST)
                stop = Mock()
                stop.wait.side_effect = [False, True]
                with redirect_stderr(io.StringIO()):
                    verify_worker(self.state, stop, "test", "test")
                self.assertEqual(request.call_count, calls)
                if calls:
                    self.assertEqual(request.call_args.kwargs["params"]["resolution"], "15")
                    self.assertEqual(self.state.official["NSE:ABB-EQ"]["status"], "match")
