#!/usr/bin/env python3
"""Seed the local SQLite memory database with the default agent team."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ensure_memory import main as ensure_memory_main  # noqa: E402


def main() -> int:
    return ensure_memory_main()


if __name__ == "__main__":
    raise SystemExit(main())
