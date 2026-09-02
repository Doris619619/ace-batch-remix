"""Windows entry point for the ACE-Step batch remix client."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.runner import BatchRemixRunner


def parse_args() -> argparse.Namespace:
    """Parse CLI mode, selection, and text2music identity arguments."""
    parser = argparse.ArgumentParser(description="Submit ACE-Step batch Remix or text2music jobs.")
    parser.add_argument("--mode", choices=("remix", "text2music"), default="remix", help="Generation workflow (default: remix).")
    parser.add_argument("--count", type=int, help="Required total output count for --mode text2music.")
    parser.add_argument("--label", help="Stable text2music output label, used in its folder and filenames.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="Process only the first N sorted input files.")
    selection.add_argument("--file", dest="selected_file", help="Process exactly one audio file under input/.")
    parser.add_argument("--caption", dest="caption_override", help="Use this caption only for this run; config.json is not modified.")
    return parser.parse_args()


def main() -> int:
    """Run the selected generation workflow and return its process status."""
    args = parse_args()
    return BatchRemixRunner(
        Path(__file__).resolve().parent,
        limit=args.limit,
        selected_file=args.selected_file,
        caption_override=args.caption_override,
        mode=args.mode,
        count=args.count,
        text_label=args.label,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
