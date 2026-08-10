#!/usr/bin/env python3
"""Synchronize the repository's native Codex and Claude marketplaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sensai_plugin.package_builder import build_packages, plugin_version  # noqa: E402

CODEX_MARKETPLACE_PATH = REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json"
CLAUDE_MARKETPLACE_PATH = REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_PATH = REPOSITORY_ROOT / "plugins" / "sensai"


def _document_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _codex_marketplace_bytes() -> bytes:
    return _document_json(
        {
            "name": "sensai",
            "interface": {"displayName": "Sensai"},
            "plugins": [
                {
                    "name": "sensai",
                    "source": {"source": "local", "path": "./plugins/sensai"},
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Productivity",
                }
            ],
        }
    )


def _claude_marketplace_bytes(version: str) -> bytes:
    return _document_json(
        {
            "name": "sensai",
            "owner": {
                "name": "Black Vector",
                "email": "sergey@black-vector.com",
            },
            "description": "Sensai guidance for AI agents.",
            "plugins": [
                {
                    "name": "sensai",
                    "source": "./plugins/sensai",
                    "description": "Practical guidance for an AI agent.",
                    "version": version,
                    "category": "productivity",
                }
            ],
        }
    )


def _regular_files(root: Path) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _combined_plugin_files(codex: Path, claude: Path) -> dict[str, bytes]:
    codex_files = _regular_files(codex)
    claude_files = _regular_files(claude)
    codex_files.pop("MANIFEST.sha256")
    claude_files.pop("MANIFEST.sha256")

    shared = {".mcp.json", "skills/sensai/SKILL.md"}
    for relative in shared:
        if codex_files.get(relative) != claude_files.get(relative):
            raise RuntimeError(f"platform payloads disagree on shared file: {relative}")

    combined = dict(codex_files)
    for relative, content in claude_files.items():
        if relative in shared:
            continue
        if relative in combined:
            raise RuntimeError(f"platform payload collision: {relative}")
        combined[relative] = content
    combined["MANIFEST.sha256"] = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {relative}\n"
        for relative, content in sorted(combined.items())
    ).encode()
    return dict(sorted(combined.items()))


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def synchronize(*, check: bool) -> bool:
    with tempfile.TemporaryDirectory(prefix="sensai-public-marketplace-") as temporary:
        version = plugin_version(REPOSITORY_ROOT)
        built = build_packages(
            source_root=REPOSITORY_ROOT / "payload-src",
            output_root=Path(temporary) / "packages",
            version=version,
        )
        expected_plugin = _combined_plugin_files(built.codex, built.claude)
        expected_codex = _codex_marketplace_bytes()
        claude_manifest = json.loads(
            (built.claude / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        built_version = claude_manifest.get("version")
        if built_version != version:
            raise RuntimeError("Claude plugin manifest has no version")
        expected_claude = _claude_marketplace_bytes(version)

        if check:
            return (
                _regular_files(PLUGIN_PATH) == expected_plugin
                and CODEX_MARKETPLACE_PATH.is_file()
                and CODEX_MARKETPLACE_PATH.read_bytes() == expected_codex
                and CLAUDE_MARKETPLACE_PATH.is_file()
                and CLAUDE_MARKETPLACE_PATH.read_bytes() == expected_claude
            )

        for path in (PLUGIN_PATH,):
            if path.is_symlink():
                raise RuntimeError(f"public path must not be a symlink: {path}")
            if path.exists():
                shutil.rmtree(path)
        _write_files(PLUGIN_PATH, expected_plugin)
        CODEX_MARKETPLACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CODEX_MARKETPLACE_PATH.write_bytes(expected_codex)
        CLAUDE_MARKETPLACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_MARKETPLACE_PATH.write_bytes(expected_claude)
        return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if not synchronize(check=arguments.check):
        parser.exit(1, "public marketplaces are out of date\n")


if __name__ == "__main__":
    main()
