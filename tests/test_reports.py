from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import date, datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scanner import IST, fetch_fyers, main, opening_request, save_report


class ReportTests(unittest.TestCase):
    def test_request_covers_only_first_three_candle_starts(self):
        request = opening_request("NSE:ABB-EQ", date(2026, 10, 30))
        self.assertEqual(request["date_format"], "0")
        start = datetime.fromtimestamp(int(request["range_from"]), IST)
        end = datetime.fromtimestamp(int(request["range_to"]), IST)
        self.assertEqual(start.isoformat(), "2026-10-30T09:15:00+05:30")
        self.assertEqual(end.isoformat(), "2026-10-30T09:29:59+05:30")

    @patch("scanner.time.sleep")
    @patch("fyers_apiv3.fyersModel.FyersModel")
    @patch.dict("os.environ", {"FYERS_CLIENT_ID": "test", "FYERS_ACCESS_TOKEN": "test"})
    def test_extra_returned_candles_are_excluded(self, model, sleep):
        epoch = int(datetime(2026, 10, 30, 9, 15, tzinfo=IST).timestamp())
        model.return_value.history.return_value = {"s": "ok", "candles": [
            [epoch + offset, 100, 110, 90, 105] for offset in (0, 300, 600, 900)]}
        with redirect_stderr(io.StringIO()):
            grouped, errors = fetch_fyers(["NSE:ABB-EQ"], date(2026, 10, 30))
        self.assertEqual(len(grouped["NSE:ABB-EQ"]), 3)
        self.assertFalse(errors)

    def test_saved_offline_results_and_watchlist_match_inclusive_rule(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["--csv", "examples/candles.csv", "--date", "2026-10-30", "--output-dir", directory])
            self.assertEqual(code, 0)
            folder = next(Path(directory).iterdir())
            report = json.loads((folder / "report.json").read_text())
            self.assertTrue(report["complete"])
            self.assertEqual((folder / "watchlist.txt").read_text(), "DEMO_MATCH\n")
            with (folder / "matches.csv").open(newline="") as stream:
                matches = list(csv.DictReader(stream))
            self.assertEqual(len(matches), 1)
            self.assertEqual(float(matches[0]["second_high"]), float(matches[0]["first_high"]))
            self.assertEqual(float(matches[0]["third_low"]), float(matches[0]["first_low"]))
            self.assertEqual((folder / "failed-symbols.txt").read_text(), "")
            self.assertTrue((folder / "candles.csv").exists())

    def test_partial_coverage_and_separate_run_folders(self):
        report = {"session": "2026-10-30", "complete": False, "matches": [],
                  "results": [{"symbol": "NSE:ABB-EQ", "status": "incomplete"}],
                  "errors": {"NSE:ACC-EQ": "Request failed"}}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            first = save_report(directory, report, {})
            second = save_report(directory, report, {})
            self.assertNotEqual(first, second)
            self.assertEqual((first / "failed-symbols.txt").read_text().splitlines(), ["NSE:ABB-EQ", "NSE:ACC-EQ"])
            self.assertFalse(json.loads((first / "report.json").read_text())["complete"])
