import unittest
from datetime import date, datetime

from scanner import Candle, IST, evaluate


class PatternTests(unittest.TestCase):
    session = date(2026, 10, 30)
    previous = date(2026, 10, 29)
    now = datetime(2026, 10, 30, 9, 45, tzinfo=IST)

    def candles(self, second=(110, 90)):
        return [Candle(datetime(2026, 10, 29, tzinfo=IST), 95, 99, 90, 95, "D")] + [Candle(datetime(2026, 10, 30, 9, m, tzinfo=IST), 100, h, l, 100)
                for m, (h, l) in zip((15, 30), ((110, 90), second))]

    def test_inclusive_boundaries(self):
        self.assertEqual(evaluate(self.candles(second=(110, 90)), self.session, self.now, self.previous)[0], "match")

    def test_second_candle_outside_rejects(self):
        for second in ((111, 90), (110, 89)):
            with self.subTest(second=second):
                self.assertEqual(evaluate(self.candles(second), self.session, self.now, self.previous)[0], "no_match")

    def test_missing_candle(self):
        self.assertEqual(evaluate(self.candles()[:2], self.session, self.now, self.previous)[0], "incomplete")

    def test_open_second_candle_not_used(self):
        now = self.now.replace(minute=44, second=59)
        self.assertEqual(evaluate(self.candles(), self.session, now, self.previous)[0], "pending")

    def test_other_session_not_used(self):
        self.assertEqual(evaluate(self.candles(), date(2026, 10, 29), self.now)[0], "incomplete")

    def test_duplicate_candle_rejected(self):
        candles = self.candles()
        with self.assertRaises(ValueError):
            evaluate(candles + candles[1:2], self.session, self.now, self.previous)

    def test_invalid_price_rejected(self):
        with self.assertRaises(ValueError):
            Candle(self.now, 100, float("nan"), 90, 100)

    def test_no_third_candle_is_required_or_evaluated(self):
        candles = self.candles()
        status, levels = evaluate(candles, self.session, self.now, self.previous)
        self.assertEqual(status, "match")
        self.assertNotIn("third_high", levels)
        self.assertNotIn("third_low", levels)
        later = Candle(datetime(2026, 10, 30, 9, 45, tzinfo=IST), 100, 200, 50, 100)
        self.assertEqual(evaluate(candles + [later], self.session, self.now, self.previous)[0], "match")

    def test_five_minute_bars_cannot_masquerade_as_fifteen_minute_bars(self):
        from dataclasses import replace
        candles = [replace(c, interval="5") if c.interval != "D" else c for c in self.candles()]
        self.assertEqual(evaluate(candles, self.session, self.now, self.previous)[0], "incomplete")

    def test_only_closed_second_candle_can_complete_pattern(self):
        for minute, second, expected in [(30, 0, "pending"), (44, 59, "pending"), (45, 0, "match")]:
            with self.subTest(minute=minute, second=second):
                now = self.now.replace(minute=minute, second=second)
                self.assertEqual(evaluate(self.candles(), self.session, now, self.previous)[0], expected)


if __name__ == "__main__":
    unittest.main()
