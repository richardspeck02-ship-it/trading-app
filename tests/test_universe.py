from pathlib import Path
import tempfile
import unittest

from universe import convert_constituents, load_live_symbols, validate_live_symbols


class UniverseTests(unittest.TestCase):
    def test_bundled_nifty100_snapshot_has_100_unique_symbols(self):
        path = Path(__file__).resolve().parents[1] / "nifty100.txt"
        symbols = load_live_symbols(path)
        self.assertEqual(len(symbols), 100)
        self.assertEqual(len(set(symbols)), 100)
        self.assertTrue(all(s.endswith("-EQ") for s in symbols))
        self.assertFalse(any("DUMMY" in s for s in symbols))

    def test_exactly_100_accepted_and_101_rejected(self):
        self.assertEqual(len(validate_live_symbols([f"NSE:S{i}-EQ" for i in range(100)])), 100)
        with self.assertRaisesRegex(ValueError, "Nothing was subscribed or truncated"):
            validate_live_symbols([f"NSE:S{i}-EQ" for i in range(101)])

    def test_csv_conversion_preserves_series_and_special_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.csv", Path(directory) / "nifty100.txt"
            source.write_text("Company Name,Symbol,Series\nExample,M&M,EQ\nExample,HFCL,BE\n")
            converted = convert_constituents(source, output)
            self.assertEqual(converted, ["NSE:M&M-EQ", "NSE:HFCL-BE"])
            self.assertEqual(load_live_symbols(output), converted)
            with self.assertRaises(FileExistsError):
                convert_constituents(source, output)

    def test_invalid_csv_never_creates_output(self):
        for text in ("Symbol,Series\nABB,EQ\nABB,EQ\n", "Wrong,Header\nABB,EQ\n",
                     "Symbol,Series\nABB,BAD\n", "Symbol,Series\n"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                source, output = Path(directory) / "source.csv", Path(directory) / "nifty100.txt"
                source.write_text(text)
                with self.assertRaises(ValueError):
                    convert_constituents(source, output)
                self.assertFalse(output.exists())
