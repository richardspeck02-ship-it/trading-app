from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import unittest
from unittest.mock import patch

from scanner import fetch_fyers, history_error, history_with_backoff, main


class ProgressTests(unittest.TestCase):
    def test_error_diagnostics_are_safe_and_do_not_assume_rate_limits(self):
        detail, limited = history_error({"s": "error", "code": -123, "message": "private-response"})
        self.assertIn("FYERS code -123", detail)
        self.assertNotIn("private", detail)
        self.assertFalse(limited)
        self.assertFalse(history_error({"s": "error", "code": 403})[1])
        self.assertTrue(history_error({"s": "error", "code": 429})[1])

    @patch("scanner.time.sleep")
    def test_rate_limit_retries_then_recovers(self, sleep):
        from unittest.mock import Mock
        client = Mock()
        client.history.side_effect = [{"s": "error", "code": 429},
                                      {"s": "ok", "candles": []}]
        with redirect_stderr(io.StringIO()):
            result = history_with_backoff(client, {"symbol": "NSE:ABB-EQ"}, "stock")
        self.assertEqual(result["s"], "ok")
        self.assertEqual(client.history.call_count, 2)
        sleep.assert_called_once_with(30)

    @patch("scanner.time.sleep")
    @patch("fyers_apiv3.fyersModel.FyersModel")
    @patch.dict("os.environ", {"FYERS_CLIENT_ID": "private-client", "FYERS_ACCESS_TOKEN": "private-token"})
    def test_persistent_rate_limit_stops_remaining_requests(self, model, sleep):
        model.return_value.history.return_value = {"s": "error", "code": 429}
        with redirect_stderr(io.StringIO()):
            grouped, errors = fetch_fyers(["NSE:ABB-EQ", "NSE:ACC-EQ"], date(2026, 10, 30))
        self.assertEqual(grouped, {})
        self.assertEqual(model.return_value.history.call_count, 3)
        self.assertIn("Not fetched", errors["NSE:ACC-EQ"])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60, 0.5])

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
        self.assertLess(log.index("fetching candles"), log.index("received 1 opening candles"))
        self.assertIn("Completed 3/3; fetched 1; errors 2", log)
        self.assertIn("Fetching complete", log)
        self.assertNotIn("private", log)

    def test_terminal_summary_shows_names_without_prices(self):
        output, logs = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(logs):
            result = main(["--csv", "examples/candles.csv", "--date", "2026-10-30"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "\nMATCH\nDEMO_MATCH\n\nNO-MATCH\nDEMO_REJECT\n\nERROR\n(none)\n")
        self.assertNotIn("110", output.getvalue())
        self.assertIn("MATCH: DEMO_MATCH", logs.getvalue())
        self.assertIn("Scan complete: 1 matches", logs.getvalue())
