"""Public acceptance context for one verified Codex plugin release."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERIFY_RELEASE = REPOSITORY_ROOT / "scripts" / "verify_release.py"
CODEX_TIMEOUT_SECONDS = 30
TERMINATION_GRACE_SECONDS = 2
MAX_BUNDLE_ENTRIES = 3
MAX_BUNDLE_FILE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 128
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_EXTRACTED_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
PASSTHROUGH_ENVIRONMENT_NAMES = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_BROWSER_GUARD_MARKER = "browser_open_attempt_blocked\n"
_BROWSER_LAUNCHER_NAMES = (
    "xdg-open",
    "gio",
    "sensible-browser",
    "wslview",
    "open",
    "cmd.exe",
    "powershell.exe",
)
PLUGIN_LIFECYCLE_BOUNDARY = (
    ".tmp/marketplaces",
    "plugins/cache/sensai-local",
)
SENSAI_TMP_PLUGIN_PARENTS = (
    ".tmp/plugins",
    ".tmp/plugins/.agents/plugins",
    ".tmp/plugins/plugins",
)


class CodexAcceptanceError(AssertionError):
    """Raised when Codex plugin acceptance fails closed."""


@dataclass(frozen=True, slots=True)
class InstalledCodexPlugin:
    """Observed plugin identity while its isolated Codex profile is alive."""

    selector: str
    version: str
    mcp_url: str
    profile: Path


def _json_object_bytes(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexAcceptanceError(f"Invalid JSON object: {label}") from error
    if not isinstance(value, dict):
        raise CodexAcceptanceError(f"Expected JSON object: {label}")
    return value


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(READ_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise CodexAcceptanceError(f"Could not read file: {path.name}") from error
    return digest.hexdigest()


def _fingerprint(path: Path) -> tuple[tuple[str, str], ...]:
    if not path.exists() and not path.is_symlink():
        return ((".", "missing"),)
    if path.is_symlink():
        return ((".", f"symlink:{os.readlink(path)}"),)
    if path.is_file():
        return ((".", _sha256_file(path)),)

    entries: list[tuple[str, str]] = []
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda child: child.name, reverse=True)
        except OSError as error:
            raise CodexAcceptanceError("Could not enumerate Codex profile") from error
        for child in children:
            child_path = Path(child.path)
            relative = child_path.relative_to(path).as_posix()
            try:
                if child.is_symlink():
                    entries.append((relative, f"symlink:{os.readlink(child.path)}"))
                elif child.is_file(follow_symlinks=False):
                    entries.append((relative, _sha256_file(child_path)))
                elif child.is_dir(follow_symlinks=False):
                    entries.append((relative, "directory"))
                    pending.append(child_path)
                else:
                    entries.append((relative, "other"))
            except OSError as error:
                raise CodexAcceptanceError("Could not fingerprint Codex profile") from error
    return tuple(sorted(entries))


def _sensai_tmp_plugins_fingerprint(resolved_home: Path) -> tuple[Any, ...]:
    parents: list[tuple[Any, ...]] = []
    for relative in SENSAI_TMP_PLUGIN_PARENTS:
        parent = resolved_home / relative
        if parent.is_symlink():
            parents.append((relative, f"symlink:{os.readlink(parent)}"))
            continue
        if not parent.exists():
            parents.append((relative, "missing"))
            continue
        if not parent.is_dir():
            parents.append((relative, "not-directory", _fingerprint(parent)))
            continue
        try:
            owned = sorted(
                Path(entry.path)
                for entry in os.scandir(parent)
                if "sensai" in entry.name.casefold()
            )
        except OSError as error:
            raise CodexAcceptanceError("Could not enumerate Sensai plugin staging") from error
        parents.append(
            (relative, "directory", tuple((path.name, _fingerprint(path)) for path in owned))
        )
    return tuple(parents)


def fingerprint_codex_plugin_state(codex_home: Path) -> tuple[Any, ...]:
    """Fingerprint the real-profile surface mutable by Codex plugin commands."""
    configured = codex_home.absolute()
    resolved = configured.resolve(strict=False)
    boundary_paths = {resolved / relative for relative in PLUGIN_LIFECYCLE_BOUNDARY}
    if resolved.is_dir():
        boundary_paths.update(resolved.glob("config.toml*"))
        boundary_paths.update(resolved.glob(".codex-global-state.json*"))
        boundary_paths.update(resolved.glob("..codex-global-state.json.tmp-*"))
    boundary = tuple(
        (path.relative_to(resolved).as_posix(), _fingerprint(path))
        for path in sorted(boundary_paths)
    )
    staging = _sensai_tmp_plugins_fingerprint(resolved)
    if configured.is_symlink():
        return (
            ("configured", f"symlink:{os.readlink(configured)}"),
            ("resolved", str(resolved)),
            boundary,
            staging,
        )
    return (("configured", "direct"), boundary, staging)


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = REPOSITORY_ROOT,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=CODEX_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise CodexAcceptanceError(f"Command timed out: {command[0]} {command[1]}") from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _snapshot_bundle(source: Path, destination: Path) -> tuple[tuple[str, str], ...]:
    try:
        entries = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError as error:
        raise CodexAcceptanceError("Could not enumerate release bundle") from error
    if len(entries) > MAX_BUNDLE_ENTRIES:
        raise CodexAcceptanceError("Release bundle contains too many entries")
    destination.mkdir(mode=0o700)
    fingerprint: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            raise CodexAcceptanceError("Release bundle contains a non-regular entry")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(entry.path, flags)
            with os.fdopen(descriptor, "rb") as source_file:
                before = os.fstat(source_file.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BUNDLE_FILE_BYTES:
                    raise CodexAcceptanceError("Invalid release bundle file")
                content = source_file.read(MAX_BUNDLE_FILE_BYTES + 1)
                after = os.fstat(source_file.fileno())
        except OSError as error:
            raise CodexAcceptanceError("Could not snapshot release bundle") from error
        if len(content) > MAX_BUNDLE_FILE_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise CodexAcceptanceError("Release bundle changed while snapshotting")
        target = destination / entry.name
        target.write_bytes(content)
        target.chmod(0o444)
        fingerprint.append((entry.name, _sha256_bytes(content)))
    destination.chmod(0o555)
    return tuple(fingerprint)


def _snapshot_fingerprint(snapshot: Path) -> tuple[tuple[Any, ...], ...]:
    """Bind the snapshot root and every direct entry to physical filesystem identity."""
    try:
        root_before = os.lstat(snapshot)
    except OSError as error:
        raise CodexAcceptanceError("Could not inspect private release snapshot") from error
    if not stat.S_ISDIR(root_before.st_mode):
        raise CodexAcceptanceError("Private release snapshot is not a directory")
    result: list[tuple[Any, ...]] = [
        (
            ".",
            "directory",
            stat.S_IMODE(root_before.st_mode),
            root_before.st_dev,
            root_before.st_ino,
            root_before.st_mtime_ns,
        )
    ]
    try:
        entries = sorted(os.scandir(snapshot), key=lambda entry: entry.name)
    except OSError as error:
        raise CodexAcceptanceError("Could not enumerate private release snapshot") from error
    for entry in entries:
        try:
            before = os.lstat(entry.path)
        except OSError as error:
            raise CodexAcceptanceError("Could not inspect private snapshot entry") from error
        mode = stat.S_IMODE(before.st_mode)
        if stat.S_ISREG(before.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(entry.path, flags)
                with os.fdopen(descriptor, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    content = handle.read(MAX_BUNDLE_FILE_BYTES + 1)
                    after = os.fstat(handle.fileno())
            except OSError as error:
                raise CodexAcceptanceError("Could not fingerprint private snapshot file") from error
            expected_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            if (
                len(content) > MAX_BUNDLE_FILE_BYTES
                or expected_identity
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                or expected_identity
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise CodexAcceptanceError("Private snapshot file changed while fingerprinting")
            result.append(
                (
                    entry.name,
                    "file",
                    mode,
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    _sha256_bytes(content),
                )
            )
        elif stat.S_ISDIR(before.st_mode):
            result.append(
                (
                    entry.name,
                    "directory",
                    mode,
                    before.st_dev,
                    before.st_ino,
                    before.st_mtime_ns,
                )
            )
        elif stat.S_ISLNK(before.st_mode):
            result.append((entry.name, "symlink", mode, os.readlink(entry.path)))
        else:
            result.append((entry.name, "other", mode, before.st_dev, before.st_ino))
    try:
        root_after = os.lstat(snapshot)
    except OSError as error:
        raise CodexAcceptanceError("Private release snapshot disappeared") from error
    if (
        root_before.st_dev,
        root_before.st_ino,
        root_before.st_mode,
        root_before.st_mtime_ns,
    ) != (
        root_after.st_dev,
        root_after.st_ino,
        root_after.st_mode,
        root_after.st_mtime_ns,
    ):
        raise CodexAcceptanceError("Private release snapshot changed while fingerprinting")
    return tuple(result)


def _source_bundle_fingerprint(source: Path) -> tuple[tuple[str, str], ...]:
    try:
        entries = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError as error:
        raise CodexAcceptanceError("Could not enumerate source release bundle") from error
    result: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            raise CodexAcceptanceError("Source release bundle contains a non-regular entry")
        result.append((entry.name, _sha256_file(Path(entry.path))))
    return tuple(result)


def _verify_release(bundle: Path) -> dict[str, Any]:
    completed = _run([sys.executable, str(VERIFY_RELEASE), "--bundle", str(bundle)])
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "independent verifier rejected the bundle"
        raise CodexAcceptanceError(detail)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CodexAcceptanceError("Independent verifier returned invalid JSON") from error
    if not isinstance(value, dict) or value.get("verified") is not True:
        raise CodexAcceptanceError("Independent verifier did not confirm the release")
    return value


def _safe_member(name: str) -> PurePosixPath:
    relative = PurePosixPath(name)
    if (
        not name
        or name.endswith("/")
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in name
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise CodexAcceptanceError(f"Unsafe archive member: {name!r}")
    return relative


def _extract_codex_archive(
    bundle: Path, release: dict[str, Any], destination: Path
) -> dict[str, bytes]:
    platforms = release.get("platforms")
    if not isinstance(platforms, dict) or not isinstance(platforms.get("codex"), dict):
        raise CodexAcceptanceError("Release has no Codex platform metadata")
    codex = platforms["codex"]
    archive_name = codex.get("archive")
    expected_digest = codex.get("archive_sha256")
    expected_files = codex.get("files")
    if (
        not isinstance(archive_name, str)
        or PurePosixPath(archive_name).name != archive_name
        or not isinstance(expected_digest, str)
        or not isinstance(expected_files, dict)
    ):
        raise CodexAcceptanceError("Invalid Codex release metadata")
    archive = bundle / archive_name
    if _sha256_file(archive) != expected_digest:
        raise CodexAcceptanceError("Codex archive differs from verified metadata")
    actual: dict[str, bytes] = {}
    total = 0
    try:
        opened = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as error:
        raise CodexAcceptanceError("Could not open verified Codex archive") from error
    with opened:
        members = opened.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise CodexAcceptanceError("Codex archive contains too many members")
        for member in members:
            relative = _safe_member(member.filename)
            mode = member.external_attr >> 16
            if (
                member.filename in actual
                or member.is_dir()
                or not stat.S_ISREG(mode)
                or stat.S_IMODE(mode) != 0o444
                or member.file_size > MAX_MEMBER_BYTES
            ):
                raise CodexAcceptanceError(f"Unsafe Codex archive member: {member.filename}")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = bytearray()
            with opened.open(member) as source, target.open("xb") as output:
                while chunk := source.read(READ_CHUNK_BYTES):
                    total += len(chunk)
                    if total > MAX_EXTRACTED_BYTES or len(content) + len(chunk) > MAX_MEMBER_BYTES:
                        raise CodexAcceptanceError("Codex archive exceeds extraction limits")
                    content.extend(chunk)
                    output.write(chunk)
            if len(content) != member.file_size:
                raise CodexAcceptanceError(f"Codex member size changed: {member.filename}")
            target.chmod(0o444)
            actual[member.filename] = bytes(content)
    actual_digests = {name: _sha256_bytes(content) for name, content in actual.items()}
    if actual_digests != expected_files:
        raise CodexAcceptanceError("Extracted Codex bytes do not match release metadata")
    return actual


def _verify_payload(files: dict[str, bytes], release: dict[str, Any]) -> tuple[str, str, str]:
    prefix = "plugins/sensai/"
    manifest_name = prefix + "MANIFEST.sha256"
    expected_manifest = "".join(
        f"{_sha256_bytes(content)}  {name.removeprefix(prefix)}\n"
        for name, content in sorted(files.items())
        if name.startswith(prefix) and name != manifest_name
    ).encode()
    if files.get(manifest_name) != expected_manifest:
        raise CodexAcceptanceError("Codex manifest differs from extracted bytes")
    attestation = _json_object_bytes(
        files.get(prefix + "sensai-mcp-attestation.json", b""), "sensai-mcp-attestation.json"
    )
    expected_attestation = {
        "format_version": "1",
        "mcp_contract_version": release.get("mcp_contract_version"),
        "mcp_schema_sha256": release.get("mcp_schema_sha256"),
        "mcp_url": release.get("mcp_url"),
    }
    if attestation != expected_attestation:
        raise CodexAcceptanceError("Codex MCP attestation differs from release metadata")

    marketplace = _json_object_bytes(files[".agents/plugins/marketplace.json"], "marketplace.json")
    plugin = _json_object_bytes(files[prefix + ".codex-plugin/plugin.json"], "plugin.json")
    mcp = _json_object_bytes(files[prefix + ".mcp.json"], ".mcp.json")
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
        raise CodexAcceptanceError("Codex marketplace must contain one plugin")
    try:
        marketplace_name = marketplace["name"]
        listed_name = plugins[0]["name"]
        version = plugin["version"]
        mcp_url = mcp["mcpServers"]["sensai"]["url"]
    except (KeyError, TypeError) as error:
        raise CodexAcceptanceError("Codex marketplace contract is incomplete") from error
    if not all(
        isinstance(value, str) for value in (marketplace_name, listed_name, version, mcp_url)
    ):
        raise CodexAcceptanceError("Codex marketplace contract has invalid values")
    if listed_name != plugin.get("name"):
        raise CodexAcceptanceError("Codex marketplace contract is inconsistent")
    return f"{listed_name}@{marketplace_name}", version, mcp_url


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _make_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            current.chmod(0o700)
            entries = list(os.scandir(current))
        except OSError as error:
            raise CodexAcceptanceError("Could not prepare temporary profile for cleanup") from error
        for entry in entries:
            if entry.is_symlink():
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                path.chmod(0o600)


def _isolated_environment(profile: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in PASSTHROUGH_ENVIRONMENT_NAMES if name in os.environ
    }
    environment.update(
        {
            "CODEX_HOME": str(profile / "codex-home"),
            "HOME": str(profile / "home"),
            "TMPDIR": str(profile / "tmp"),
            "TMP": str(profile / "tmp"),
            "TEMP": str(profile / "tmp"),
            "XDG_CACHE_HOME": str(profile / "xdg-cache"),
            "XDG_CONFIG_HOME": str(profile / "xdg-config"),
            "XDG_DATA_HOME": str(profile / "xdg-data"),
        }
    )
    return environment


def _block_browser_launches(environment: dict[str, str], profile: Path) -> Path:
    """Prevent an isolated CLI acceptance run from opening host UI.

    Codex is allowed to attempt OAuth discovery while it installs an MCP
    plugin.  This acceptance lane intentionally does not authenticate, so a
    browser launch is both unexpected and harmful: it could put an error page
    in front of the person running the test.  The guard intercepts the normal
    Linux and WSL launchers before they can delegate to the host browser.  It
    records only a fixed marker, never a URL or OAuth query value.
    """
    guard_directory = profile / "browser-guard"
    guard_directory.mkdir(mode=0o700)
    evidence = guard_directory / "browser-open-attempt.log"
    launcher = guard_directory / "blocked-browser-launcher"
    launcher.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'browser_open_attempt_blocked' "
        ">> \"$SENSAI_BROWSER_GUARD_EVIDENCE\"\n"
        "exit 73\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    for name in _BROWSER_LAUNCHER_NAMES:
        path = guard_directory / name
        path.hardlink_to(launcher)

    safe_path = [
        entry
        for entry in environment["PATH"].split(os.pathsep)
        if entry and not entry.casefold().startswith("/mnt/")
    ]
    if not safe_path:
        raise CodexAcceptanceError("Browser guard removed every executable path")
    environment["BROWSER"] = str(launcher)
    environment["SENSAI_BROWSER_GUARD_EVIDENCE"] = str(evidence)
    environment["PATH"] = os.pathsep.join((str(guard_directory), *safe_path))
    # WSL_INTEROP is the bridge that lets a Linux process execute Windows .exe
    # files by an absolute path. This acceptance lane must not reach host UI.
    environment.pop("WSL_INTEROP", None)
    environment["DISPLAY"] = "sensai-browser-guard"
    environment["WAYLAND_DISPLAY"] = "sensai-browser-guard"
    return evidence


def _raise_if_browser_launch_was_blocked(evidence: Path | None) -> None:
    if evidence is None or not evidence.exists():
        return
    try:
        marker = evidence.read_text(encoding="utf-8")
    except OSError as error:
        raise CodexAcceptanceError("browser_open_attempt_evidence_unreadable") from error
    if marker != _BROWSER_GUARD_MARKER:
        raise CodexAcceptanceError("browser_open_attempt_evidence_invalid")
    raise CodexAcceptanceError("browser_open_attempt_blocked")


def _run_codex_json(
    codex: str,
    env: dict[str, str],
    profile: Path,
    *arguments: str,
    browser_guard_evidence: Path | None = None,
) -> Any:
    completed = _run([codex, *arguments], env=env, cwd=profile)
    _raise_if_browser_launch_was_blocked(browser_guard_evidence)
    if completed.returncode != 0:
        raise CodexAcceptanceError(
            f"Codex command failed: codex {' '.join(arguments[:3])}\n{completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CodexAcceptanceError("Codex command returned invalid JSON") from error


def _assert_installed(result: Any, profile: Path, version: str, source: Path) -> None:
    if not isinstance(result, dict) or result.get("version") != version:
        raise CodexAcceptanceError(f"Codex did not install exact plugin version {version}")
    installed_value = result.get("installedPath")
    if not isinstance(installed_value, str):
        raise CodexAcceptanceError("Codex did not report an installed path")
    installed = Path(installed_value).resolve()
    if not installed.is_relative_to((profile / "codex-home").resolve()):
        raise CodexAcceptanceError("Codex installed outside the isolated profile")
    if _fingerprint(installed) != _fingerprint(source):
        raise CodexAcceptanceError("Codex mutated plugin bytes while installing")


def _observed_mcp_url(result: Any) -> str:
    if not isinstance(result, list):
        raise CodexAcceptanceError("codex mcp list --json did not return a list")
    matches = [item for item in result if isinstance(item, dict) and item.get("name") == "sensai"]
    if len(matches) != 1:
        raise CodexAcceptanceError("Codex did not expose exactly one Sensai MCP server")
    transport = matches[0].get("transport")
    if not isinstance(transport, dict) or transport.get("type") != "streamable_http":
        raise CodexAcceptanceError("Codex exposed an unexpected Sensai transport")
    observed = transport.get("url")
    if not isinstance(observed, str):
        raise CodexAcceptanceError("Codex did not expose a Sensai MCP URL")
    return observed


@contextmanager
def installed_codex_plugin(
    bundle: Path,
    *,
    codex_executable: str | None = None,
    real_codex_home: Path | None = None,
    block_browser_launch: bool = False,
) -> Iterator[InstalledCodexPlugin]:
    """Install one immutable verified release and keep its isolated profile alive."""
    source = bundle.resolve()
    codex = codex_executable or shutil.which("codex")
    if codex is None:
        raise CodexAcceptanceError("codex is required on PATH")
    configured_home = real_codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    real_home = configured_home.absolute()
    real_before = fingerprint_codex_plugin_state(real_home)
    source_before = _source_bundle_fingerprint(source)
    body_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    temporary: tempfile.TemporaryDirectory[str] | None = None
    marketplace: Path | None = None
    snapshot: Path | None = None
    snapshot_before: tuple[tuple[Any, ...], ...] | None = None

    def assert_snapshot_unchanged(stage: str) -> None:
        if snapshot is None or snapshot_before is None:
            return
        if _snapshot_fingerprint(snapshot) != snapshot_before:
            raise CodexAcceptanceError(f"Private release snapshot changed {stage}")

    try:
        temporary = tempfile.TemporaryDirectory(prefix="sensai-installed-codex-", dir="/tmp")
        profile = Path(temporary.name).resolve()
        physical_home = real_home.resolve(strict=False)
        if profile.is_relative_to(physical_home) or physical_home.is_relative_to(profile):
            raise CodexAcceptanceError("Isolated profile overlaps the real Codex profile")
        snapshot = profile / "release-snapshot"
        snapshotted = _snapshot_bundle(source, snapshot)
        if snapshotted != source_before:
            raise CodexAcceptanceError("Private release snapshot differs from source bundle")
        snapshot_before = _snapshot_fingerprint(snapshot)
        verified = _verify_release(snapshot)
        assert_snapshot_unchanged("after independent verification")
        release = _json_object_bytes((snapshot / "release.json").read_bytes(), "release.json")
        if verified.get("release_version") != release.get("release_version"):
            raise CodexAcceptanceError("Verifier and release metadata disagree")

        marketplace = profile / "marketplace"
        marketplace.mkdir()
        assert_snapshot_unchanged("before archive extraction")
        files = _extract_codex_archive(snapshot, release, marketplace)
        assert_snapshot_unchanged("after archive extraction")
        selector, version, expected_url = _verify_payload(files, release)
        if (
            version != release.get("release_version")
            or expected_url != release.get("mcp_url")
            or expected_url != release["platforms"]["codex"].get("mcp_url")
        ):
            raise CodexAcceptanceError("Codex identity differs from release metadata")
        _make_read_only(marketplace)
        for relative in ("codex-home", "home", "tmp", "xdg-cache", "xdg-config", "xdg-data"):
            (profile / relative).mkdir()
        environment = _isolated_environment(profile)
        browser_guard_evidence = (
            _block_browser_launches(environment, profile) if block_browser_launch else None
        )
        added = _run_codex_json(
            codex,
            environment,
            profile,
            "plugin",
            "marketplace",
            "add",
            str(marketplace),
            "--json",
            browser_guard_evidence=browser_guard_evidence,
        )
        if not isinstance(added, dict) or added.get("marketplaceName") != selector.split("@", 1)[1]:
            raise CodexAcceptanceError("Codex registered a different marketplace")
        installed = _run_codex_json(
            codex,
            environment,
            profile,
            "plugin",
            "add",
            selector,
            "--json",
            browser_guard_evidence=browser_guard_evidence,
        )
        _assert_installed(installed, profile, version, marketplace / "plugins" / "sensai")
        assert_snapshot_unchanged("after Codex plugin installation")
        observed_url = _observed_mcp_url(
            _run_codex_json(
                codex,
                environment,
                profile,
                "mcp",
                "list",
                "--json",
                browser_guard_evidence=browser_guard_evidence,
            )
        )
        assert_snapshot_unchanged("after Codex lifecycle commands")
        if observed_url != expected_url:
            raise CodexAcceptanceError("Observed MCP URL differs from verified release")
        if _source_bundle_fingerprint(source) != source_before:
            raise CodexAcceptanceError("Source release bundle changed during installation")
        yield InstalledCodexPlugin(selector, version, observed_url, profile)
        if not profile.exists():
            raise CodexAcceptanceError("Isolated profile disappeared while in use")
    except BaseException as error:
        body_error = error
    finally:
        try:
            assert_snapshot_unchanged("before cleanup")
        except BaseException as error:
            cleanup_errors.append(error)
        if temporary is not None:
            try:
                _make_writable(Path(temporary.name))
            except BaseException as error:
                cleanup_errors.append(error)
        if temporary is not None:
            try:
                temporary.cleanup()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            if fingerprint_codex_plugin_state(real_home) != real_before:
                raise CodexAcceptanceError("Real Codex plugin profile changed")
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            if _source_bundle_fingerprint(source) != source_before:
                raise CodexAcceptanceError("Source release bundle changed")
        except BaseException as error:
            cleanup_errors.append(error)
    if body_error is not None and cleanup_errors:
        raise BaseExceptionGroup(
            "Codex acceptance and cleanup failed", [body_error, *cleanup_errors]
        )
    if cleanup_errors:
        raise BaseExceptionGroup("Codex acceptance cleanup failed", cleanup_errors)
    if body_error is not None:
        raise body_error
