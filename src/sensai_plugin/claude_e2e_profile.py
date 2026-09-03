"""Provision and use a narrowly scoped local Claude E2E profile.

The persistent profile contains only Claude's own OAuth record.  Each caller
gets a new, fully isolated run directory and is responsible for running Claude
with the returned environment.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported local runner is POSIX/WSL.
    fcntl = None  # type: ignore[assignment]


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_E2E_MODEL = "claude-sonnet-5"
_MAX_CREDENTIAL_BYTES = 1024 * 1024
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


@dataclass(frozen=True, slots=True)
class ProvisionDescription:
    """Non-sensitive result of validating one requested provisioning action."""

    profile: Path
    model: str
    auth_record_count: int

    def safe_summary(self) -> str:
        return (
            "Claude authorization source is valid; provisioning would copy one "
            f"Claude login record for model {self.model}."
        )


@dataclass(frozen=True, slots=True)
class ClaudeE2EProfile:
    """A persistent profile that contains no plugin, browser, or Sensai state."""

    root: Path

    @property
    def baseline_credentials(self) -> Path:
        return self.root / "baseline" / "config" / ".credentials.json"


@dataclass(frozen=True, slots=True)
class ClaudeE2ERun:
    """One disposable execution directory and its complete Claude environment."""

    root: Path
    environment: dict[str, str]
    model: str = CLAUDE_E2E_MODEL


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def _assert_profile_location(profile: Path) -> Path:
    absolute = _absolute(profile)
    physical = absolute.resolve(strict=False)
    repository = REPOSITORY_ROOT.resolve(strict=True)
    if physical.is_relative_to(repository) or repository.is_relative_to(physical):
        raise ClaudeE2EProfileError("persistent profile must be outside the plugin repository")
    return absolute


def _assert_separate_from_source(profile: Path, source_credentials: Path) -> None:
    source_parent = _absolute(source_credentials).parent.resolve(strict=False)
    target = profile.resolve(strict=False)
    if target.is_relative_to(source_parent) or source_parent.is_relative_to(target):
        raise ClaudeE2EProfileError(
            "persistent profile must be separate from the source Claude profile"
        )


def _read_regular_bytes(path: Path) -> bytes:
    absolute = _absolute(path)
    try:
        before = os.lstat(absolute)
    except OSError as error:
        raise ClaudeE2EProfileError("Claude authorization source is unavailable") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ClaudeE2EProfileError("Claude authorization source must be a regular file")
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
    if len(data) > _MAX_CREDENTIAL_BYTES or len(data) != before.st_size:
        raise ClaudeE2EProfileError("Claude authorization source changed while reading")
    return data


def _minimal_credentials(source: Path) -> bytes:
    try:
        document = json.loads(_read_regular_bytes(source))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("Claude authorization source is not valid JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("claudeAiOauth"), dict):
        raise ClaudeE2EProfileError("Claude authorization source has no Claude login")
    minimal = {"claudeAiOauth": document["claudeAiOauth"]}
    return (json.dumps(minimal, sort_keys=True) + "\n").encode()


def discover_current_credentials() -> Path:
    """Return the one configured Claude credential file without trying alternatives.

    The caller can instead pass a concrete ``--source-credentials`` path.  A
    missing or invalid configured path fails; it never falls back to another
    profile such as a home-level config file.
    """

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    config = Path(configured) if configured else Path.home() / ".claude"
    return _absolute(config / ".credentials.json")


def describe_provision(profile: Path, source_credentials: Path) -> ProvisionDescription:
    target = _assert_profile_location(profile)
    _minimal_credentials(source_credentials)
    _assert_separate_from_source(target, source_credentials)
    if target.exists() or target.is_symlink():
        raise ClaudeE2EProfileError("persistent profile already exists")
    return ProvisionDescription(target, CLAUDE_E2E_MODEL, auth_record_count=1)


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


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


def _remove_owned_tree(root: Path) -> None:
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ClaudeE2EProfileError("owned run directory is unsafe")
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
    shutil.rmtree(root)


def provision_profile(profile: Path, source_credentials: Path) -> ClaudeE2EProfile:
    """Create exactly one persistent profile from an explicitly chosen source.

    This function never overwrites a profile and never calls Claude, a browser,
    or Sensai.  It copies only ``claudeAiOauth`` from the supplied credentials.
    """

    description = describe_provision(profile, source_credentials)
    credentials = _minimal_credentials(source_credentials)
    root = description.profile
    try:
        _mkdir_private(root)
        with _profile_lock(root):
            baseline = root / "baseline"
            config = baseline / "config"
            _mkdir_private(baseline)
            _mkdir_private(config)
            _mkdir_private(root / "runs")
            _write_private(config / ".credentials.json", credentials)
            _write_private(
                root / "manifest.json",
                (json.dumps(_MANIFEST, sort_keys=True) + "\n").encode(),
            )
    except BaseException:
        if root.exists() and root.is_dir() and not root.is_symlink():
            _remove_owned_tree(root)
        raise
    return ClaudeE2EProfile(root)


def _load_profile(profile: Path) -> ClaudeE2EProfile:
    root = _assert_profile_location(profile)
    try:
        root_stat = os.lstat(root)
    except OSError as error:
        raise ClaudeE2EProfileError("persistent profile is unavailable") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ClaudeE2EProfileError("persistent profile is unsafe")
    manifest = root / "manifest.json"
    try:
        value = json.loads(_read_regular_bytes(manifest))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeE2EProfileError("persistent profile manifest is invalid") from error
    if value != _MANIFEST:
        raise ClaudeE2EProfileError("persistent profile manifest is not recognized")
    result = ClaudeE2EProfile(root)
    _minimal_credentials(result.baseline_credentials)
    runs = root / "runs"
    try:
        runs_stat = os.lstat(runs)
    except OSError as error:
        raise ClaudeE2EProfileError("persistent profile run directory is unavailable") from error
    if stat.S_ISLNK(runs_stat.st_mode) or not stat.S_ISDIR(runs_stat.st_mode):
        raise ClaudeE2EProfileError("persistent profile run directory is unsafe")
    return result


def _run_environment(root: Path) -> dict[str, str]:
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
    environment["DISABLE_AUTOUPDATER"] = "1"
    return environment


@contextmanager
def create_fresh_run(profile: Path) -> Iterator[ClaudeE2ERun]:
    """Yield a new temporary run that is removed whether the caller passes or fails."""

    persistent = _load_profile(profile)
    with _profile_lock(persistent.root):
        run_root = Path(tempfile.mkdtemp(prefix="run-", dir=persistent.root / "runs"))
        try:
            run_root.chmod(0o700)
            environment = _run_environment(run_root)
            _write_private(
                Path(environment["CLAUDE_CONFIG_DIR"]) / ".credentials.json",
                _minimal_credentials(persistent.baseline_credentials),
            )
            yield ClaudeE2ERun(run_root, environment)
        finally:
            _remove_owned_tree(run_root)


def _source_from_arguments(arguments: argparse.Namespace) -> Path:
    source = arguments.source_credentials
    if isinstance(source, Path):
        return source
    if arguments.detect_source is True:
        return discover_current_credentials()
    raise ClaudeE2EProfileError("choose --source-credentials or --detect-source")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a minimal persistent Claude authorization baseline for Sensai E2E."
    )
    parser.add_argument("--profile", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-credentials", type=Path)
    source.add_argument("--detect-source", action="store_true")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--provision", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        credentials = _source_from_arguments(arguments)
        if arguments.dry_run:
            print(describe_provision(arguments.profile, credentials).safe_summary())
            return 0
        provisioned = provision_profile(arguments.profile, credentials)
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
