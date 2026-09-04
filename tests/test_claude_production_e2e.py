from __future__ import annotations

import json
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
    ProductionE2EError,
    ProductionE2EReport,
    ProductionSensaiE2E,
    TextEvidence,
    ToolKind,
    ToolResultEvidence,
    _assert_normal_browser_path,
    _classify_bash_command,
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
    monkeypatch.setenv("HOME", str(home))


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
