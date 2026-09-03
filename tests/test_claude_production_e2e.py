from __future__ import annotations

import json
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest

import sensai_plugin.claude_production_e2e as production_module
from sensai_plugin.claude_e2e_profile import provision_profile
from sensai_plugin.installation_e2e_contract import _public_contract_from_markdown
from sensai_plugin.claude_production_e2e import (
    AgentEvidence,
    ClaudeDriver,
    ProductionE2EError,
    ProductionSensaiE2E,
    SensaiReplyKind,
    SubprocessClaudeDriver,
    TextEvidence,
    ToolResultEvidence,
    ToolKind,
    _assert_normal_browser_path,
    _classify_bash_command,
    _consume_stream,
)


@pytest.fixture(autouse=True)
def _linux_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _profile(tmp_path: Path) -> Path:
    source = tmp_path / "source" / ".credentials.json"
    source.parent.mkdir()
    source.write_text(
        json.dumps({"claudeAiOauth": {"token": "private-token"}}), encoding="utf-8"
    )
    source.chmod(0o600)
    target = Path.home() / ".local" / "share" / "sensai-e2e"
    return provision_profile(target, source).root


def _test_contract():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    return _public_contract_from_markdown(readme.read_text(encoding="utf-8"))


def _runner(profile: Path, driver: ClaudeDriver) -> ProductionSensaiE2E:
    return ProductionSensaiE2E(
        profile=profile,
        driver=driver,
        contract_loader=_test_contract,
        executable_resolver=lambda: "claude",
    )


def _text(*, expected: bool = False) -> TextEvidence:
    return TextEvidence(matches_expected=expected, cyrillic_letters=12, latin_letters=2)


def _evidence(
    *tools: ToolKind,
    texts: tuple[TextEvidence, ...] = (),
    sensai_reply: SensaiReplyKind | None = None,
) -> AgentEvidence:
    return AgentEvidence(
        result_seen=True,
        session_verified=True,
        malformed=False,
        timed_out=False,
        returncode=0,
        text_messages=texts,
        tool_calls=tools,
        successful_tool_results=tools,
        tool_results=tuple(
            ToolResultEvidence(
                kind=tool,
                succeeded=True,
                sensai_reply=sensai_reply if tool is ToolKind.TELL_SENSAI else None,
            )
            for tool in tools
        ),
    )


@dataclass(frozen=True)
class _Call:
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    timeout_seconds: int
    expected_visible_messages: tuple[str, ...] | None
    expected_session: uuid.UUID | None


class _FakeDriver(ClaudeDriver):
    def __init__(self, agent_results: Sequence[AgentEvidence], *, connected: bool = True) -> None:
        self._agent_results = iter(agent_results)
        self.connected = connected
        self.calls: list[_Call] = []

    def run_agent(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
        expected_visible_messages: Sequence[str],
        expected_session: uuid.UUID,
        expected_new_chat_uri: str | None,
    ) -> AgentEvidence:
        self.calls.append(
            _Call(
                tuple(command),
                cwd,
                dict(environment),
                timeout_seconds,
                tuple(expected_visible_messages),
                expected_session,
            )
        )
        return next(self._agent_results)

    def mcp_connected(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        self.calls.append(
            _Call(tuple(command), cwd, dict(environment), timeout_seconds, None, None)
        )
        return self.connected

    def claude_authenticated(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        self.calls.append(_Call(tuple(command), cwd, dict(environment), timeout_seconds, None, None))
        return True


def _successful_driver() -> _FakeDriver:
    return _FakeDriver(
        (
            _evidence(
                ToolKind.LOGIN,
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=True)),
            ),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.INITIAL_DISCOVERY),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.TELEGRAM_COMPOSED),
            _evidence(ToolKind.FORGET_ME),
        )
    )


def _argument(command: Sequence[str], option: str) -> str:
    return command[command.index(option) + 1]


def test_production_route_uses_public_input_production_model_and_resumed_telegram_session(
    tmp_path: Path,
) -> None:
    driver = _successful_driver()
    profile = _profile(tmp_path)

    report = _runner(profile, driver).run()

    assert report.forget_me_completed
    assert len(driver.calls) == 6
    auth_status, installation, status, telegram_start, continuation, cleanup = driver.calls
    assert auth_status.command == ("claude", "auth", "status")
    assert installation.command[0:2] == ("claude", "-p")
    assert _argument(installation.command, "--model") == "claude-sonnet-5"
    assert "--output-format" in installation.command
    assert _argument(installation.command, "--output-format") == "stream-json"
    assert "--no-browser" not in installation.command
    assert installation.expected_visible_messages is not None
    assert installation.command[-1].startswith("Установи Sensai ")
    assert status.command == ("claude", "mcp", "get", "plugin:sensai:sensai")
    assert "--session-id" in telegram_start.command
    assert "--resume" in continuation.command
    assert _argument(telegram_start.command, "--session-id") == _argument(
        continuation.command, "--resume"
    )
    assert _argument(continuation.command, "--resume") == _argument(
        cleanup.command, "--resume"
    )
    assert _argument(installation.command, "--session-id") != _argument(
        telegram_start.command, "--session-id"
    )
    assert all(call.cwd.name == "work" for call in driver.calls)
    assert all(call.environment["HOME"].endswith("/home") for call in driver.calls)
    assert all(call.environment["CLAUDE_CONFIG_DIR"].endswith("/config") for call in driver.calls)
    assert not list((profile / "runs").iterdir())


def test_report_and_persistent_profile_contain_no_prompt_stream_or_oauth_material(
    tmp_path: Path,
) -> None:
    driver = _successful_driver()
    profile = _profile(tmp_path)

    report = _runner(profile, driver).run()

    rendered = repr(report)
    for forbidden in ("Установи", "Telegram", "private-token", "oauth", "session"):
        assert forbidden not in rendered
    assert not list((profile / "runs").iterdir())
    persistent_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in profile.rglob("*")
        if path.is_file() and path.name != ".credentials.json"
    )
    assert "Установи" not in persistent_text
    assert "Telegram" not in persistent_text


def test_refuses_to_suppress_the_normal_browser_login_path() -> None:
    with pytest.raises(ProductionE2EError, match="normal_login_path_required"):
        _assert_normal_browser_path(("claude", "mcp", "login", "--no-browser"))


def test_bash_evidence_requires_real_command_semantics_and_rejects_no_browser() -> None:
    uri = "claude://code/new?q=%D0%A2%D0%B5%D1%81%D1%82"

    assert _classify_bash_command("echo 'claude mcp login plugin:sensai:sensai'", uri) is ToolKind.OTHER
    assert (
        _classify_bash_command(
            "script -q -c 'claude mcp login plugin:sensai:sensai' /dev/null", uri
        )
        is ToolKind.LOGIN
    )
    assert (
        _classify_bash_command(
            "claude plugin marketplace add blackvctr/sensai-plugin", uri
        )
        is ToolKind.MARKETPLACE_ADD
    )
    assert (
        _classify_bash_command(
            "claude plugin install sensai@sensai --scope user", uri
        )
        is ToolKind.PLUGIN_INSTALL
    )
    assert _classify_bash_command(f"xdg-open '{uri}'", uri) is ToolKind.NEW_CHAT_URI
    assert _classify_bash_command("claude mcp login --no-browser", uri) is ToolKind.FORBIDDEN_BROWSER_MODE


def test_real_driver_reduces_a_claude_launch_error_to_a_safe_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("private local executable detail")

    monkeypatch.setattr(production_module.subprocess, "Popen", unavailable)
    with pytest.raises(ProductionE2EError, match="claude_process_unavailable") as captured:
        SubprocessClaudeDriver().run_agent(
            ("claude", "-p"),
            cwd=tmp_path,
            environment={},
            timeout_seconds=1,
                expected_visible_messages=(),
                expected_session=uuid.uuid4(),
                expected_new_chat_uri=None,
        )

    assert "private" not in str(captured.value)


def test_runner_requires_exact_two_russian_installation_messages(tmp_path: Path) -> None:
    driver = _FakeDriver(
        (
            _evidence(
                ToolKind.LOGIN,
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=False)),
            ),
            _evidence(ToolKind.FORGET_ME),
        )
    )

    with pytest.raises(ProductionE2EError, match="installation_messages_not_exact"):
        _runner(_profile(tmp_path), driver).run()

    assert len(driver.calls) == 3


def test_failed_telegram_turn_still_calls_forget_me_before_temporary_profile_is_deleted(
    tmp_path: Path,
) -> None:
    driver = _FakeDriver(
        (
            _evidence(
                ToolKind.LOGIN,
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=True)),
            ),
            _evidence(ToolKind.OTHER),
            _evidence(ToolKind.FORGET_ME),
        )
    )
    profile = _profile(tmp_path)

    with pytest.raises(ProductionE2EError, match="telegram_start_tool_result_invalid"):
        _runner(profile, driver).run()

    assert len(driver.calls) == 5
    assert driver.calls[-1].command[-1].startswith("Заверши проверку")
    assert not list((profile / "runs").iterdir())


def test_cleanup_failure_is_reported_and_temporary_profile_is_still_deleted(tmp_path: Path) -> None:
    driver = _FakeDriver(
        (
            _evidence(
                ToolKind.LOGIN,
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=True)),
            ),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.INITIAL_DISCOVERY),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.TELEGRAM_COMPOSED),
            _evidence(ToolKind.OTHER),
        )
    )
    profile = _profile(tmp_path)

    with pytest.raises(ProductionE2EError, match="forget_me_tool_result_invalid"):
        _runner(profile, driver).run()

    assert not list((profile / "runs").iterdir())


def test_parser_reduces_fake_stream_to_safe_categories_without_retaining_raw_lines(
    tmp_path: Path,
) -> None:
    expected = uuid.uuid4()
    events = [
        {"type": "system", "subtype": "init", "session_id": str(expected)},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "name": "mcp__sensai__tell_sensai",
                    "id": "safe-tool-id",
                    "input": {"private": "do-not-retain"},
                },
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "safe-tool-id",
                        "content": "private-result",
                    }
                ]
            },
        },
        {"type": "result", "result": "private-assistant-text"},
    ]
    script = "import json\nfor value in " + repr(events) + ": print(json.dumps(value))\n"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    evidence = _consume_stream(
        process,
        timeout_seconds=2,
        expected_visible_messages=(),
        expected_session=expected,
    )

    assert evidence.result_seen and evidence.session_verified
    assert evidence.tool_calls == (ToolKind.TELL_SENSAI,)
    assert evidence.successful_tool_results == (ToolKind.TELL_SENSAI,)
    assert "private" not in repr(evidence)
    assert "assistant" not in repr(evidence)


def test_parser_recognizes_normal_login_split_across_tool_json_deltas(tmp_path: Path) -> None:
    expected = uuid.uuid4()
    events = [
        {"type": "system", "subtype": "init", "session_id": str(expected)},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "name": "Bash",
                    "id": "safe-login-id",
                    "input": {},
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"command":"script -q -c \\"claude mcp',
                },
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": ' login plugin:sensai:sensai\\" /dev/null"}',
                },
            },
        },
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "safe-login-id", "content": "done"}
                ]
            },
        },
        {"type": "result"},
    ]
    script = "import json\nfor value in " + repr(events) + ": print(json.dumps(value))\n"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    evidence = _consume_stream(
        process,
        timeout_seconds=2,
        expected_visible_messages=(),
        expected_session=expected,
    )

    assert evidence.tool_calls == (ToolKind.LOGIN,)
    assert evidence.successful_tool_results == (ToolKind.LOGIN,)
