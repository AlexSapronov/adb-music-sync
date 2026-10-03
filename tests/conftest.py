"""Test path bootstrap: add src/ and tests/ to sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TESTS = ROOT / "tests"

for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
