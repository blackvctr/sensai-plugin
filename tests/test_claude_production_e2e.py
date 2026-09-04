from __future__ import annotations

import json
import os
import runpy
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from sensai_plugin.claude_e2e_profile import provision_profile
from sensai_plugin.claude_production_e2e import (
    AgentEvidence,
    ClaudeDriver,
    ExitCategory,
    ExitStage,
    ProductionE2EError,
    ProductionE2EReport,
    ProductionSensaiE2E,
    TextEvidence,
    ToolKind,
    ToolResultEvidence,
    _assert_normal_browser_path,
    _classify_bash_command,
    _consume_stream,
    _is_exact_public_sensai_inventory,
    fetch_public_readme_contract,
)
from sensai_plugin.installation_e2e_contract import (
    PublicReadmeContract,
    _public_contract_from_markdown,
)


@pytest.fixture(autouse=True)
def _linux_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside_development = tmp_path / "outside-development"
    outside_development.mkdir()
    monkeypatch.setenv("HOME", str(home))
    import sensai_plugin.claude_e2e_profile as profile_module

    monkeypatch.setattr(profile_module, "DEVELOPMENT_ROOT", outside_development)


def _profile() -> Path:
    credentials = Path.home() / ".credentials-source" / ".credentials.json"
    credentials.parent.mkdir(mode=0o700, exist_ok=True)
    credentials.write_text(json.dumps({"claudeAiOauth": {"token": "private"}}), encoding="utf-8")
    credentials.chmod(0o600)
    account = Path.home() / ".account-source" / ".claude.json"
    account.parent.mkdir(mode=0o700, exist_ok=True)
    account.write_text(json.dumps({"oauthAccount": {"accountUuid": "private"}}), encoding="utf-8")
    account.chmod(0o600)
    return provision_profile(
        Path.home() / ".local" / "share" / "sensai-e2e", credentials, account
    ).root


def _contract() -> PublicReadmeContract:
    return _public_contract_from_markdown(
        (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    )


def _text(*, expected: bool = True) -> TextEvidence:
    return TextEvidence(expected, cyrillic_letters=12, latin_letters=2)


def _evidence(*tools: ToolKind, texts: tuple[TextEvidence, ...] = ()) -> AgentEvidence:
    return AgentEvidence(
        result_seen=True,
        session_verified=True,
        malformed=False,
        unclosed_block=False,
        stream_limit_exceeded=False,
        timed_out=False,
        returncode=0,
        text_messages=texts,
        tool_calls=tools,
        successful_tool_results=tools,
        tool_results=tuple(ToolResultEvidence(tool, True) for tool in tools),
        event_order=(
            ("visible", *(tool.value for tool in tools), "visible")
            if texts
            else tuple(tool.value for tool in tools)
        ),
        record_kinds=(),
        exit_category=ExitCategory.CLEAN,
        exit_stage=ExitStage.UNKNOWN,
        stderr_seen=False,
    )


@dataclass(frozen=True)
class _Call:
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    expected_session: uuid.UUID | None


class _Driver(ClaudeDriver):
    def __init__(
        self,
        evidence: AgentEvidence,
        *,
        authenticated: bool = True,
        connected: bool = True,
        installed: bool = True,
    ) -> None:
        self.evidence = evidence
        self.authenticated = authenticated
        self.connected = connected
        self.installed = installed
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
        self.calls.append(_Call(tuple(command), cwd, dict(environment), expected_session))
        return self.evidence

    def mcp_configuration_observed(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        self.calls.append(_Call(tuple(command), cwd, dict(environment), None))
        return self.connected

    def claude_authenticated(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        self.calls.append(_Call(tuple(command), cwd, dict(environment), None))
        return self.authenticated

    def public_sensai_plugin_installed(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        self.calls.append(_Call(tuple(command), cwd, dict(environment), None))
        return self.installed


def _successful_driver() -> _Driver:
    return _Driver(
        _evidence(
            ToolKind.MARKETPLACE_ADD,
            ToolKind.PLUGIN_INSTALL,
            ToolKind.LOGIN,
            ToolKind.NEW_CHAT_URI,
            texts=(_text(), _text()),
        )
    )


def _runner(profile: Path, driver: ClaudeDriver) -> ProductionSensaiE2E:
    return ProductionSensaiE2E(
        profile=profile,
        driver=driver,
        contract_loader=_contract,
        executable_resolver=lambda: "claude",
    )


class _StreamProcess:
    def __init__(self, payload: bytes, *, returncode: int = 0) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, payload)
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb")
        self.returncode = returncode
        self.pid = os.getpid()
        self._first_poll = True

    def poll(self) -> int | None:
        if self._first_poll:
            self._first_poll = False
            return None
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def _parse(records: list[dict[str, object]], *, returncode: int = 0) -> AgentEvidence:
    session = uuid.uuid4()
    for record in records:
        if record.get("type") == "system":
            record["session_id"] = str(session)
    payload = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    return _consume_stream(
        _StreamProcess(payload, returncode=returncode),  # type: ignore[arg-type]
        timeout_seconds=1,
        expected_visible_messages=(),
        expected_session=session,
        expected_new_chat_uri=None,
    )


def _parse_raw(payload: bytes) -> AgentEvidence:
    return _consume_stream(
        _StreamProcess(payload),  # type: ignore[arg-type]
        timeout_seconds=1,
        expected_visible_messages=(),
        expected_session=uuid.uuid4(),
        expected_new_chat_uri=None,
    )


def test_installation_route_stops_after_public_plugin_connection_and_new_chat() -> None:
    profile = _profile()
    driver = _successful_driver()
    report = _runner(profile, driver).run()
    assert report.complete
    assert len(driver.calls) == 4
    auth, installation, connection, plugin = driver.calls
    assert auth.command == ("claude", "auth", "status")
    assert installation.command[:2] == ("claude", "-p")
    assert installation.command[-1].startswith("Установи Sensai ")
    assert installation.command[installation.command.index("--model") + 1] == "claude-sonnet-5"
    assert "--no-browser" not in installation.command
    assert connection.command == ("claude", "mcp", "get", "plugin:sensai:sensai")
    assert plugin.command == ("claude", "plugin", "list", "--json")
    assert all(call.cwd.name == "work" for call in driver.calls)
    assert not list((profile / "runs").iterdir())


def test_installation_rejects_nonexact_visible_message() -> None:
    profile = _profile()
    evidence = _evidence(
        ToolKind.MARKETPLACE_ADD,
        ToolKind.PLUGIN_INSTALL,
        ToolKind.LOGIN,
        ToolKind.NEW_CHAT_URI,
        texts=(_text(), _text(expected=False)),
    )
    with pytest.raises(ProductionE2EError, match="installation_messages_not_exact"):
        _runner(profile, _Driver(evidence)).run()


@pytest.mark.parametrize(
    "kind", [ToolKind.MARKETPLACE_ADD, ToolKind.PLUGIN_INSTALL, ToolKind.LOGIN]
)
def test_installation_requires_each_real_action(kind: ToolKind) -> None:
    tools = [
        ToolKind.MARKETPLACE_ADD,
        ToolKind.PLUGIN_INSTALL,
        ToolKind.LOGIN,
        ToolKind.NEW_CHAT_URI,
    ]
    tools.remove(kind)
    with pytest.raises(ProductionE2EError, match=f"installation_{kind}_not_observed"):
        _runner(_profile(), _Driver(_evidence(*tools, texts=(_text(), _text())))).run()


def test_installation_requires_connection_and_public_plugin_after_login() -> None:
    evidence = _successful_driver().evidence
    profile = _profile()
    with pytest.raises(ProductionE2EError, match="sensai_endpoint_configuration_not_verified"):
        _runner(profile, _Driver(evidence, connected=False)).run()
    with pytest.raises(ProductionE2EError, match="public_sensai_plugin_not_verified"):
        _runner(profile, _Driver(evidence, installed=False)).run()


def test_installation_has_exactly_one_agent_turn() -> None:
    driver = _successful_driver()
    _runner(_profile(), driver).run()
    assert len(driver.calls) == 4
    assert all("--resume" not in call.command for call in driver.calls)


def test_refuses_to_suppress_normal_browser_login() -> None:
    with pytest.raises(ProductionE2EError, match="normal_login_path_required"):
        _assert_normal_browser_path(("claude", "mcp", "login", "--no-browser"))


def test_bash_classifier_requires_real_installation_command_semantics() -> None:
    uri = "claude://code/new?q=%D0%A2%D0%B5%D1%81%D1%82"
    assert (
        _classify_bash_command("echo 'claude mcp login plugin:sensai:sensai'", uri)
        is ToolKind.OTHER
    )
    assert (
        _classify_bash_command(
            "script -q -c 'claude mcp login plugin:sensai:sensai' /dev/null", uri
        )
        is ToolKind.LOGIN
    )
    assert (
        _classify_bash_command("claude plugin marketplace add blackvctr/sensai-plugin", uri)
        is ToolKind.MARKETPLACE_ADD
    )
    assert (
        _classify_bash_command("claude plugin install sensai@sensai --scope user", uri)
        is ToolKind.PLUGIN_INSTALL
    )
    assert _classify_bash_command(f"xdg-open {uri!r}", uri) is ToolKind.NEW_CHAT_URI


def test_parser_handles_empty_initial_tool_input_and_partial_json() -> None:
    evidence = _parse(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "tool", "input": {}},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "partial_json": (
                            '{"command":"claude plugin marketplace add blackvctr/sensai-plugin"}'
                        )
                    },
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "tool", "is_error": False}]
                },
            },
            {"type": "result"},
        ]
    )
    assert evidence.result_seen and not evidence.malformed and not evidence.unclosed_block
    assert evidence.has_successful(ToolKind.MARKETPLACE_ADD)
    assert evidence.record_kinds == (
        "system",
        "stream:content_block_start",
        "stream:content_block_delta",
        "stream:content_block_stop",
        "user",
        "result",
    )


def test_parser_keeps_terminal_nonzero_unclosed_and_limit_categories_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _parse([{"type": "system", "subtype": "init"}])
    nonzero = _parse([{"type": "system", "subtype": "init"}, {"type": "result"}], returncode=3)
    unclosed = _parse(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text"},
                },
            },
            {"type": "result"},
        ]
    )
    import sensai_plugin.claude_production_e2e as module

    monkeypatch.setattr(module, "MAX_STREAM_EVENTS", 1)
    limited = _parse([{"type": "system", "subtype": "init"}, {"type": "result"}])
    with pytest.raises(ProductionE2EError, match="installation_terminal_result_missing"):
        ProductionSensaiE2E._require_installation(missing)
    with pytest.raises(ProductionE2EError, match="installation_claude_exit_nonzero"):
        ProductionSensaiE2E._require_installation(nonzero)
    with pytest.raises(ProductionE2EError, match="installation_stream_unclosed_block"):
        ProductionSensaiE2E._require_installation(unclosed)
    with pytest.raises(ProductionE2EError, match="installation_stream_limit_exceeded"):
        ProductionSensaiE2E._require_installation(limited)


def test_parser_marks_invalid_json_and_invalid_block_stop_as_malformed() -> None:
    invalid_json = _parse_raw(b"{invalid-json}\n")
    invalid_stop = _parse(
        [
            {"type": "system", "subtype": "init"},
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": "bad"}},
            {"type": "result"},
        ]
    )
    unknown = _parse(
        [
            {"type": "system", "subtype": "init"},
            {"type": "stream_event", "event": {"type": "unknown_event"}},
            {"type": "result"},
        ]
    )
    assert invalid_json.malformed
    assert invalid_stop.malformed
    assert unknown.record_kinds == ("system", "stream:other", "result")
    with pytest.raises(ProductionE2EError, match="installation_stream_malformed"):
        ProductionSensaiE2E._require_installation(invalid_stop)


def test_nonzero_exit_uses_only_fixed_tool_stage_and_category() -> None:
    evidence = _parse(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool",
                        "input": {
                            "command": "claude plugin marketplace add blackvctr/sensai-plugin"
                        },
                    },
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "tool", "is_error": True}]
                },
            },
            {"type": "result", "is_error": True},
        ],
        returncode=7,
    )
    assert evidence.exit_category is ExitCategory.TOOL_RESULT_ERROR
    assert evidence.exit_stage is ExitStage.BEFORE_MARKETPLACE
    assert not evidence.stderr_seen
    with pytest.raises(
        ProductionE2EError,
        match="installation_claude_exit_tool_result_error_at_before_marketplace",
    ):
        ProductionSensaiE2E._require_installation(evidence)
    assert "blackvctr" not in str(evidence)


def test_generic_nonzero_exit_is_safe_and_unclassified() -> None:
    evidence = _parse([{"type": "system", "subtype": "init"}, {"type": "result"}], returncode=2)
    assert evidence.exit_category is ExitCategory.NONZERO_UNCLASSIFIED
    assert evidence.exit_stage is ExitStage.BEFORE_MARKETPLACE
    with pytest.raises(
        ProductionE2EError,
        match="installation_claude_exit_nonzero_unclassified_at_before_marketplace",
    ):
        ProductionSensaiE2E._require_installation(evidence)


def test_terminal_error_exit_uses_terminal_category_without_raw_content() -> None:
    evidence = _parse(
        [
            {"type": "system", "subtype": "init"},
            {"type": "result", "is_error": True, "result": "private terminal detail"},
        ],
        returncode=1,
    )
    assert evidence.exit_category is ExitCategory.TERMINAL_ERROR
    assert evidence.exit_stage is ExitStage.BEFORE_MARKETPLACE
    with pytest.raises(
        ProductionE2EError,
        match="installation_claude_exit_terminal_error_at_before_marketplace",
    ):
        ProductionSensaiE2E._require_installation(evidence)
    assert "private terminal detail" not in str(evidence)


def test_public_plugin_inventory_requires_exact_enabled_public_plugin() -> None:
    assert _is_exact_public_sensai_inventory(
        [{"id": "sensai@sensai", "scope": "user", "enabled": True}]
    )
    assert not _is_exact_public_sensai_inventory(
        [{"id": "sensai@sensai", "scope": "project", "enabled": True}]
    )


class _Response:
    def __init__(self, body: bytes, url: str) -> None:
        self.body = body
        self.url = url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, _size: int) -> bytes:
        return self.body


def test_public_readme_fetch_accepts_only_exact_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import sensai_plugin.claude_production_e2e as module

    body = (Path(__file__).resolve().parents[1] / "README.md").read_bytes()
    monkeypatch.setattr(
        module, "urlopen", lambda request, timeout: _Response(body, request.full_url)
    )
    assert fetch_public_readme_contract().russian_install_prompt.startswith("Установи Sensai ")
    monkeypatch.setattr(module, "urlopen", lambda _request, timeout: _Response(body, "https://bad"))
    with pytest.raises(ProductionE2EError, match="public_readme_redirected"):
        fetch_public_readme_contract()


def _script_namespace() -> dict[str, object]:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "run_claude_production_e2e.py"),
        run_name="install_e2e_script_test",
    )
    main = cast(Callable[[list[str]], int], namespace["main"])
    return cast(dict[str, object], main.__globals__)


def test_cli_accepts_only_the_installation_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    namespace = _script_namespace()

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self) -> ProductionE2EReport:
            return ProductionE2EReport(True, True, True, True, True, True)

    namespace["ProductionSensaiE2E"] = Runner
    main = cast(Callable[[list[str]], int], namespace["main"])
    assert main(["--profile", str(tmp_path / "profile")]) == 0
    assert capsys.readouterr().out == "PRODUCTION_E2E_PASS installation=connected=new_chat\n"
    with pytest.raises(SystemExit):
        main(["--profile", str(tmp_path / "profile"), "--unrelated-option"])
