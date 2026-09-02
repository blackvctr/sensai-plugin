#!/usr/bin/env python3
"""Run one real, redacted Claude first-reply acceptance check."""

from __future__ import annotations

import argparse

from sensai_plugin.claude_first_reply import FirstReplyScenario, run_real_claude_first_reply


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=[item.value for item in FirstReplyScenario],
        required=True,
    )
    arguments = parser.parse_args()
    result = run_real_claude_first_reply(scenario=FirstReplyScenario(arguments.scenario))
    print(result.safe_json())
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
