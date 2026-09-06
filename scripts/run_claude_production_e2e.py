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
        help="Observe one tool-free first response after the out-of-band README SHA-256 audit",
    )
    arguments = parser.parse_args(argv)
    try:
        runner = ProductionSensaiE2E(
            profile=arguments.profile,
            expected_public_readme_sha256=arguments.expected_public_readme_sha256,
            first_comparison=arguments.first_comparison,
        )
        if arguments.first_comparison:
            comparison = runner.observe_tool_free_first_response()
            print(
                "PRODUCTION_E2E_TOOL_FREE_FIRST_RESPONSE "
                f"text={comparison.first_text_kind}"
            )
            return 0
        report = runner.run()
    except (ClaudeE2EProfileError, ProductionE2EError) as error:
        receipt = (
            error.before_marketplace_receipt
            if isinstance(error, ProductionE2EError)
            else None
        )
        detail = f" {receipt.machine_line()}" if receipt is not None else ""
        parser.exit(1, f"PRODUCTION_E2E_FAILED phase={error}{detail}\n")
    except Exception:
        parser.exit(1, "PRODUCTION_E2E_FAILED phase=unexpected\n")
    if not report.complete:
        parser.exit(1, "PRODUCTION_E2E_FAILED phase=incomplete_safe_report\n")
    print("PRODUCTION_E2E_PASS installation=connected=new_chat")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
