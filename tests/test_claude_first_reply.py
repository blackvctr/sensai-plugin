from __future__ import annotations

import inspect
import textwrap
from pathlib import Path

import pytest

import sensai_plugin.claude_first_reply as first_reply_module
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
        ),
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
    timeout_seconds: float = 1,
) -> ClaudeFirstReplyAcceptance:
    executable = tmp_path / "claude"
    _fake_claude(
        executable,
        mode=mode,
        first_reply=first_reply,
        second_reply=second_reply,
    )
    configured_profile = tmp_path / "real-profile"
    configured_profile.mkdir()
    sentinel = configured_profile / "unchanged"
    sentinel.write_text("preserved", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured_profile))
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_CACHE_DIR", str(tmp_path / "plugin-cache"))
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
    assert "Установи" not in result.safe_json()
    assert "profile" not in result.safe_json()
    assert "credential" not in result.safe_json()


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


def test_unset_secure_storage_does_not_block_first_reply_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

    result = _run(tmp_path, monkeypatch)

    assert result.passed


def test_harness_has_no_profile_or_credential_reader() -> None:
    source = inspect.getsource(first_reply_module)

    for forbidden in (
        "_real_profile",
        "_bounded_surface",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "os.lstat",
        "os.scandir",
    ):
        assert forbidden not in source


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
