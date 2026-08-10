from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import pytest

from sensai_plugin.package_builder import (
    BuiltPackages,
    MissingRequiredSourceFileError,
    SourceTreeError,
    UnexpectedSourceFileError,
    UnsafeSourceError,
)
from sensai_plugin.package_builder import (
    build_packages as _build_packages,
)

SourceMutation = Callable[[Path, Path], None]
HASH_LINE_SEPARATOR = "  "
EXPECTED_PAYLOAD_FILES = {
    "codex": {
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "MANIFEST.sha256",
        "skills/sensai/SKILL.md",
    },
    "claude": {
        ".claude-plugin/plugin.json",
        ".mcp.json",
        "MANIFEST.sha256",
        "skills/sensai/SKILL.md",
    },
}


def build_packages(*, source_root: Path, output_root: Path) -> BuiltPackages:
    return _build_packages(source_root=source_root, output_root=output_root, version="1.2.3")


def _regular_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _assert_manifest(root: Path) -> None:
    files = _regular_files(root)
    manifest = files.pop("MANIFEST.sha256").decode("utf-8")
    lines = manifest.splitlines()
    assert lines == sorted(lines, key=lambda line: line.split(HASH_LINE_SEPARATOR, 1)[1])
    assert manifest.endswith("\n")
    assert len(lines) == len(files)

    entries: dict[str, str] = {}
    for line in lines:
        digest, separator, relative_path = line.partition(HASH_LINE_SEPARATOR)
        assert separator == HASH_LINE_SEPARATOR
        assert len(digest) == 64 and digest == digest.lower()
        int(digest, 16)
        assert "\\" not in relative_path
        entries[relative_path] = digest

    assert set(entries) == set(files)
    for relative_path, content in files.items():
        assert entries[relative_path] == hashlib.sha256(content).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _assert_independent_root(root: Path, platform: str) -> None:
    manifest_path = root / f".{platform}-plugin" / "plugin.json"
    manifest = _load_json(manifest_path)
    for field in ("skills", "mcpServers"):
        raw_reference = manifest[field]
        assert isinstance(raw_reference, str)
        reference = PurePosixPath(raw_reference)
        assert not reference.is_absolute() and ".." not in reference.parts
        assert (root / reference).resolve(strict=True).is_relative_to(root.resolve(strict=True))

    assert (root / "skills" / "sensai" / "SKILL.md").is_file()
    assert (root / ".mcp.json").is_file()


def _inject_unexpected_regular_file(source_root: Path, _: Path) -> None:
    (source_root / "shared" / "notes.txt").write_text("not allowlisted\n", encoding="utf-8")


def _inject_unexpected_directory_content(source_root: Path, _: Path) -> None:
    unexpected = source_root / "shared" / "extras" / "notes.txt"
    unexpected.parent.mkdir()
    unexpected.write_text("not allowlisted\n", encoding="utf-8")


def _inject_unexpected_empty_directory(source_root: Path, _: Path) -> None:
    (source_root / "shared" / "empty-extra").mkdir()


def _inject_unexpected_source_symlink(source_root: Path, outside_root: Path) -> None:
    outside_root.mkdir()
    target = outside_root / "outside-notes.txt"
    target.write_text("outside source tree\n", encoding="utf-8")
    link = source_root / "shared" / "outside-notes.txt"
    link.symlink_to(target)
    assert link.is_symlink()


UNEXPECTED_ENTRY_CASES: tuple[tuple[SourceMutation, type[SourceTreeError]], ...] = (
    (_inject_unexpected_regular_file, UnexpectedSourceFileError),
    (_inject_unexpected_directory_content, UnexpectedSourceFileError),
    (_inject_unexpected_empty_directory, UnexpectedSourceFileError),
    (_inject_unexpected_source_symlink, UnsafeSourceError),
)


def _remove_required_file(source_root: Path, _: Path) -> None:
    (source_root / "codex" / ".codex-plugin" / "plugin.json").unlink()


def _replace_allowlisted_file_with_symlink(source_root: Path, outside_root: Path) -> None:
    required = source_root / "shared" / "skills" / "sensai" / "SKILL.md"
    target = outside_root / "outside-skill.md"
    target.write_text("outside source tree\n", encoding="utf-8")
    required.unlink()
    required.symlink_to(target)
    assert required.is_symlink()


def _replace_allowlisted_directory_with_escape(source_root: Path, outside_root: Path) -> None:
    required_directory = source_root / "shared" / "skills"
    escaped_skill = outside_root / "skills" / "sensai" / "SKILL.md"
    escaped_skill.parent.mkdir(parents=True)
    escaped_skill.write_text("outside source tree\n", encoding="utf-8")
    shutil.rmtree(required_directory)
    required_directory.symlink_to(escaped_skill.parents[1], target_is_directory=True)
    assert required_directory.is_symlink()
    assert (required_directory / "sensai" / "SKILL.md").resolve() == escaped_skill.resolve()


REQUIRED_SOURCE_CASES: tuple[tuple[SourceMutation, type[SourceTreeError]], ...] = (
    (_remove_required_file, MissingRequiredSourceFileError),
    (_replace_allowlisted_file_with_symlink, UnsafeSourceError),
    (_replace_allowlisted_directory_with_escape, UnsafeSourceError),
)


def _inject_windows_absolute_path(source_root: Path, _: Path) -> None:
    mcp_path = source_root / "shared" / ".mcp.json"
    value = _load_json(mcp_path)
    value["mcpServers"]["sensai"]["url"] = r"C:\Users\alice\private\mcp"
    _write_json(mcp_path, value)


def _inject_posix_absolute_path(source_root: Path, _: Path) -> None:
    mcp_path = source_root / "shared" / ".mcp.json"
    value = _load_json(mcp_path)
    value["mcpServers"]["sensai"]["url"] = "/home/alice/private/mcp"
    _write_json(mcp_path, value)


def _inject_secret_content(source_root: Path, _: Path) -> None:
    mcp_path = source_root / "shared" / ".mcp.json"
    value = _load_json(mcp_path)
    value["mcpServers"]["sensai"]["headers"] = {
        "Authorization": "Bearer sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    }
    _write_json(mcp_path, value)


def _inject_private_server_import(source_root: Path, _: Path) -> None:
    skill_path = source_root / "shared" / "skills" / "sensai" / "SKILL.md"
    with skill_path.open("a", encoding="utf-8") as handle:
        handle.write("\nfrom sensai.server.runtime import run_private_runtime\n")


def _inject_private_server_path_reference(source_root: Path, _: Path) -> None:
    skill_path = source_root / "shared" / "skills" / "sensai" / "SKILL.md"
    with skill_path.open("a", encoding="utf-8") as handle:
        handle.write("\nLoad server/src/sensai/private_runtime.py.\n")


def _inject_unresolved_parent_reference(source_root: Path, _: Path) -> None:
    manifest_path = source_root / "codex" / ".codex-plugin" / "plugin.json"
    value = _load_json(manifest_path)
    value["skills"] = "../shared/skills/"
    _write_json(manifest_path, value)


def _inject_one_segment_posix_path(source_root: Path, _: Path) -> None:
    mcp_path = source_root / "shared" / ".mcp.json"
    value = _load_json(mcp_path)
    value["mcpServers"]["sensai"]["url"] = "/tmp"
    _write_json(mcp_path, value)


def _inject_windows_unc_path(source_root: Path, _: Path) -> None:
    mcp_path = source_root / "shared" / ".mcp.json"
    value = _load_json(mcp_path)
    value["mcpServers"]["sensai"]["url"] = r"\\server\share\secrets"
    _write_json(mcp_path, value)


def _inject_general_private_server_path(source_root: Path, _: Path) -> None:
    skill_path = source_root / "shared" / "skills" / "sensai" / "SKILL.md"
    with skill_path.open("a", encoding="utf-8") as handle:
        handle.write("\nLoad server/src/sensai/runtime.py.\n")


def _inject_github_token(source_root: Path, _: Path) -> None:
    mcp_path = source_root / "shared" / ".mcp.json"
    value = _load_json(mcp_path)
    value["mcpServers"]["sensai"]["headers"] = {"X-Token": "ghp_" + "a" * 36}
    _write_json(mcp_path, value)


UNSAFE_CONTENT_CASES: tuple[SourceMutation, ...] = (
    _inject_windows_absolute_path,
    _inject_posix_absolute_path,
    _inject_secret_content,
    _inject_private_server_import,
    _inject_private_server_path_reference,
    _inject_unresolved_parent_reference,
)

REVIEWER_UNSAFE_CONTENT_CASES: tuple[SourceMutation, ...] = (
    _inject_one_segment_posix_path,
    _inject_windows_unc_path,
    _inject_general_private_server_path,
    _inject_github_token,
)


def _inject_environment_file(source_root: Path, _: Path) -> None:
    (source_root / ".env").write_text("SENSAI_TOKEN=secret\n", encoding="utf-8")


def _inject_secret_like_name(source_root: Path, _: Path) -> None:
    (source_root / "shared" / "credentials.json").write_text("{}\n", encoding="utf-8")


def _inject_test_file(source_root: Path, _: Path) -> None:
    test_file = source_root / "tests" / "test_payload.py"
    test_file.parent.mkdir()
    test_file.write_text("assert True\n", encoding="utf-8")


def _inject_build_file(source_root: Path, _: Path) -> None:
    build_file = source_root / "scripts" / "build_payload.py"
    build_file.parent.mkdir()
    build_file.write_text("raise SystemExit(0)\n", encoding="utf-8")


UNSAFE_NAME_CASES: tuple[SourceMutation, ...] = (
    _inject_environment_file,
    _inject_secret_like_name,
    _inject_test_file,
    _inject_build_file,
)


def test_plugin_package_001_r01_builds_are_byte_reproducible(
    source_copy: Path, tmp_path: Path
) -> None:
    first = build_packages(source_root=source_copy, output_root=tmp_path / "first")
    second = build_packages(source_root=source_copy, output_root=tmp_path / "second")

    assert _regular_files(first.codex) == _regular_files(second.codex)
    assert _regular_files(first.claude) == _regular_files(second.claude)
    _assert_manifest(first.codex)
    _assert_manifest(first.claude)
    _assert_manifest(second.codex)
    _assert_manifest(second.claude)


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    UNEXPECTED_ENTRY_CASES,
    ids=("regular-file", "directory-content", "empty-directory", "source-symlink"),
)
def test_plugin_package_001_r02_rejects_unexpected_source_entries(
    mutate: SourceMutation,
    expected_error: type[SourceTreeError],
    source_copy: Path,
    tmp_path: Path,
) -> None:
    mutate(source_copy, tmp_path / "outside")

    with pytest.raises(expected_error):
        build_packages(source_root=source_copy, output_root=tmp_path / "output")

    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    REQUIRED_SOURCE_CASES,
    ids=("missing-file", "file-symlink", "directory-symlink-escape"),
)
def test_plugin_package_001_r02_rejects_missing_or_substituted_required_source(
    mutate: SourceMutation,
    expected_error: type[SourceTreeError],
    source_copy: Path,
    tmp_path: Path,
) -> None:
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    mutate(source_copy, outside_root)

    with pytest.raises(expected_error):
        build_packages(source_root=source_copy, output_root=tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_plugin_package_001_r03_platform_contracts_derive_shared_runtime_material(
    source_copy: Path, tmp_path: Path
) -> None:
    skill_source = source_copy / "shared" / "skills" / "sensai" / "SKILL.md"
    skill_source.write_text(
        skill_source.read_text(encoding="utf-8") + "\nR03 source derivation marker.\n",
        encoding="utf-8",
    )
    mcp_source = source_copy / "shared" / ".mcp.json"
    mcp_value = _load_json(mcp_source)
    mcp_value["mcpServers"]["sensai"]["url"] = "https://example.test/r03-source-marker/mcp"
    _write_json(mcp_source, mcp_value)

    built = build_packages(source_root=source_copy, output_root=tmp_path / "output")

    assert set(_regular_files(built.codex)) == EXPECTED_PAYLOAD_FILES["codex"]
    assert set(_regular_files(built.claude)) == EXPECTED_PAYLOAD_FILES["claude"]
    expected_skill = skill_source.read_bytes()
    expected_mcp = mcp_source.read_bytes()
    for payload_root in (built.codex, built.claude):
        assert (payload_root / "skills" / "sensai" / "SKILL.md").read_bytes() == expected_skill
        assert (payload_root / ".mcp.json").read_bytes() == expected_mcp
    for platform in ("codex", "claude"):
        source_manifest = _load_json(
            source_copy / platform / f".{platform}-plugin" / "plugin.json"
        )
        built_manifest = _load_json(
            getattr(built, platform) / f".{platform}-plugin" / "plugin.json"
        )
        assert "version" not in source_manifest
        assert built_manifest == {**source_manifest, "version": "1.2.3"}


def test_plugin_version_cannot_be_reintroduced_into_a_payload_template(
    source_copy: Path, tmp_path: Path
) -> None:
    manifest_path = source_copy / "codex" / ".codex-plugin" / "plugin.json"
    manifest = _load_json(manifest_path)
    manifest["version"] = "9.9.9"
    _write_json(manifest_path, manifest)

    with pytest.raises(UnsafeSourceError, match=r"belongs in pyproject\.toml"):
        build_packages(source_root=source_copy, output_root=tmp_path / "output")


@pytest.mark.parametrize(
    "mutate",
    UNSAFE_CONTENT_CASES,
    ids=(
        "windows-absolute-path",
        "posix-absolute-path",
        "secret-content",
        "private-server-import",
        "private-server-path-reference",
        "unresolved-parent-reference",
    ),
)
def test_plugin_package_001_r04_rejects_unsafe_allowlisted_content(
    mutate: SourceMutation, source_copy: Path, tmp_path: Path
) -> None:
    mutate(source_copy, tmp_path / "outside")

    with pytest.raises(UnsafeSourceError):
        build_packages(source_root=source_copy, output_root=tmp_path / "output")

    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "mutate",
    REVIEWER_UNSAFE_CONTENT_CASES,
    ids=("one-segment-posix", "windows-unc", "general-server-path", "github-token"),
)
def test_plugin_package_001_review_rejects_additional_unsafe_content(
    mutate: SourceMutation, source_copy: Path, tmp_path: Path
) -> None:
    mutate(source_copy, tmp_path / "outside")

    with pytest.raises(UnsafeSourceError):
        build_packages(source_root=source_copy, output_root=tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_plugin_package_001_review_allows_non_secret_github_prose(
    source_copy: Path, tmp_path: Path
) -> None:
    skill_path = source_copy / "shared" / "skills" / "sensai" / "SKILL.md"
    with skill_path.open("a", encoding="utf-8") as handle:
        handle.write("\nGitHub personal access tokens commonly use the ghp_ prefix.\n")

    built = build_packages(source_root=source_copy, output_root=tmp_path / "output")

    assert built.codex.is_dir()
    assert built.claude.is_dir()


@pytest.mark.parametrize(
    "mutate",
    UNSAFE_NAME_CASES,
    ids=("environment-file", "secret-like-name", "test-file", "build-file"),
)
def test_plugin_package_001_r04_rejects_secret_or_development_source_names(
    mutate: SourceMutation, source_copy: Path, tmp_path: Path
) -> None:
    mutate(source_copy, tmp_path / "outside")

    with pytest.raises(SourceTreeError):
        build_packages(source_root=source_copy, output_root=tmp_path / "output")

    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("platform", ["codex", "claude"])
def test_plugin_package_001_r05_each_payload_is_a_self_contained_root(
    platform: str, source_copy: Path, tmp_path: Path
) -> None:
    built = build_packages(source_root=source_copy, output_root=tmp_path / "output")
    isolated_root = tmp_path / "isolated" / platform / "sensai"
    shutil.copytree(getattr(built, platform), isolated_root)

    _assert_independent_root(isolated_root, platform)


def test_mcp_min_r05_packages_exact_public_http_manifest(source_copy: Path, tmp_path: Path) -> None:
    source_manifest_path = source_copy / "shared" / ".mcp.json"
    source_manifest_bytes = source_manifest_path.read_bytes()
    expected_manifest = {
        "mcpServers": {
            "sensai": {
                "type": "http",
                "url": "https://black-vector.com/sensai/mcp",
            }
        }
    }

    source_manifest = _load_json(source_manifest_path)
    assert source_manifest == expected_manifest
    assert set(source_manifest["mcpServers"]) == {"sensai"}
    assert set(source_manifest["mcpServers"]["sensai"]) == {"type", "url"}

    endpoint = urlsplit(source_manifest["mcpServers"]["sensai"]["url"])
    assert endpoint.scheme == "https"
    assert endpoint.hostname == "black-vector.com"
    assert endpoint.username is None
    assert endpoint.password is None
    assert endpoint.query == ""
    assert endpoint.fragment == ""

    built = build_packages(source_root=source_copy, output_root=tmp_path / "output")
    codex_manifest_bytes = (built.codex / ".mcp.json").read_bytes()
    claude_manifest_bytes = (built.claude / ".mcp.json").read_bytes()

    assert codex_manifest_bytes == source_manifest_bytes
    assert claude_manifest_bytes == source_manifest_bytes
    assert codex_manifest_bytes == claude_manifest_bytes
    assert json.loads(codex_manifest_bytes) == expected_manifest
    assert json.loads(claude_manifest_bytes) == expected_manifest


def test_plugin_package_001_review_restores_previous_output_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, source_copy: Path, tmp_path: Path
) -> None:
    output_root = tmp_path / "output"
    build_packages(source_root=source_copy, output_root=output_root)
    previous_files = _regular_files(output_root)
    skill_path = source_copy / "shared" / "skills" / "sensai" / "SKILL.md"
    with skill_path.open("a", encoding="utf-8") as handle:
        handle.write("\nReplacement that must not be published.\n")

    real_rmtree = shutil.rmtree
    cleanup_failures = 0

    def fail_previous_cleanup(path: str | Path, *args: Any, **kwargs: Any) -> None:
        nonlocal cleanup_failures
        candidate = Path(path)
        if candidate.name.startswith(".output-previous-") and cleanup_failures == 0:
            cleanup_failures += 1
            raise OSError("injected previous-output cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("sensai_plugin.package_builder._remove_tree", fail_previous_cleanup)

    with pytest.raises(OSError, match="injected previous-output cleanup failure"):
        build_packages(source_root=source_copy, output_root=output_root)

    assert cleanup_failures == 1
    assert _regular_files(output_root) == previous_files
    assert sorted(path.name for path in tmp_path.iterdir()) == ["output", "payload-src"]
