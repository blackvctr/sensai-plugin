from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from sensai_plugin.package_builder import (
    BuiltPackages,
    plugin_version,
)
from sensai_plugin.package_builder import (
    build_packages as _build_packages,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SOURCE_URL = "https://github.com/blackvctr/sensai-plugin"
PLUGIN_VERSION = plugin_version(REPOSITORY_ROOT)


def build_packages(*, source_root: Path, output_root: Path) -> BuiltPackages:
    return _build_packages(
        source_root=source_root,
        output_root=output_root,
        version=plugin_version(REPOSITORY_ROOT),
    )


def _regular_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_public_repository_is_a_ready_codex_marketplace(tmp_path: Path) -> None:
    marketplace_path = REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))

    assert marketplace == {
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

    claude_marketplace = json.loads(
        (REPOSITORY_ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    assert claude_marketplace == {
        "name": "sensai",
        "owner": {"name": "Black Vector", "email": "sergey@black-vector.com"},
        "description": "Sensai guidance for AI agents.",
        "plugins": [
            {
                "name": "sensai",
                "source": "./plugins/sensai",
                "description": "Practical guidance for an AI agent.",
                "version": PLUGIN_VERSION,
                "category": "productivity",
            }
        ],
    }

    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )
    committed = REPOSITORY_ROOT / "plugins" / "sensai"
    committed_files = _regular_files(committed)
    codex_files = _regular_files(built.codex)
    claude_files = _regular_files(built.claude)

    assert committed_files[".codex-plugin/plugin.json"] == codex_files[".codex-plugin/plugin.json"]
    assert (
        committed_files[".claude-plugin/plugin.json"] == claude_files[".claude-plugin/plugin.json"]
    )
    for shared in (".mcp.json", "skills/sensai/SKILL.md"):
        assert committed_files[shared] == codex_files[shared] == claude_files[shared]
    assert set(committed_files) == {
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "MANIFEST.sha256",
        "skills/sensai/SKILL.md",
    }


def test_committed_plugin_manifest_is_computed_from_packaged_files() -> None:
    committed = REPOSITORY_ROOT / "plugins" / "sensai"
    files = _regular_files(committed)
    manifest = files.pop("MANIFEST.sha256")
    expected = b"".join(
        f"{hashlib.sha256(content).hexdigest()}  {relative}\n".encode()
        for relative, content in sorted(files.items())
    )

    assert manifest == expected


def test_plugin_version_is_consistent_across_build_and_public_marketplaces(tmp_path: Path) -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_version = project["project"]["version"]
    assert isinstance(build_version, str)

    assert plugin_version(REPOSITORY_ROOT) == build_version
    for platform in ("codex", "claude"):
        source_manifest = json.loads(
            (REPOSITORY_ROOT / f"payload-src/{platform}/.{platform}-plugin/plugin.json").read_text(
                encoding="utf-8"
            )
        )
        assert "version" not in source_manifest
    marketplace = json.loads(
        (REPOSITORY_ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )

    assert marketplace["plugins"][0]["version"] == build_version

    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )
    for platform in ("codex", "claude"):
        manifest = json.loads(
            (getattr(built, platform) / f".{platform}-plugin/plugin.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["version"] == build_version


def test_both_platform_manifests_expose_the_public_source_repository(tmp_path: Path) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    codex_manifest = json.loads(
        (built.codex / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    claude_manifest = json.loads(
        (built.claude / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert codex_manifest["repository"] == PUBLIC_SOURCE_URL
    assert claude_manifest["repository"] == PUBLIC_SOURCE_URL
    assert codex_manifest["homepage"] == PUBLIC_SOURCE_URL
    assert claude_manifest["homepage"] == PUBLIC_SOURCE_URL
