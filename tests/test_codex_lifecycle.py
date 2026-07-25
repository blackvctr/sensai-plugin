from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sensai_plugin import codex_acceptance
from sensai_plugin.codex_acceptance import (
    CodexAcceptanceError,
    InstalledCodexPlugin,
    fingerprint_codex_plugin_state,
    installed_codex_plugin,
)
from sensai_plugin.release_builder import build_release

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MCP_URL = "https://black-vector.com/sensai/mcp"


@pytest.fixture(scope="module")
def release_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    bundle = tmp_path_factory.mktemp("codex-release") / "release"
    build_release(
        repository_root=REPOSITORY_ROOT,
        output=bundle,
        mcp_url=MCP_URL,
    )
    return bundle


def _run_lifecycle(
    *,
    bundle: Path,
    executable_directory: Path,
    real_profile: Path,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "CODEX_HOME": str(real_profile),
        "PATH": f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "test_codex_lifecycle.py"),
            "--bundle",
            str(bundle),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _write_fake_codex(executable: Path, log: Path) -> None:
    fake_codex = """#!/usr/bin/env python3
import json
import os
import shutil
import stat
import sys
from pathlib import Path

arguments = sys.argv[1:]
codex_home = Path(os.environ["CODEX_HOME"])
home = Path(os.environ["HOME"])
with Path(__LOG__).open("a", encoding="utf-8") as output:
    entry = {"arguments": arguments, "codex_home": str(codex_home), "home": str(home)}
    output.write(json.dumps(entry) + "\\n")

if arguments[:3] == ["plugin", "marketplace", "add"]:
    marketplace = Path(arguments[3])
    assert marketplace.is_dir() and marketplace.suffix != ".zip"
    for path in marketplace.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o444
        elif path.is_dir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o555
    print(json.dumps({"marketplaceName": "sensai-local"}))
elif arguments[:2] == ["plugin", "add"]:
    assert arguments[2] == "sensai@sensai-local"
    marketplace = Path(json.loads(Path(__LOG__).read_text().splitlines()[0])["arguments"][3])
    installed = codex_home / "plugins" / "cache" / "sensai-local" / "sensai" / "0.2.0"
    installed.parent.mkdir(parents=True)
    shutil.copytree(marketplace / "plugins" / "sensai", installed)
    print(json.dumps({"version": "0.2.0", "installedPath": str(installed)}))
elif arguments == ["mcp", "list", "--json"]:
    transport = {"type": "streamable_http", "url": __URL__}
    print(json.dumps([{"name": "sensai", "transport": transport}]))
else:
    raise SystemExit("unexpected command: " + repr(arguments))
"""
    executable.write_text(
        fake_codex.replace("__LOG__", repr(str(log))).replace("__URL__", repr(MCP_URL)),
        encoding="utf-8",
    )
    executable.chmod(0o755)


def test_codex_profile_fingerprint_covers_complete_tree_and_resolved_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "profile-target"
    target.mkdir()
    configured = tmp_path / "configured-codex-home"
    configured.symlink_to(target, target_is_directory=True)
    before = fingerprint_codex_plugin_state(configured)

    marketplace_state = target / ".tmp" / "marketplaces" / "global.json"
    marketplace_state.parent.mkdir(parents=True)
    marketplace_state.write_text('{"changed": true}\n', encoding="utf-8")

    assert fingerprint_codex_plugin_state(configured) != before

    after_boundary_change = fingerprint_codex_plugin_state(configured)
    unrelated = target / "sessions" / "unrelated.jsonl"
    unrelated.parent.mkdir()
    unrelated.write_text("unrelated runtime state\n", encoding="utf-8")

    assert fingerprint_codex_plugin_state(configured) == after_boundary_change


def test_codex_profile_fingerprint_skips_unrelated_tmp_plugins_but_detects_sensai(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "codex-home"
    unrelated = profile / ".tmp" / "plugins" / "plugins" / "unrelated-backup"
    unrelated.mkdir(parents=True)
    for index in range(200):
        (unrelated / f"payload-{index}.bin").write_bytes(b"x" * 4096)
    started = time.monotonic()
    before = fingerprint_codex_plugin_state(profile)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    (unrelated / "payload-0.bin").write_bytes(b"changed but unrelated")
    assert fingerprint_codex_plugin_state(profile) == before

    sensai = profile / ".tmp" / "plugins" / "plugins" / "sensai" / "marker.json"
    sensai.parent.mkdir()
    sensai.write_text('{"sensai": true}\n', encoding="utf-8")

    assert fingerprint_codex_plugin_state(profile) != before


def test_codex_lifecycle_rejects_tampering_before_invoking_codex(
    tmp_path: Path,
    release_bundle: Path,
) -> None:
    bundle = tmp_path / "release"
    shutil.copytree(release_bundle, bundle)
    archive = bundle / "sensai-0.2.0-codex-marketplace.zip"
    archive.write_bytes(archive.read_bytes() + b"tampered")

    marker = tmp_path / "codex-was-invoked"
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!/bin/sh\nprintf invoked > {marker!s}\nprintf '{{}}\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    completed = _run_lifecycle(
        bundle=bundle,
        executable_directory=tmp_path,
        real_profile=tmp_path / "real-profile",
    )

    assert completed.returncode != 0
    assert "release verification failed" in completed.stderr
    assert not marker.exists()


def test_codex_lifecycle_uses_exact_read_only_marketplace_and_isolated_profile(
    tmp_path: Path,
    release_bundle: Path,
) -> None:
    log = tmp_path / "commands.jsonl"
    executable = tmp_path / "codex"
    _write_fake_codex(executable, log)
    real_profile = tmp_path / "real-profile"
    real_profile.mkdir()
    sentinel = real_profile / "config.toml"
    sentinel.write_text("model = 'unchanged'\n", encoding="utf-8")
    bundle_before = {
        path.relative_to(release_bundle).as_posix(): path.read_bytes()
        for path in release_bundle.iterdir()
    }

    completed = _run_lifecycle(
        bundle=release_bundle,
        executable_directory=tmp_path,
        real_profile=real_profile,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PASS selector=sensai@sensai-local" in completed.stdout
    assert "PASS version=0.2.0" in completed.stdout
    assert f"PASS mcp={MCP_URL}" in completed.stdout
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [entry["arguments"] for entry in entries] == [
        ["plugin", "marketplace", "add", entries[0]["arguments"][3], "--json"],
        ["plugin", "add", "sensai@sensai-local", "--json"],
        ["mcp", "list", "--json"],
    ]
    assert all(entry["codex_home"] != str(real_profile) for entry in entries)
    assert all(entry["home"] != str(Path.home()) for entry in entries)
    assert not Path(entries[0]["codex_home"]).parent.exists()
    assert sentinel.read_text(encoding="utf-8") == "model = 'unchanged'\n"
    assert bundle_before == {
        path.relative_to(release_bundle).as_posix(): path.read_bytes()
        for path in release_bundle.iterdir()
    }


def test_public_acceptance_context_keeps_profile_alive_and_cleans_after_body_failure(
    tmp_path: Path,
    release_bundle: Path,
) -> None:
    log = tmp_path / "public-api-commands.jsonl"
    executable = tmp_path / "codex"
    _write_fake_codex(executable, log)
    real_profile = tmp_path / "real-profile"
    real_profile.mkdir()
    sentinel = real_profile / "config.toml"
    sentinel.write_text("model = 'unchanged'\n", encoding="utf-8")
    live_profile: Path | None = None

    with (
        pytest.raises(RuntimeError, match="body failed"),
        installed_codex_plugin(
            release_bundle,
            codex_executable=str(executable),
            real_codex_home=real_profile,
        ) as installed,
    ):
        assert isinstance(installed, InstalledCodexPlugin)
        assert installed.selector == "sensai@sensai-local"
        assert installed.version == "0.2.0"
        assert installed.mcp_url == MCP_URL
        assert installed.profile.exists()
        assert (installed.profile / "codex-home").is_dir()
        live_profile = installed.profile
        raise RuntimeError("body failed")

    assert live_profile is not None and not live_profile.exists()
    assert sentinel.read_text(encoding="utf-8") == "model = 'unchanged'\n"


def test_public_acceptance_blocks_a_browser_launch_without_retaining_its_url(
    tmp_path: Path,
    release_bundle: Path,
) -> None:
    executable = tmp_path / "codex"
    secret_url = "https://login.example.invalid/oauth?code=do-not-retain"
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"plugin marketplace\" ]; then\n"
        "  printf '{\"marketplaceName\": \"sensai-local\"}\\n'\n"
        "elif [ \"$1 $2\" = \"plugin add\" ]; then\n"
        f"  xdg-open {secret_url!r}\n"
        "  printf '{\"version\": \"0.2.0\", \"installedPath\": \"/ignored\"}\\n'\n"
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    with (
        pytest.raises(CodexAcceptanceError, match=r"^browser_open_attempt_blocked$") as caught,
        installed_codex_plugin(
            release_bundle,
            codex_executable=str(executable),
            real_codex_home=tmp_path / "real-profile",
            block_browser_launch=True,
        ),
    ):
        pytest.fail("blocked browser launch must not reach the test body")

    assert secret_url not in str(caught.value)


def test_browser_guard_disables_wsl_windows_execution_paths(tmp_path: Path) -> None:
    environment = {
        "PATH": "/usr/local/bin:/mnt/c/Windows/System32:/usr/bin:/mnt/d/tools",
        "WSL_INTEROP": "/run/WSL/123_interop",
    }

    codex_acceptance._block_browser_launches(environment, tmp_path)

    assert "WSL_INTEROP" not in environment
    assert all(not entry.startswith("/mnt/") for entry in environment["PATH"].split(os.pathsep))
    assert environment["PATH"].split(os.pathsep)[0] == str(tmp_path / "browser-guard")


def test_public_acceptance_rejects_snapshot_replacement_after_verification_before_codex(
    tmp_path: Path,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "codex-was-invoked"
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!/bin/sh\nprintf invoked > {marker!s}\nprintf '{{}}\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    original_verify = codex_acceptance._verify_release

    def verify_then_replace(snapshot: Path) -> dict[str, object]:
        result = original_verify(snapshot)
        original = snapshot.with_name("release-snapshot-original")
        snapshot.rename(original)
        shutil.copytree(original, snapshot, copy_function=shutil.copy2)
        return result

    monkeypatch.setattr(codex_acceptance, "_verify_release", verify_then_replace)

    with (
        pytest.raises(BaseException) as caught,
        installed_codex_plugin(
            release_bundle,
            codex_executable=str(executable),
            real_codex_home=tmp_path / "real-profile",
        ),
    ):
        pytest.fail("replaced snapshot must not reach the caller")

    assert "changed after independent verification" in repr(caught.value)
    assert not marker.exists()


def test_public_acceptance_rejects_physical_overlap_through_codex_home_symlink(
    tmp_path: Path,
    release_bundle: Path,
) -> None:
    configured_home = tmp_path / "configured-codex-home"
    configured_home.symlink_to("/tmp", target_is_directory=True)
    marker = tmp_path / "codex-was-invoked"
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!/bin/sh\nprintf invoked > {marker!s}\nprintf '{{}}\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    with (
        pytest.raises(CodexAcceptanceError, match="overlaps"),
        installed_codex_plugin(
            release_bundle,
            codex_executable=str(executable),
            real_codex_home=configured_home,
        ),
    ):
        pytest.fail("overlapping profile must not reach the caller")

    assert not marker.exists()


@pytest.mark.codex_real_cli
def test_codex_lifecycle_with_installed_official_cli(release_bundle: Path) -> None:
    codex = shutil.which("codex")
    assert codex is not None, "official Codex CLI is required for explicit real-CLI acceptance"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "test_codex_lifecycle.py"),
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
    assert "PASS version=0.2.0" in completed.stdout
    assert f"PASS mcp={MCP_URL}" in completed.stdout
    assert (
        "PASS isolated-profile=removed real-plugin-lifecycle-boundary=unchanged" in completed.stdout
    )


def test_explicit_real_cli_acceptance_fails_when_codex_is_unavailable(
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(AssertionError, match="official Codex CLI is required"):
        test_codex_lifecycle_with_installed_official_cli(release_bundle)
