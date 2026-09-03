#!/usr/bin/env python3
"""Explicitly provision a local-only Claude profile for the production Sensai E2E."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sensai_plugin.claude_e2e_profile import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
