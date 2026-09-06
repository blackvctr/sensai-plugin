from __future__ import annotations

import asyncio
import hashlib
import json
import os
import runpy
import shlex
import sys
import types
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

from sensai_plugin.claude_e2e_profile import provision_profile
from sensai_plugin.claude_production_e2e import (
    INSTALLATION_SCENARIO,
    PUBLIC_INSTALL_PROMPT,
    PUBLIC_README_URL,
    AgentEvidence,
    ClaudeDriver,
    ExitCategory,
    ExitStage,
    FirstTextKind,
    InstallationPermissionPolicy,
    PermissionDecision,
    PreMarketplaceFailureKind,
    PreMarketplaceFailureReceipt,
    ProductionE2EError,
    ProductionE2EReport,
    ProductionSensaiE2E,
    SdkClaudeDriver,
    SdkCleanupKind,
    SdkExceptionKind,
    SdkResultCause,
    SdkResultKind,
    TerminalResultKind,
    TextEvidence,
    ToolKind,
    ToolResultEvidence,
    _agent_command,
    _assert_normal_browser_path,
    _bash_action_argv,
    _classify_bash_command,
    _classify_sdk_exception,
    _consume_stream,
    _force_permission_request,
    _is_allowed_oauth_entry_url,
    _is_exact_public_sensai_inventory,
    _is_exact_public_sensai_mcp_status,
    _pre_marketplace_failure_receipt,
    _sdk_result_cause,
    fetch_public_readme_sha256,
)
from sensai_plugin.installation_e2e_contract import (
    CLAUDE_LINUX_ACTIONS,
    CLAUDE_NEW_CHAT_REQUEST,
    CLAUDE_NEW_CHAT_URI,
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


def _text(*, expected: bool = True) -> TextEvidence:
    del expected
    return TextEvidence(
        cyrillic_letters=12,
        latin_letters=2,
        contains_code_block=False,
        contains_terminal_reference=False,
    )


def _evidence(*tools: ToolKind, texts: tuple[TextEvidence, ...] = ()) -> AgentEvidence:
    if texts:
        # The public August flow installs first, explains the imminent Google
        # sign-in, then opens the prepared session and reports completion.
        event_order = (
            *(tool.value for tool in tools[:2]),
            "visible",
            *(tool.value for tool in tools[2:]),
            "visible",
        )
    else:
        event_order = tuple(tool.value for tool in tools)
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
        event_order=event_order,
        record_kinds=(),
        exit_category=ExitCategory.CLEAN,
        exit_stage=ExitStage.UNKNOWN,
        terminal_result_kind=TerminalResultKind.NONE,
        terminal_error_count=0,
        stderr_seen=False,
        sensai_connection_verified=True,
        public_sensai_plugin_installed=True,
    )


@dataclass(frozen=True)
class _Call:
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    expected_session: uuid.UUID | None
    expected_new_chat_uri: str | None = None
    expected_visible_messages: tuple[str, ...] = ()


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
        self.calls.append(
            _Call(
                tuple(command),
                cwd,
                dict(environment),
                expected_session,
                expected_new_chat_uri,
                tuple(expected_visible_messages),
            )
        )
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
        expected_public_readme_sha256="0" * 64,
        public_readme_validator=lambda expected: expected,
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


def _fake_sdk_module(**attributes: object) -> types.ModuleType:
    """Build a tiny SDK module for driver tests without type escapes."""

    module = types.ModuleType("claude_agent_sdk")
    for name, value in attributes.items():
        setattr(module, name, value)
    return module


def _install_minimal_dialogue_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    replies: tuple[str, ...],
    query_error: Exception | None = None,
) -> list[dict[str, object]]:
    """Install a fake SDK that exposes only visible assistant text."""

    option_calls: list[dict[str, object]] = []

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            option_calls.append(kwargs)
            self.session_id = cast(str, kwargs["session_id"])

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAssistantMessage:
        def __init__(self, text: str) -> None:
            self.content = [FakeTextBlock(text)]

    class FakeResultMessage:
        def __init__(self, session_id: str) -> None:
            self.is_error = False
            self.session_id = session_id

    class FakeClient:
        def __init__(self, *, options: FakeOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            if query_error is not None:
                raise query_error

        async def receive_response(self) -> object:
            for reply in replies:
                yield FakeAssistantMessage(reply)
            yield FakeResultMessage(self.options.session_id)

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    class FakeHookMatcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakePermissionResult:
        def __init__(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        _fake_sdk_module(
            AssistantMessage=FakeAssistantMessage,
            ClaudeAgentOptions=FakeOptions,
            ClaudeSDKClient=FakeClient,
            HookMatcher=FakeHookMatcher,
            PermissionResultAllow=FakePermissionResult,
            PermissionResultDeny=FakePermissionResult,
            ResultMessage=FakeResultMessage,
            TextBlock=FakeTextBlock,
            ToolResultBlock=type("ToolResultBlock", (), {}),
            ToolUseBlock=type("ToolUseBlock", (), {}),
            UserMessage=type("UserMessage", (), {}),
        ),
    )
    return option_calls


def test_installation_route_stops_after_public_plugin_connection_and_new_chat() -> None:
    profile = _profile()
    driver = _successful_driver()
    report = _runner(profile, driver).run()
    assert report.complete
    assert len(driver.calls) == 2
    auth, installation = driver.calls
    assert auth.command == ("claude", "auth", "status")
    assert installation.command[:2] == ("claude", "-p")
    assert installation.command[-1] == PUBLIC_INSTALL_PROMPT
    assert installation.command[installation.command.index("--model") + 1] == "claude-sonnet-5"
    assert "--no-browser" not in installation.command
    assert installation.expected_new_chat_uri == INSTALLATION_SCENARIO.new_chat_uri
    assert installation.expected_visible_messages == ()
    assert all(call.cwd.name == "work" for call in driver.calls)


def test_no_claude_preflight_leaves_last_dialogue_unchanged() -> None:
    profile = _profile()
    from sensai_plugin.claude_e2e_profile import write_last_dialogue

    record = write_last_dialogue(profile, replies=("kept",))
    before = record.read_bytes()
    runner = _runner(profile, _Driver(_evidence(), authenticated=False))

    with pytest.raises(ProductionE2EError, match="isolated_claude_auth_not_verified"):
        runner.run()

    assert record.read_bytes() == before
    assert not list((profile / "runs").iterdir())


def test_installation_accepts_localized_pre_login_prose_without_exact_wording() -> None:
    evidence = replace(
        _successful_driver().evidence,
        text_messages=(_text(), _text(), _text()),
        event_order=(
            ToolKind.MARKETPLACE_ADD.value,
            ToolKind.PLUGIN_INSTALL.value,
            "visible",
            "visible",
            ToolKind.LOGIN.value,
            ToolKind.NEW_CHAT_URI.value,
            "visible",
        ),
    )

    ProductionSensaiE2E._require_installation(evidence)


def test_installation_requires_one_completion_message_after_the_prepared_new_chat() -> None:
    evidence = replace(
        _successful_driver().evidence,
        event_order=(
            ToolKind.MARKETPLACE_ADD.value,
            ToolKind.PLUGIN_INSTALL.value,
            "visible",
            ToolKind.LOGIN.value,
            ToolKind.NEW_CHAT_URI.value,
            "visible",
            "visible",
        ),
        text_messages=(_text(), _text(), _text()),
    )

    with pytest.raises(ProductionE2EError, match="installation_event_order_invalid"):
        ProductionSensaiE2E._require_installation(evidence)


def test_published_august_contract_opens_the_plugin_command_not_a_consultation_prompt() -> None:
    assert CLAUDE_NEW_CHAT_REQUEST == "/sensai:sensai"
    assert CLAUDE_NEW_CHAT_URI == "claude://code/new?q=%2Fsensai%3Asensai"
    assert INSTALLATION_SCENARIO.new_chat_uri == CLAUDE_NEW_CHAT_URI
    assert CLAUDE_LINUX_ACTIONS[-1] == ("new_chat", ("xdg-open", CLAUDE_NEW_CHAT_URI))


def test_installation_prompt_is_fixed_test_input_not_a_readme_value() -> None:
    driver = _successful_driver()
    runner = ProductionSensaiE2E(
        profile=_profile(),
        driver=driver,
        expected_public_readme_sha256="a" * 64,
        public_readme_validator=lambda expected: expected,
        executable_resolver=lambda: "claude",
    )

    assert runner.run().complete
    assert driver.calls[1].command[-1] == INSTALLATION_SCENARIO.prompt


def test_tool_free_first_response_keeps_only_redacted_first_reply() -> None:
    evidence = replace(
        _evidence(),
        first_text_kind=FirstTextKind.TRUST_QUESTION,
    )
    driver = _Driver(evidence)
    runner = ProductionSensaiE2E(
        profile=_profile(),
        driver=driver,
        expected_public_readme_sha256="b" * 64,
        public_readme_validator=lambda expected: expected,
        executable_resolver=lambda: "claude",
        first_comparison=True,
    )

    report = runner.observe_tool_free_first_response()

    assert report.public_readme_sha256 == "b" * 64
    assert report.first_text_kind is FirstTextKind.TRUST_QUESTION
    assert driver.calls[1].command[-1] == PUBLIC_INSTALL_PROMPT


def test_sdk_tool_free_first_response_has_no_tools_hooks_or_permission_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    option_calls = _install_minimal_dialogue_sdk(monkeypatch, replies=("Первый ответ",))
    work = tmp_path / "run" / "work"
    work.mkdir(parents=True)

    evidence = asyncio.run(
        SdkClaudeDriver(first_comparison=True)._run_agent_async(
            executable="claude",
            prompt=PUBLIC_INSTALL_PROMPT,
            cwd=work,
            environment={},
            timeout_seconds=1,
            expected_visible_messages=(),
            expected_session=uuid.uuid4(),
            expected_new_chat_uri=None,
        )
    )

    assert evidence.first_text_kind is FirstTextKind.OTHER
    assert len(option_calls) == 1
    options = option_calls[0]
    assert options["tools"] == []
    assert options["allowed_tools"] == []
    assert "can_use_tool" not in options
    assert "hooks" not in options


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (
            replace(
                _evidence(),
                timed_out=True,
                returncode=1,
                exit_stage=ExitStage.BEFORE_MARKETPLACE,
            ),
            PreMarketplaceFailureKind.TIMEOUT,
        ),
        (
            replace(
                _evidence(),
                returncode=1,
                exit_stage=ExitStage.BEFORE_MARKETPLACE,
                sdk_exception_kind=SdkExceptionKind.RUNTIME,
            ),
            PreMarketplaceFailureKind.SDK_EXCEPTION,
        ),
        (
            replace(
                _evidence(),
                returncode=1,
                exit_stage=ExitStage.BEFORE_MARKETPLACE,
                sdk_result_kind=SdkResultKind.ERROR,
            ),
            PreMarketplaceFailureKind.SDK_RESULT_ERROR,
        ),
        (
            replace(
                _evidence(),
                result_seen=False,
                returncode=1,
                exit_stage=ExitStage.BEFORE_MARKETPLACE,
            ),
            PreMarketplaceFailureKind.MISSING_RESULT,
        ),
    ],
)
def test_pre_marketplace_receipt_distinguishes_closed_sdk_failures(
    evidence: AgentEvidence, expected: PreMarketplaceFailureKind
) -> None:
    receipt = _pre_marketplace_failure_receipt(evidence)

    assert receipt is not None
    assert receipt.kind is expected
    assert receipt.stage is ExitStage.BEFORE_MARKETPLACE


def test_pre_marketplace_receipt_uses_attempted_action_not_successful_result() -> None:
    evidence = replace(
        _evidence(),
        returncode=1,
        exit_stage=ExitStage.BEFORE_MARKETPLACE,
        sdk_result_kind=SdkResultKind.ERROR,
        tool_intents=(ToolKind.MARKETPLACE_ADD,),
    )

    assert _pre_marketplace_failure_receipt(evidence) is None


def test_pre_marketplace_receipt_keeps_only_categories_when_exception_is_poisoned() -> None:
    poison = "oauth-token=private https://private.example/path -- command"
    evidence = replace(
        _evidence(),
        returncode=1,
        exit_stage=ExitStage.BEFORE_MARKETPLACE,
        first_text_kind=FirstTextKind.REFUSAL,
        tool_intents=(ToolKind.PUBLIC_METADATA_INVENTORY_BASH,),
        denied_tool_intents=(ToolKind.PUBLIC_METADATA_INVENTORY_BASH,),
        sdk_exception_kind=_classify_sdk_exception(RuntimeError(poison)),
    )

    receipt = _pre_marketplace_failure_receipt(evidence)

    assert receipt is not None
    assert receipt.sdk_exception is SdkExceptionKind.RUNTIME
    assert receipt.first_text is FirstTextKind.REFUSAL
    assert receipt.first_tool_intent is ToolKind.PUBLIC_METADATA_INVENTORY_BASH
    assert receipt.first_denied_tool_intent is ToolKind.PUBLIC_METADATA_INVENTORY_BASH
    assert poison not in receipt.machine_line()
    assert "private.example" not in receipt.machine_line()


@pytest.mark.parametrize(
    ("terminal_reason", "subtype", "api_error_status", "expected"),
    [
        ("api_error", "success", None, SdkResultCause.API_ERROR),
        ("max_turns", "success", None, SdkResultCause.TURN_LIMIT),
        ("aborted_tools", "success", None, SdkResultCause.INTERRUPTED),
        (None, "error_during_execution", None, SdkResultCause.EXECUTION),
        (None, "error_max_budget_usd", None, SdkResultCause.BUDGET),
        (
            None,
            "error_max_structured_output_retries",
            None,
            SdkResultCause.STRUCTURED_OUTPUT_RETRIES,
        ),
        (None, "error_max_turns", None, SdkResultCause.TURN_LIMIT),
        (None, "error_permission", None, SdkResultCause.PERMISSION),
        (None, "success", 429, SdkResultCause.API_ERROR),
        ("future_reason", "error_max_turns", None, SdkResultCause.TURN_LIMIT),
        (None, "unrecognized_error", 418, SdkResultCause.OTHER),
    ],
)
def test_sdk_result_cause_uses_only_allowlisted_structured_fields(
    terminal_reason: str | None,
    subtype: str,
    api_error_status: int | None,
    expected: SdkResultCause,
) -> None:
    poison = "oauth-token=private https://private.example/path -- command"
    message = types.SimpleNamespace(
        is_error=True,
        terminal_reason=terminal_reason,
        subtype=subtype,
        api_error_status=api_error_status,
        errors=[poison],
        result=poison,
        session_id=poison,
    )

    assert _sdk_result_cause(message) is expected


def test_sdk_result_cause_is_present_in_receipt_without_free_form_result_data() -> None:
    poison = "oauth-token=private https://private.example/path -- command"
    message = types.SimpleNamespace(
        is_error=True,
        terminal_reason="api_error",
        subtype="success",
        api_error_status=429,
        errors=[poison],
        result=poison,
        session_id=poison,
    )
    evidence = replace(
        _evidence(),
        returncode=1,
        exit_stage=ExitStage.BEFORE_MARKETPLACE,
        sdk_result_kind=SdkResultKind.ERROR,
        sdk_result_cause=_sdk_result_cause(message),
    )

    receipt = _pre_marketplace_failure_receipt(evidence)

    assert receipt is not None
    assert receipt.sdk_result_cause is SdkResultCause.API_ERROR
    assert "sdk_result_cause:api_error" in receipt.machine_line()
    assert poison not in receipt.machine_line()
    assert "private.example" not in receipt.machine_line()


def test_sdk_driver_records_poisoned_exception_as_a_closed_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poison = "oauth-token=private https://private.example/path -- command"

    class FakeOptions:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakeClient:
        def __init__(self, *, options: object) -> None:
            del options

        async def connect(self) -> None:
            raise RuntimeError(poison)

        async def query(self, _prompt: str) -> None:
            raise AssertionError("query must not run after the connection failure")

        async def receive_response(self) -> object:
            if False:  # pragma: no cover - makes this an async generator for the SDK shape.
                yield None

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            raise OSError("private cleanup detail")

    class FakeHookMatcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakePermissionResult:
        def __init__(self, **_kwargs: object) -> None:
            pass

    fake_sdk = _fake_sdk_module(
        AssistantMessage=type("AssistantMessage", (), {}),
        ClaudeAgentOptions=FakeOptions,
        ClaudeSDKClient=FakeClient,
        HookMatcher=FakeHookMatcher,
        PermissionResultAllow=FakePermissionResult,
        PermissionResultDeny=FakePermissionResult,
        ResultMessage=type("ResultMessage", (), {}),
        TextBlock=type("TextBlock", (), {}),
        ToolResultBlock=type("ToolResultBlock", (), {}),
        ToolUseBlock=type("ToolUseBlock", (), {}),
        UserMessage=type("UserMessage", (), {}),
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    work = tmp_path / "run" / "work"
    work.mkdir(parents=True)

    evidence = asyncio.run(
        SdkClaudeDriver(first_comparison=True)._run_agent_async(
            executable="claude",
            prompt="fixed test input",
            cwd=work,
            environment={},
            timeout_seconds=1,
            expected_visible_messages=(),
            expected_session=uuid.uuid4(),
            expected_new_chat_uri=INSTALLATION_SCENARIO.new_chat_uri,
        )
    )

    assert evidence.sdk_exception_kind is SdkExceptionKind.RUNTIME
    assert evidence.sdk_result_kind is SdkResultKind.NONE
    assert evidence.sdk_cleanup_kind is SdkCleanupKind.DISCONNECT_FAILED
    assert evidence.returncode == 1
    receipt = _pre_marketplace_failure_receipt(evidence)
    assert receipt is not None
    assert receipt.kind is PreMarketplaceFailureKind.SDK_EXCEPTION
    assert receipt.sdk_cleanup is SdkCleanupKind.DISCONNECT_FAILED
    assert poison not in receipt.machine_line()
    assert "private.example" not in receipt.machine_line()


def test_sdk_driver_records_only_allowlisted_error_result_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poison = "oauth-token=private https://private.example/path -- command"

    class FakeOptions:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakeResultMessage:
        def __init__(self) -> None:
            self.is_error = True
            self.terminal_reason = "api_error"
            self.subtype = "success"
            self.api_error_status = 429
            self.errors = [poison]
            self.result = poison
            self.session_id = "private-session"

    class FakeClient:
        def __init__(self, *, options: object) -> None:
            del options

        async def connect(self) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self) -> object:
            yield FakeResultMessage()

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    class FakeHookMatcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakePermissionResult:
        def __init__(self, **_kwargs: object) -> None:
            pass

    fake_sdk = _fake_sdk_module(
        AssistantMessage=type("AssistantMessage", (), {}),
        ClaudeAgentOptions=FakeOptions,
        ClaudeSDKClient=FakeClient,
        HookMatcher=FakeHookMatcher,
        PermissionResultAllow=FakePermissionResult,
        PermissionResultDeny=FakePermissionResult,
        ResultMessage=FakeResultMessage,
        TextBlock=type("TextBlock", (), {}),
        ToolResultBlock=type("ToolResultBlock", (), {}),
        ToolUseBlock=type("ToolUseBlock", (), {}),
        UserMessage=type("UserMessage", (), {}),
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    work = tmp_path / "run" / "work"
    work.mkdir(parents=True)

    evidence = asyncio.run(
        SdkClaudeDriver(first_comparison=True)._run_agent_async(
            executable="claude",
            prompt="fixed test input",
            cwd=work,
            environment={},
            timeout_seconds=1,
            expected_visible_messages=(),
            expected_session=uuid.uuid4(),
            expected_new_chat_uri=INSTALLATION_SCENARIO.new_chat_uri,
        )
    )

    assert evidence.sdk_result_kind is SdkResultKind.ERROR
    assert evidence.sdk_result_cause is SdkResultCause.API_ERROR
    receipt = _pre_marketplace_failure_receipt(evidence)
    assert receipt is not None
    assert receipt.sdk_result_cause is SdkResultCause.API_ERROR
    assert poison not in str(evidence)
    assert poison not in receipt.machine_line()
    assert "private.example" not in receipt.machine_line()
    assert "private-session" not in receipt.machine_line()


def test_sdk_driver_replaces_one_private_last_dialogue_after_started_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            self.session_id = cast(str, kwargs["session_id"])

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeToolUseBlock:
        pass

    class FakeToolResultBlock:
        def __init__(self) -> None:
            self.tool_use_id = "private-tool-use"
            self.is_error = True
            self.content = "oauth-token=private"

    class FakeAssistantMessage:
        def __init__(self, content: list[object]) -> None:
            self.content = content

    class FakeUserMessage:
        def __init__(self, content: list[FakeToolResultBlock]) -> None:
            self.content = content

    class FakeResultMessage:
        def __init__(self, session_id: str) -> None:
            self.is_error = True
            self.session_id = session_id
            self.errors = ["oauth-token=private"]
            self.result = "curl https://private.example"

    class FakeClient:
        def __init__(self, *, options: FakeOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self) -> object:
            yield FakeAssistantMessage([FakeTextBlock("Первый ответ."), FakeToolUseBlock()])
            yield FakeUserMessage([FakeToolResultBlock()])
            yield FakeAssistantMessage([FakeTextBlock("Второй ответ.")])
            yield FakeResultMessage(self.options.session_id)

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    class FakeHookMatcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class FakePermissionResult:
        def __init__(self, **_kwargs: object) -> None:
            pass

    fake_sdk = _fake_sdk_module(
        AssistantMessage=FakeAssistantMessage,
        ClaudeAgentOptions=FakeOptions,
        ClaudeSDKClient=FakeClient,
        HookMatcher=FakeHookMatcher,
        PermissionResultAllow=FakePermissionResult,
        PermissionResultDeny=FakePermissionResult,
        ResultMessage=FakeResultMessage,
        TextBlock=FakeTextBlock,
        ToolResultBlock=FakeToolResultBlock,
        ToolUseBlock=FakeToolUseBlock,
        UserMessage=FakeUserMessage,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    driver = SdkClaudeDriver(first_comparison=True)
    driver.record_last_dialogue_for(profile)
    work = profile / "runs" / "manual-run" / "work"
    work.mkdir(parents=True)

    evidence = asyncio.run(
        driver._run_agent_async(
            executable="claude",
            prompt="Установи Sensai",
            cwd=work,
            environment={},
            timeout_seconds=1,
            expected_visible_messages=(),
            expected_session=uuid.uuid4(),
            expected_new_chat_uri=INSTALLATION_SCENARIO.new_chat_uri,
        )
    )

    record = profile.with_name(f"{profile.name}.last-dialogue.txt")
    assert evidence.returncode == 1
    assert record.stat().st_mode & 0o777 == 0o600
    assert record.read_text(encoding="utf-8") == (
        "Last visible Claude replies:\n"
        "[1]\n"
        "Первый ответ.\n\n"
        "[2]\n"
        "Второй ответ.\n"
    )
    stored = record.read_text(encoding="utf-8")
    assert "oauth-token" not in stored
    assert "private.example" not in stored
    assert "private-tool-use" not in stored
    assert "Установи Sensai" not in stored


def test_sdk_driver_keeps_prior_dialogue_when_query_never_hands_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sensai_plugin.claude_e2e_profile import write_last_dialogue

    profile = _profile()
    record = write_last_dialogue(profile, replies=("older reply",))
    before = record.read_bytes()
    _install_minimal_dialogue_sdk(monkeypatch, replies=(), query_error=OSError("not sent"))
    driver = SdkClaudeDriver(first_comparison=True)
    driver.record_last_dialogue_for(profile)
    work = profile / "runs" / "query-failure" / "work"
    work.mkdir(parents=True)

    evidence = asyncio.run(
        driver._run_agent_async(
            executable="claude",
            prompt="ignored",
            cwd=work,
            environment={},
            timeout_seconds=1,
            expected_visible_messages=(),
            expected_session=uuid.uuid4(),
            expected_new_chat_uri=INSTALLATION_SCENARIO.new_chat_uri,
        )
    )

    assert evidence.sdk_exception_kind is SdkExceptionKind.OS
    assert record.read_bytes() == before


def test_sdk_driver_replaces_prior_dialogue_when_started_attempt_has_no_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sensai_plugin.claude_e2e_profile import write_last_dialogue

    profile = _profile()
    record = write_last_dialogue(profile, replies=("older reply",))
    _install_minimal_dialogue_sdk(monkeypatch, replies=())
    driver = SdkClaudeDriver(first_comparison=True)
    driver.record_last_dialogue_for(profile)
    work = profile / "runs" / "zero-reply" / "work"
    work.mkdir(parents=True)

    asyncio.run(
        driver._run_agent_async(
            executable="claude",
            prompt="ignored",
            cwd=work,
            environment={},
            timeout_seconds=1,
            expected_visible_messages=(),
            expected_session=uuid.uuid4(),
            expected_new_chat_uri=INSTALLATION_SCENARIO.new_chat_uri,
        )
    )

    assert record.read_text(encoding="utf-8") == (
        "Last visible Claude replies:\n[no visible Claude reply]\n"
    )


def test_dialogue_write_failure_still_cleans_the_disposable_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sensai_plugin.claude_production_e2e as production_module

    profile = _profile()
    _install_minimal_dialogue_sdk(monkeypatch, replies=("reply",))
    driver = SdkClaudeDriver(first_comparison=True)
    monkeypatch.setattr(driver, "claude_authenticated", lambda *_args, **_kwargs: True)

    def fail_write(_profile: Path, **_kwargs: object) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(production_module, "write_last_dialogue", fail_write)
    runner = ProductionSensaiE2E(
        profile=profile,
        expected_public_readme_sha256="a" * 64,
        driver=driver,
        public_readme_validator=lambda _expected: "a" * 64,
        executable_resolver=lambda: "claude",
        first_comparison=True,
    )

    with pytest.raises(ProductionE2EError, match="last_claude_dialogue_not_saved"):
        runner.observe_tool_free_first_response()

    assert list((profile / "runs").iterdir()) == []


def test_full_run_attaches_before_marketplace_receipt_only_for_that_red_stage() -> None:
    evidence = replace(
        _evidence(),
        returncode=1,
        exit_stage=ExitStage.BEFORE_MARKETPLACE,
        sdk_result_kind=SdkResultKind.ERROR,
        first_text_kind=FirstTextKind.TRUST_QUESTION,
        tool_intents=(ToolKind.PUBLIC_README_FETCH,),
    )

    with pytest.raises(ProductionE2EError) as caught:
        _runner(_profile(), _Driver(evidence)).run()

    receipt = caught.value.before_marketplace_receipt
    assert receipt is not None
    assert receipt.kind is PreMarketplaceFailureKind.SDK_RESULT_ERROR
    assert receipt.first_text is FirstTextKind.TRUST_QUESTION
    assert receipt.first_tool_intent is ToolKind.PUBLIC_README_FETCH


def test_full_run_does_not_attach_early_receipt_after_marketplace() -> None:
    evidence = replace(
        _evidence(ToolKind.MARKETPLACE_ADD),
        returncode=1,
        exit_stage=ExitStage.AFTER_MARKETPLACE_BEFORE_PLUGIN,
        sdk_result_kind=SdkResultKind.ERROR,
    )

    with pytest.raises(ProductionE2EError) as caught:
        _runner(_profile(), _Driver(evidence)).run()

    assert caught.value.before_marketplace_receipt is None


def test_agent_command_exposes_tools_without_a_brittle_shell_allowlist() -> None:
    command = _agent_command(
        "claude",
        prompt=PUBLIC_INSTALL_PROMPT,
        session=uuid.uuid4(),
    )

    assert command[command.index("--tools") + 1] == "WebFetch,Bash"
    assert "--allowed-tools" not in command
    assert "--permission-prompts" not in command


def test_installation_rejects_terminal_or_code_block_visible_message() -> None:
    profile = _profile()
    evidence = _evidence(
        ToolKind.MARKETPLACE_ADD,
        ToolKind.PLUGIN_INSTALL,
        ToolKind.LOGIN,
        ToolKind.NEW_CHAT_URI,
        texts=(
            _text(),
            TextEvidence(
                cyrillic_letters=12,
                latin_letters=2,
                contains_code_block=True,
                contains_terminal_reference=False,
            ),
        ),
    )
    with pytest.raises(
        ProductionE2EError, match="installation_visible_message_contains_code_block"
    ):
        _runner(profile, _Driver(evidence)).run()

    terminal = replace(
        evidence,
        text_messages=(
            _text(),
            TextEvidence(
                cyrillic_letters=12,
                latin_letters=2,
                contains_code_block=False,
                contains_terminal_reference=True,
            ),
        ),
    )
    with pytest.raises(ProductionE2EError, match="installation_visible_message_mentions_terminal"):
        _runner(profile, _Driver(terminal)).run()


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
        _runner(profile, _Driver(replace(evidence, sensai_connection_verified=False))).run()
    with pytest.raises(ProductionE2EError, match="public_sensai_plugin_not_verified"):
        _runner(profile, _Driver(replace(evidence, public_sensai_plugin_installed=False))).run()


def test_installation_has_exactly_one_agent_turn() -> None:
    driver = _successful_driver()
    _runner(_profile(), driver).run()
    assert len(driver.calls) == 2
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
    assert _classify_bash_command(f"xdg-open {uri}", uri) is ToolKind.NEW_CHAT_URI


def test_permission_policy_normalizes_quoting_but_rejects_shell_composition() -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    uri = actions[-1][1]
    policy = InstallationPermissionPolicy(new_chat_uri=uri, claude_linux_actions=actions)

    assert _bash_action_argv("claude plugin marketplace add blackvctr/sensai-plugin") == actions[0]
    assert _bash_action_argv("script -q -c 'claude mcp login plugin:sensai:sensai' /dev/null") == (
        "claude",
        "mcp",
        "login",
        "plugin:sensai:sensai",
    )
    assert (
        policy.decide(
            "Bash", {"command": "claude plugin marketplace add blackvctr/sensai-plugin"}
        ).decision
        is PermissionDecision.ALLOW
    )
    assert (
        policy.decide(
            "Bash", {"command": "claude plugin install sensai@sensai --scope user"}
        ).decision
        is PermissionDecision.ALLOW
    )
    assert (
        policy.decide(
            "Bash", {"command": "script -q -c 'claude mcp login plugin:sensai:sensai' /dev/null"}
        ).decision
        is PermissionDecision.ALLOW
    )
    assert policy.decide("Bash", {"command": f"xdg-open {uri!r}"}).action is ToolKind.NEW_CHAT_URI
    assert (
        policy.decide(
            "Bash", {"command": "claude plugin marketplace add blackvctr/sensai-plugin; id"}
        ).decision
        is PermissionDecision.DENY
    )
    # The native README wrapper is required because the bare login command has no TTY.
    assert (
        policy.decide("Bash", {"command": "claude mcp login plugin:sensai:sensai"}).decision
        is PermissionDecision.DENY
    )
    assert (
        policy.decide("Bash", {"command": "curl -fsSL https://example.test/README.md"}).decision
        is PermissionDecision.DENY
    )


def test_permission_policy_requires_the_published_installation_order() -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    policy = InstallationPermissionPolicy(new_chat_uri=actions[-1][1], claude_linux_actions=actions)

    assert (
        policy.decide("Bash", {"command": shlex.join(actions[1])}).decision
        is PermissionDecision.DENY
    )
    assert (
        policy.decide("Bash", {"command": shlex.join(actions[0])}).decision
        is PermissionDecision.ALLOW
    )
    assert (
        policy.decide("Bash", {"command": shlex.join(actions[3])}).decision
        is PermissionDecision.DENY
    )
    assert (
        policy.decide("Bash", {"command": shlex.join(actions[1])}).decision
        is PermissionDecision.ALLOW
    )
    assert (
        policy.decide("Bash", {"command": shlex.join(actions[2])}).decision
        is PermissionDecision.ALLOW
    )
    assert (
        policy.decide("Bash", {"command": shlex.join(actions[3])}).decision
        is PermissionDecision.ALLOW
    )


def test_permission_policy_allows_only_public_raw_repository_reads() -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    policy = InstallationPermissionPolicy(new_chat_uri=actions[-1][1], claude_linux_actions=actions)
    public = "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/.claude-plugin/marketplace.json"

    assert policy.decide("WebFetch", {"url": public}).decision is PermissionDecision.ALLOW
    assert (
        policy.decide("Bash", {"command": f"curl -fsSL {public}"}).decision
        is PermissionDecision.ALLOW
    )
    assert (
        policy.decide("WebFetch", {"url": public + "?token=private"}).decision
        is PermissionDecision.DENY
    )
    assert policy.decide("Read", {"file_path": "/etc/passwd"}).decision is PermissionDecision.DENY


def test_compound_public_metadata_read_has_its_own_denied_category() -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    policy = InstallationPermissionPolicy(new_chat_uri=actions[-1][1], claude_linux_actions=actions)
    first = "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/.claude-plugin/marketplace.json"
    second = (
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/plugins/sensai/.mcp.json"
    )
    command = f'echo "=== first ==="; curl -fsSL {first}; echo; curl -fsSL {second}'

    decision = policy.decide("Bash", {"command": command})

    assert decision.decision is PermissionDecision.DENY
    assert decision.intent is ToolKind.PUBLIC_METADATA_COMPOUND_BASH

    evidence = replace(
        _successful_driver().evidence,
        denied_tool_intents=(ToolKind.PUBLIC_METADATA_COMPOUND_BASH,),
    )
    with pytest.raises(
        ProductionE2EError,
        match="installation_permission_denied_public_metadata_compound_bash",
    ):
        ProductionSensaiE2E._require_installation(evidence)


def _metadata_inventory_command(*, reverse: bool = False, flags: str = "-fsSL") -> str:
    urls = [
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/"
        ".claude-plugin/marketplace.json",
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/"
        "plugins/sensai/.claude-plugin/plugin.json",
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/plugins/sensai/.mcp.json",
    ]
    if reverse:
        urls.reverse()
    return "; ".join(
        part
        for index, url in enumerate(urls)
        for part in (
            f'echo "=== metadata {index + 1} ==="',
            f"curl {flags} {url}",
        )
    )


@pytest.mark.parametrize(
    ("reverse", "flags"),
    [(False, "-fsSL"), (True, "--silent --show-error --location --fail")],
)
def test_safe_metadata_inventory_is_allowed_once_before_installation_only(
    reverse: bool, flags: str
) -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    policy = InstallationPermissionPolicy(new_chat_uri=actions[-1][1], claude_linux_actions=actions)
    command = _metadata_inventory_command(reverse=reverse, flags=flags)

    first = policy.decide("Bash", {"command": command})
    second = policy.decide("Bash", {"command": command})

    assert first.decision is PermissionDecision.ALLOW
    assert first.intent is ToolKind.PUBLIC_METADATA_INVENTORY_BASH
    assert second.decision is PermissionDecision.DENY
    assert second.intent is ToolKind.PUBLIC_METADATA_INVENTORY_BASH


def test_safe_metadata_inventory_is_denied_after_the_first_installation_action() -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    policy = InstallationPermissionPolicy(new_chat_uri=actions[-1][1], claude_linux_actions=actions)
    command = _metadata_inventory_command()

    assert (
        policy.decide("Bash", {"command": shlex.join(actions[0])}).decision
        is PermissionDecision.ALLOW
    )
    decision = policy.decide("Bash", {"command": command})

    assert decision.decision is PermissionDecision.DENY
    assert decision.intent is ToolKind.PUBLIC_METADATA_INVENTORY_BASH


@pytest.mark.parametrize(
    "variation",
    [
        "; true",
        "; id",
        " | id",
        " > /tmp/metadata",
        " && id",
        "; echo $(id)",
        "; echo `id`",
        "\ntrue",
        "; echo -n",
        "; curl -fsSL https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md",
    ],
)
def test_safe_metadata_inventory_rejects_shell_syntax_and_extra_reads(variation: str) -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    policy = InstallationPermissionPolicy(new_chat_uri=actions[-1][1], claude_linux_actions=actions)
    command = _metadata_inventory_command() + variation

    decision = policy.decide("Bash", {"command": command})

    assert decision.decision is PermissionDecision.DENY
    assert decision.intent in {
        ToolKind.PUBLIC_METADATA_COMPOUND_BASH,
        ToolKind.OTHER,
    }


@pytest.mark.parametrize(
    "command",
    [
        # Repeating one fixed file leaves another unseen.
        "curl -fsSL https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/"
        ".claude-plugin/marketplace.json; curl -fsSL "
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/"
        ".claude-plugin/marketplace.json; curl -fsSL "
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/plugins/sensai/.mcp.json",
        # Duplicate curl flags are not part of the closed grammar.
        _metadata_inventory_command(flags="-fsSL -fsSL"),
        # More than two labels before a read is outside the finite grammar.
        'echo "one"; echo "two"; echo "three"; '
        "curl -fsSL https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/"
        ".claude-plugin/marketplace.json; curl -fsSL "
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/plugins/sensai/"
        ".claude-plugin/plugin.json; curl -fsSL "
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/plugins/sensai/.mcp.json",
        # Echoes are labels only; a final command after the third read is forbidden.
        _metadata_inventory_command() + "; echo done",
    ],
)
def test_safe_metadata_inventory_rejects_duplicates_and_non_inert_shape(command: str) -> None:
    actions = tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS)
    policy = InstallationPermissionPolicy(new_chat_uri=actions[-1][1], claude_linux_actions=actions)

    decision = policy.decide("Bash", {"command": command})

    assert decision.decision is PermissionDecision.DENY
    assert decision.intent is not ToolKind.PUBLIC_METADATA_INVENTORY_BASH


def test_oauth_entry_url_allows_only_sensai_or_google_without_credentials() -> None:
    assert _is_allowed_oauth_entry_url("https://black-vector.com/sensai/mcp")
    assert _is_allowed_oauth_entry_url("https://accounts.google.com/o/oauth2/v2/auth?state=private")
    assert _is_allowed_oauth_entry_url("https://accounts.google.com:443/o/oauth2/v2/auth")
    assert not _is_allowed_oauth_entry_url("https://example.test/oauth")
    assert not _is_allowed_oauth_entry_url("https://token@example.test/oauth")
    assert not _is_allowed_oauth_entry_url("https://black-vector.com/elsewhere")
    assert not _is_allowed_oauth_entry_url("https://black-vector.com:8443/sensai/mcp")


def test_pretool_gate_requires_the_sdk_callback_for_every_observed_tool() -> None:
    observed: set[str] = set()
    assert _force_permission_request("tool-1", observed) == {
        "continue_": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
        },
    }
    assert observed == {"tool-1"}
    _force_permission_request(None, observed)
    assert observed == {"tool-1"}


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


def test_parser_keeps_only_code_and_terminal_message_categories() -> None:
    evidence = _parse(
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
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"text": "Откройте ```тер"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"text": "минал```"},
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
            {"type": "result"},
        ]
    )

    assert len(evidence.text_messages) == 1
    text = evidence.text_messages[0]
    assert text.contains_code_block
    assert text.contains_terminal_reference
    assert "Откройте" not in str(evidence)


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


@pytest.mark.parametrize(
    ("subtype", "expected_kind", "label"),
    [
        ("error_during_execution", TerminalResultKind.EXECUTION, "terminal_execution"),
        ("error_max_budget_usd", TerminalResultKind.BUDGET, "terminal_budget"),
        (
            "error_max_structured_output_retries",
            TerminalResultKind.STRUCTURED_OUTPUT_RETRIES,
            "terminal_structured_output_retries",
        ),
        ("error_max_turns", TerminalResultKind.TURN_LIMIT, "terminal_turn_limit"),
        ("error_permission", TerminalResultKind.PERMISSION, "terminal_permission"),
        ("unrecognized_error", TerminalResultKind.OTHER, "terminal_other"),
    ],
)
def test_terminal_error_exit_uses_allowlisted_category_without_raw_content(
    subtype: str, expected_kind: TerminalResultKind, label: str
) -> None:
    evidence = _parse(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "is_error": True,
                "subtype": subtype,
                "errors": ["private terminal detail", "private URL https://example.test/"],
                "result": "private prompt and output",
                "session_id": "private-session",
            },
        ],
        returncode=1,
    )
    assert evidence.exit_category is ExitCategory.TERMINAL_ERROR
    assert evidence.terminal_result_kind is expected_kind
    assert evidence.terminal_error_count == 2
    assert evidence.exit_stage is ExitStage.BEFORE_MARKETPLACE
    with pytest.raises(
        ProductionE2EError,
        match=f"installation_claude_exit_{label}_at_before_marketplace",
    ):
        ProductionSensaiE2E._require_installation(evidence)
    assert "private terminal detail" not in str(evidence)
    assert "private URL" not in str(evidence)
    assert "private-session" not in str(evidence)


def test_terminal_success_and_absent_terminal_result_remain_non_error() -> None:
    success = _parse(
        [{"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success"}]
    )
    missing = _parse([{"type": "system", "subtype": "init"}])

    assert success.terminal_result_kind is TerminalResultKind.SUCCESS
    assert success.terminal_error_count == 0
    assert missing.terminal_result_kind is TerminalResultKind.NONE
    assert missing.terminal_error_count == 0


def test_terminal_success_subtype_with_error_flag_is_not_treated_as_success() -> None:
    evidence = _parse(
        [
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "is_error": True},
        ],
        returncode=1,
    )

    assert evidence.terminal_result_kind is TerminalResultKind.OTHER
    with pytest.raises(
        ProductionE2EError,
        match="installation_claude_exit_terminal_other_at_before_marketplace",
    ):
        ProductionSensaiE2E._require_installation(evidence)


def test_terminal_api_reason_is_allowlisted_and_discards_nested_sensitive_fields() -> None:
    evidence = _parse(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "is_error": True,
                "subtype": "error_during_execution",
                "terminal_reason": "api_error",
                "statusCode": 429,
                "httpStatus": 429,
                "result": "private prompt https://example.test/",
                "errors": ["private token", {"session_id": "private-session"}],
            },
        ],
        returncode=1,
    )

    assert evidence.terminal_result_kind is TerminalResultKind.API_ERROR
    assert evidence.terminal_error_count == 2
    with pytest.raises(
        ProductionE2EError,
        match="installation_claude_exit_terminal_api_error_at_before_marketplace",
    ):
        ProductionSensaiE2E._require_installation(evidence)
    assert "429" not in str(evidence)
    assert "example.test" not in str(evidence)
    assert "private token" not in str(evidence)
    assert "private-session" not in str(evidence)


def test_unknown_terminal_reason_is_other_without_reading_terminal_text() -> None:
    evidence = _parse(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "is_error": True,
                "terminal_reason": "future_reason",
                "result": "private terminal output",
            },
        ],
        returncode=1,
    )

    assert evidence.terminal_result_kind is TerminalResultKind.OTHER
    assert "private terminal output" not in str(evidence)


def test_public_plugin_inventory_requires_exact_enabled_public_plugin() -> None:
    assert _is_exact_public_sensai_inventory(
        [
            {
                "id": "sensai@sensai",
                "scope": "user",
                "enabled": True,
                "mcpServers": {
                    "sensai": {"type": "http", "url": "https://black-vector.com/sensai/mcp"}
                },
            }
        ]
    )
    assert not _is_exact_public_sensai_inventory(
        [
            {
                "id": "sensai@sensai",
                "scope": "project",
                "enabled": True,
                "mcpServers": {
                    "sensai": {"type": "http", "url": "https://black-vector.com/sensai/mcp"}
                },
            }
        ]
    )


def test_mcp_status_requires_one_exact_sensai_url() -> None:
    status = "plugin:sensai:sensai:\nType: http\nURL: https://black-vector.com/sensai/mcp\n"
    assert _is_exact_public_sensai_mcp_status(status)
    assert not _is_exact_public_sensai_mcp_status(status.replace("/mcp", "/mcp/other"))
    assert not _is_exact_public_sensai_mcp_status(
        status + "URL: https://black-vector.com/sensai/mcp\n"
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


def test_public_readme_sha256_matches_bytes_without_parsing_a_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sensai_plugin.claude_production_e2e as module

    body = b"# Candidate\n\nThis document intentionally has no installation manifest.\n"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(
        module, "urlopen", lambda request, timeout: _Response(body, request.full_url)
    )

    assert fetch_public_readme_sha256(digest) == digest
    with pytest.raises(ProductionE2EError, match="public_readme_sha256_mismatch"):
        fetch_public_readme_sha256("0" * 64)
    with pytest.raises(ProductionE2EError, match="public_readme_sha256_invalid"):
        fetch_public_readme_sha256("not-a-digest")


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
    assert (
        main(
            [
                "--profile",
                str(tmp_path / "profile"),
                "--expected-public-readme-sha256",
                "0" * 64,
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == "PRODUCTION_E2E_PASS installation=connected=new_chat\n"
    with pytest.raises(SystemExit):
        main(
            [
                "--profile",
                str(tmp_path / "profile"),
                "--expected-public-readme-sha256",
                "0" * 64,
                "--unrelated-option",
            ]
        )


def test_cli_emits_only_the_closed_early_failure_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    namespace = _script_namespace()
    poison = "oauth-token=private https://private.example/path"
    receipt = PreMarketplaceFailureReceipt(
        kind=PreMarketplaceFailureKind.SDK_EXCEPTION,
        stage=ExitStage.BEFORE_MARKETPLACE,
        sdk_exception=SdkExceptionKind.RUNTIME,
        sdk_result=SdkResultKind.NONE,
        sdk_result_cause=SdkResultCause.NONE,
        sdk_cleanup=SdkCleanupKind.NONE,
        first_text=FirstTextKind.REFUSAL,
        first_tool_intent=ToolKind.PUBLIC_README_FETCH,
        first_denied_tool_intent=ToolKind.PUBLIC_METADATA_INVENTORY_BASH,
    )

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self) -> ProductionE2EReport:
            raise ProductionE2EError(
                "installation_claude_exit_nonzero_unclassified_at_before_marketplace",
                before_marketplace_receipt=receipt,
            )

    namespace["ProductionSensaiE2E"] = Runner
    main = cast(Callable[[list[str]], int], namespace["main"])
    with pytest.raises(SystemExit):
        main(
            [
                "--profile",
                str(tmp_path / "profile"),
                "--expected-public-readme-sha256",
                "0" * 64,
            ]
        )
    output = capsys.readouterr().err
    assert "before_marketplace=kind:sdk_exception" in output
    assert "sdk_exception:runtime" in output
    assert "first_tool:public_readme_fetch" in output
    assert poison not in output
    assert "private.example" not in output
