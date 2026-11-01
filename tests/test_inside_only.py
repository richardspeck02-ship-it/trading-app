"""Regression checks for removing the previous-session breakout filter."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, time
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from live_app import LiveState, verify_worker
from scanner import Candle, IST, evaluate, fetch_fyers, load_csv, main


class InsideOnlyTests(unittest.TestCase):
    session = date(2026, 10, 30)
    now = datetime(2026, 10, 30, 9, 46, tzinfo=IST)

    def candles(self, close=100):
        return [Candle(datetime.combine(self.session, time(9, minute), IST),
                       100, 110, 90, close if minute == 15 else 100)
                for minute in (15, 30)]

    def payload(self):
        return {"s": "ok", "candles": [
            [c.timestamp.timestamp(), c.open, c.high, c.low, c.close] for c in self.candles()]}

    def test_first_close_has_no_effect_on_inside_match(self):
        for close in (90, 95, 96, 100, 104, 105, 110):
            with self.subTest(close=close):
                status, levels = evaluate(self.candles(close), self.session, self.now)
                self.assertEqual(status, "match")
                self.assertEqual(levels["first_close"], close)
                self.assertNotIn("previous_session", levels)
                self.assertNotIn("previous_high", levels)
                self.assertNotIn("previous_low", levels)

    def test_second_wick_outside_first_still_rejects_regardless_of_close(self):
        for field, value in (("high", 111), ("low", 89)):
            for close in (90, 100, 110):
                candles = self.candles(close)
                candles[1] = replace(candles[1], **{field: value})
                self.assertEqual(evaluate(candles, self.session, self.now)[0], "no_match")

    @patch("scanner.time.sleep")
    @patch("fyers_apiv3.fyersModel.FyersModel")
    @patch.dict("os.environ", {"FYERS_CLIENT_ID": "test", "FYERS_ACCESS_TOKEN": "test"})
    def test_history_fetches_only_opening_bars_once_per_stock(self, model, sleep):
        model.return_value.history.return_value = self.payload()
        symbols = ["NSE:HCLTECH-EQ", "NSE:ABB-EQ"]
        with redirect_stderr(io.StringIO()):
            grouped, errors = fetch_fyers(symbols, self.session)
        self.assertFalse(errors)
        self.assertEqual(set(grouped), set(symbols))
        requests = [call.kwargs["data"] for call in model.return_value.history.call_args_list]
        self.assertEqual([r["symbol"] for r in requests], symbols)
        self.assertTrue(all(r["resolution"] == "15" for r in requests))
        for symbol in symbols:
            self.assertEqual(evaluate(grouped[symbol], self.session, self.now)[0], "match")

    def test_live_preview_and_exports_work_without_previous_session_data(self):
        state = LiveState(["NSE:HCLTECH-EQ"], self.session)
        record = state.records["NSE:HCLTECH-EQ"]
        for close in (90, 100, 110):
            record["bars"] = {0: [100, 110, 90, close], 1: [100, 110, 90, 100]}
            self.assertEqual(state.preview(record, self.now), "Candidate (provisional)")
            state.apply_history("NSE:HCLTECH-EQ", self.candles(close), self.now)
            self.assertEqual(state.export("txt"), b"NSE:HCLTECH-EQ\n")
            self.assertNotIn("previous_", state.export("csv").decode())
        self.assertEqual(state.snapshot()["pattern"], "inside_only")
        self.assertNotIn("previous_session", state.snapshot())

    @patch("live_app.datetime")
    @patch("live_app.requests.get")
    def test_live_confirmation_needs_one_intraday_request_only(self, request, clock):
        clock.now.return_value = self.now
        clock.combine = datetime.combine
        clock.fromtimestamp = datetime.fromtimestamp
        response = request.return_value.__enter__.return_value
        response.status_code = 200
        response.json.return_value = self.payload()
        state = LiveState(["NSE:HCLTECH-EQ"], self.session)
        stop = Mock()
        stop.wait.side_effect = [False, True]
        with redirect_stderr(io.StringIO()):
            verify_worker(state, stop, "test", "test")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["params"]["resolution"], "15")
        self.assertEqual(request.call_args.kwargs["params"]["symbol"], "NSE:HCLTECH-EQ")
        self.assertEqual(state.export("txt"), b"NSE:HCLTECH-EQ\n")

    def test_old_daily_reference_rows_are_ignored_without_becoming_extra_stocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.csv"
            path.write_text(Path("examples/candles.csv").read_text() +
                            "NSE:NIFTY50-INDEX,2026-10-29T00:00:00+05:30,100,200,50,100,D\n" +
                            "DEMO_MATCH,2026-10-29T00:00:00+05:30,100,200,50,100,D\n")
            grouped = load_csv(path)
            self.assertNotIn("NSE:NIFTY50-INDEX", grouped)
            self.assertEqual(len(grouped["DEMO_MATCH"]), 2)
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--csv", str(path), "--date", "2026-10-30", "--output-dir", directory]), 0)
            folder = next(p for p in Path(directory).iterdir() if p.is_dir())
            report = json.loads((folder / "report.json").read_text())
            self.assertEqual(report["pattern"], "inside_only")
            self.assertEqual(len(report["matches"]), 1)
            self.assertNotIn("previous_", (folder / "matches.csv").read_text())

    def test_missing_opening_bars_on_closed_days_never_match(self):
        for session in (date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4)):
            self.assertEqual(evaluate([], session, self.now)[0], "incomplete")
