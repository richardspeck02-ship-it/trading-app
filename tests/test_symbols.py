from pathlib import Path
import tempfile
import unittest

from scanner import load_symbols


class SymbolTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "symbols.txt"
            path.write_text(text)
            return load_symbols(path)

    def test_preserves_supported_series_and_special_characters(self):
        symbols = ["NSE:M&M-EQ", "NSE:BAJAJ-AUTO-EQ", "NSE:HFCL-BE", "NSE:BIRET-RR"]
        self.assertEqual(self.load("\n".join(symbols)), symbols)

    def test_duplicate_and_invalid_symbols_fail(self):
        for text in ("NSE:ABB-EQ\nNSE:ABB-EQ", "NSE:ABB-INVALID", "", "NSE:ABB-EQ\nBAD"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)
