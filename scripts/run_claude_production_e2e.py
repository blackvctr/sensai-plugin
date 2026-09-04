#!/usr/bin/env python3
"""Explicitly run the local production Sensai installation-and-Telegram E2E."""

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
    SshOperatorProofVerifier,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the public Sensai production E2E in a disposable local Claude profile. "
            "The profile must already have been provisioned explicitly."
        )
    )
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument(
        "--verify-telegram-provenance",
        action="store_true",
        help="also require the separately configured strict SSH provenance proof",
    )
    arguments = parser.parse_args(argv)
    try:
        operator_proof = (
            SshOperatorProofVerifier() if arguments.verify_telegram_provenance else None
        )
        report = ProductionSensaiE2E(
            profile=arguments.profile,
            operator_proof=operator_proof,
        ).run()
    except (ClaudeE2EProfileError, ProductionE2EError) as error:
        parser.exit(1, f"PRODUCTION_E2E_FAILED phase={error}\n")
    except Exception:
        parser.exit(1, "PRODUCTION_E2E_FAILED phase=unexpected\n")
    if not report.complete:
        parser.exit(1, "PRODUCTION_E2E_FAILED phase=incomplete_safe_report\n")
    print(
        "PRODUCTION_E2E_PASS installation=telegram=cleanup "
        f"telegram_provenance={report.telegram_provenance.value}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
