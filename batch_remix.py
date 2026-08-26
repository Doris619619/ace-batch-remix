"""Windows entry point for the ACE-Step batch remix client."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.runner import BatchRemixRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit ACE-Step batch remix jobs from input/.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="Process only the first N sorted input files.")
    selection.add_argument("--file", dest="selected_file", help="Process exactly one audio file under input/.")
    parser.add_argument("--caption", dest="caption_override", help="Use this caption only for this run; config.json is not modified.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return BatchRemixRunner(
        Path(__file__).resolve().parent,
        limit=args.limit,
        selected_file=args.selected_file,
        caption_override=args.caption_override,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
