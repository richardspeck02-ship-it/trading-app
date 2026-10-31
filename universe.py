"""Prepare a live universe from an official NSE/Nifty constituent CSV."""

import argparse
import csv
from pathlib import Path
import re

from scanner import load_symbols

STANDARD_LIMIT = 100


def validate_live_symbols(symbols):
    if not symbols:
        raise ValueError("The live watchlist is empty")
    if len(symbols) > STANDARD_LIMIT:
        raise ValueError(f"Standard-account live mode supports at most {STANDARD_LIMIT} symbols; "
                         f"this list contains {len(symbols)}. Nothing was subscribed or truncated.")
    return symbols


def load_live_symbols(path):
    return validate_live_symbols(load_symbols(path))


def convert_constituents(source, destination):
    with Path(source).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"Symbol", "Series"}.issubset(reader.fieldnames):
            raise ValueError("Expected an official constituent CSV with Symbol and Series columns")
        symbols = []
        for row in reader:
            symbol, series = row["Symbol"].strip(), row["Series"].strip()
            if not re.fullmatch(r"[A-Z0-9&-]+", symbol) or series not in ("EQ", "BE", "RR"):
                raise ValueError("CSV contains an invalid symbol or unsupported NSE series")
            symbols.append(f"NSE:{symbol}-{series}")
    if len(symbols) != len(set(symbols)):
        raise ValueError("CSV contains duplicate symbols")
    validate_live_symbols(symbols)
    # Preserve existing lists; never overwrite a user's edited universe.
    with Path(destination).open("x", encoding="utf-8") as stream:
        stream.write("".join(symbol + "\n" for symbol in symbols))
    return symbols


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="Official NIFTY 100 constituent CSV downloaded from Nifty Indices")
    parser.add_argument("--output", default="nifty100.txt")
    args = parser.parse_args()
    try:
        symbols = convert_constituents(args.csv, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(f"Saved {len(symbols)} symbols to {args.output}. Series preserved; no rows dropped.")


if __name__ == "__main__":
    main()
