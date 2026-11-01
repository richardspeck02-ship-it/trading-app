"""Mandatory breakout filter: pure rules, real-session selection, both fetch paths."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scanner import (Candle, IST, SESSION_INDEX, daily_request, evaluate,
                     fetch_fyers, load_csv, main, previous_session)
from live_app import LiveState, verify_worker


class BreakoutTests(unittest.TestCase):
    session = date(2026, 10, 30)
    prior_date = date(2026, 10, 29)
    now = datetime(2026, 10, 30, 9, 31, tzinfo=IST)

    def prior(self, day=None, high=104, low=96):
        return Candle(datetime.combine(day or self.prior_date, time(), IST), 100, high, low, 100, "D")

    def opening(self, close=105, day=None):
        return [Candle(datetime.combine(day or self.session, time(9, m), IST),
                       100, 110, 90, close if m == 15 else 100) for m in (15, 20, 25)]

    def test_strict_close_above_or_below_not_wicks_or_equalities(self):
        for close, expected in [(105, "match"), (95, "match"), (104, "no_match"),
                                (96, "no_match"), (100, "no_match")]:
            with self.subTest(close=close):
                status, levels = evaluate([self.prior()] + self.opening(close), self.session,
                                          self.now, self.prior_date)
                self.assertEqual(status, expected)
                self.assertEqual(levels["first_close"], close)
                self.assertEqual(levels["previous_session"], "2026-10-29")
                self.assertEqual(levels["previous_high"], 104)
                self.assertEqual(levels["previous_low"], 96)

    def test_inclusive_inside_rule_is_still_required_after_breakout(self):
        candles = [self.prior()] + self.opening()
        self.assertEqual(evaluate(candles, self.session, self.now, self.prior_date)[0], "match")
        for index in (2, 3):
            for field, value in [("high", 111), ("low", 89)]:
                changed = list(candles)
                changed[index] = replace(changed[index], **{field: value})
                self.assertEqual(evaluate(changed, self.session, self.now, self.prior_date)[0], "no_match")

    def test_missing_exact_prior_daily_bar_never_uses_older_or_intraday_data(self):
        for candles in [self.opening(), [self.prior(self.prior_date - timedelta(days=1))] + self.opening(),
                        [replace(self.prior(), interval="5")] + self.opening()]:
            self.assertEqual(evaluate(candles, self.session, self.now, self.prior_date)[0], "incomplete")
        with self.assertRaises(ValueError):
            evaluate([self.prior(), self.prior()] + self.opening(), self.session, self.now, self.prior_date)

    def test_previous_session_uses_actual_exchange_bars_across_holiday_and_weekend(self):
        # Gandhi Jayanti Friday Oct 2, followed by the weekend: Monday uses Thursday.
        session = date(2026, 10, 5)
        reference = [self.prior(date(2026, 10, 1)), self.prior(date(2026, 9, 30)),
                     self.prior(session), self.prior(date(2026, 10, 6))]
        self.assertEqual(previous_session(reference, session), date(2026, 10, 1))
        self.assertEqual(previous_session([self.prior(date(2026, 10, 9))], date(2026, 10, 12)), date(2026, 10, 9))
        # Special weekend trading is included when the exchange supplies a daily bar.
        self.assertEqual(previous_session([self.prior(date(2026, 10, 10))], date(2026, 10, 12)), date(2026, 10, 10))
        self.assertIsNone(previous_session([], session))
        self.assertIsNone(previous_session([self.prior(date(2026, 8, 1))], session))

    def test_daily_request_excludes_current_session_and_fetches_exact_reference_day(self):
        request = daily_request("NSE:ABB-EQ", date(2026, 10, 5), date(2026, 10, 1))
        self.assertEqual(request["resolution"], "D")
        self.assertEqual(request["range_from"], "2026-10-01")
        self.assertEqual(request["range_to"], "2026-10-01")
        self.assertEqual(daily_request(SESSION_INDEX, self.session)["range_to"], "2026-10-29")

    @staticmethod
    def response(candles):
        return {"s": "ok", "candles": [[int(c.timestamp.timestamp()), c.open, c.high, c.low, c.close] for c in candles]}

    @patch("scanner.time.sleep")
    @patch("fyers_apiv3.fyersModel.FyersModel")
    @patch.dict("os.environ", {"FYERS_CLIENT_ID": "test", "FYERS_ACCESS_TOKEN": "test"})
    def test_historical_fetch_uses_index_session_then_exact_stock_daily_range(self, model, sleep):
        session, prior = date(2026, 10, 5), date(2026, 10, 1)
        daily = self.prior(prior)
        model.return_value.history.side_effect = [self.response([daily]), self.response([daily]),
                                                  self.response(self.opening(day=session))]
        with redirect_stderr(io.StringIO()):
            grouped, errors = fetch_fyers(["NSE:ABB-EQ"], session)
        self.assertFalse(errors)
        calls = [call.kwargs["data"] for call in model.return_value.history.call_args_list]
        self.assertEqual(calls[0]["symbol"], SESSION_INDEX)
        self.assertEqual(calls[1], daily_request("NSE:ABB-EQ", session, prior))
        self.assertEqual(calls[2]["resolution"], "5")
        now = datetime.combine(session, time(9, 31), IST)
        self.assertEqual(evaluate(grouped["NSE:ABB-EQ"], session, now,
                                  previous_session(grouped[SESSION_INDEX], session))[0], "match")

    @patch("scanner.time.sleep")
    @patch("fyers_apiv3.fyersModel.FyersModel")
    @patch.dict("os.environ", {"FYERS_CLIENT_ID": "test", "FYERS_ACCESS_TOKEN": "test"})
    def test_missing_daily_history_is_incomplete_and_not_a_match(self, model, sleep):
        model.return_value.history.side_effect = [self.response([self.prior()]), self.response([])]
        with redirect_stderr(io.StringIO()):
            grouped, errors = fetch_fyers(["NSE:ABB-EQ"], self.session)
        self.assertFalse(errors)
        self.assertEqual(evaluate(grouped["NSE:ABB-EQ"], self.session, self.now, self.prior_date)[0], "incomplete")
        self.assertEqual(model.return_value.history.call_count, 2)

    def test_offline_report_roundtrip_retains_daily_reference_and_breakout_evidence(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--csv", "examples/candles.csv", "--date", "2026-10-30", "--output-dir", directory]), 0)
            folder = next(Path(directory).iterdir())
            report = json.loads((folder / "report.json").read_text())
            self.assertEqual(report["matches"][0]["previous_high"], 104)
            self.assertEqual(report["matches"][0]["first_close"], 105)
            loaded = load_csv(folder / "candles.csv")
            self.assertEqual(previous_session(loaded[SESSION_INDEX], self.session), self.prior_date)
            self.assertEqual(main(["--csv", str(folder / "candles.csv"), "--date", "2026-10-30"]), 0)

    def test_live_preview_waits_for_prior_range_and_rejects_equal_close(self):
        state = LiveState(["NSE:ABB-EQ"], self.session)
        record = state.records["NSE:ABB-EQ"]
        record["bars"] = {i: [100, 110, 90, 104] for i in range(3)}
        self.assertEqual(state.preview(record, self.now), "Waiting for previous-session range")
        state.previous["NSE:ABB-EQ"] = self.prior()
        self.assertEqual(state.preview(record, self.now), "No opening breakout (provisional)")
        record["bars"][0][3] = 105
        self.assertEqual(state.preview(record, self.now), "Candidate (provisional)")
        self.assertEqual(state.export("txt"), b"")

    def run_worker(self, payloads, session=None, now=None):
        state = LiveState(["NSE:ABB-EQ"], session or self.session)
        stop = Mock()
        stop.wait.side_effect = [False] * len(payloads) + [True]
        responses = []
        for data in payloads:
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.status_code = 200
            response.json.return_value = data
            responses.append(response)
        with patch("live_app.datetime") as clock, patch("live_app.requests.get", side_effect=responses) as request, redirect_stderr(io.StringIO()):
            clock.now.return_value = now or self.now
            clock.combine = datetime.combine
            clock.fromtimestamp = datetime.fromtimestamp
            verify_worker(state, stop, "test", "test")
        self.assertTrue(all(c.args == (1,) for c in stop.wait.call_args_list))
        return state, request

    def test_live_worker_fetches_reference_daily_and_opening_then_exports_only_breakout(self):
        for close, status in [(105, "match"), (95, "match"), (104, "no_match"), (96, "no_match"), (100, "no_match")]:
            with self.subTest(close=close):
                state, request = self.run_worker([self.response([self.prior()]), self.response([self.prior()]), self.response(self.opening(close))])
                self.assertEqual(state.official["NSE:ABB-EQ"]["status"], status)
                self.assertEqual(state.export("txt"), b"NSE:ABB-EQ\n" if status == "match" else b"")
                self.assertEqual([c.kwargs["params"]["resolution"] for c in request.call_args_list], ["D", "D", "5"])

    def test_live_worker_uses_session_before_holiday_weekend(self):
        session, prior = date(2026, 10, 5), date(2026, 10, 1)
        daily = self.prior(prior)
        state, request = self.run_worker([self.response([daily]), self.response([daily]), self.response(self.opening(day=session))],
                                         session, datetime.combine(session, time(9, 31), IST))
        self.assertEqual(state.previous_session, prior)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["range_from"], "2026-10-01")
        self.assertEqual(state.export("txt"), b"NSE:ABB-EQ\n")

    def test_live_worker_missing_stock_or_index_daily_data_fails_closed(self):
        state, _ = self.run_worker([self.response([self.prior()]), self.response([])])
        self.assertEqual(state.official["NSE:ABB-EQ"]["status"], "incomplete")
        self.assertEqual(state.export("txt"), b"")
        state, _ = self.run_worker([self.response([])])
        self.assertEqual(state.official["NSE:ABB-EQ"]["status"], "error")
        self.assertEqual(state.export("txt"), b"")

    def test_current_or_future_daily_range_cannot_be_used_as_previous_session(self):
        for day in [self.session, self.session + timedelta(days=1)]:
            with self.assertRaises(ValueError):
                evaluate([self.prior(day)] + self.opening(), self.session, self.now, day)

    def test_live_prefetches_daily_ranges_before_opening_candles_close(self):
        state, request = self.run_worker([self.response([self.prior()]), self.response([self.prior()]),
                                          self.response([])], now=self.now.replace(minute=25))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(state.previous_session, self.prior_date)
        self.assertIn("NSE:ABB-EQ", state.previous)
        self.assertEqual(state.official, {})
        self.assertEqual(state.export("txt"), b"")

    def test_live_daily_rate_limit_stops_worker_without_export(self):
        state, request = self.run_worker([self.response([self.prior()]), {"s": "error", "code": 429}])
        self.assertTrue(state.history_paused)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(state.export("txt"), b"")

    def test_legacy_opening_only_csv_cannot_bypass_filter(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path = Path(directory) / "legacy.csv"
            path.write_text("symbol,timestamp,open,high,low,close\nNSE:ABB-EQ,2026-10-30T09:15:00+05:30,100,110,90,105\n")
            with self.assertRaises(SystemExit) as caught:
                main(["--csv", str(path), "--date", "2026-10-30"])
            self.assertEqual(caught.exception.code, 2)
