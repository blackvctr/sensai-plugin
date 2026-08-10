"""Boundary for deterministic Sensai plugin package generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SourceTreeError(ValueError):
    """Base error for an incomplete, unexpected, or unsafe payload source tree."""


class UnexpectedSourceFileError(SourceTreeError):
    """Raised when payload source contains a file outside the explicit allowlist."""


class MissingRequiredSourceFileError(SourceTreeError):
    """Raised when an allowlisted payload source file is absent."""


class UnsafeSourceError(SourceTreeError):
    """Raised when source material could escape or expose private data."""


@dataclass(frozen=True)
class BuiltPackages:
    """Roots of the independently installable platform payloads."""

    codex: Path
    claude: Path


_SOURCE_TO_PAYLOADS: dict[str, tuple[tuple[str, str], ...]] = {
    "shared/.mcp.json": (
        ("codex", ".mcp.json"),
        ("claude", ".mcp.json"),
    ),
    "shared/skills/sensai/SKILL.md": (
        ("codex", "skills/sensai/SKILL.md"),
        ("claude", "skills/sensai/SKILL.md"),
    ),
    "codex/.codex-plugin/plugin.json": (("codex", ".codex-plugin/plugin.json"),),
    "claude/.claude-plugin/plugin.json": (("claude", ".claude-plugin/plugin.json"),),
}
_REQUIRED_FILES = frozenset(_SOURCE_TO_PAYLOADS)
_REQUIRED_DIRECTORIES = frozenset(
    {
        "shared",
        "shared/skills",
        "shared/skills/sensai",
        "codex",
        "codex/.codex-plugin",
        "claude",
        "claude/.claude-plugin",
    }
)
_EXPECTED_PAYLOAD_FILES: dict[str, frozenset[str]] = {
    "codex": frozenset(
        {
            ".codex-plugin/plugin.json",
            ".mcp.json",
            "skills/sensai/SKILL.md",
        }
    ),
    "claude": frozenset(
        {
            ".claude-plugin/plugin.json",
            ".mcp.json",
            "skills/sensai/SKILL.md",
        }
    ),
}
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![/:A-Za-z0-9_])/(?!/)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")
_POSIX_SINGLE_SEGMENT_ROOT = re.compile(
    r"(?<![/:A-Za-z0-9_])/(?:bin|dev|etc|home|media|mnt|opt|proc|root|run|sbin|srv|sys|"
    r"tmp|usr|var|Users|Volumes)(?=$|[\s`'\"\]),.;])"
)
_WINDOWS_UNC_PATH = re.compile(r"\\\\[A-Za-z0-9._$-]+\\+[A-Za-z0-9._$-]+")
_PARENT_REFERENCE = re.compile(r"(?:^|[\s`'\"(])\.\./")
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~-]{16,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{40,255})"
)
_PRIVATE_SERVER_REFERENCE = re.compile(
    r"(?i)(?:\bfrom\s+sensai\.server\b|\bimport\s+sensai\.server\b|"
    r"(?:^|[\s`'\"])(?:server/src/sensai|src/sensai)/[^\s`'\"]+)"
)
_SEMVER = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?\Z")


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def plugin_version(repository_root: Path) -> str:
    """Read the single public package version from pyproject.toml."""

    project_path = repository_root / "pyproject.toml"
    try:
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))
        version = project["project"]["version"]
    except (FileNotFoundError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise UnsafeSourceError("pyproject.toml must declare the plugin version") from error
    if not isinstance(version, str) or _SEMVER.fullmatch(version) is None:
        raise UnsafeSourceError("Plugin version must be a semantic version")
    return version


def _validate_version(version: str) -> None:
    if _SEMVER.fullmatch(version) is None:
        raise UnsafeSourceError("Plugin version must be a semantic version")


def _validate_source_tree(source_root: Path) -> dict[str, bytes]:
    if source_root.is_symlink() or not source_root.is_dir():
        raise UnsafeSourceError("Payload source root must be a regular directory")

    found_files: set[str] = set()
    found_directories: set[str] = set()
    for current_root, directory_names, file_names in os.walk(source_root, followlinks=False):
        current = Path(current_root)
        for name in sorted((*directory_names, *file_names)):
            entry = current / name
            relative = _relative_posix(entry, source_root)
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise UnsafeSourceError(f"Source symlink is not allowed: {relative}")
            if stat.S_ISDIR(mode):
                found_directories.add(relative)
            elif stat.S_ISREG(mode):
                found_files.add(relative)
            else:
                raise UnsafeSourceError(f"Unsupported source entry: {relative}")

    missing = _REQUIRED_FILES - found_files
    if missing:
        raise MissingRequiredSourceFileError(
            f"Missing required payload source: {sorted(missing)[0]}"
        )

    unexpected_files = found_files - _REQUIRED_FILES
    unexpected_directories = found_directories - _REQUIRED_DIRECTORIES
    if unexpected_files or unexpected_directories:
        unexpected = sorted((*unexpected_files, *unexpected_directories))[0]
        raise UnexpectedSourceFileError(f"Unexpected payload source entry: {unexpected}")

    source_bytes = {
        relative: (source_root / relative).read_bytes() for relative in sorted(_REQUIRED_FILES)
    }
    for relative, content in source_bytes.items():
        _validate_public_content(relative, content)
    _validate_structured_contracts(source_bytes)
    return source_bytes


def _decode_utf8(relative: str, content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeSourceError(f"Payload source is not UTF-8 text: {relative}") from error


def _validate_public_content(relative: str, content: bytes) -> None:
    text = _decode_utf8(relative, content)
    if "\r" in text:
        raise UnsafeSourceError(f"Non-canonical line endings in payload source: {relative}")
    if (
        _WINDOWS_ABSOLUTE_PATH.search(text)
        or _WINDOWS_UNC_PATH.search(text)
        or _POSIX_ABSOLUTE_PATH.search(text)
        or _POSIX_SINGLE_SEGMENT_ROOT.search(text)
    ):
        raise UnsafeSourceError(f"Local absolute path in payload source: {relative}")
    if _PARENT_REFERENCE.search(text):
        raise UnsafeSourceError(f"Unresolved parent reference in payload source: {relative}")
    if _SECRET_VALUE.search(text):
        raise UnsafeSourceError(f"Secret-like value in payload source: {relative}")
    if _PRIVATE_SERVER_REFERENCE.search(text):
        raise UnsafeSourceError(f"Private server reference in payload source: {relative}")
    if "payload-src" in text:
        raise UnsafeSourceError(f"Build-tree reference in payload source: {relative}")


def _load_json_object(relative: str, content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(_decode_utf8(relative, content))
    except json.JSONDecodeError as error:
        raise UnsafeSourceError(f"Invalid JSON payload source: {relative}") from error
    if not isinstance(value, dict):
        raise UnsafeSourceError(f"JSON payload source must be an object: {relative}")
    return value


def _validate_relative_reference(relative: str, field: str, value: object) -> None:
    if not isinstance(value, str):
        raise UnsafeSourceError(f"Plugin manifest field {field} must be a string: {relative}")
    normalized = value.replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if value.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH.match(value) or ".." in parts:
        raise UnsafeSourceError(f"Unsafe plugin manifest reference in {relative}: {field}")


def _validate_structured_contracts(source_bytes: dict[str, bytes]) -> None:
    mcp = _load_json_object("shared/.mcp.json", source_bytes["shared/.mcp.json"])
    if not isinstance(mcp.get("mcpServers"), dict):
        raise UnsafeSourceError("MCP configuration must contain an mcpServers object")

    for platform in ("codex", "claude"):
        relative = f"{platform}/.{platform}-plugin/plugin.json"
        manifest = _load_json_object(relative, source_bytes[relative])
        if "version" in manifest:
            raise UnsafeSourceError(f"Plugin version belongs in pyproject.toml, not {relative}")
        _validate_relative_reference(relative, "skills", manifest.get("skills"))
        _validate_relative_reference(relative, "mcpServers", manifest.get("mcpServers"))
        if manifest["skills"].rstrip("/") not in ("skills", "./skills"):
            raise UnsafeSourceError(
                f"Plugin skills must resolve inside its package root: {relative}"
            )
        if manifest["mcpServers"] not in (".mcp.json", "./.mcp.json"):
            raise UnsafeSourceError(
                f"Plugin MCP config must resolve inside its package root: {relative}"
            )


def _render_manifest(content: bytes, version: str) -> bytes:
    manifest = _load_json_object("plugin.json", content)
    manifest["version"] = version
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_payload(
    staging_root: Path, source_bytes: dict[str, bytes], platform: str, version: str
) -> Path:
    payload_root = staging_root / platform / "sensai"
    for source_relative, destinations in _SOURCE_TO_PAYLOADS.items():
        for destination_platform, payload_relative in destinations:
            if destination_platform != platform:
                continue
            destination = payload_root / payload_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = source_bytes[source_relative]
            if payload_relative.endswith("plugin.json"):
                content = _render_manifest(content, version)
            destination.write_bytes(content)

    manifest_lines = []
    for relative in sorted(_EXPECTED_PAYLOAD_FILES[platform]):
        content = (payload_root / relative).read_bytes()
        manifest_lines.append(f"{hashlib.sha256(content).hexdigest()}  {relative}\n")
    (payload_root / "MANIFEST.sha256").write_text("".join(manifest_lines), encoding="utf-8")
    _validate_built_payload(payload_root, platform, version)
    return payload_root


def _validate_built_payload(payload_root: Path, platform: str, version: str) -> None:
    expected = _EXPECTED_PAYLOAD_FILES[platform] | {"MANIFEST.sha256"}
    found: set[str] = set()
    for path in payload_root.rglob("*"):
        relative = path.relative_to(payload_root).as_posix()
        if path.is_symlink():
            raise UnsafeSourceError(f"Generated payload contains a symlink: {relative}")
        if path.is_file():
            found.add(relative)
    if found != expected:
        raise UnsafeSourceError("Generated payload file set does not match its contract")
    _validate_generated_manifest(payload_root)

    root = payload_root.resolve(strict=True)
    manifest_relative = f".{platform}-plugin/plugin.json"
    manifest = _load_json_object(manifest_relative, (payload_root / manifest_relative).read_bytes())
    if manifest.get("version") != version:
        raise UnsafeSourceError(
            "Generated plugin manifest version does not match the build version"
        )
    for field in ("skills", "mcpServers"):
        raw = manifest[field]
        assert isinstance(raw, str)
        target = (payload_root / raw).resolve(strict=True)
        if not target.is_relative_to(root):
            raise UnsafeSourceError(f"Generated plugin reference escapes package root: {field}")


def _validate_generated_manifest(payload_root: Path) -> None:
    manifest_path = payload_root / "MANIFEST.sha256"
    manifest = _decode_utf8("MANIFEST.sha256", manifest_path.read_bytes())
    expected_lines = []
    for path in sorted(payload_root.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        relative = path.relative_to(payload_root).as_posix()
        expected_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
    if manifest != "".join(expected_lines):
        raise UnsafeSourceError("Generated SHA-256 manifest does not match payload bytes")


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path)


def _publish(staging_root: Path, output_root: Path) -> None:
    if output_root.is_symlink():
        raise UnsafeSourceError("Package output root must not be a symlink")
    if output_root.exists() and not output_root.is_dir():
        raise UnsafeSourceError("Package output root must be a directory")

    if not output_root.exists():
        staging_root.rename(output_root)
        return

    backup_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}-previous-", dir=output_root.parent)
    )
    backup_root.rmdir()
    output_root.rename(backup_root)
    try:
        staging_root.rename(output_root)
    except BaseException:
        if backup_root.exists() and not output_root.exists():
            backup_root.rename(output_root)
        raise

    try:
        _remove_tree(backup_root)
    except BaseException as cleanup_error:
        output_root.rename(staging_root)
        backup_root.rename(output_root)
        _remove_tree(staging_root)
        raise cleanup_error


def build_packages(*, source_root: Path, output_root: Path, version: str) -> BuiltPackages:
    """Build deterministic Codex and Claude payloads from allowlisted source files."""
    _validate_version(version)
    source_bytes = _validate_source_tree(source_root)
    resolved_source = source_root.resolve(strict=True)
    resolved_output = output_root.resolve(strict=False)
    if resolved_output.is_relative_to(resolved_source) or resolved_source.is_relative_to(
        resolved_output
    ):
        raise UnsafeSourceError("Payload source and package output roots must not overlap")
    output_parent = output_root.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f".{output_root.name}-build-", dir=output_parent))
    try:
        _write_payload(staging_root, source_bytes, "codex", version)
        _write_payload(staging_root, source_bytes, "claude", version)
        _publish(staging_root, output_root)
    except BaseException:
        if staging_root.exists():
            _remove_tree(staging_root)
        raise

    return BuiltPackages(
        codex=output_root / "codex" / "sensai",
        claude=output_root / "claude" / "sensai",
    )
