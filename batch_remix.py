"""Windows entry point for the ACE-Step batch remix client."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.audio_concat import AudioConcatRunner


def parse_args() -> argparse.Namespace:
    """Parse CLI mode, selection, and text2music identity arguments."""
    parser = argparse.ArgumentParser(description="Run ACE-Step batch generation or local audio concatenation.")
    parser.add_argument("--mode", choices=("remix", "text2music", "concat"), default="remix", help="Workflow (default: remix).")
    parser.add_argument("--count", type=int, help="Required total output count for --mode text2music.")
    parser.add_argument("--label", help="Output label for text2music or required filename label for concat.")
    parser.add_argument("--playlist", help="UTF-8 playlist with one local audio path per line; required for --mode concat.")
    parser.add_argument("--ffmpeg-bin", help="FFmpeg command or full executable path; only valid for --mode concat.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="Process only the first N sorted input files.")
    selection.add_argument("--file", dest="selected_file", help="Process exactly one audio file under input/.")
    parser.add_argument("--caption", dest="caption_override", help="Use this caption only for this run; config.json is not modified.")
    args = parser.parse_args()
    if args.mode == "concat":
        if not args.playlist or not args.label:
            parser.error("--mode concat requires both --playlist and --label")
        if any(value is not None for value in (args.count, args.limit, args.selected_file, args.caption_override)):
            parser.error("--count, --limit, --file, and --caption are not valid for --mode concat")
    elif args.playlist or args.ffmpeg_bin:
        parser.error("--playlist and --ffmpeg-bin are only valid for --mode concat")
    return args


def main() -> int:
    """Run the selected generation workflow and return its process status."""
    args = parse_args()
    root = Path(__file__).resolve().parent
    if args.mode == "concat":
        return AudioConcatRunner(
            root,
            playlist=args.playlist,
            label=args.label,
            ffmpeg_bin=args.ffmpeg_bin or "ffmpeg",
        ).run()
    # Keep the local concat workflow usable even when requests is not installed.
    from src.runner import BatchRemixRunner

    return BatchRemixRunner(
        root,
        limit=args.limit,
        selected_file=args.selected_file,
        caption_override=args.caption_override,
        mode=args.mode,
        count=args.count,
        text_label=args.label,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
