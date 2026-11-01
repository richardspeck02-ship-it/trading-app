from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import date, datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scanner import IST, fetch_fyers, main, opening_request, save_report, print_summary


class ReportTests(unittest.TestCase):
    def test_summary_preserves_punctuation_and_treats_incomplete_as_error(self):
        report = {"results": [{"symbol": "NSE:M&M-EQ", "status": "match", "first_high": 123},
                              {"symbol": "NSE:BAJAJ-AUTO-EQ", "status": "no_match"},
                              {"symbol": "NSE:ABB-EQ", "status": "incomplete"}],
                  "errors": {"NSE:ACC-EQ": "private-response"}}
        output = io.StringIO()
        with redirect_stdout(output):
            print_summary(report)
        self.assertEqual(output.getvalue(), "\nMATCH\nM&M\n\nNO-MATCH\nBAJAJ-AUTO\n\nERROR\nABB\nACC\n")

    @patch("scanner.fetch_fyers")
    def test_saved_report_can_be_summarized_without_api_calls(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"complete": True, "results": [{"symbol": "NSE:ABB-EQ", "status": "match"}], "errors": {}}))
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--report", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("MATCH\nABB", output.getvalue())
            fetch.assert_not_called()

    def test_request_covers_only_first_two_fifteen_minute_starts(self):
        request = opening_request("NSE:ABB-EQ", date(2026, 10, 30))
        self.assertEqual(request["date_format"], "0")
        self.assertEqual(request["resolution"], "15")
        start = datetime.fromtimestamp(int(request["range_from"]), IST)
        end = datetime.fromtimestamp(int(request["range_to"]), IST)
        self.assertEqual(start.isoformat(), "2026-10-30T09:15:00+05:30")
        self.assertEqual(end.isoformat(), "2026-10-30T09:44:59+05:30")

    @patch("scanner.time.sleep")
    @patch("fyers_apiv3.fyersModel.FyersModel")
    @patch.dict("os.environ", {"FYERS_CLIENT_ID": "test", "FYERS_ACCESS_TOKEN": "test"})
    def test_extra_returned_candles_are_excluded(self, model, sleep):
        epoch = int(datetime(2026, 10, 30, 9, 15, tzinfo=IST).timestamp())
        daily = {"s": "ok", "candles": [[epoch - 86400, 95, 99, 90, 95]]}
        model.return_value.history.side_effect = [daily, daily, {"s": "ok", "candles": [
            [epoch + offset, 100, 110, 90, 105] for offset in (0, 900, 1800)]}]
        with redirect_stderr(io.StringIO()):
            grouped, errors = fetch_fyers(["NSE:ABB-EQ"], date(2026, 10, 30))
        self.assertEqual(len(grouped["NSE:ABB-EQ"]), 3)
        self.assertEqual(grouped["NSE:ABB-EQ"][0].interval, "D")
        self.assertFalse(errors)

    def test_saved_offline_results_and_watchlist_match_inclusive_rule(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["--csv", "examples/candles.csv", "--date", "2026-10-30", "--output-dir", directory])
            self.assertEqual(code, 0)
            folder = next(Path(directory).iterdir())
            report = json.loads((folder / "report.json").read_text())
            self.assertTrue(report["complete"])
            self.assertEqual(report["interval"], "15")
            self.assertEqual(report["candle_count"], 2)
            self.assertEqual((folder / "watchlist.txt").read_text(), "DEMO_MATCH\n")
            with (folder / "matches.csv").open(newline="") as stream:
                matches = list(csv.DictReader(stream))
            self.assertEqual(len(matches), 1)
            self.assertNotIn("third_high", matches[0])
            self.assertNotIn("third_low", matches[0])
            self.assertEqual(float(matches[0]["second_high"]), float(matches[0]["first_high"]))
            self.assertEqual(float(matches[0]["second_low"]), float(matches[0]["first_low"]))
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

    @patch("scanner.datetime")
    def test_cli_refuses_scan_before_second_fifteen_minute_candle_closes(self, clock):
        clock.now.return_value = datetime(2026, 10, 30, 9, 44, 59, tzinfo=IST)
        clock.combine = datetime.combine
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as caught:
            main(["--csv", "examples/candles.csv", "--date", "2026-10-30"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Wait until 09:45 IST", output.getvalue())
