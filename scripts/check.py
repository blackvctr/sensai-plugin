#!/usr/bin/env python3
"""Run the checks required before publishing a Sensai plugin commit."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MCP_URL = "https://black-vector.com/sensai/mcp"


def run(command: list[str], *, environment: dict[str, str]) -> None:
    print(f"+ {shlex.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=REPOSITORY_ROOT, env=environment, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sensai-plugin-check-", dir="/tmp") as temporary_root:
        environment = os.environ.copy()
        environment.update(
            {
                "TMPDIR": temporary_root,
                "TMP": temporary_root,
                "TEMP": temporary_root,
            }
        )
        release_bundle = str(Path(temporary_root) / "release")
        run(["uv", "run", "ruff", "check", "src", "tests"], environment=environment)
        run(
            [
                "uv",
                "run",
                "pytest",
                "-q",
                "-m",
                "not codex_real_cli and not claude_real_cli",
            ],
            environment=environment,
        )
        run(
            [
                "uv",
                "run",
                "python",
                "scripts/build_release.py",
                "--output",
                release_bundle,
                "--mcp-url",
                MCP_URL,
            ],
            environment=environment,
        )
        run(
            [
                "uv",
                "run",
                "python",
                "scripts/verify_release.py",
                "--bundle",
                release_bundle,
            ],
            environment=environment,
        )


if __name__ == "__main__":
    main()
