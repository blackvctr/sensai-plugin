from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sensai_plugin import claude_acceptance as lifecycle
from sensai_plugin.claude_acceptance import (
    ClaudeAcceptanceError,
    InstalledClaudePlugin,
    installed_claude_plugin,
)
from sensai_plugin.release_builder import build_release

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

MCP_URL = "http://127.0.0.1:8765/mcp"


@pytest.fixture(scope="session")
def built_release_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    bundle = tmp_path_factory.mktemp("claude-release") / "release"
    build_release(
        repository_root=REPOSITORY_ROOT,
        output=bundle,
        mcp_url=MCP_URL,
    )
    return bundle


@pytest.fixture
def release_bundle(built_release_bundle: Path, tmp_path: Path) -> Path:
    bundle = tmp_path / "tampered-release"
    shutil.copytree(built_release_bundle, bundle)
    return bundle


@pytest.fixture
def claude_workspace() -> Iterator[Path]:
    workspace = Path(tempfile.mkdtemp(prefix="sensai-claude-unit-", dir="/tmp"))
    try:
        yield workspace
    finally:
        lifecycle.remove_readonly_tree(workspace)


def test_prepare_verified_marketplace_extracts_exact_read_only_claude_release(
    release_bundle: Path,
    claude_workspace: Path,
) -> None:
    destination = claude_workspace / "marketplace"

    release = lifecycle.prepare_verified_marketplace(release_bundle, destination)

    metadata = json.loads((release_bundle / "release.json").read_text(encoding="utf-8"))
    expected_files = metadata["platforms"]["claude"]["files"]
    actual_files = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files
    assert release.marketplace == destination
    assert release.selector == "sensai@sensai-local"
    assert release.version == metadata["release_version"]
    assert release.mcp_url == MCP_URL
    assert release.mcp_attestation == {
        "format_version": "1",
        "mcp_contract_version": metadata["mcp_contract_version"],
        "mcp_schema_sha256": metadata["mcp_schema_sha256"],
        "mcp_url": MCP_URL,
    }
    for path in (destination, *destination.rglob("*")):
        mode = path.stat().st_mode
        assert mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert destination.parent.stat().st_mode & stat.S_IWUSR

    lifecycle.remove_readonly_tree(destination)
    assert not destination.exists()
    assert destination.parent.exists()


def test_prepare_verified_marketplace_fails_before_extracting_tampered_release(
    release_bundle: Path,
    claude_workspace: Path,
) -> None:
    metadata = json.loads((release_bundle / "release.json").read_text(encoding="utf-8"))
    archive = release_bundle / metadata["platforms"]["claude"]["archive"]
    archive.write_bytes(archive.read_bytes() + b"tampered")
    destination = claude_workspace / "marketplace"

    with pytest.raises(ClaudeAcceptanceError, match="release verification failed"):
        lifecycle.prepare_verified_marketplace(release_bundle, destination)

    assert not destination.exists()


def test_prepare_uses_verified_snapshot_when_source_changes_after_verification(
    release_bundle: Path,
    claude_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_verify = lifecycle._verify_release_independently

    def verify_then_replace_source(snapshot: Path) -> dict[str, object]:
        result = original_verify(snapshot)
        metadata = json.loads((release_bundle / "release.json").read_text(encoding="utf-8"))
        metadata["mcp_url"] = "https://attacker.invalid/mcp"
        (release_bundle / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
        return result

    monkeypatch.setattr(lifecycle, "_verify_release_independently", verify_then_replace_source)
    destination = claude_workspace / "marketplace"

    release = lifecycle.prepare_verified_marketplace(release_bundle, destination)

    assert release.mcp_url == MCP_URL
    lifecycle.remove_readonly_tree(destination)


def test_prepare_rejects_snapshot_mutation_after_verification(
    release_bundle: Path,
    claude_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_verify = lifecycle._verify_release_independently

    def verify_then_mutate_snapshot(snapshot: Path) -> dict[str, object]:
        result = original_verify(snapshot)
        metadata_path = snapshot / "release.json"
        metadata_path.chmod(0o644)
        metadata_path.write_bytes(metadata_path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(lifecycle, "_verify_release_independently", verify_then_mutate_snapshot)

    with pytest.raises(
        ClaudeAcceptanceError, match="snapshot changed after independent verification"
    ):
        lifecycle.prepare_verified_marketplace(release_bundle, claude_workspace / "marketplace")


def test_real_profile_paths_cover_all_relevant_claude_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "custom-config"
    plugin_cache = tmp_path / "custom-cache"
    secure_storage = tmp_path / "custom-secure-storage"
    xdg_cache = tmp_path / "cache"
    xdg_config = tmp_path / "config"
    xdg_data = tmp_path / "data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", str(plugin_cache))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure_storage))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    paths = set(lifecycle._real_profile_paths())

    expected = {
        config / "plugins",
        config / "backups",
        plugin_cache,
        secure_storage,
        home / ".claude.json",
        xdg_cache / "claude",
        xdg_cache / "claude-cli-nodejs",
        xdg_config / "claude",
        xdg_data / "claude",
        xdg_data / "claude-code",
        lifecycle.REPOSITORY_ROOT / ".claude",
    }
    assert expected <= paths
    assert {path.resolve(strict=False) for path in expected} <= paths
    roots = set(lifecycle._real_config_roots())
    assert config in roots
    assert config.resolve(strict=False) in roots
    assert config / "file-history" not in paths


def test_real_profile_fingerprint_detects_marketplace_and_config_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    known_marketplaces = config / "plugins" / "known_marketplaces.json"
    known_marketplaces.parent.mkdir(parents=True)
    known_marketplaces.write_text('{"sensai-local": true}\n', encoding="utf-8")
    (config / "settings.json").write_text('{"enabledPlugins": []}\n', encoding="utf-8")

    before = lifecycle._real_profile_fingerprint()
    known_marketplaces.write_text('{"sensai-local": false}\n', encoding="utf-8")
    marketplace_changed = lifecycle._real_profile_fingerprint()
    (config / "settings.json").write_text('{"enabledPlugins": ["sensai"]}\n', encoding="utf-8")

    assert marketplace_changed != before
    assert lifecycle._real_profile_fingerprint() != marketplace_changed


def test_real_profile_fingerprint_does_not_recurse_into_unrelated_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    cache = Path(os.environ["XDG_CACHE_HOME"]) / "claude-cli-nodejs" / "unrelated"
    cache.mkdir(parents=True)
    for index in range(300):
        nested = cache / f"project-{index % 3}" / f"session-{index}"
        nested.mkdir(parents=True)
        (nested / "large-history.jsonl").write_bytes(b"x" * (64 * 1024))

    started = time.monotonic()
    before = lifecycle._real_profile_fingerprint()
    elapsed = time.monotonic() - started
    (cache / "project-0" / "session-0" / "large-history.jsonl").write_bytes(b"changed")

    assert elapsed < 1.0
    assert lifecycle._real_profile_fingerprint() == before


@pytest.mark.claude_real_cli
def test_real_claude_cli_installs_exact_verified_release(release_bundle: Path) -> None:
    claude = shutil.which("claude")
    assert claude is not None, "official Claude CLI is required for explicit real-CLI acceptance"
    temporary_root = lifecycle._temporary_root()
    before = set(temporary_root.glob("sensai-claude-profile-*"))

    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "test_claude_lifecycle.py"),
            "--bundle",
            str(release_bundle),
        ],
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PASS selector=sensai@sensai-local" in completed.stdout
    assert "PASS version=0.2.6" in completed.stdout
    assert f"PASS mcp={MCP_URL}" in completed.stdout
    assert set(temporary_root.glob("sensai-claude-profile-*")) == before


def test_explicit_real_cli_acceptance_fails_when_claude_is_unavailable(
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(AssertionError, match="official Claude CLI is required"):
        test_real_claude_cli_installs_exact_verified_release(release_bundle)


def _write_fake_claude(
    executable: Path,
    log: Path,
    *,
    installed_payload: str = "exact",
    mcp_get_url: str = MCP_URL,
) -> None:
    fake = """#!/usr/bin/env python3
import json
import os
import shutil
import stat
import sys
from pathlib import Path

arguments = sys.argv[1:]
config = Path(os.environ["CLAUDE_CONFIG_DIR"])
cache = Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"])
with Path(__LOG__).open("a", encoding="utf-8") as output:
    entry = {"arguments": arguments, "config": str(config), "cache": str(cache)}
    output.write(json.dumps(entry) + "\\n")

if arguments[:3] == ["plugin", "marketplace", "add"]:
    marketplace = Path(arguments[3])
    assert marketplace.is_dir() and marketplace.suffix != ".zip"
    for path in marketplace.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o444 if path.is_file() else 0o555)
    (config / "marketplace.txt").write_text(str(marketplace), encoding="utf-8")
elif arguments[:2] == ["plugin", "install"]:
    assert arguments[2:] == ["sensai@sensai-local", "--scope", "user"]
    marketplace = Path((config / "marketplace.txt").read_text(encoding="utf-8"))
    installed = cache / "sensai" / "0.2.6"
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(marketplace / "plugins" / "sensai", installed)
    if __INSTALLED_PAYLOAD__ == "changed":
        for path in (installed, *installed.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)
        (installed / "SKILL.md").write_text("changed payload\\n", encoding="utf-8")
    elif __INSTALLED_PAYLOAD__ == "symlink":
        installed.chmod(0o700)
        (installed / "unexpected-link").symlink_to("SKILL.md")
elif arguments == ["plugin", "list", "--json"]:
    installed = cache / "sensai" / "0.2.6"
    print(json.dumps([{
        "id": "sensai@sensai-local",
        "version": "0.2.6",
        "scope": "user",
        "enabled": True,
        "installPath": str(installed),
        "mcpServers": {"sensai": {
            "type": "http",
            "url": __URL__,
        }},
    }]))
elif arguments == ["mcp", "get", "plugin:sensai:sensai"]:
    print("plugin:sensai:sensai:\\nType: http\\nURL: " + __MCP_GET_URL__)
else:
    raise SystemExit("unexpected command: " + repr(arguments))
"""
    executable.write_text(
        textwrap.dedent(fake)
        .replace("__LOG__", repr(str(log)))
        .replace("__URL__", repr(MCP_URL))
        .replace("__INSTALLED_PAYLOAD__", repr(installed_payload))
        .replace("__MCP_GET_URL__", repr(mcp_get_url)),
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _use_empty_real_claude_profile(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    home = root / "real-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "real-config"))
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", str(root / "real-plugin-cache"))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(root / "real-secure-storage"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "real-xdg-cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "real-xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "real-xdg-data"))


def test_public_context_rejects_tampering_before_invoking_claude(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    bundle = tmp_path / "release"
    shutil.copytree(release_bundle, bundle)
    archive = bundle / "sensai-0.2.6-claude-marketplace.zip"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    marker = tmp_path / "claude-was-invoked"
    executable = tmp_path / "claude"
    executable.write_text(f"#!/bin/sh\nprintf invoked > {marker!s}\n", encoding="utf-8")
    executable.chmod(0o755)

    with (
        pytest.raises(ClaudeAcceptanceError, match="release verification failed"),
        installed_claude_plugin(bundle, claude_executable=str(executable)),
    ):
        pytest.fail("tampered release must not reach the caller")

    assert not marker.exists()


def test_public_context_rejects_symlink_bundle_before_invoking_claude(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    bundle_link = tmp_path / "release-link"
    bundle_link.symlink_to(release_bundle, target_is_directory=True)
    marker = tmp_path / "claude-was-invoked"
    executable = tmp_path / "claude"
    executable.write_text(f"#!/bin/sh\nprintf invoked > {marker!s}\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(ClaudeAcceptanceError, match="release bundle must not be a symlink"):
        lifecycle.prepare_verified_marketplace(bundle_link, tmp_path / "marketplace")

    with (
        pytest.raises(ClaudeAcceptanceError, match="release bundle must not be a symlink"),
        installed_claude_plugin(bundle_link, claude_executable=str(executable)),
    ):
        pytest.fail("symlinked release must not reach the caller")

    assert not marker.exists()


def test_public_context_keeps_profile_alive_and_cleans_after_caller_failure(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    executable = tmp_path / "claude"
    log = tmp_path / "commands.jsonl"
    _write_fake_claude(executable, log)
    live_profile: Path | None = None

    with (
        pytest.raises(RuntimeError, match="caller failed"),
        installed_claude_plugin(release_bundle, claude_executable=str(executable)) as installed,
    ):
        assert isinstance(installed, InstalledClaudePlugin)
        assert installed.selector == "sensai@sensai-local"
        assert installed.version == "0.2.6"
        assert installed.mcp_url == MCP_URL
        assert installed.profile.exists()
        assert (installed.profile / "plugin-cache").is_dir()
        live_profile = installed.profile
        raise RuntimeError("caller failed")

    assert live_profile is not None and not live_profile.exists()
    commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [entry["arguments"] for entry in commands] == [
        ["plugin", "marketplace", "add", commands[0]["arguments"][3]],
        ["plugin", "install", "sensai@sensai-local", "--scope", "user"],
        ["plugin", "list", "--json"],
        ["mcp", "get", "plugin:sensai:sensai"],
    ]
    assert all(entry["config"].startswith(str(live_profile)) for entry in commands)
    assert all(entry["cache"].startswith(str(live_profile)) for entry in commands)


def test_public_context_rejects_replaced_private_snapshot_before_claude_runs(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    marker = tmp_path / "claude-was-invoked"
    executable = tmp_path / "claude"
    executable.write_text(f"#!/bin/sh\nprintf invoked > {marker!s}\n", encoding="utf-8")
    executable.chmod(0o755)
    original_verify = lifecycle._verify_release_independently

    def verify_then_replace(snapshot: Path) -> dict[str, object]:
        result = original_verify(snapshot)
        original = snapshot.with_name("bundle-original")
        snapshot.rename(original)
        shutil.copytree(original, snapshot, copy_function=shutil.copy2)
        return result

    monkeypatch.setattr(lifecycle, "_verify_release_independently", verify_then_replace)

    with (
        pytest.raises(BaseException, match="changed after independent verification"),
        installed_claude_plugin(release_bundle, claude_executable=str(executable)),
    ):
        pytest.fail("replaced snapshot must not reach the caller")

    assert not marker.exists()


@pytest.mark.parametrize("installed_payload", ["changed", "symlink"])
def test_public_context_rejects_installed_payload_not_identical_to_marketplace(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_payload: str,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    executable = tmp_path / "claude"
    _write_fake_claude(
        executable,
        tmp_path / "commands.jsonl",
        installed_payload=installed_payload,
    )

    with (
        pytest.raises(ClaudeAcceptanceError, match="installed Claude plugin payload"),
        installed_claude_plugin(release_bundle, claude_executable=str(executable)),
    ):
        pytest.fail("invalid installed payload must not reach the caller")


def test_public_context_rejects_profile_nested_in_real_claude_config_boundary(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("SENSAI_CLAUDE_LIFECYCLE_TMPDIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    marker = tmp_path / "claude-was-invoked"
    executable = tmp_path / "claude"
    executable.write_text(f"#!/bin/sh\nprintf invoked > {marker!s}\n", encoding="utf-8")
    executable.chmod(0o755)

    with (
        pytest.raises(ClaudeAcceptanceError, match="overlaps real Claude profile boundary"),
        installed_claude_plugin(release_bundle, claude_executable=str(executable)),
    ):
        pytest.fail("overlapping isolated profile must not reach the caller")

    assert not marker.exists()


def test_isolated_profile_rejects_containing_real_claude_config_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "isolated-profile"
    profile.mkdir()
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile / "real-config"))

    with pytest.raises(ClaudeAcceptanceError, match="overlaps real Claude profile boundary"):
        lifecycle._assert_isolated_profile_separate(profile)


@pytest.mark.parametrize("mcp_get_url", [f"{MCP_URL}/unexpected-suffix", f"prefix-{MCP_URL}"])
def test_public_context_rejects_mcp_get_url_with_prefix_or_suffix(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    mcp_get_url: str,
) -> None:
    _use_empty_real_claude_profile(monkeypatch, tmp_path)
    executable = tmp_path / "claude"
    _write_fake_claude(
        executable,
        tmp_path / "commands.jsonl",
        mcp_get_url=mcp_get_url,
    )

    with (
        pytest.raises(ClaudeAcceptanceError, match="did not report exact plugin details"),
        installed_claude_plugin(release_bundle, claude_executable=str(executable)),
    ):
        pytest.fail("ambiguous MCP URL must not reach the caller")
