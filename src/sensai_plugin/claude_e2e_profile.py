"""Provision and use a narrowly scoped local Claude E2E profile.

The persistent profile contains only Claude's own OAuth record.  Each caller
gets a new, fully isolated run directory and is responsible for running Claude
with the returned environment.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sensai_plugin.installation_e2e_contract import CLAUDE_SONNET_5_MODEL

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported local runner is POSIX/WSL.
    fcntl = None  # type: ignore[assignment]


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_ROOT = REPOSITORY_ROOT.parents[1]
MOUNTED_ROOT = Path("/mnt")
CLAUDE_E2E_MODEL = CLAUDE_SONNET_5_MODEL
_MAX_CREDENTIAL_BYTES = 1024 * 1024
_OWNER_MARKER_NAME = ".sensai-e2e-owner.json"
_FIREFOX_OPENER_NAME = "open-in-windows-firefox.py"
_FIREFOX_OPEN_MARKER_NAME = "windows-firefox-open-requested"
_WINDOWS_FIREFOX_EXECUTABLE = "/mnt/c/Program Files/Mozilla Firefox/firefox.exe"
_MANIFEST = {
    "auth_records": ["claudeAiOauth"],
    "format_version": 1,
    "model": CLAUDE_E2E_MODEL,
}
_PASSTHROUGH_ENVIRONMENT_NAMES = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


class ClaudeE2EProfileError(ValueError):
    """Raised before a profile can be created or used safely."""


class SourceTrust(StrEnum):
    """How the one Claude login entered the private E2E baseline."""

    PRIVATE_LOCAL_SOURCE = "private_local_source"
    USER_APPROVED_CURRENT_SOURCE_ONCE = "user_approved_current_source_once"


@dataclass(frozen=True, slots=True)
class ProvisionDescription:
    """Non-sensitive result of validating one requested provisioning action."""

    profile: Path
    model: str
    auth_record_count: int
    source_trust: SourceTrust

    def safe_summary(self) -> str:
        source = (
            "by one-time user approval from the configured current source"
            if self.source_trust is SourceTrust.USER_APPROVED_CURRENT_SOURCE_ONCE
            else "from one private local Claude source"
        )
        return (
            "Provisioning would copy one Claude login record "
            f"{source} for model {self.model}."
        )


@dataclass(frozen=True, slots=True)
class ClaudeE2EProfile:
    """A persistent profile that contains no plugin, browser, or Sensai state."""

    root: Path

    @property
    def baseline_credentials(self) -> Path:
        return self.root / "baseline" / "config" / ".credentials.json"

    @property
    def baseline_account_config(self) -> Path:
        return self.root / "baseline" / "config" / ".claude.json"

    @property
    def owner_marker(self) -> Path:
        return self.root / _OWNER_MARKER_NAME


@dataclass(frozen=True, slots=True)
class ClaudeE2ERun:
    """One disposable execution directory and its complete Claude environment."""

    root: Path
    work: Path
    environment: dict[str, str]
    firefox_opener: Path
    firefox_open_marker: Path
    model: str = CLAUDE_E2E_MODEL


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def _assert_profile_location(profile: Path) -> Path:
    absolute = _absolute(profile)
    physical = absolute.resolve(strict=False)
    repository = REPOSITORY_ROOT.resolve(strict=True)
    development = DEVELOPMENT_ROOT.resolve(strict=True)
    if physical.is_relative_to(repository) or repository.is_relative_to(physical):
        raise ClaudeE2EProfileError("persistent profile must be outside the plugin repository")
    if physical.is_relative_to(development):
        raise ClaudeE2EProfileError("persistent profile must be outside the development directory")
    permitted_root = (Path.home() / ".local" / "share").resolve(strict=False)
    if physical == permitted_root or not physical.is_relative_to(permitted_root):
        raise ClaudeE2EProfileError(
            "persistent profile must be under the local Linux home share directory"
        )
    return physical


def _assert_separate_from_source(profile: Path, source_credentials: Path) -> None:
    source_parent = _absolute(source_credentials).parent.resolve(strict=False)
    target = profile.resolve(strict=False)
    if target.is_relative_to(source_parent) or source_parent.is_relative_to(target):
        raise ClaudeE2EProfileError(
            "persistent profile must be separate from the source Claude profile"
        )


def _read_regular_bytes(path: Path, *, require_private_owner: bool = False) -> bytes:
    absolute = _absolute(path)
    try:
        before = os.lstat(absolute)
    except OSError as error:
        raise ClaudeE2EProfileError("Claude authorization source is unavailable") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ClaudeE2EProfileError("Claude authorization source must be a regular file")
    if require_private_owner and (
        before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise ClaudeE2EProfileError("Claude authorization source must be private and owned")
    if before.st_size > _MAX_CREDENTIAL_BYTES:
        raise ClaudeE2EProfileError("Claude authorization source is too large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(_MAX_CREDENTIAL_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise ClaudeE2EProfileError("could not read Claude authorization source") from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity != opened_identity or identity != after_identity:
        raise ClaudeE2EProfileError("Claude authorization source changed while reading")
    if require_private_owner and (
        opened.st_uid != os.getuid()
        or after.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        raise ClaudeE2EProfileError("Claude authorization source changed while reading")
    if len(data) > _MAX_CREDENTIAL_BYTES or len(data) != before.st_size:
        raise ClaudeE2EProfileError("Claude authorization source changed while reading")
    return data


def _minimal_credentials(source: Path, *, require_private_owner: bool) -> bytes:
    try:
        document = json.loads(
            _read_regular_bytes(source, require_private_owner=require_private_owner)
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("Claude authorization source is not valid JSON") from error
    if source.name != ".credentials.json":
        raise ClaudeE2EProfileError(
            "Claude authorization source must be the dedicated .credentials.json file"
        )
    if not isinstance(document, dict) or not isinstance(document.get("claudeAiOauth"), dict):
        raise ClaudeE2EProfileError("Claude authorization source has no Claude login")
    minimal = {"claudeAiOauth": document["claudeAiOauth"]}
    return (json.dumps(minimal, sort_keys=True) + "\n").encode()


def _minimal_account_config(source: Path, *, require_private_owner: bool) -> bytes:
    try:
        document = json.loads(
            _read_regular_bytes(source, require_private_owner=require_private_owner)
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("Claude account source is not valid JSON") from error
    if source.name != ".claude.json":
        raise ClaudeE2EProfileError("Claude account source must be the dedicated .claude.json file")
    if not isinstance(document, dict) or not isinstance(document.get("oauthAccount"), dict):
        raise ClaudeE2EProfileError("Claude account source has no oauthAccount")
    minimal = {"oauthAccount": document["oauthAccount"]}
    return (json.dumps(minimal, sort_keys=True) + "\n").encode()


def _configured_current_credentials_path() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    config = Path(configured) if configured else Path.home() / ".claude"
    return _absolute(config / ".credentials.json")


def _configured_current_account_config_path() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    config = Path(configured) if configured else Path.home()
    return _absolute(config / ".claude.json")


def _assert_no_symlink_components(path: Path) -> None:
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            item = os.lstat(current)
        except OSError as error:
            raise ClaudeE2EProfileError("Claude authorization source is unavailable") from error
        if stat.S_ISLNK(item.st_mode):
            raise ClaudeE2EProfileError("Claude authorization source must not include symlinks")


def _assert_private_linux_source_location(source: Path) -> None:
    absolute = _absolute(source)
    _assert_no_symlink_components(absolute)
    try:
        physical = absolute.resolve(strict=True)
        home = Path.home().resolve(strict=True)
    except OSError as error:
        raise ClaudeE2EProfileError("Claude authorization source is unavailable") from error
    development = DEVELOPMENT_ROOT.resolve(strict=True)
    if physical.is_relative_to(MOUNTED_ROOT) or physical.is_relative_to(development):
        raise ClaudeE2EProfileError(
            "Claude authorization source must be outside mounted and development directories"
        )
    if not physical.is_relative_to(home):
        raise ClaudeE2EProfileError(
            "Claude authorization source must be under the local Linux home"
        )
    current = home
    components = ("", *physical.parent.relative_to(home).parts)
    for component in components:
        if component:
            current /= component
        try:
            item = os.lstat(current)
        except OSError as error:
            raise ClaudeE2EProfileError("Claude authorization source is unavailable") from error
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) & 0o022
        ):
            raise ClaudeE2EProfileError("Claude authorization source parent is not private")


def _profile_manifest(
    credentials: bytes,
    account_config: bytes,
    source_trust: SourceTrust,
) -> dict[str, object]:
    return {
        **_MANIFEST,
        "claude_login_sha256": hashlib.sha256(credentials).hexdigest(),
        "oauth_account_sha256": hashlib.sha256(account_config).hexdigest(),
        "source_trust": source_trust.value,
    }


def discover_current_credentials() -> Path:
    """Return the one configured Claude credential file without trying alternatives.

    The caller can instead pass a concrete ``--source-credentials`` path.  A
    missing or invalid configured path fails; it never falls back to another
    profile such as a home-level config file.
    """

    source = _configured_current_credentials_path()
    # This validates only availability and the dedicated Claude-login shape;
    # it neither copies a record nor probes broad ~/.claude.json state.
    _assert_private_linux_source_location(source)
    _minimal_credentials(source, require_private_owner=True)
    return source


def discover_current_account_config() -> Path:
    """Return the configured Claude main config after strict source validation."""

    source = _configured_current_account_config_path()
    _assert_private_linux_source_location(source)
    _minimal_account_config(source, require_private_owner=True)
    return source


def _prepare_provision(
    profile: Path,
    source_credentials: Path,
    source_account_config: Path,
    *,
    source_trust: SourceTrust,
) -> tuple[ProvisionDescription, bytes, bytes]:
    target = _assert_profile_location(profile)
    _assert_no_symlink_components(source_credentials)
    _assert_no_symlink_components(source_account_config)
    if source_trust is SourceTrust.PRIVATE_LOCAL_SOURCE:
        _assert_private_linux_source_location(source_credentials)
    _assert_private_linux_source_location(source_account_config)
    credentials = _minimal_credentials(
        source_credentials,
        require_private_owner=source_trust is SourceTrust.PRIVATE_LOCAL_SOURCE,
    )
    account_config = _minimal_account_config(source_account_config, require_private_owner=True)
    _assert_separate_from_source(target, source_credentials)
    if target.exists() or target.is_symlink():
        raise ClaudeE2EProfileError("persistent profile already exists")
    return (
        ProvisionDescription(
            target,
            CLAUDE_E2E_MODEL,
            auth_record_count=1,
            source_trust=source_trust,
        ),
        credentials,
        account_config,
    )


def describe_provision(
    profile: Path,
    source_credentials: Path,
    source_account_config: Path,
) -> ProvisionDescription:
    return _prepare_provision(
        profile,
        source_credentials,
        source_account_config,
        source_trust=SourceTrust.PRIVATE_LOCAL_SOURCE,
    )[0]


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    _assert_observed_mode(path, 0o700)


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise
    path.chmod(0o600)
    _assert_observed_mode(path, 0o600)


def _write_private_executable(path: Path, content: bytes) -> None:
    """Create one owned executable without inheriting any host launcher."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise
    path.chmod(0o700)
    _assert_observed_mode(path, 0o700)


def _assert_observed_mode(path: Path, expected: int) -> None:
    try:
        actual = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError as error:
        raise ClaudeE2EProfileError(
            "could not verify private local filesystem permissions"
        ) from error
    if actual != expected:
        raise ClaudeE2EProfileError(
            "persistent profile requires a Linux filesystem that enforces private permissions"
        )


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        item = os.lstat(path)
    except OSError as error:
        raise ClaudeE2EProfileError("owned directory is unavailable") from error
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise ClaudeE2EProfileError("owned directory is unsafe")
    return item.st_dev, item.st_ino


def _owner_marker(nonce: str) -> bytes:
    return (json.dumps({"format_version": 1, "nonce": nonce}, sort_keys=True) + "\n").encode()


def _has_owner_marker(profile: Path, nonce: str) -> bool:
    try:
        return _read_regular_bytes(profile / _OWNER_MARKER_NAME) == _owner_marker(nonce)
    except ClaudeE2EProfileError:
        return False


def _is_empty_owned_directory(root: Path, expected_identity: tuple[int, int]) -> bool:
    try:
        if _directory_identity(root) != expected_identity:
            return False
        return not any(root.iterdir())
    except (ClaudeE2EProfileError, OSError):
        return False


@contextmanager
def _profile_lock(profile: Path) -> Iterator[None]:
    lock_path = profile / ".operation.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ClaudeE2EProfileError("persistent profile lock is unsafe") from error
    try:
        os.fchmod(descriptor, 0o600)
        if fcntl is None:  # pragma: no cover - see import guard above.
            raise ClaudeE2EProfileError("profile locking is unavailable on this platform")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ClaudeE2EProfileError(
                "another Claude E2E operation is already running"
            ) from error
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _remove_owned_tree(root: Path, expected_identity: tuple[int, int]) -> None:
    if _directory_identity(root) != expected_identity:
        raise ClaudeE2EProfileError("owned directory changed before cleanup")
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
    shutil.rmtree(root)


def _publish_private_profile(staging: Path, target: Path) -> None:
    """Publish a complete staging directory without replacing a concurrent target."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise ClaudeE2EProfileError("atomic private profile publication is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,  # AT_FDCWD
        os.fsencode(staging),
        -100,  # AT_FDCWD
        os.fsencode(target),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ClaudeE2EProfileError("persistent profile already exists")
    raise ClaudeE2EProfileError("atomic private profile publication failed")


def _create_profile(
    description: ProvisionDescription,
    credentials: bytes,
    account_config: bytes,
) -> ClaudeE2EProfile:
    """Create a private profile from one already validated in-memory login record."""

    root = description.profile
    root.parent.mkdir(parents=True, exist_ok=True)
    created_identity: tuple[int, int] | None = None
    owner_nonce: str | None = None
    staging: Path | None = None
    published = False
    try:
        if root.exists() or root.is_symlink():
            raise ClaudeE2EProfileError("persistent profile already exists")
        staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.staging-", dir=root.parent))
        staging.chmod(0o700)
        _assert_observed_mode(staging, 0o700)
        created_identity = _directory_identity(staging)
        owner_nonce = secrets.token_hex(32)
        _write_private(staging / _OWNER_MARKER_NAME, _owner_marker(owner_nonce))
        with _profile_lock(staging):
            baseline = staging / "baseline"
            config = baseline / "config"
            _mkdir_private(baseline)
            _mkdir_private(config)
            _mkdir_private(staging / "runs")
            _write_private(config / ".credentials.json", credentials)
            _write_private(config / ".claude.json", account_config)
            _write_private(
                staging / "manifest.json",
                (
                    json.dumps(
                        _profile_manifest(credentials, account_config, description.source_trust),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode(),
            )
        _load_profile(staging)
        _publish_private_profile(staging, root)
        published = True
    except BaseException:
        if (
            staging is not None
            and created_identity is not None
            and (
                (owner_nonce is not None and _has_owner_marker(staging, owner_nonce))
                or _is_empty_owned_directory(staging, created_identity)
            )
        ):
            _remove_owned_tree(staging, created_identity)
        raise
    if not published:  # pragma: no cover - guards future control-flow edits.
        raise AssertionError("private profile was not published")
    return ClaudeE2EProfile(root)


def provision_profile(
    profile: Path,
    source_credentials: Path,
    source_account_config: Path,
) -> ClaudeE2EProfile:
    """Create a profile from two strictly private Linux Claude auth records."""

    description, credentials, account_config = _prepare_provision(
        profile,
        source_credentials,
        source_account_config,
        source_trust=SourceTrust.PRIVATE_LOCAL_SOURCE,
    )
    return _create_profile(description, credentials, account_config)


def provision_trusted_current_profile(profile: Path) -> ClaudeE2EProfile:
    """One-time user-approved migration from exactly the configured current source.

    This is deliberately the only path that accepts a source outside the
    private-local contract.  It reads the source once, reduces it to Claude's
    login record in memory, and never reads that source again.
    """

    description, credentials, account_config = _prepare_provision(
        profile,
        _configured_current_credentials_path(),
        _configured_current_account_config_path(),
        source_trust=SourceTrust.USER_APPROVED_CURRENT_SOURCE_ONCE,
    )
    return _create_profile(description, credentials, account_config)


def _load_profile(profile: Path) -> ClaudeE2EProfile:
    root = _assert_profile_location(profile)
    try:
        root_stat = os.lstat(root)
    except OSError as error:
        raise ClaudeE2EProfileError("persistent profile is unavailable") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ClaudeE2EProfileError("persistent profile is unsafe")
    _assert_observed_mode(root, 0o700)
    manifest = root / "manifest.json"
    try:
        value = json.loads(_read_regular_bytes(manifest))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("persistent profile manifest is invalid") from error
    if not isinstance(value, dict):
        raise ClaudeE2EProfileError("persistent profile manifest is not recognized")
    expected_manifest_fields = set(_MANIFEST) | {
        "claude_login_sha256",
        "oauth_account_sha256",
        "source_trust",
    }
    digest = value.get("claude_login_sha256")
    account_digest = value.get("oauth_account_sha256")
    source_trust = value.get("source_trust")
    if (
        set(value) != expected_manifest_fields
        or {key: value[key] for key in _MANIFEST} != _MANIFEST
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(account_digest, str)
        or len(account_digest) != 64
        or any(character not in "0123456789abcdef" for character in account_digest)
        or source_trust not in {item.value for item in SourceTrust}
    ):
        raise ClaudeE2EProfileError("persistent profile manifest is not recognized")
    result = ClaudeE2EProfile(root)
    try:
        owner = json.loads(_read_regular_bytes(result.owner_marker))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("persistent profile ownership marker is invalid") from error
    if (
        not isinstance(owner, dict)
        or owner.get("format_version") != 1
        or not isinstance(owner.get("nonce"), str)
        or len(owner["nonce"]) != 64
    ):
        raise ClaudeE2EProfileError("persistent profile ownership marker is not recognized")
    _assert_observed_mode(result.owner_marker, 0o600)
    credentials = _load_baseline_credentials(result.baseline_credentials)
    if hashlib.sha256(credentials).hexdigest() != digest:
        raise ClaudeE2EProfileError("persistent Claude login record does not match its profile")
    account_config = _load_baseline_account_config(result.baseline_account_config)
    if hashlib.sha256(account_config).hexdigest() != account_digest:
        raise ClaudeE2EProfileError("persistent Claude account record does not match its profile")
    runs = root / "runs"
    try:
        runs_stat = os.lstat(runs)
    except OSError as error:
        raise ClaudeE2EProfileError("persistent profile run directory is unavailable") from error
    if stat.S_ISLNK(runs_stat.st_mode) or not stat.S_ISDIR(runs_stat.st_mode):
        raise ClaudeE2EProfileError("persistent profile run directory is unsafe")
    return result


def _load_baseline_credentials(path: Path) -> bytes:
    try:
        value = json.loads(_read_regular_bytes(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("persistent Claude login record is invalid") from error
    if not isinstance(value, dict) or set(value) != {"claudeAiOauth"}:
        raise ClaudeE2EProfileError("persistent Claude login record was changed")
    if not isinstance(value["claudeAiOauth"], dict):
        raise ClaudeE2EProfileError("persistent Claude login record was changed")
    _assert_observed_mode(path, 0o600)
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _load_baseline_account_config(path: Path) -> bytes:
    try:
        value = json.loads(_read_regular_bytes(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("persistent Claude account record is invalid") from error
    if not isinstance(value, dict) or set(value) != {"oauthAccount"}:
        raise ClaudeE2EProfileError("persistent Claude account record was changed")
    if not isinstance(value["oauthAccount"], dict):
        raise ClaudeE2EProfileError("persistent Claude account record was changed")
    _assert_observed_mode(path, 0o600)
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _validated_windows_firefox_executable() -> Path:
    """Require the one local Firefox executable before an E2E run is created."""

    executable = Path(_WINDOWS_FIREFOX_EXECUTABLE)
    try:
        item = os.lstat(executable)
    except OSError as error:
        raise ClaudeE2EProfileError("Windows Firefox executable is unavailable") from error
    if (
        stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or not (stat.S_IMODE(item.st_mode) & 0o111)
    ):
        raise ClaudeE2EProfileError("Windows Firefox executable is unsafe")
    return executable


def _firefox_opener_source(marker: Path, executable: Path) -> bytes:
    """Return the one-purpose browser handoff kept inside a disposable run."""

    return f'''#!/usr/bin/python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

FIREFOX_EXECUTABLE = {str(executable)!r}
MARKER = Path({str(marker)!r})
MAX_URL_BYTES = 16 * 1024


def reject() -> None:
    raise SystemExit(64)


if len(sys.argv) != 2:
    reject()
raw_url = sys.argv[1]
try:
    encoded_url = raw_url.encode("utf-8", errors="strict")
    parsed = urlsplit(raw_url)
except (UnicodeError, ValueError):
    reject()
if (
    not raw_url
    or len(encoded_url) > MAX_URL_BYTES
    or any(ord(character) < 32 or ord(character) == 127 for character in raw_url)
    or parsed.scheme not in {{"http", "https"}}
    or not parsed.netloc
    or parsed.username is not None
    or parsed.password is not None
):
    reject()

descriptor = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as output:
    output.write(b"opened\\n")
    output.flush()
    os.fsync(output.fileno())
MARKER.chmod(0o600)
os.execv(FIREFOX_EXECUTABLE, (FIREFOX_EXECUTABLE, raw_url))
'''.encode()


def _run_environment(root: Path, firefox_opener: Path) -> dict[str, str]:
    locations = {
        "CLAUDE_CONFIG_DIR": root / "config",
        "CLAUDE_CODE_PLUGIN_CACHE_DIR": root / "plugin-cache",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": root / "secure-storage",
        "HOME": root / "home",
        "TMPDIR": root / "tmp",
        "TMP": root / "tmp",
        "TEMP": root / "tmp",
        "XDG_CACHE_HOME": root / "xdg-cache",
        "XDG_CONFIG_HOME": root / "xdg-config",
        "XDG_STATE_HOME": root / "xdg-state",
        "XDG_DATA_HOME": root / "xdg-data",
    }
    for location in set(locations.values()):
        _mkdir_private(location)
    environment = {
        name: os.environ[name] for name in _PASSTHROUGH_ENVIRONMENT_NAMES if name in os.environ
    }
    environment.update({name: str(location) for name, location in locations.items()})
    # The run never inherits BROWSER.  This private helper routes only this OAuth
    # request to the existing Windows Firefox profile without copying its state or
    # changing the Windows default browser.
    environment["BROWSER"] = str(firefox_opener)
    environment["DISABLE_AUTOUPDATER"] = "1"
    return environment


@contextmanager
def create_fresh_run(profile: Path) -> Iterator[ClaudeE2ERun]:
    """Yield a new temporary run that is removed whether the caller passes or fails."""

    persistent = _load_profile(profile)
    with _profile_lock(persistent.root):
        firefox_executable = _validated_windows_firefox_executable()
        run_root = Path(tempfile.mkdtemp(prefix="run-", dir=persistent.root / "runs"))
        run_identity = _directory_identity(run_root)
        try:
            run_root.chmod(0o700)
            _assert_observed_mode(run_root, 0o700)
            work = run_root / "work"
            _mkdir_private(work)
            if any(work.iterdir()):
                raise ClaudeE2EProfileError("fresh Claude E2E working directory is not empty")
            firefox_opener = run_root / _FIREFOX_OPENER_NAME
            firefox_open_marker = run_root / _FIREFOX_OPEN_MARKER_NAME
            _write_private_executable(
                firefox_opener,
                _firefox_opener_source(firefox_open_marker, firefox_executable),
            )
            environment = _run_environment(run_root, firefox_opener)
            _write_private(
                Path(environment["CLAUDE_CONFIG_DIR"]) / ".credentials.json",
                _load_baseline_credentials(persistent.baseline_credentials),
            )
            _write_private(
                Path(environment["CLAUDE_CONFIG_DIR"]) / ".claude.json",
                _load_baseline_account_config(persistent.baseline_account_config),
            )
            yield ClaudeE2ERun(
                run_root,
                work,
                environment,
                firefox_opener,
                firefox_open_marker,
            )
        finally:
            _remove_owned_tree(run_root, run_identity)


def _sources_from_arguments(arguments: argparse.Namespace) -> tuple[Path, Path]:
    credentials = arguments.source_credentials
    account_config = arguments.source_account_config
    if isinstance(credentials, Path):
        if not isinstance(account_config, Path):
            raise ClaudeE2EProfileError("--source-credentials requires --source-account-config")
        return credentials, account_config
    if arguments.detect_source is True:
        if account_config is not None:
            raise ClaudeE2EProfileError("--detect-source does not accept --source-account-config")
        return _configured_current_credentials_path(), _configured_current_account_config_path()
    raise ClaudeE2EProfileError("choose --source-credentials or --detect-source")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a minimal persistent Claude authorization baseline for Sensai E2E."
    )
    parser.add_argument("--profile", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-credentials", type=Path)
    source.add_argument("--detect-source", action="store_true")
    source.add_argument("--trust-current-credentials-once", action="store_true")
    parser.add_argument("--source-account-config", type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--provision", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.trust_current_credentials_once and not arguments.provision:
        parser.error("--trust-current-credentials-once requires --provision")
    if arguments.trust_current_credentials_once and arguments.source_account_config is not None:
        parser.error("--trust-current-credentials-once does not accept --source-account-config")
    try:
        if arguments.trust_current_credentials_once:
            provisioned = provision_trusted_current_profile(arguments.profile)
            print(
                "Claude E2E profile provisioned with one user-approved current Claude login "
                f"record; model={CLAUDE_E2E_MODEL}; plugins, browser data, and Sensai access "
                "were not copied."
            )
            if not provisioned.root.exists():  # pragma: no cover
                raise AssertionError("provisioned profile disappeared")
            return 0
        credentials, account_config = _sources_from_arguments(arguments)
        if arguments.dry_run:
            print(describe_provision(arguments.profile, credentials, account_config).safe_summary())
            return 0
        provisioned = provision_profile(arguments.profile, credentials, account_config)
    except ClaudeE2EProfileError as error:
        parser.exit(1, f"Claude E2E profile was not provisioned: {error}\n")
    print(
        "Claude E2E profile provisioned with one Claude login record; "
        f"model={CLAUDE_E2E_MODEL}; plugins, browser data, and Sensai access were not copied."
    )
    # Guards future edits to provision_profile.
    if not provisioned.root.exists():  # pragma: no cover
        raise AssertionError("provisioned profile disappeared")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
