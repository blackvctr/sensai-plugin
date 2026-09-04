from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

import sensai_plugin.claude_production_e2e as production_module
from sensai_plugin.claude_e2e_profile import provision_profile
from sensai_plugin.claude_production_e2e import (
    PUBLIC_README_URL,
    AgentEvidence,
    ClaudeDriver,
    ProductionE2EError,
    ProductionSensaiE2E,
    SensaiReplyKind,
    SshOperatorProofVerifier,
    SubprocessClaudeDriver,
    TextEvidence,
    ToolKind,
    ToolResultEvidence,
    _assert_normal_browser_path,
    _classify_bash_command,
    _consume_stream,
    _is_exact_public_sensai_inventory,
    fetch_public_readme_contract,
)
from sensai_plugin.installation_e2e_contract import _public_contract_from_markdown


@pytest.fixture(autouse=True)
def _linux_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _profile(tmp_path: Path) -> Path:
    source = tmp_path / "source" / ".credentials.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({"claudeAiOauth": {"token": "private-token"}}), encoding="utf-8")
    source.chmod(0o600)
    target = Path.home() / ".local" / "share" / "sensai-e2e"
    return provision_profile(target, source).root


def _test_contract():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    return _public_contract_from_markdown(readme.read_text(encoding="utf-8"))


class _PublicResponse:
    def __init__(self, body: bytes, final_url: str) -> None:
        self._body = body
        self._final_url = final_url

    def __enter__(self) -> _PublicResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, _limit: int) -> bytes:
        return self._body


def _operator_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_bytes: bytes | None = None,
) -> tuple[Path, Path, Path]:
    root = tmp_path / "sensai"
    root.mkdir(mode=0o700)
    config = root / "local-e2e-proof-ssh.json"
    known_hosts = root / "local-e2e-proof-known_hosts"
    config.write_bytes(
        config_bytes
        or json.dumps({"schema": "sensai-local-e2e-ssh-v1", "host": "proof.example"}).encode()
    )
    known_hosts.write_text("proof.example ssh-ed25519 public-key\n", encoding="utf-8")
    config.chmod(0o600)
    known_hosts.chmod(0o600)
    monkeypatch.setattr(production_module, "_OPERATOR_CONFIG_ROOT", root)
    monkeypatch.setattr(production_module, "_OPERATOR_CONFIG", config)
    monkeypatch.setattr(production_module, "_OPERATOR_KNOWN_HOSTS", known_hosts)
    return root, config, known_hosts


class _OpenStdin(io.BytesIO):
    """Preserve bytes after the code under test has requested close."""

    close_requested = False

    def close(self) -> None:
        self.close_requested = True


class _ProofProcess:
    def __init__(self, output: bytes, *, returncode: int | None = 0) -> None:
        read_fd, write_fd = os.pipe()
        if output:
            os.write(write_fd, output)
            os.close(write_fd)
            self._write_fd: int | None = None
        else:
            self._write_fd = write_fd
        self.stdin = _OpenStdin()
        self.stdout = os.fdopen(read_fd, "rb")
        self.returncode = returncode
        self.pid = 4242
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return self.returncode if self.returncode is not None else -15

    def close(self) -> None:
        self.stdout.close()
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None


def test_public_readme_fetch_uses_exact_url_and_rejects_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (Path(__file__).resolve().parents[1] / "README.md").read_bytes()

    def public_source(request: object, *, timeout: int) -> _PublicResponse:
        assert request.full_url == PUBLIC_README_URL
        assert timeout == 20
        return _PublicResponse(body, PUBLIC_README_URL)

    monkeypatch.setattr(production_module, "urlopen", public_source)
    assert fetch_public_readme_contract().russian_install_prompt.startswith("Установи Sensai ")

    monkeypatch.setattr(
        production_module,
        "urlopen",
        lambda *_args, **_kwargs: _PublicResponse(body, "https://example.invalid/README.md"),
    )
    with pytest.raises(ProductionE2EError, match="public_readme_redirected"):
        fetch_public_readme_contract()


def test_operator_proof_sends_only_hash_and_accepts_only_exact_verified_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, known_hosts = _operator_files(tmp_path, monkeypatch)
    monkeypatch.setattr(production_module, "_strict_ssh_binary", lambda: None)
    seen: dict[str, object] = {}
    process = _ProofProcess(b'{"schema":"sensai-local-e2e-proof-v1","result":"verified"}\n')

    def ssh(argv: list[str], **_kwargs: object) -> object:
        seen["argv"] = argv
        seen["kwargs"] = _kwargs
        return process

    monkeypatch.setattr(production_module.subprocess, "Popen", ssh)
    reply_digest = "a" * 64
    assert SshOperatorProofVerifier().verifies_digest(reply_digest)
    assert process.stdin.close_requested
    assert process.stdin.getvalue() == (
        b'{"schema":"sensai-local-e2e-proof-v1","response_sha256":"'
        + reply_digest.encode()
        + b'"}\n'
    )
    assert seen["kwargs"] == {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }
    assert f"UserKnownHostsFile={known_hosts}" in seen["argv"]
    assert seen["argv"][-1] == "/opt/sensai/bin/sensai_local_e2e_proof.py"
    process.close()


def test_operator_proof_fails_closed_for_missing_config_bad_output_and_ssh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(production_module, "_OPERATOR_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(production_module, "_OPERATOR_CONFIG_ROOT", tmp_path)
    assert not SshOperatorProofVerifier().verifies_digest("a" * 64)


@pytest.mark.parametrize(
    "output",
    [
        b'{"schema":"sensai-local-e2e-proof-v1","result":"not_verified"}\n',
        b'{"schema":"sensai-local-e2e-proof-v1","result":"verified"}\nX',
    ],
    ids=["not_verified", "trailing_byte"],
)
def test_operator_proof_rejects_any_response_other_than_the_exact_success_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    _operator_files(tmp_path, monkeypatch)
    monkeypatch.setattr(production_module, "_strict_ssh_binary", lambda: None)
    process = _ProofProcess(output)
    monkeypatch.setattr(production_module.subprocess, "Popen", lambda *_args, **_kwargs: process)

    assert not SshOperatorProofVerifier().verifies_digest("a" * 64)
    process.close()


@pytest.mark.parametrize("name", ["config", "known_hosts"])
@pytest.mark.parametrize("defect", ["symlink", "nonregular", "wrong_mode", "wrong_owner"])
def test_private_operator_files_reject_every_unsafe_file_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    defect: str,
) -> None:
    root, config, known_hosts = _operator_files(tmp_path, monkeypatch)
    path = config if name == "config" else known_hosts
    if defect == "symlink":
        target = root / f"{name}.target"
        target.write_text("private\n", encoding="utf-8")
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    elif defect == "nonregular":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif defect == "wrong_mode":
        path.chmod(0o640)
    else:
        original_lstat = os.lstat

        def wrong_owner(candidate: str | os.PathLike[str]) -> os.stat_result:
            item = original_lstat(candidate)
            if Path(candidate) == path:
                values = list(item)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return item

        monkeypatch.setattr(production_module.os, "lstat", wrong_owner)

    with pytest.raises(ValueError):
        production_module._strict_private_file(path)


def test_private_operator_files_reject_an_unsafe_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, _ = _operator_files(tmp_path, monkeypatch)
    root.chmod(0o755)

    with pytest.raises(ValueError, match="configuration directory"):
        production_module._strict_private_file(config)


@pytest.mark.parametrize("defect", ["symlink", "wrong_owner", "non_directory"])
def test_private_operator_files_reject_each_unsafe_configuration_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    root, config, known_hosts = _operator_files(tmp_path, monkeypatch)
    if defect == "symlink":
        target = tmp_path / "real-sensai"
        root.replace(target)
        root.symlink_to(target, target_is_directory=True)
    elif defect == "wrong_owner":
        original_lstat = os.lstat

        def wrong_owner(candidate: str | os.PathLike[str]) -> os.stat_result:
            item = original_lstat(candidate)
            if Path(candidate) == root:
                values = list(item)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return item

        monkeypatch.setattr(production_module.os, "lstat", wrong_owner)
    else:
        config.unlink()
        known_hosts.unlink()
        root.rmdir()
        root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="configuration directory"):
        production_module._strict_private_file(config)


def test_private_operator_file_rejects_a_path_outside_the_private_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _operator_files(tmp_path, monkeypatch)
    outsider = tmp_path / "outside.json"
    outsider.write_text("{}", encoding="utf-8")
    outsider.chmod(0o600)

    with pytest.raises(ValueError, match="outside"):
        production_module._strict_private_file(outsider)


def test_private_operator_file_detects_replacement_between_check_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, _ = _operator_files(tmp_path, monkeypatch)
    replacement = root / "replacement.json"
    replacement.write_text("{}", encoding="utf-8")
    replacement.chmod(0o600)
    original_open = os.open

    def replace_before_open(candidate: str | os.PathLike[str], *args: object) -> int:
        if Path(candidate) == config:
            replacement.replace(config)
        return original_open(candidate, *args)

    monkeypatch.setattr(production_module.os, "open", replace_before_open)

    with pytest.raises(ValueError, match="changed while reading"):
        production_module._strict_private_file(config)


@pytest.mark.parametrize(
    "config_bytes",
    [
        b'{"schema":"sensai-local-e2e-ssh-v1","schema":"other","host":"proof.example"}',
        b'{"schema":"sensai-local-e2e-ssh-v1","host":"proof.example","extra":true}',
        b'{"schema":"other","host":"proof.example"}',
        b'{"schema":"sensai-local-e2e-ssh-v1","host":42}',
        b'{"schema":"sensai-local-e2e-ssh-v1","host":"-not-a-host"}',
        b"x" * 4097,
    ],
    ids=["duplicate", "unknown", "wrong_schema", "non_string_host", "invalid_host", "oversize"],
)
def test_operator_proof_rejects_invalid_private_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_bytes: bytes
) -> None:
    _operator_files(tmp_path, monkeypatch, config_bytes=config_bytes)
    monkeypatch.setattr(production_module, "_strict_ssh_binary", lambda: None)
    monkeypatch.setattr(
        production_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("SSH must not start for invalid configuration"),
    )

    assert not SshOperatorProofVerifier().verifies_digest("a" * 64)


def test_strict_ssh_binary_accepts_only_a_root_owned_regular_nonwritable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ssh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(production_module, "_SSH_EXECUTABLE", executable)
    original_lstat = os.lstat

    def root_owned(candidate: str | os.PathLike[str]) -> os.stat_result:
        item = original_lstat(candidate)
        if Path(candidate) == executable:
            values = list(item)
            values[4] = 0
            return os.stat_result(values)
        return item

    monkeypatch.setattr(production_module.os, "lstat", root_owned)
    production_module._strict_ssh_binary()

    executable.chmod(0o775)
    with pytest.raises(ValueError, match="ssh executable"):
        production_module._strict_ssh_binary()


@pytest.mark.parametrize("defect", ["symlink", "nonregular", "wrong_owner", "wrong_mode"])
def test_strict_ssh_binary_rejects_each_untrusted_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    executable = tmp_path / "ssh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(production_module, "_SSH_EXECUTABLE", executable)
    if defect == "symlink":
        target = tmp_path / "real-ssh"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
        executable.unlink()
        executable.symlink_to(target)
    elif defect == "nonregular":
        executable.unlink()
        os.mkfifo(executable, 0o700)
    elif defect == "wrong_mode":
        executable.chmod(0o775)

    if defect != "wrong_owner":
        original_lstat = os.lstat

        def root_owned(candidate: str | os.PathLike[str]) -> os.stat_result:
            item = original_lstat(candidate)
            if Path(candidate) == executable:
                values = list(item)
                values[4] = 0
                return os.stat_result(values)
            return item

        monkeypatch.setattr(production_module.os, "lstat", root_owned)

    with pytest.raises(ValueError, match="ssh executable"):
        production_module._strict_ssh_binary()


def test_operator_proof_caps_stdout_and_reaps_the_isolated_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _operator_files(tmp_path, monkeypatch)
    monkeypatch.setattr(production_module, "_strict_ssh_binary", lambda: None)
    process = _ProofProcess(b"x" * (production_module._MAX_OPERATOR_PROOF_OUTPUT + 1))
    terminated: list[object] = []
    popen_kwargs: dict[str, object] = {}

    def popen(*_args: object, **kwargs: object) -> _ProofProcess:
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(production_module.subprocess, "Popen", popen)
    monkeypatch.setattr(production_module, "_terminate", terminated.append)

    assert not SshOperatorProofVerifier().verifies_digest("a" * 64)
    assert popen_kwargs["start_new_session"] is True
    assert terminated == [process]
    process.close()


def test_operator_proof_timeout_reaps_the_process_group_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _operator_files(tmp_path, monkeypatch)
    monkeypatch.setattr(production_module, "_strict_ssh_binary", lambda: None)
    process = _ProofProcess(b"", returncode=None)
    terminated: list[object] = []

    class NeverReadySelector:
        def __init__(self) -> None:
            self._items: dict[int, object] = {}

        def register(self, fileobj: object, _events: int) -> None:
            self._items[id(fileobj)] = fileobj

        def get_map(self) -> dict[int, object]:
            return self._items

        def select(self, _timeout: float) -> list[object]:
            return []

        def close(self) -> None:
            return None

    clock = iter((0.0, 61.0))
    monkeypatch.setattr(production_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(production_module.selectors, "DefaultSelector", NeverReadySelector)
    monkeypatch.setattr(production_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(production_module, "_terminate", terminated.append)

    assert not SshOperatorProofVerifier().verifies_digest("a" * 64)
    assert terminated == [process]
    process.close()


def test_terminate_kills_the_whole_process_group_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class Process:
        pid = 9876

        def __init__(self) -> None:
            self.wait_timeouts: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) == 1:
                raise subprocess.TimeoutExpired("ssh", timeout)
            return -9

    process = Process()
    monkeypatch.setattr(
        production_module.os,
        "killpg",
        lambda process_group, signal: calls.append((process_group, signal)),
    )

    production_module._terminate(process)  # type: ignore[arg-type]

    assert calls == [
        (process.pid, production_module.signal.SIGTERM),
        (process.pid, production_module.signal.SIGKILL),
    ]
    assert process.wait_timeouts == [production_module.PROCESS_TERMINATION_GRACE_SECONDS, None]


def _runner(
    profile: Path, driver: ClaudeDriver, proof: object | None = None
) -> ProductionSensaiE2E:
    return ProductionSensaiE2E(
        profile=profile,
        driver=driver,
        contract_loader=_test_contract,
        executable_resolver=lambda: "claude",
        operator_proof=proof,
    )


def _text(*, expected: bool = False) -> TextEvidence:
    return TextEvidence(matches_expected=expected, cyrillic_letters=12, latin_letters=2)


def _evidence(
    *tools: ToolKind,
    texts: tuple[TextEvidence, ...] = (),
    sensai_reply: SensaiReplyKind | None = None,
    reply_sha256: str | None = None,
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
                reply_sha256=reply_sha256 if tool is ToolKind.TELL_SENSAI else None,
            )
            for tool in tools
        ),
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
    timeout_seconds: int
    expected_visible_messages: tuple[str, ...] | None
    expected_session: uuid.UUID | None


class _FakeDriver(ClaudeDriver):
    def __init__(
        self,
        agent_results: Sequence[AgentEvidence],
        *,
        connected: bool = True,
        raise_on_agent_call: int | None = None,
    ) -> None:
        self._agent_results = iter(agent_results)
        self.connected = connected
        self._raise_on_agent_call = raise_on_agent_call
        self._agent_calls = 0
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
        self._agent_calls += 1
        if self._agent_calls == self._raise_on_agent_call:
            raise ProductionE2EError("telegram_start_stream_invalid")
        return next(self._agent_results)

    def mcp_configuration_observed(
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
        self.calls.append(
            _Call(tuple(command), cwd, dict(environment), timeout_seconds, None, None)
        )
        return True

    def public_sensai_plugin_installed(
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
        return True


def _successful_driver() -> _FakeDriver:
    return _FakeDriver(
        (
            _evidence(
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.LOGIN,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=True)),
            ),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.INITIAL_DISCOVERY),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.OTHER),
            _evidence(ToolKind.FORGET_ME),
        )
    )


class _Proof:
    def __init__(self, result: bool | Exception, events: list[str]) -> None:
        self._result = result
        self._events = events

    def verifies_digest(self, _digest: str) -> bool:
        self._events.append("proof")
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_operator_proof_runs_after_telegram_reply_and_before_forget_me(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class OrderedDriver(_FakeDriver):
        def run_agent(self, *args: object, **kwargs: object) -> AgentEvidence:
            evidence = super().run_agent(*args, **kwargs)  # type: ignore[arg-type]
            prompt = args[0][-1]
            if prompt == production_module._TELEGRAM_FACTS:
                events.append("telegram_reply")
            elif prompt == production_module._FORGET_ME_REQUEST:
                events.append("forget_me")
            return evidence

    driver = OrderedDriver(
        (
            _evidence(
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.LOGIN,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=True)),
            ),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.INITIAL_DISCOVERY),
            _evidence(ToolKind.TELL_SENSAI, reply_sha256="a" * 64),
            _evidence(ToolKind.FORGET_ME),
        )
    )
    assert _runner(_profile(tmp_path), driver, _Proof(True, events)).run().forget_me_completed
    assert events == ["telegram_reply", "proof", "forget_me"]


def test_operator_proof_failure_and_exception_still_reach_forget_me(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    for result in (False, RuntimeError("private")):
        events: list[str] = []
        driver = _FakeDriver(
            (
                _evidence(
                    ToolKind.MARKETPLACE_ADD,
                    ToolKind.PLUGIN_INSTALL,
                    ToolKind.LOGIN,
                    ToolKind.NEW_CHAT_URI,
                    texts=(_text(expected=True), _text(expected=True)),
                ),
                _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.INITIAL_DISCOVERY),
                _evidence(ToolKind.TELL_SENSAI, reply_sha256="a" * 64),
                _evidence(ToolKind.FORGET_ME),
            )
        )
        proof = _Proof(result, events)
        with pytest.raises((ProductionE2EError, RuntimeError)):
            _runner(profile, driver, proof).run()
        assert events == ["proof"]
        assert driver.calls[-1].command[-1].startswith("Заверши проверку")


def _argument(command: Sequence[str], option: str) -> str:
    return command[command.index(option) + 1]


def test_production_route_uses_public_input_production_model_and_resumed_telegram_session(
    tmp_path: Path,
) -> None:
    driver = _successful_driver()
    profile = _profile(tmp_path)

    with pytest.raises(ProductionE2EError, match="telegram_operator_proof_not_verified"):
        _runner(profile, driver).run()
    assert len(driver.calls) == 7
    auth_status, installation, status, plugin_list, telegram_start, continuation, cleanup = (
        driver.calls
    )
    assert auth_status.command == ("claude", "auth", "status")
    assert installation.command[0:2] == ("claude", "-p")
    assert _argument(installation.command, "--model") == "claude-sonnet-5"
    assert "--output-format" in installation.command
    assert _argument(installation.command, "--output-format") == "stream-json"
    assert "--no-browser" not in installation.command
    assert installation.expected_visible_messages is not None
    assert installation.command[-1].startswith("Установи Sensai ")
    assert status.command == ("claude", "mcp", "get", "plugin:sensai:sensai")
    assert plugin_list.command == ("claude", "plugin", "list", "--json")
    assert "--session-id" in telegram_start.command
    assert "--resume" in continuation.command
    assert _argument(telegram_start.command, "--session-id") == _argument(
        continuation.command, "--resume"
    )
    assert _argument(continuation.command, "--resume") == _argument(cleanup.command, "--resume")
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

    with pytest.raises(ProductionE2EError, match="telegram_operator_proof_not_verified"):
        _runner(profile, driver).run()

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
    assert _classify_bash_command(f"xdg-open '{uri}'", uri) is ToolKind.NEW_CHAT_URI
    assert (
        _classify_bash_command("claude mcp login --no-browser", uri)
        is ToolKind.FORBIDDEN_BROWSER_MODE
    )


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([{"id": "sensai@sensai", "scope": "user", "enabled": True}], True),
        ([], False),
        ([{"id": "sensai@sensai-local", "scope": "user", "enabled": True}], False),
        ([{"id": "sensai@sensai", "scope": "project", "enabled": True}], False),
        ([{"id": "sensai@sensai", "scope": "user", "enabled": False}], False),
        (
            [
                {"id": "sensai@sensai", "scope": "user", "enabled": True},
                {"id": "sensai@stale", "scope": "user", "enabled": True},
            ],
            False,
        ),
        ({"id": "sensai@sensai"}, False),
    ],
)
def test_public_plugin_inventory_rejects_absent_wrong_and_duplicate_entries(
    entries: object, expected: bool
) -> None:
    assert _is_exact_public_sensai_inventory(entries) is expected


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
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.LOGIN,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=False)),
            ),
            _evidence(ToolKind.FORGET_ME),
        )
    )

    with pytest.raises(ProductionE2EError, match="installation_messages_not_exact"):
        _runner(_profile(tmp_path), driver).run()

    assert len(driver.calls) == 3
    assert _argument(driver.calls[1].command, "--session-id") == _argument(
        driver.calls[2].command, "--resume"
    )


def test_failed_telegram_turn_still_calls_forget_me_before_temporary_profile_is_deleted(
    tmp_path: Path,
) -> None:
    driver = _FakeDriver(
        (
            _evidence(
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.LOGIN,
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

    assert len(driver.calls) == 6
    assert driver.calls[-1].command[-1].startswith("Заверши проверку")
    assert not list((profile / "runs").iterdir())


def test_first_telegram_exception_cleans_up_with_the_installation_session(tmp_path: Path) -> None:
    driver = _FakeDriver(
        (
            _evidence(
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.LOGIN,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=True)),
            ),
            _evidence(ToolKind.FORGET_ME),
        ),
        raise_on_agent_call=2,
    )

    with pytest.raises(ProductionE2EError, match="telegram_start_stream_invalid"):
        _runner(_profile(tmp_path), driver).run()

    installation = driver.calls[1]
    cleanup = driver.calls[-1]
    assert _argument(installation.command, "--session-id") == _argument(cleanup.command, "--resume")
    assert installation.expected_session == cleanup.expected_session


def test_cleanup_failure_is_reported_and_temporary_profile_is_still_deleted(tmp_path: Path) -> None:
    driver = _FakeDriver(
        (
            _evidence(
                ToolKind.MARKETPLACE_ADD,
                ToolKind.PLUGIN_INSTALL,
                ToolKind.LOGIN,
                ToolKind.NEW_CHAT_URI,
                texts=(_text(expected=True), _text(expected=True)),
            ),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.INITIAL_DISCOVERY),
            _evidence(ToolKind.TELL_SENSAI, sensai_reply=SensaiReplyKind.OTHER),
            _evidence(ToolKind.OTHER),
        )
    )
    profile = _profile(tmp_path)

    with pytest.raises(ProductionE2EError, match="telegram_operator_proof_not_verified"):
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
