"""Windows entry point for the ACE-Step batch remix client."""

from __future__ import annotations

from pathlib import Path
import sys

from src.runner import BatchRemixRunner


def main() -> int:
    return BatchRemixRunner(Path(__file__).resolve().parent).run()


if __name__ == "__main__":
    raise SystemExit(main())
