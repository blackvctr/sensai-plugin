from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sensai_plugin.claude_e2e_profile import (
    CLAUDE_E2E_MODEL,
    ClaudeE2EProfileError,
    create_fresh_run,
    describe_provision,
    main,
    provision_profile,
)


def _source_path(tmp_path: Path) -> Path:
    source = tmp_path / "source-profile" / ".credentials.json"
    source.parent.mkdir()
    return source


def _credentials(path: Path) -> dict[str, object]:
    value = {
        "claudeAiOauth": {"accessToken": "private-Claude-token", "expiresAt": 123},
        "mcpOAuth": {"plugin:sensai:sensai": {"accessToken": "must-not-be-copied"}},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return value


def test_describe_provision_reads_only_one_explicit_valid_credential_file(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    target = tmp_path / "profile"

    description = describe_provision(target, source)

    assert description.model == "claude-sonnet-5"
    assert CLAUDE_E2E_MODEL == "claude-sonnet-5"
    assert description.auth_record_count == 1
    assert not target.exists()
    assert "private-Claude-token" not in description.safe_summary()


def test_cli_dry_run_does_not_create_profile_or_print_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    target = tmp_path / "persistent-profile"

    assert main(
        [
            "--profile",
            str(target),
            "--source-credentials",
            str(source),
            "--dry-run",
        ]
    ) == 0

    assert not target.exists()
    assert "private-Claude-token" not in capsys.readouterr().out


def test_provision_copies_only_claude_login_not_sensai_or_work_profile(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    original = _credentials(source)
    source_parent_file = source.parent / "history.jsonl"
    source_parent_file.write_text("not an authorization record", encoding="utf-8")
    target = tmp_path / "persistent-profile"

    profile = provision_profile(target, source)

    baseline = profile.baseline_credentials
    copied = json.loads(baseline.read_text(encoding="utf-8"))
    assert copied == {"claudeAiOauth": original["claudeAiOauth"]}
    assert not (profile.root / "baseline" / "config" / "history.jsonl").exists()
    assert not (profile.root / "baseline" / "config" / "plugins").exists()
    assert not (profile.root / "baseline" / "secure-storage").exists()
    assert profile.root.stat().st_mode & 0o777 == 0o700
    assert baseline.stat().st_mode & 0o777 == 0o600
    assert (profile.root / "runs").stat().st_mode & 0o777 == 0o700
    assert json.loads((profile.root / "manifest.json").read_text(encoding="utf-8")) == {
        "auth_records": ["claudeAiOauth"],
        "format_version": 1,
        "model": "claude-sonnet-5",
    }


def test_provision_refuses_existing_target_without_overwriting_it(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    target = tmp_path / "persistent-profile"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ClaudeE2EProfileError, match="already exists"):
        provision_profile(target, source)

    assert marker.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"claudeAiOauth": None},
        {"claudeAiOauth": []},
    ],
)
def test_provision_rejects_credential_file_without_claude_login(
    tmp_path: Path, value: object
) -> None:
    source = _source_path(tmp_path)
    source.write_text(json.dumps(value), encoding="utf-8")
    source.chmod(0o600)

    with pytest.raises(ClaudeE2EProfileError, match="Claude login"):
        provision_profile(tmp_path / "persistent-profile", source)


def test_provision_rejects_symlinked_or_repository_target(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    linked = tmp_path / "linked-credentials.json"
    linked.symlink_to(source)

    with pytest.raises(ClaudeE2EProfileError, match="regular file"):
        provision_profile(tmp_path / "persistent-profile", linked)

    with pytest.raises(ClaudeE2EProfileError, match="outside the plugin repository"):
        provision_profile(Path.cwd() / ".temporary-profile", source)


def test_provision_rejects_target_inside_source_profile(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)

    with pytest.raises(ClaudeE2EProfileError, match="separate from the source"):
        provision_profile(source.parent / "sensai-e2e", source)


def test_fresh_run_has_new_complete_environment_and_is_removed_after_context(
    tmp_path: Path,
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    profile = provision_profile(tmp_path / "persistent-profile", source)
    old_env = {
        name: os.environ.get(name)
        for name in (
            "HOME",
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR",
            "CLAUDE_CODE_PLUGIN_CACHE_DIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "TMPDIR",
            "TMP",
            "TEMP",
        )
    }

    with create_fresh_run(profile.root) as run:
        assert run.root.parent == profile.root / "runs"
        assert run.root.stat().st_mode & 0o777 == 0o700
        copied = json.loads(
            (run.root / "config" / ".credentials.json").read_text(encoding="utf-8")
        )
        assert copied == {
            "claudeAiOauth": {"accessToken": "private-Claude-token", "expiresAt": 123}
        }
        expected_roots = {
            "HOME": run.root / "home",
            "CLAUDE_CONFIG_DIR": run.root / "config",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": run.root / "secure-storage",
            "CLAUDE_CODE_PLUGIN_CACHE_DIR": run.root / "plugin-cache",
            "XDG_CACHE_HOME": run.root / "xdg-cache",
            "XDG_CONFIG_HOME": run.root / "xdg-config",
            "XDG_STATE_HOME": run.root / "xdg-state",
            "XDG_DATA_HOME": run.root / "xdg-data",
            "TMPDIR": run.root / "tmp",
            "TMP": run.root / "tmp",
            "TEMP": run.root / "tmp",
        }
        actual_roots = {
            name: Path(value) for name, value in run.environment.items() if name in expected_roots
        }
        assert actual_roots == expected_roots
        for root in set(expected_roots.values()):
            assert root.is_dir()
            assert root.stat().st_mode & 0o777 == 0o700
        assert all(
            run.environment[name] != old_env[name] for name in expected_roots if old_env[name]
        )
        run_file = run.root / "tmp" / "ephemeral"
        run_file.write_text("remove", encoding="utf-8")

    assert not run.root.exists()
    assert not list((profile.root / "runs").iterdir())


def test_fresh_run_rejects_tampered_persistent_profile(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    profile = provision_profile(tmp_path / "persistent-profile", source)
    (profile.root / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ClaudeE2EProfileError, match="manifest"), create_fresh_run(profile.root):
        pytest.fail("tampered profile must not create a run")
