from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sensai_plugin.claude_first_reply import (
    _SCENARIO_PROMPTS,
    ClaudeFirstReplyAcceptance,
    ClaudeFirstReplyError,
    FirstReplyScenario,
    run_real_claude_first_reply,
)


def _fake_claude(
    executable: Path,
    *,
    mode: str,
    first_reply: str,
    second_reply: str = "",
    mutate_profile: bool = False,
    mutate_root_config: bool = False,
    mutate_direct_config: bool = False,
    mutate_sensai_registry: bool = False,
    critical_surface: str = "",
    normal_startup_churn: bool = False,
) -> None:
    source = r'''#!/usr/bin/env python3
import json
import os
import shlex
import subprocess
import sys
import time

arguments = sys.argv[1:]
if arguments[arguments.index("--model") + 1] != "sonnet":
    raise SystemExit(20)
if "--verbose" not in arguments or "--include-partial-messages" not in arguments:
    raise SystemExit(21)
if arguments[-1] != __EXPECTED_PROMPT__:
    raise SystemExit(23)
settings = json.loads(open(arguments[arguments.index("--settings") + 1], encoding="utf-8").read())
hooks = settings["hooks"]
if "matcher" in hooks["MessageDisplay"][0]:
    raise SystemExit(22)

def run_hook(event, payload):
    item = hooks[event][0]["hooks"][0]
    command = shlex.split(item["command"])
    completed = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True)
    if completed.returncode:
        raise SystemExit(30)
    return completed.stdout

def emit(value):
    print(json.dumps(value), flush=True)

def text_block(index, text):
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": index,
        "content_block": {"type": "text", "text": ""}
    }})
    run_hook("MessageDisplay", {"delta": text, "message": "ignored"})
    emit({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": index,
        "delta": {"type": "text_delta", "text": text}
    }})
    emit({
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": index}
    })

def tool_block(index):
    if __MODE__ == "hook-state-unavailable":
        os.unlink(os.environ["SENSAI_CLAUDE_FIRST_REPLY_STATE"])
    answer = run_hook("PreToolUse", {"tool_name": "Bash"})
    if '"permissionDecision": "deny"' not in answer:
        raise SystemExit(31)
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": index,
        "content_block": {"type": "tool_use", "name": "Bash", "input": {}}
    }})

if __MODE__ == "tool-first":
    tool_block(0)
    text_block(1, __FIRST_REPLY__)
elif __MODE__ == "english-then-russian":
    text_block(0, __FIRST_REPLY__)
    text_block(1, __SECOND_REPLY__)
else:
    text_block(0, __FIRST_REPLY__)
    if __MODE__ in {"tool-after-text", "hook-state-unavailable"}:
        tool_block(1)
if __MUTATE_PROFILE__:
    settings_path = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json")
    with open(settings_path, "w", encoding="utf-8") as output:
        output.write("{}")
if __MUTATE_ROOT_CONFIG__:
    root_config = os.path.join(os.environ["HOME"], ".claude.json")
    os.makedirs(os.path.dirname(root_config), exist_ok=True)
    with open(root_config, "w", encoding="utf-8") as output:
        output.write("critical root state")
if __MUTATE_DIRECT_CONFIG__:
    direct_config = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "account-state.json")
    with open(direct_config, "w", encoding="utf-8") as output:
        output.write("critical direct config")
if __MUTATE_SENSAI_REGISTRY__:
    registry = os.path.join(
        os.environ["CLAUDE_CONFIG_DIR"], "plugins", "installed_plugins.json"
    )
    os.makedirs(os.path.dirname(registry), exist_ok=True)
    with open(registry, "w", encoding="utf-8") as output:
        output.write("critical registry")
if __CRITICAL_SURFACE__:
    targets = {
        "marketplace": os.path.join(
            os.environ["CLAUDE_CONFIG_DIR"], "plugins", "marketplaces", "sensai-local", "marker"
        ),
        "cache": os.path.join(
            os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"], "sensai-local", "marker"
        ),
        "secure": os.path.join(os.environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"], "marker"),
        "xdg-config": os.path.join(os.environ["XDG_CONFIG_HOME"], "claude", "marker"),
        "xdg-data": os.path.join(os.environ["XDG_DATA_HOME"], "claude", "marker"),
        "xdg-data-code": os.path.join(os.environ["XDG_DATA_HOME"], "claude-code", "marker"),
    }
    target = targets[__CRITICAL_SURFACE__]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as output:
        output.write("critical surface")
if __NORMAL_STARTUP_CHURN__:
    backup = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "backups", "normal-startup")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup, "w", encoding="utf-8") as output:
        output.write("normal backup")
    cache = os.path.join(os.environ["XDG_CACHE_HOME"], "claude-cli-nodejs", "normal-startup")
    os.makedirs(cache, exist_ok=True)
if __MODE__ == "timeout":
    time.sleep(2)
else:
    emit({"type": "result"})
    if __MODE__ == "multiple-lines-then-live":
        time.sleep(2)
'''
    executable.write_text(
        textwrap.dedent(source)
        .replace("__MODE__", repr(mode))
        .replace("__FIRST_REPLY__", repr(first_reply))
        .replace("__SECOND_REPLY__", repr(second_reply))
        .replace(
            "__EXPECTED_PROMPT__",
            repr(_SCENARIO_PROMPTS[FirstReplyScenario.URL_BOOTSTRAP]),
        )
        .replace("__MUTATE_PROFILE__", repr(mutate_profile))
        .replace("__MUTATE_ROOT_CONFIG__", repr(mutate_root_config))
        .replace("__MUTATE_DIRECT_CONFIG__", repr(mutate_direct_config))
        .replace("__MUTATE_SENSAI_REGISTRY__", repr(mutate_sensai_registry))
        .replace("__CRITICAL_SURFACE__", repr(critical_surface))
        .replace("__NORMAL_STARTUP_CHURN__", repr(normal_startup_churn)),
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "tool-after-text",
    first_reply: str = "Я установлю Sensai самостоятельно.",
    second_reply: str = "",
    scenario: FirstReplyScenario = FirstReplyScenario.URL_BOOTSTRAP,
    mutate_profile: bool = False,
    mutate_root_config: bool = False,
    mutate_direct_config: bool = False,
    mutate_sensai_registry: bool = False,
    critical_surface: str = "",
    normal_startup_churn: bool = False,
    credential_surface_available: bool = True,
    timeout_seconds: float = 1,
) -> ClaudeFirstReplyAcceptance:
    executable = tmp_path / "claude"
    _fake_claude(
        executable,
        mode=mode,
        first_reply=first_reply,
        second_reply=second_reply,
        mutate_profile=mutate_profile,
        mutate_root_config=mutate_root_config,
        mutate_direct_config=mutate_direct_config,
        mutate_sensai_registry=mutate_sensai_registry,
        critical_surface=critical_surface,
        normal_startup_churn=normal_startup_churn,
    )
    configured_profile = tmp_path / "real-profile"
    configured_profile.mkdir()
    sentinel = configured_profile / "unchanged"
    sentinel.write_text("preserved", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured_profile))
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", str(tmp_path / "plugin-cache"))
    if credential_surface_available:
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(tmp_path / "secure-storage"))
    else:
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    result = run_real_claude_first_reply(
        scenario=scenario,
        claude_executable=str(executable),
        cwd=tmp_path,
        timeout_seconds=timeout_seconds,
        temporary_root=tmp_path,
    )
    assert sentinel.read_text(encoding="utf-8") == "preserved"
    assert not list(tmp_path.glob("sensai-claude-first-reply-*"))
    return result


def test_canonical_url_scenario_requires_russian_visible_reply_before_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch)

    assert result.passed
    assert result.event_order == ("assistant_reply", "tool_attempt", "result")
    assert result.first_reply_captured
    assert result.cyrillic_present and result.cyrillic_preponderates
    assert not result.terminal_lexeme_present
    assert not result.code_block_present
    assert result.tool_requested
    assert result.tool_gate_reached
    assert result.result == "completed"
    assert result.profile_integrity == "unchanged"
    assert not result.service_churn_detected
    assert "Установи" not in result.safe_json()


def test_tool_before_visible_reply_fails_even_if_text_arrives_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, mode="tool-first")

    assert not result.passed
    assert result.event_order == ("tool_attempt", "assistant_reply", "result")
    assert result.tool_requested
    assert result.tool_gate_reached
    assert result.result == "stream_evidence_missing"


def test_english_first_reply_cannot_be_hidden_by_later_russian_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(
        tmp_path,
        monkeypatch,
        mode="english-then-russian",
        first_reply="I will do this myself.",
        second_reply="Я установлю Sensai самостоятельно.",
    )

    assert not result.passed
    assert result.first_reply_captured
    assert not result.cyrillic_present


@pytest.mark.parametrize(
    ("first_reply", "expected_terminal", "expected_code"),
    [
        ("Открой терминал.", True, False),
        ("```bash\nexample\n```", True, True),
    ],
)
def test_canonical_url_scenario_rejects_technical_first_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_reply: str,
    expected_terminal: bool,
    expected_code: bool,
) -> None:
    result = _run(tmp_path, monkeypatch, first_reply=first_reply)

    assert not result.passed
    assert result.terminal_lexeme_present is expected_terminal
    assert result.code_block_present is expected_code


def test_settings_change_fails_profile_integrity_without_disclosing_the_changed_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, mutate_profile=True)

    assert not result.passed
    assert result.result == "completed"
    assert result.profile_integrity == "changed"
    assert "settings" not in result.safe_json()


def test_normal_claude_backup_and_cache_churn_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, normal_startup_churn=True)

    assert result.passed
    assert result.result == "completed"
    assert result.profile_integrity == "unchanged"
    assert result.service_churn_detected


@pytest.mark.parametrize(
    "mutation",
    [
        "mutate_root_config",
        "mutate_direct_config",
        "mutate_sensai_registry",
        "marketplace",
        "cache",
        "secure",
        "xdg-config",
        "xdg-data",
        "xdg-data-code",
    ],
)
def test_critical_profile_surface_change_never_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    if mutation == "mutate_root_config":
        result = _run(tmp_path, monkeypatch, mutate_root_config=True)
    elif mutation == "mutate_direct_config":
        result = _run(tmp_path, monkeypatch, mutate_direct_config=True)
    elif mutation == "mutate_sensai_registry":
        result = _run(tmp_path, monkeypatch, mutate_sensai_registry=True)
    else:
        result = _run(tmp_path, monkeypatch, critical_surface=mutation)

    assert not result.passed
    assert result.result == "completed"
    assert result.profile_integrity == "changed"


def test_missing_observable_credential_surface_makes_integrity_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, credential_surface_available=False)

    assert not result.passed
    assert result.result == "completed"
    assert result.profile_integrity == "unavailable"


def test_multiple_stream_lines_are_consumed_before_a_live_child_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, mode="multiple-lines-then-live", timeout_seconds=0.5)

    assert result.passed
    assert result.event_order == ("assistant_reply", "result")
    assert not result.timed_out


def test_unavailable_pretool_state_still_denies_the_tool_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, mode="hook-state-unavailable")

    assert not result.passed
    assert result.tool_requested
    assert not result.tool_gate_reached
    assert result.result == "hook_evidence_missing"


def test_timeout_is_safe_and_cleans_temporary_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, mode="timeout", timeout_seconds=0.05)

    assert not result.passed
    assert result.result == "timed_out"
    assert result.timed_out


def test_all_hard_coded_scenarios_remain_natural_russian_install_requests() -> None:
    assert _SCENARIO_PROMPTS == {
        FirstReplyScenario.URL_BOOTSTRAP: (
            "Установи Sensai https://github.com/grayvectorblack/sensai-plugin"
        )
    }


def test_invalid_hard_coded_scenario_input_fails_before_claude_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(_SCENARIO_PROMPTS, FirstReplyScenario.URL_BOOTSTRAP, "Install Sensai")

    with pytest.raises(ClaudeFirstReplyError, match="natural Russian"):
        run_real_claude_first_reply(scenario=FirstReplyScenario.URL_BOOTSTRAP)
