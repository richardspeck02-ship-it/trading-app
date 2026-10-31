from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import unittest
from unittest.mock import patch

from scanner import fetch_fyers, main


class ProgressTests(unittest.TestCase):
    @patch("scanner.time.sleep")
    @patch("fyers_apiv3.fyersModel.FyersModel")
    @patch.dict("os.environ", {"FYERS_CLIENT_ID": "private-client", "FYERS_ACCESS_TOKEN": "private-token"})
    def test_progress_for_success_rejection_and_exception_without_secrets(self, model, sleep):
        model.return_value.history.side_effect = [
            {"s": "ok", "candles": [[1793331900, 100, 110, 90, 105, 12]]},
            {"s": "error", "message": "private-response", "access_token": "private-token"},
            RuntimeError("private-exception"),
        ]
        output = io.StringIO()
        with redirect_stderr(output):
            grouped, errors = fetch_fyers(["NSE:ABB-EQ", "NSE:ACC-EQ", "NSE:HFCL-BE"], date(2026, 10, 30))
        self.assertEqual(list(grouped), ["NSE:ABB-EQ"])
        self.assertEqual(len(errors), 2)
        log = output.getvalue()
        self.assertLess(log.index("fetching candles"), log.index("received 1 candles"))
        self.assertIn("Completed 3/3; fetched 1; errors 2", log)
        self.assertIn("Fetching complete", log)
        self.assertNotIn("private", log)

    def test_progress_does_not_mix_with_json_results(self):
        output, logs = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(logs):
            result = main(["--csv", "examples/candles.csv", "--date", "2026-10-30"])
        self.assertEqual(result, 0)
        self.assertEqual(len(json.loads(output.getvalue())["matches"]), 1)
        self.assertIn("MATCH: DEMO_MATCH", logs.getvalue())
        self.assertIn("Scan complete: 1 matches", logs.getvalue())
