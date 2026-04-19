#!/usr/bin/env python3
"""
Temporary HH API probe: GET /vacancies with only per_page=1, page=0 (no text=).

Run from repository root:

    ./venv/bin/python scripts/hh_no_text_probe.py

Exit code: 0 on HTTP 200, 1 otherwise.

Logs use tag [HH_API_PROBE_NO_TEXT] (see app.hh_api.hh_api_no_text_probe).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.hh_api import hh_api_no_text_probe  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return hh_api_no_text_probe()


if __name__ == "__main__":
    raise SystemExit(main())
