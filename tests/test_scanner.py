import unittest
from datetime import date, datetime

from scanner import Candle, IST, evaluate


class PatternTests(unittest.TestCase):
    session = date(2026, 10, 30)
    now = datetime(2026, 10, 30, 9, 30, tzinfo=IST)

    def candles(self, second=(110, 90), third=(109, 91)):
        return [Candle(datetime(2026, 10, 30, 9, m, tzinfo=IST), 100, h, l, 100)
                for m, (h, l) in zip((15, 20, 25), ((110, 90), second, third))]

    def test_inclusive_boundaries(self):
        self.assertEqual(evaluate(self.candles(third=(110, 90)), self.session, self.now)[0], "match")

    def test_either_later_candle_outside_rejects(self):
        for second, third in (((111, 90), (109, 91)), ((110, 90), (109, 89))):
            with self.subTest(second=second, third=third):
                self.assertEqual(evaluate(self.candles(second, third), self.session, self.now)[0], "no_match")

    def test_missing_candle(self):
        self.assertEqual(evaluate(self.candles()[:2], self.session, self.now)[0], "incomplete")

    def test_open_third_candle_not_used(self):
        now = self.now.replace(minute=29, second=59)
        self.assertEqual(evaluate(self.candles(), self.session, now)[0], "pending")

    def test_other_session_not_used(self):
        self.assertEqual(evaluate(self.candles(), date(2026, 10, 29), self.now)[0], "incomplete")

    def test_duplicate_candle_rejected(self):
        candles = self.candles()
        with self.assertRaises(ValueError):
            evaluate(candles + candles[:1], self.session, self.now)

    def test_invalid_price_rejected(self):
        with self.assertRaises(ValueError):
            Candle(self.now, 100, float("nan"), 90, 100)


if __name__ == "__main__":
    unittest.main()
