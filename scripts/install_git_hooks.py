#!/usr/bin/env python3
"""Enable this repository's versioned Git hooks for the current clone."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOOKS_PATH = ".githooks"


def main() -> None:
    hooks_directory = REPOSITORY_ROOT / HOOKS_PATH
    if not (hooks_directory / "pre-push").is_file():
        raise SystemExit("pre-push hook is missing")
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", HOOKS_PATH],
        cwd=REPOSITORY_ROOT,
        check=True,
    )
    print(f"Enabled Git hooks from {HOOKS_PATH} for this clone.")


if __name__ == "__main__":
    main()
