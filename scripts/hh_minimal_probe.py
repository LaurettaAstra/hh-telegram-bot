#!/usr/bin/env python3
"""
Temporary HH API minimal probe (same shared Session + HH-User-Agent as the bot).

Run from repository root:

    ./venv/bin/python scripts/hh_minimal_probe.py

Or:

    python scripts/hh_minimal_probe.py

Exit code: 0 on HTTP 200, 1 on failure.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.hh_api import hh_api_minimal_probe  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return hh_api_minimal_probe()


if __name__ == "__main__":
    raise SystemExit(main())
