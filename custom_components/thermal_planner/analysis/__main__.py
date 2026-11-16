"""Replay exported fixtures without installing Home Assistant."""

import argparse
import json
from pathlib import Path

from . import replay, to_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    report = replay(fixture["config"], fixture["history"], fixture["start"], fixture["end"])
    if args.csv:
        args.csv.write_text(to_csv(report))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
