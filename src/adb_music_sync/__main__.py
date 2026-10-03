"""Entry point."""

from __future__ import annotations

import sys


def main() -> int:
    # Import here so `python -m adb_music_sync` is cheap and GUI-free on import.
    from .gui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
