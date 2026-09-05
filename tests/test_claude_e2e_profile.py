from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sensai_plugin import claude_e2e_profile as profile_module
from sensai_plugin.claude_e2e_profile import (
    CLAUDE_E2E_MODEL,
    ClaudeE2EProfile,
    ClaudeE2EProfileError,
    ProvisionDescription,
    SourceTrust,
    create_fresh_run,
    main,
    provision_trusted_current_profile,
)
from sensai_plugin.claude_e2e_profile import (
    describe_provision as _describe_provision,
)
from sensai_plugin.claude_e2e_profile import (
    provision_profile as _provision_profile,
)


@pytest.fixture(autouse=True)
def _local_linux_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "linux-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _profile_path() -> Path:
    return Path.home() / ".local" / "share" / "sensai-claude-e2e"


def _source_path(tmp_path: Path) -> Path:
    source = Path.home() / ".private-source" / ".credentials.json"
    source.parent.mkdir(mode=0o700)
    source.parent.chmod(0o700)
    return source


def _credentials(path: Path) -> dict[str, object]:
    value = {
        "claudeAiOauth": {"accessToken": "private-Claude-token", "expiresAt": 123},
        "mcpOAuth": {"plugin:sensai:sensai": {"accessToken": "must-not-be-copied"}},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return value


def _account_source_path() -> Path:
    source = Path.home() / ".private-account-source" / ".claude.json"
    source.parent.mkdir(mode=0o700, exist_ok=True)
    source.parent.chmod(0o700)
    return source


def _account_config(path: Path) -> dict[str, object]:
    value: dict[str, object] = {
        "oauthAccount": {"accountUuid": "private-account", "emailAddress": "private@example"},
        "unrelated": "must-not-be-copied",
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return value


def provision_profile(profile: Path, source_credentials: Path) -> ClaudeE2EProfile:
    account_config = _account_source_path()
    _account_config(account_config)
    return _provision_profile(profile, source_credentials, account_config)


def describe_provision(profile: Path, source_credentials: Path) -> ProvisionDescription:
    account_config = _account_source_path()
    _account_config(account_config)
    return _describe_provision(profile, source_credentials, account_config)


def _current_unsafe_source() -> Path:
    source = Path.home() / ".claude" / ".credentials.json"
    source.parent.mkdir(mode=0o700)
    _credentials(source)
    source.parent.chmod(0o777)
    source.chmod(0o777)
    _account_config(Path.home() / ".claude.json")
    return source


def test_describe_provision_reads_only_one_explicit_valid_credential_file(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    target = _profile_path()

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
    account_config = _account_source_path()
    _account_config(account_config)
    target = _profile_path()

    assert main(
        [
            "--profile",
            str(target),
            "--source-credentials",
            str(source),
            "--source-account-config",
            str(account_config),
            "--dry-run",
        ]
    ) == 0

    assert not target.exists()
    assert "private-Claude-token" not in capsys.readouterr().out


def test_cli_normal_source_requires_a_separate_account_config(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)

    with pytest.raises(SystemExit) as captured:
        main(
            [
                "--profile",
                str(_profile_path()),
                "--source-credentials",
                str(source),
                "--dry-run",
            ]
        )

    assert captured.value.code == 1
    assert not _profile_path().exists()


def test_normal_provision_rejects_a_nonprivate_account_config(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    account_config = _account_source_path()
    _account_config(account_config)
    account_config.chmod(0o777)

    with pytest.raises(ClaudeE2EProfileError, match="private and owned"):
        _provision_profile(_profile_path(), source, account_config)

    assert not _profile_path().exists()


def test_provision_copies_only_claude_login_not_sensai_or_work_profile(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    original = _credentials(source)
    source_parent_file = source.parent / "history.jsonl"
    source_parent_file.write_text("not an authorization record", encoding="utf-8")
    target = _profile_path()

    profile = provision_profile(target, source)

    baseline = profile.baseline_credentials
    baseline_account = profile.baseline_account_config
    copied = json.loads(baseline.read_text(encoding="utf-8"))
    assert copied == {"claudeAiOauth": original["claudeAiOauth"]}
    assert not (profile.root / "baseline" / "config" / "history.jsonl").exists()
    assert not (profile.root / "baseline" / "config" / "plugins").exists()
    assert profile.baseline_credentials == (
        profile.root / "baseline" / "secure-storage" / ".credentials.json"
    )
    assert not (profile.root / "baseline" / "config" / ".credentials.json").exists()
    assert profile.root.stat().st_mode & 0o777 == 0o700
    assert baseline.stat().st_mode & 0o777 == 0o600
    assert baseline_account.stat().st_mode & 0o777 == 0o600
    assert json.loads(baseline_account.read_text(encoding="utf-8")) == {
        "oauthAccount": {"accountUuid": "private-account", "emailAddress": "private@example"}
    }
    assert profile.owner_marker.stat().st_mode & 0o777 == 0o600
    assert (profile.root / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert (profile.root / "runs").stat().st_mode & 0o777 == 0o700
    manifest = json.loads((profile.root / "manifest.json").read_text(encoding="utf-8"))
    assert {
        key: value
        for key, value in manifest.items()
        if key not in {"claude_login_sha256", "oauth_account_sha256"}
    } == {
        "auth_records": ["claudeAiOauth"],
        "format_version": 1,
        "model": "claude-sonnet-5",
        "source_trust": SourceTrust.PRIVATE_LOCAL_SOURCE.value,
    }
    assert isinstance(manifest["claude_login_sha256"], str)
    assert len(manifest["claude_login_sha256"]) == 64
    assert isinstance(manifest["oauth_account_sha256"], str)
    assert len(manifest["oauth_account_sha256"]) == 64


def test_normal_provision_rejects_a_world_writable_source(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    source.chmod(0o777)

    with pytest.raises(ClaudeE2EProfileError, match="private and owned"):
        provision_profile(_profile_path(), source)

    assert not _profile_path().exists()


def test_normal_provision_rejects_a_source_under_development_or_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)

    monkeypatch.setattr(profile_module, "DEVELOPMENT_ROOT", source.parent)
    with pytest.raises(ClaudeE2EProfileError, match="mounted and development"):
        provision_profile(_profile_path(), source)

    other_development = tmp_path / "not-development"
    other_development.mkdir()
    monkeypatch.setattr(profile_module, "DEVELOPMENT_ROOT", other_development)
    monkeypatch.setattr(profile_module, "MOUNTED_ROOT", source.parent)
    with pytest.raises(ClaudeE2EProfileError, match="mounted and development"):
        provision_profile(_profile_path(), source)


def test_normal_provision_rejects_a_symlinked_source_parent(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    linked_parent = Path.home() / ".linked-source"
    linked_parent.symlink_to(source.parent)

    with pytest.raises(ClaudeE2EProfileError, match="symlinks"):
        provision_profile(_profile_path(), linked_parent / ".credentials.json")


def test_one_time_current_migration_copies_only_minimal_login_and_never_revisits_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _current_unsafe_source()
    original = json.loads(source.read_text(encoding="utf-8"))
    reads = 0
    original_read = profile_module._read_regular_bytes

    def read_once(path: Path, *, require_private_owner: bool = False) -> bytes:
        nonlocal reads
        result = original_read(path, require_private_owner=require_private_owner)
        if path == source:
            reads += 1
            source.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "later-login"}}),
                encoding="utf-8",
            )
            source.chmod(0o777)
        return result

    monkeypatch.setattr(profile_module, "_read_regular_bytes", read_once)
    profile = provision_trusted_current_profile(_profile_path())

    assert reads == 1
    assert json.loads(profile.baseline_credentials.read_text(encoding="utf-8")) == {
        "claudeAiOauth": original["claudeAiOauth"]
    }
    assert profile.root.stat().st_mode & 0o777 == 0o700
    assert profile.baseline_credentials.stat().st_mode & 0o777 == 0o600
    manifest = json.loads((profile.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_trust"] == SourceTrust.USER_APPROVED_CURRENT_SOURCE_ONCE.value
    assert "mcpOAuth" not in profile.baseline_credentials.read_text(encoding="utf-8")

    source.unlink()
    (Path.home() / ".claude.json").unlink()
    with create_fresh_run(profile.root) as run:
        assert run.work.is_dir()


def test_one_time_current_migration_uses_home_account_config_when_credentials_are_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = Path.home() / ".direct-current-credentials" / ".credentials.json"
    credentials.parent.mkdir(mode=0o777)
    _credentials(credentials)
    credentials.parent.chmod(0o777)
    credentials.chmod(0o777)
    account = Path.home() / ".claude.json"
    expected_account = _account_config(account)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(credentials.parent))

    profile = provision_trusted_current_profile(_profile_path())

    assert json.loads(profile.baseline_account_config.read_text(encoding="utf-8")) == {
        "oauthAccount": expected_account["oauthAccount"]
    }


def test_one_time_current_migration_rejects_invalid_source_without_creating_target() -> None:
    source = _current_unsafe_source()
    source.write_text("not json", encoding="utf-8")
    source.chmod(0o777)

    with pytest.raises(ClaudeE2EProfileError, match="valid JSON"):
        provision_trusted_current_profile(_profile_path())

    assert not _profile_path().exists()


@pytest.mark.parametrize("account", [{}, {"oauthAccount": None}, {"oauthAccount": []}])
def test_one_time_current_migration_rejects_missing_or_invalid_account_without_target(
    account: object,
) -> None:
    _current_unsafe_source()
    account_path = Path.home() / ".claude.json"
    account_path.write_text(json.dumps(account), encoding="utf-8")
    account_path.chmod(0o600)

    with pytest.raises(ClaudeE2EProfileError, match="oauthAccount"):
        provision_trusted_current_profile(_profile_path())

    assert not _profile_path().exists()


def test_one_time_current_migration_rejects_a_symlinked_configured_source_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _current_unsafe_source()
    linked_parent = Path.home() / ".linked-current-source"
    linked_parent.symlink_to(source.parent)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(linked_parent))

    with pytest.raises(ClaudeE2EProfileError, match="symlinks"):
        provision_trusted_current_profile(_profile_path())

    assert not _profile_path().exists()


def test_one_time_current_migration_rejects_a_source_changed_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _current_unsafe_source()
    original_open = os.open

    def replace_before_open(path: str | Path, flags: int, mode: int = 0o777) -> int:
        if Path(path) == source:
            source.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "replacement-login"}}),
                encoding="utf-8",
            )
            source.chmod(0o777)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", replace_before_open)

    with pytest.raises(ClaudeE2EProfileError, match="changed while reading"):
        provision_trusted_current_profile(_profile_path())

    assert not _profile_path().exists()


def test_one_time_current_migration_removes_only_its_partial_target_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current_unsafe_source()
    original_write = profile_module._write_private

    def fail_only_for_manifest(path: Path, content: bytes) -> None:
        if path.name == "manifest.json":
            raise RuntimeError("stop before profile publication")
        original_write(path, content)

    monkeypatch.setattr(profile_module, "_write_private", fail_only_for_manifest)

    with pytest.raises(RuntimeError, match="stop before profile publication"):
        provision_trusted_current_profile(_profile_path())

    assert not _profile_path().exists()


def test_profile_target_stays_absent_until_complete_staging_profile_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _current_unsafe_source()
    target = _profile_path()
    original_publish = profile_module._publish_private_profile
    observations: list[Path] = []

    def inspect_then_publish(staging: Path, final_target: Path) -> None:
        assert final_target == target
        assert not final_target.exists()
        inspected = profile_module._load_profile(staging)
        assert inspected.baseline_credentials.is_file()
        observations.append(staging)
        original_publish(staging, final_target)

    monkeypatch.setattr(profile_module, "_publish_private_profile", inspect_then_publish)
    profile = provision_trusted_current_profile(target)

    assert observations
    assert profile.root == target and target.is_dir()
    assert not any(target.parent.glob(f".{target.name}.staging-*"))
    assert source.exists()


def test_profile_publish_failure_removes_owned_staging_without_creating_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current_unsafe_source()
    target = _profile_path()

    def fail_publish(staging: Path, final_target: Path) -> None:
        assert not final_target.exists()
        assert profile_module._load_profile(staging).baseline_credentials.is_file()
        raise ClaudeE2EProfileError("atomic private profile publication failed")

    monkeypatch.setattr(profile_module, "_publish_private_profile", fail_publish)

    with pytest.raises(ClaudeE2EProfileError, match="atomic private profile publication failed"):
        provision_trusted_current_profile(target)

    assert not target.exists()
    assert not any(target.parent.glob(f".{target.name}.staging-*"))


def test_one_time_current_migration_cli_requires_explicit_provision_and_never_prints_login(
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _current_unsafe_source()
    source.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "do-not-print"}}), encoding="utf-8"
    )
    source.chmod(0o777)
    account_path = Path.home() / ".claude.json"
    account_path.write_text(
        json.dumps({"oauthAccount": {"accountUuid": "account-do-not-print"}}), encoding="utf-8"
    )
    account_path.chmod(0o600)

    with pytest.raises(SystemExit) as dry_run:
        main(
            [
                "--profile",
                str(_profile_path()),
                "--trust-current-credentials-once",
                "--dry-run",
            ]
        )
    assert dry_run.value.code == 2
    assert "do-not-print" not in capsys.readouterr().out

    assert (
        main(
            [
                "--profile",
                str(_profile_path()),
                "--trust-current-credentials-once",
                "--provision",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "do-not-print" not in output
    assert "user-approved current Claude login record" in output


def test_one_time_current_migration_cli_rejects_an_unrelated_source_argument(
    tmp_path: Path,
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)

    with pytest.raises(SystemExit) as captured:
        main(
            [
                "--profile",
                str(_profile_path()),
                "--trust-current-credentials-once",
                "--source-credentials",
                str(source),
                "--provision",
            ]
        )

    assert captured.value.code == 2
    assert not _profile_path().exists()


def test_provision_refuses_existing_target_without_overwriting_it(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    target = _profile_path()
    target.mkdir(parents=True)
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
        provision_profile(_profile_path(), source)


def test_provision_rejects_symlinked_or_repository_target(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    linked = tmp_path / "linked-credentials.json"
    linked.symlink_to(source)

    with pytest.raises(ClaudeE2EProfileError, match="symlinks"):
        provision_profile(_profile_path(), linked)

    with pytest.raises(ClaudeE2EProfileError, match="outside the plugin repository"):
        provision_profile(Path.cwd() / ".temporary-profile", source)

    development_sibling = profile_module.DEVELOPMENT_ROOT / "another-project" / "profile"
    with pytest.raises(ClaudeE2EProfileError, match="outside the development directory"):
        provision_profile(development_sibling, source)


def test_provision_rejects_target_inside_source_profile(tmp_path: Path) -> None:
    source = Path.home() / ".local" / "share" / "source-profile" / ".credentials.json"
    source.parent.mkdir(parents=True)
    _credentials(source)

    with pytest.raises(ClaudeE2EProfileError, match="separate from the source"):
        provision_profile(source.parent / "sensai-e2e", source)


def test_fresh_run_has_new_complete_environment_and_is_removed_after_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    profile = provision_profile(_profile_path(), source)
    monkeypatch.setenv("BROWSER", "host-browser-must-not-leak")
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
            "BROWSER",
        )
    }

    with create_fresh_run(profile.root) as run:
        assert run.root.parent == profile.root / "runs"
        assert run.root.stat().st_mode & 0o777 == 0o700
        assert run.work == run.root / "work"
        assert run.work.is_dir() and not list(run.work.iterdir())
        assert run.work.stat().st_mode & 0o777 == 0o700
        assert "BROWSER" not in run.environment
        copied = json.loads(
            (run.root / "secure-storage" / ".credentials.json").read_text(encoding="utf-8")
        )
        assert copied == {
            "claudeAiOauth": {"accessToken": "private-Claude-token", "expiresAt": 123}
        }
        copied_account = json.loads(
            (run.root / "config" / ".claude.json").read_text(encoding="utf-8")
        )
        assert copied_account == {
            "oauthAccount": {"accountUuid": "private-account", "emailAddress": "private@example"}
        }
        assert not (run.root / "config" / ".credentials.json").exists()
        assert not (run.root / "home" / ".claude.json").exists()
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
    profile = provision_profile(_profile_path(), source)
    (profile.root / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ClaudeE2EProfileError, match="manifest"), create_fresh_run(profile.root):
        pytest.fail("tampered profile must not create a run")


def test_fresh_run_rejects_tampered_or_symlinked_credential_baseline(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    profile = provision_profile(_profile_path(), source)
    baseline = profile.baseline_credentials
    baseline.write_text(
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "private-Claude-token"},
                "mcpOAuth": {"plugin:sensai:sensai": {"accessToken": "injected"}},
            }
        ),
        encoding="utf-8",
    )
    baseline.chmod(0o600)

    with pytest.raises(
        ClaudeE2EProfileError, match="login record was changed"
    ), create_fresh_run(profile.root):
        pytest.fail("tampered baseline must not create a run")

    baseline.unlink()
    baseline.symlink_to(source)
    with pytest.raises(ClaudeE2EProfileError, match="regular file"), create_fresh_run(
        profile.root
    ):
        pytest.fail("symlinked baseline must not create a run")


def test_fresh_run_rejects_tampered_account_baseline(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    profile = provision_profile(_profile_path(), source)
    account = profile.baseline_account_config
    account.write_text(
        json.dumps({"oauthAccount": {"accountUuid": "private-account"}, "other": "injected"}),
        encoding="utf-8",
    )
    account.chmod(0o600)

    with pytest.raises(ClaudeE2EProfileError, match="account record was changed"), create_fresh_run(
        profile.root
    ):
        pytest.fail("tampered account must not create a run")


def test_fresh_run_rejects_replacement_with_a_different_valid_claude_login(
    tmp_path: Path,
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    profile = provision_profile(_profile_path(), source)
    profile.baseline_credentials.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "different-but-valid", "expiresAt": 456}}),
        encoding="utf-8",
    )
    profile.baseline_credentials.chmod(0o600)

    with pytest.raises(ClaudeE2EProfileError, match="does not match its profile"), create_fresh_run(
        profile.root
    ):
        pytest.fail("replaced valid login must not create a run")


def test_run_is_removed_when_caller_raises(tmp_path: Path) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    profile = provision_profile(_profile_path(), source)
    run_root: Path | None = None

    with pytest.raises(RuntimeError, match="body failed"), create_fresh_run(profile.root) as run:
        run_root = run.root
        raise RuntimeError("body failed")

    assert run_root is not None and not run_root.exists()


def test_provision_does_not_remove_directory_created_by_another_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    target = _profile_path()
    original_publish = profile_module._publish_private_profile

    def another_operation_creates(staging: Path, final_target: Path) -> None:
        final_target.mkdir(mode=0o700)
        (final_target / "belongs-to-someone-else").write_text("keep", encoding="utf-8")
        original_publish(staging, final_target)

    monkeypatch.setattr(profile_module, "_publish_private_profile", another_operation_creates)

    with pytest.raises(ClaudeE2EProfileError, match="already exists"):
        provision_profile(target, source)

    assert (target / "belongs-to-someone-else").read_text(encoding="utf-8") == "keep"
    assert not any(target.parent.glob(f".{target.name}.staging-*"))


def test_provision_rejects_profile_when_private_modes_cannot_be_observed(
    tmp_path: Path,
) -> None:
    source = _source_path(tmp_path)
    _credentials(source)
    target = _profile_path()

    with patch(
        "sensai_plugin.claude_e2e_profile.stat.S_IMODE", return_value=0o777
    ), pytest.raises(ClaudeE2EProfileError, match="private"):
        provision_profile(target, source)

    assert not target.exists()
