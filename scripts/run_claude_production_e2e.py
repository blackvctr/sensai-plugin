#!/usr/bin/env python3
"""Explicitly run the local public Sensai installation E2E."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sensai_plugin.claude_e2e_profile import ClaudeE2EProfileError  # noqa: E402
from sensai_plugin.claude_production_e2e import (  # noqa: E402
    ProductionE2EError,
    ProductionSensaiE2E,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the public Sensai installation E2E in a disposable local Claude profile."
    )
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument(
        "--expected-public-readme-sha256",
        required=True,
        help="SHA-256 of the exact public README candidate to test",
    )
    parser.add_argument(
        "--first-comparison",
        action="store_true",
        help="Observe the first reply and deny all effects after the public README fetch",
    )
    arguments = parser.parse_args(argv)
    try:
        runner = ProductionSensaiE2E(
            profile=arguments.profile,
            expected_public_readme_sha256=arguments.expected_public_readme_sha256,
            first_comparison=arguments.first_comparison,
        )
        report = runner.compare_first_response() if arguments.first_comparison else runner.run()
    except (ClaudeE2EProfileError, ProductionE2EError) as error:
        parser.exit(1, f"PRODUCTION_E2E_FAILED phase={error}\n")
    except Exception:
        parser.exit(1, "PRODUCTION_E2E_FAILED phase=unexpected\n")
    if arguments.first_comparison:
        first_tool = report.first_tool_intent or "none"
        print(
            "PRODUCTION_E2E_COMPARISON "
            f"text={report.first_text_kind} first_tool={first_tool} "
            f"denied={','.join(report.denied_tool_intents) or 'none'}"
        )
        return 0
    if not report.complete:
        parser.exit(1, "PRODUCTION_E2E_FAILED phase=incomplete_safe_report\n")
    print("PRODUCTION_E2E_PASS installation=connected=new_chat")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
