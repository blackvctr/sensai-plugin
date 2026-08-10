"""Deterministic local release bundle generation for Sensai marketplaces."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sensai_plugin.package_builder import (
    BuiltPackages,
    UnsafeSourceError,
    build_packages,
    plugin_version,
)

MCP_CONTRACT_VERSION = "1"
_ATTESTATION_NAME = "sensai-mcp-attestation.json"
_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MARKETPLACE_NAME = "sensai-local"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _document_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _canonical_contract_surface(value: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if set(value) != {"tools", "resources", "prompts"}:
        raise UnsafeSourceError("MCP contract must contain tools, resources, and prompts")
    surface: dict[str, list[dict[str, Any]]] = {}
    for field in ("prompts", "resources", "tools"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise UnsafeSourceError(f"MCP contract field must be an array of objects: {field}")
        surface[field] = sorted(items, key=_canonical_json)
    return surface


def _attestation_bytes(*, schema_hash: str, mcp_url: str) -> bytes:
    return _document_json(
        {
            "format_version": "1",
            "mcp_contract_version": MCP_CONTRACT_VERSION,
            "mcp_schema_sha256": schema_hash,
            "mcp_url": mcp_url,
        }
    )


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise UnsafeSourceError(f"Expected a JSON object: {path.name}")
    return value


def _validate_mcp_url(mcp_url: str) -> None:
    parsed = urlsplit(mcp_url)
    try:
        _ = parsed.port
    except ValueError as error:
        raise UnsafeSourceError("MCP URL is malformed") from error
    if (
        any(ord(character) <= 32 or ord(character) == 127 for character in mcp_url)
        or "\\" in mcp_url
        or parsed.geturl() != mcp_url
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise UnsafeSourceError("MCP URL must be an HTTP(S) URL without credentials or fragment")


def _prepare_source(source_root: Path, destination: Path, mcp_url: str) -> Path:
    copied = destination / "payload-src"
    shutil.copytree(source_root, copied, symlinks=True)
    mcp_path = copied / "shared" / ".mcp.json"
    mcp = _load_object(mcp_path)
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {"sensai"}:
        raise UnsafeSourceError("MCP source must define exactly the Sensai server")
    sensai = servers["sensai"]
    if not isinstance(sensai, dict):
        raise UnsafeSourceError("Sensai MCP configuration must be an object")
    sensai["url"] = mcp_url
    mcp_path.write_bytes(_document_json(mcp))
    return copied


def _write_marketplace(root: Path, packages: BuiltPackages, platform: str) -> Path:
    marketplace = root / platform
    plugin_root = marketplace / "plugins" / "sensai"
    shutil.copytree(getattr(packages, platform), plugin_root)
    if platform == "codex":
        manifest_path = marketplace / ".agents" / "plugins" / "marketplace.json"
        manifest: dict[str, Any] = {
            "name": _MARKETPLACE_NAME,
            "plugins": [
                {
                    "name": "sensai",
                    "policy": {"installation": "AVAILABLE"},
                    "source": {"path": "./plugins/sensai", "source": "local"},
                }
            ],
        }
    else:
        manifest_path = marketplace / ".claude-plugin" / "marketplace.json"
        manifest = {
            "description": "Local immutable Sensai release marketplace.",
            "name": _MARKETPLACE_NAME,
            "owner": {"name": "Sensai"},
            "plugins": [
                {
                    "description": "Sensai guidance plugin.",
                    "name": "sensai",
                    "source": "./plugins/sensai",
                }
            ],
        }
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(_document_json(manifest))
    return marketplace


def _regular_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise UnsafeSourceError(f"Unsafe marketplace entry: {relative}")
        if stat.S_ISREG(mode):
            files[relative] = path.read_bytes()
    return files


def _write_payload_attestation(payload_root: Path, content: bytes) -> None:
    (payload_root / _ATTESTATION_NAME).write_bytes(content)
    files = _regular_files(payload_root)
    files.pop("MANIFEST.sha256")
    (payload_root / "MANIFEST.sha256").write_bytes(
        "".join(
            f"{hashlib.sha256(file_content).hexdigest()}  {relative}\n"
            for relative, file_content in sorted(files.items())
        ).encode()
    )


def _write_archive(marketplace: Path, archive_path: Path) -> dict[str, str]:
    files = _regular_files(marketplace)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, content in files.items():
            info = zipfile.ZipInfo(relative, date_time=_ARCHIVE_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o444) << 16
            info.flag_bits = 0
            archive.writestr(info, content)
    return {relative: hashlib.sha256(content).hexdigest() for relative, content in files.items()}


def _publish(staging: Path, output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise UnsafeSourceError("Release output already exists")
    staging.rename(output)


def build_release(
    *,
    repository_root: Path,
    output: Path,
    mcp_url: str,
) -> Path:
    """Build and atomically publish one deterministic local release bundle."""
    _validate_mcp_url(mcp_url)
    source_root = repository_root / "payload-src"
    if source_root.is_symlink() or not source_root.is_dir():
        raise UnsafeSourceError("Payload source root must be a regular directory")
    resolved_source = source_root.resolve(strict=True)
    resolved_output = output.resolve(strict=False)
    if resolved_output.is_relative_to(resolved_source) or resolved_source.is_relative_to(
        resolved_output
    ):
        raise UnsafeSourceError("Release output and payload source roots must not overlap")
    if output.exists() or output.is_symlink():
        raise UnsafeSourceError("Release output already exists")
    version = plugin_version(repository_root)
    contract = _canonical_contract_surface(
        _load_object(repository_root / "contracts" / "mcp-surface-v1.json")
    )
    schema_hash = hashlib.sha256(_canonical_json(contract)).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-release-", dir=output.parent))
    try:
        with tempfile.TemporaryDirectory(prefix="sensai-release-build-") as workspace_value:
            workspace = Path(workspace_value)
            prepared_source = _prepare_source(source_root, workspace, mcp_url)
            packages = build_packages(
                source_root=prepared_source,
                output_root=workspace / "packages",
                version=version,
            )
            attestation = _attestation_bytes(schema_hash=schema_hash, mcp_url=mcp_url)
            for payload_root in (packages.codex, packages.claude):
                _write_payload_attestation(payload_root, attestation)
            platform_metadata: dict[str, Any] = {}
            for platform in ("codex", "claude"):
                marketplace = _write_marketplace(workspace / "marketplaces", packages, platform)
                archive_name = f"sensai-{version}-{platform}-marketplace.zip"
                archive_path = staging / archive_name
                files = _write_archive(marketplace, archive_path)
                platform_metadata[platform] = {
                    "archive": archive_name,
                    "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                    "files": files,
                    "mcp_url": mcp_url,
                    "plugin_version": version,
                }
        metadata = {
            "format_version": "1",
            "mcp_contract_version": MCP_CONTRACT_VERSION,
            "mcp_schema_sha256": schema_hash,
            "mcp_url": mcp_url,
            "platforms": platform_metadata,
            "release_version": version,
        }
        (staging / "release.json").write_bytes(_document_json(metadata))
        _publish(staging, output)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return output
