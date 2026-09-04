"""Run the local, production Sensai installation-and-Telegram acceptance path.

This module intentionally has one direct route:

* Claude receives the public Russian line from the public README;
* Claude uses the public plugin and the production Sensai service;
* the normal ``mcp login`` route may open the local browser; and
* the same isolated Claude run starts and continues the public Telegram
  consultation before calling ``forget_me``.

It does not start a substitute Sensai server, replace an URL, use a browser
profile, add ``--no-browser``, or write a transcript.  The stream parser keeps
only bounded, non-secret facts: message hashes are compared in memory and then
discarded; tool names are reduced to a small fixed vocabulary; and command
output is reduced to a connection boolean.  OAuth URLs, codes, tokens,
credentials, prompts, assistant text, and raw stream records never leave the
running process.

The runner is deliberately not invoked by ordinary tests.  Unit tests inject
an in-memory driver and prove the command, sequence, continuation, redaction,
and cleanup rules.  A real invocation is an explicit local production check.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sensai_plugin.claude_e2e_profile import ClaudeE2ERun, create_fresh_run
from sensai_plugin.installation_e2e_contract import (
    CLAUDE_SONNET_5_MODEL,
    PublicReadmeContract,
    _public_contract_from_markdown,
)

INSTALL_TIMEOUT_SECONDS = 300
MCP_STATUS_TIMEOUT_SECONDS = 20
TELEGRAM_START_TIMEOUT_SECONDS = 120
TELEGRAM_CONTINUATION_TIMEOUT_SECONDS = 120
FORGET_ME_TIMEOUT_SECONDS = 60
PROCESS_TERMINATION_GRACE_SECONDS = 3
MAX_STREAM_LINE_BYTES = 256 * 1024
MAX_STREAM_EVENTS = 128
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_TOOL_INPUT_BYTES = 32 * 1024
MAX_PUBLIC_README_BYTES = 2 * 1024 * 1024
PUBLIC_README_URL = "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md"
_OPERATOR_PROOF_SCHEMA = "sensai-local-e2e-proof-v1"
_OPERATOR_CONFIG_SCHEMA = "sensai-local-e2e-ssh-v1"
_OPERATOR_CONFIG = Path.home() / ".config" / "sensai" / "local-e2e-proof-ssh.json"
_OPERATOR_CONFIG_ROOT = _OPERATOR_CONFIG.parent
_OPERATOR_KNOWN_HOSTS = _OPERATOR_CONFIG_ROOT / "local-e2e-proof-known_hosts"
_OPERATOR_IDENTITY = _OPERATOR_CONFIG_ROOT / "local-e2e-proof-identity"
_SSH_EXECUTABLE = Path("/usr/bin/ssh")
_REMOTE_PROOF_COMMAND = "/opt/sensai/bin/sensai_local_e2e_proof.py"
_MAX_OPERATOR_PROOF_OUTPUT = 256

_STATUS_FAILURE = re.compile(r"needs authentication|disconnected|\berror\b", re.IGNORECASE)
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-z]")

# These are deliberately ordinary Russian facts, not a fake tool result.
_TELEGRAM_FACTS = (
    "Я маркетолог. Регулярно отвечаю клиентам в Telegram и хочу быстрее "
    "готовить ответы на повторяющиеся вопросы."
)
_FORGET_ME_REQUEST = "Заверши проверку: вызови инструмент Sensai forget_me ровно один раз."
_INITIAL_DISCOVERY_REPLY = (
    "Я Sensai. Помоги получить от человека его роль, примерно пять привычных программ или сайтов "  # noqa: RUF001
    "и повторяющиеся рабочие задачи, затем передай мне ответы. Так рекомендации будут полезны "
    "именно для его работы."  # noqa: RUF001
)


class ProductionE2EError(RuntimeError):
    """The safe production acceptance report contains one failed phase."""


def fetch_public_readme_contract() -> PublicReadmeContract:
    """Fetch the exact public README used by the person; never consult checkout text."""

    request = Request(PUBLIC_README_URL, headers={"Accept": "text/plain"})
    try:
        with urlopen(request, timeout=20) as response:
            if response.geturl() != PUBLIC_README_URL:
                raise ProductionE2EError("public_readme_redirected")
            body = response.read(MAX_PUBLIC_README_BYTES + 1)
    except OSError as error:
        raise ProductionE2EError("public_readme_unavailable") from error
    if len(body) > MAX_PUBLIC_README_BYTES:
        raise ProductionE2EError("public_readme_too_large")
    try:
        contract = _public_contract_from_markdown(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProductionE2EError("public_readme_invalid") from error
    if contract.russian_install_prompt != f"Установи Sensai {PUBLIC_README_URL}":
        raise ProductionE2EError("public_readme_prompt_not_exact")
    return contract


def resolve_installed_wsl_claude() -> str:
    """Accept only the normal WSL Claude launcher and its private version binary."""

    found = shutil.which("claude")
    launcher = Path.home() / ".local" / "bin" / "claude"
    versions = Path.home() / ".local" / "share" / "claude" / "versions"
    if found is None or Path(found).absolute() != launcher.absolute() or not launcher.is_symlink():
        raise ProductionE2EError("approved_wsl_claude_unavailable")
    try:
        executable = launcher.resolve(strict=True)
        mode = executable.stat().st_mode
    except OSError as error:
        raise ProductionE2EError("approved_wsl_claude_unavailable") from error
    if (
        not executable.is_relative_to(versions)
        or executable.parent != versions
        or not executable.is_file()
        or mode & 0o022
        or not mode & 0o100
    ):
        raise ProductionE2EError("approved_wsl_claude_unavailable")
    return str(executable)


class ToolKind(StrEnum):
    """Only tool facts the runner may retain after a stream is consumed."""

    LOGIN = "sensai_login"
    MARKETPLACE_ADD = "public_marketplace_add"
    PLUGIN_INSTALL = "public_plugin_install"
    NEW_CHAT_URI = "new_chat_uri"
    FORBIDDEN_BROWSER_MODE = "forbidden_browser_mode"
    TELL_SENSAI = "tell_sensai"
    FORGET_ME = "forget_me"
    OTHER = "other"


class SensaiReplyKind(StrEnum):
    """Only safe classifications of an observed tell_sensai result."""

    INITIAL_DISCOVERY = "initial_discovery"
    TELEGRAM_COMPOSED = "telegram_composed"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class TextEvidence:
    """A completed Claude text block without its contents or digest."""

    matches_expected: bool
    cyrillic_letters: int
    latin_letters: int


@dataclass(frozen=True, slots=True)
class ToolResultEvidence:
    kind: ToolKind
    succeeded: bool
    sensai_reply: SensaiReplyKind | None
    reply_sha256: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AgentEvidence:
    """Bounded safe observations from one ``claude -p`` stream."""

    result_seen: bool
    session_verified: bool
    malformed: bool
    timed_out: bool
    returncode: int
    text_messages: tuple[TextEvidence, ...]
    tool_calls: tuple[ToolKind, ...]
    successful_tool_results: tuple[ToolKind, ...]
    tool_results: tuple[ToolResultEvidence, ...]
    event_order: tuple[str, ...]

    def has_successful(self, kind: ToolKind, *, exactly: int = 1) -> bool:
        return (
            self.tool_calls.count(kind) == exactly
            and self.successful_tool_results.count(kind) == exactly
        )

    def successful_reply_digest(self, kind: ToolKind) -> str | None:
        matches = [
            item.reply_sha256 for item in self.tool_results if item.kind is kind and item.succeeded
        ]
        return matches[0] if len(matches) == 1 and isinstance(matches[0], str) else None


class OperatorProofVerifier(Protocol):
    def verifies_digest(self, response_sha256: str) -> bool: ...


class SshOperatorProofVerifier:
    """One fixed SSH proof call; neither response nor target is ever logged."""

    def verifies_digest(self, response_sha256: str) -> bool:
        if re.fullmatch(r"[0-9a-f]{64}", response_sha256) is None:
            return False
        try:
            config = _strict_private_json(_OPERATOR_CONFIG)
            _strict_private_file(_OPERATOR_KNOWN_HOSTS)
            _strict_private_file(_OPERATOR_IDENTITY)
            _strict_ssh_binary()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False
        if (
            not isinstance(config, dict)
            or set(config) != {"schema", "host", "user", "port", "identity_file"}
            or config.get("schema") != _OPERATOR_CONFIG_SCHEMA
            or not isinstance(config.get("host"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", config["host"])
            or not isinstance(config.get("user"), str)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", config["user"])
            or not isinstance(config.get("port"), int)
            or not 1 <= config["port"] <= 65535
            or config.get("identity_file") != _OPERATOR_IDENTITY.name
        ):
            return False
        payload = (
            json.dumps(
                {"schema": _OPERATOR_PROOF_SCHEMA, "response_sha256": response_sha256},
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        try:
            process = subprocess.Popen(
                [
                    str(_SSH_EXECUTABLE),
                    "-F",
                    "/dev/null",
                    "-i",
                    str(_OPERATOR_IDENTITY),
                    "-p",
                    str(config["port"]),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={_OPERATOR_KNOWN_HOSTS}",
                    "-o",
                    "GlobalKnownHostsFile=/dev/null",
                    "-o",
                    "ProxyCommand=none",
                    "-o",
                    "ProxyJump=none",
                    "-o",
                    "RemoteCommand=none",
                    "-o",
                    "ControlMaster=no",
                    "-o",
                    "ForwardAgent=no",
                    f"{config['user']}@{config['host']}",
                    _REMOTE_PROOF_COMMAND,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False

        terminated = False

        def terminate_once() -> None:
            nonlocal terminated
            if not terminated:
                _terminate(process)
                terminated = True

        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(payload)
            process.stdin.close()
            os.set_blocking(process.stdout.fileno(), False)
            output = bytearray()
            deadline = time.monotonic() + 60
            selector = selectors.DefaultSelector()
            try:
                selector.register(process.stdout, selectors.EVENT_READ)
                while process.poll() is None or selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        terminate_once()
                        return False
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, _MAX_OPERATOR_PROOF_OUTPUT + 1)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > _MAX_OPERATOR_PROOF_OUTPUT:
                            terminate_once()
                            return False
            finally:
                selector.close()
            return (
                process.wait() == 0
                and bytes(output) == b'{"schema":"sensai-local-e2e-proof-v1","result":"verified"}\n'
            )
        except (OSError, BrokenPipeError):
            return False
        finally:
            with suppress(OSError, ValueError):
                process.stdin.close()
            with suppress(OSError, ValueError):
                process.stdout.close()
            if process.poll() is None:
                terminate_once()


def _strict_private_file(path: Path) -> bytes:
    root = _OPERATOR_CONFIG_ROOT
    directory = os.lstat(root)
    if (
        stat.S_ISLNK(directory.st_mode)
        or not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise ValueError("unsafe proof configuration directory")
    if path.parent != root:
        raise ValueError("proof configuration outside its directory")
    before = os.lstat(path)
    if not _private_regular_file(before):
        raise ValueError("unsafe proof configuration file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        data = handle.read(4097)
    after = os.lstat(path)
    if (
        len(data) > 4096
        or not _private_regular_file(opened)
        or not _private_regular_file(after)
        or _file_identity(before) != _file_identity(opened)
        or _file_identity(before) != _file_identity(after)
    ):
        raise ValueError("proof configuration changed while reading")
    return data


def _private_regular_file(item: os.stat_result) -> bool:
    return (
        not stat.S_ISLNK(item.st_mode)
        and stat.S_ISREG(item.st_mode)
        and item.st_uid == os.getuid()
        and stat.S_IMODE(item.st_mode) == 0o600
    )


def _file_identity(item: os.stat_result) -> tuple[int, int, int, int]:
    return item.st_dev, item.st_ino, item.st_mtime_ns, item.st_size


def _strict_private_json(path: Path) -> object:
    return json.loads(
        _strict_private_file(path).decode("utf-8"), object_pairs_hook=_reject_duplicate_object
    )


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_ssh_binary() -> None:
    item = os.lstat(_SSH_EXECUTABLE)
    if (
        stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != 0
        or stat.S_IMODE(item.st_mode) & 0o022
    ):
        raise ValueError("unsafe ssh executable")


@dataclass(frozen=True, slots=True)
class ProductionE2EReport:
    """A non-secret final report; it intentionally contains no raw text or IDs."""

    installation_messages_exact: bool
    normal_login_started: bool
    normal_login_completed: bool
    sensai_connection_verified: bool
    telegram_started: bool
    telegram_continued: bool
    forget_me_completed: bool


class ClaudeDriver(Protocol):
    """The small boundary that permits deterministic unit tests without a CLI."""

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
    ) -> AgentEvidence: ...

    def mcp_configuration_observed(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool: ...

    def public_sensai_plugin_installed(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool: ...

    def claude_authenticated(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool: ...


def _assert_normal_browser_path(command: Sequence[str]) -> None:
    """Refuse a command that would suppress the ordinary OAuth browser route."""

    prohibited = ("--no-browser", "--headless")
    if any(
        argument == option or argument.startswith(f"{option}=")
        for argument in command
        for option in prohibited
    ):
        raise ProductionE2EError("normal_login_path_required")


class SubprocessClaudeDriver:
    """Real local Claude process adapter that keeps only redacted evidence."""

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
        _assert_normal_browser_path(command)
        if timeout_seconds <= 0:
            raise ProductionE2EError("invalid_timeout")
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise ProductionE2EError("claude_process_unavailable") from error
        return _consume_stream(
            process,
            timeout_seconds=timeout_seconds,
            expected_visible_messages=expected_visible_messages,
            expected_session=expected_session,
            expected_new_chat_uri=expected_new_chat_uri,
        )

    def mcp_configuration_observed(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        _assert_normal_browser_path(command)
        if timeout_seconds <= 0:
            raise ProductionE2EError("invalid_timeout")
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise ProductionE2EError("claude_process_unavailable") from error
        try:
            stdout, _ = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate(process)
            return False
        if process.returncode != 0:
            return False
        # The raw status remains only in this local variable.  Do not expose it
        # in an exception, report, file, or test log.
        try:
            status = stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        return (
            "plugin:sensai:sensai" in status
            and "Type:" in status
            and "URL:" in status
            and _STATUS_FAILURE.search(status) is None
        )

    def claude_authenticated(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        _assert_normal_browser_path(command)
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if completed.returncode != 0:
            return False
        try:
            status = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(status, dict) and status.get("loggedIn") is True

    def public_sensai_plugin_installed(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> bool:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
            entries = json.loads(completed.stdout.decode("utf-8"))
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return _is_exact_public_sensai_inventory(entries)


def _is_exact_public_sensai_inventory(entries: object) -> bool:
    """Accept one enabled public Sensai plugin and reject every stale Sensai duplicate."""

    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        return False
    sensai = [item for item in entries if str(item.get("id", "")).startswith("sensai@")]
    return (
        len(sensai) == 1
        and sensai[0].get("id") == "sensai@sensai"
        and sensai[0].get("scope") == "user"
        and sensai[0].get("enabled") is True
    )


@dataclass(slots=True)
class _TextAccumulator:
    """Hash one text block while retaining no copy of the block."""

    digest: object
    cyrillic_letters: int = 0
    latin_letters: int = 0

    @classmethod
    def new(cls) -> _TextAccumulator:
        return cls(hashlib.sha256())

    def add(self, text: str) -> None:
        self.digest.update(text.encode("utf-8"))  # type: ignore[union-attr]
        self.cyrillic_letters += len(_CYRILLIC.findall(text))
        self.latin_letters += len(_LATIN.findall(text))

    def matches(self, expected: str) -> bool:
        expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
        return self.digest.digest() == expected_digest  # type: ignore[union-attr]


@dataclass(slots=True)
class _ToolAccumulator:
    """One tool-use block reduced to a kind once its JSON input is complete."""

    direct_kind: ToolKind | None
    result_key: bytes | None
    input_chunks: list[str]


def _direct_tool_kind(block: object) -> ToolKind | None:
    if not isinstance(block, dict):
        return None
    name = block.get("name")
    if not isinstance(name, str) or not name.startswith(("mcp__sensai__", "mcp__plugin_sensai__")):
        return None
    if name in {"mcp__sensai__forget_me", "mcp__plugin_sensai__forget_me"}:
        return ToolKind.FORGET_ME
    if name in {"mcp__sensai__tell_sensai", "mcp__plugin_sensai__tell_sensai"}:
        return ToolKind.TELL_SENSAI
    return None


def _safe_tool_result_key(value: object) -> bytes | None:
    """Bind a result to a tool call without retaining the tool-use identifier."""

    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        return None
    return hashlib.sha256(value.encode("utf-8")).digest()


def _classify_bash_command(command: str, expected_uri: str | None) -> ToolKind:
    """Accept only actual Bash command semantics, never prose containing a command."""

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ToolKind.OTHER
    if "--no-browser" in tokens:
        return ToolKind.FORBIDDEN_BROWSER_MODE
    if len(tokens) == 2 and tokens[0] in {"xdg-open", "open"} and tokens[1] == expected_uri:
        return ToolKind.NEW_CHAT_URI
    if tokens and tokens[0] == "script" and "-c" in tokens:
        position = tokens.index("-c") + 1
        if position >= len(tokens):
            return ToolKind.OTHER
        try:
            tokens = shlex.split(tokens[position], posix=True)
        except ValueError:
            return ToolKind.OTHER
    if tokens == ["claude", "mcp", "login", "plugin:sensai:sensai"]:
        return ToolKind.LOGIN
    if tokens == ["claude", "plugin", "marketplace", "add", "blackvctr/sensai-plugin"]:
        return ToolKind.MARKETPLACE_ADD
    if tokens == ["claude", "plugin", "install", "sensai@sensai", "--scope", "user"]:
        return ToolKind.PLUGIN_INSTALL
    return ToolKind.OTHER


def _new_tool_accumulator(block: object) -> _ToolAccumulator:
    identifier = block.get("id") if isinstance(block, dict) else None
    input_value = block.get("input") if isinstance(block, dict) else None
    initial = [json.dumps(input_value)] if isinstance(input_value, dict) and input_value else []
    return _ToolAccumulator(_direct_tool_kind(block), _safe_tool_result_key(identifier), initial)


def _telegram_body() -> str | None:
    """Exact production article proof needs response-time server provenance.

    Drive content and the active snapshot can move independently, so neither a
    local article nor a later snapshot can prove what was delivered.  Keep the
    value unavailable until the server returns pinned response provenance.
    """

    return None


def _sensai_reply_kind(content: object) -> SensaiReplyKind:
    text = content if isinstance(content, str) else None
    if text == _INITIAL_DISCOVERY_REPLY:
        return SensaiReplyKind.INITIAL_DISCOVERY
    body = _telegram_body()
    delimiter = "\n\nInstruction:\n"
    if body is not None and text is not None and text.count(delimiter) == 1:
        prefix, _, article = text.partition(delimiter)
        if prefix and article == body:
            return SensaiReplyKind.TELEGRAM_COMPOSED
    return SensaiReplyKind.OTHER


def _tool_results(record: object, outstanding: dict[bytes, ToolKind]) -> list[ToolResultEvidence]:
    """Reduce tool results to fixed outcome categories, retaining no text or IDs."""

    if not isinstance(record, dict) or record.get("type") != "user":
        return []
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    results: list[ToolResultEvidence] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        key = _safe_tool_result_key(block.get("tool_use_id"))
        kind = outstanding.pop(key, ToolKind.OTHER) if key is not None else ToolKind.OTHER
        succeeded = block.get("is_error") is not True
        reply_text = (
            block.get("content")
            if kind is ToolKind.TELL_SENSAI and isinstance(block.get("content"), str)
            else None
        )
        reply = _sensai_reply_kind(reply_text) if kind is ToolKind.TELL_SENSAI else None
        digest = hashlib.sha256(reply_text.encode()).hexdigest() if reply_text is not None else None
        results.append(ToolResultEvidence(kind, succeeded, reply, digest))
    return results


def _stream_event(record: object) -> object | None:
    if not isinstance(record, dict) or record.get("type") != "stream_event":
        return None
    return record.get("event")


def _verify_session(record: object, expected_session: uuid.UUID) -> bool:
    return (
        isinstance(record, dict)
        and record.get("type") == "system"
        and record.get("subtype") == "init"
        and record.get("session_id") == str(expected_session)
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _consume_stream(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: int,
    expected_visible_messages: Sequence[str],
    expected_session: uuid.UUID,
    expected_new_chat_uri: str | None = None,
) -> AgentEvidence:
    """Consume one JSON stream without retaining a raw line or error message."""

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector.register(descriptor, selectors.EVENT_READ)
    pending = bytearray()
    text_blocks: dict[int, _TextAccumulator] = {}
    tool_blocks: dict[int, _ToolAccumulator] = {}
    text_messages: list[TextEvidence] = []
    calls: list[ToolKind] = []
    event_order: list[str] = []
    results: list[ToolResultEvidence] = []
    outstanding: dict[bytes, ToolKind] = {}
    result_seen = session_verified = malformed = timed_out = False
    event_count = 0
    stream_bytes = 0
    deadline = time.monotonic() + timeout_seconds

    def consume(line: bytes) -> None:
        nonlocal event_count, malformed, result_seen, session_verified, stream_bytes
        stream_bytes += len(line)
        if stream_bytes > MAX_STREAM_BYTES:
            malformed = True
            return
        if len(line) > MAX_STREAM_LINE_BYTES:
            malformed = True
            return
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed = True
            return
        event_count += 1
        if event_count > MAX_STREAM_EVENTS:
            malformed = True
            return
        session_verified = session_verified or _verify_session(record, expected_session)
        results.extend(_tool_results(record, outstanding))
        if isinstance(record, dict) and record.get("type") == "result":
            result_seen = True
        event = _stream_event(record)
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "content_block_start":
            index = event.get("index")
            block = event.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                malformed = True
                return
            block_type = block.get("type")
            if block_type == "text":
                if index in text_blocks:
                    malformed = True
                    return
                text_blocks[index] = _TextAccumulator.new()
            elif block_type == "tool_use":
                if index in tool_blocks:
                    malformed = True
                    return
                tool_blocks[index] = _new_tool_accumulator(block)
            return
        if event_type == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            text = delta.get("text") if isinstance(delta, dict) else None
            accumulator = text_blocks.get(index) if isinstance(index, int) else None
            if accumulator is not None and isinstance(text, str):
                accumulator.add(text)
            partial_json = delta.get("partial_json") if isinstance(delta, dict) else None
            tool_accumulator = tool_blocks.get(index) if isinstance(index, int) else None
            if tool_accumulator is not None and isinstance(partial_json, str):
                if (
                    sum(len(part) for part in tool_accumulator.input_chunks) + len(partial_json)
                    > MAX_TOOL_INPUT_BYTES
                ):
                    malformed = True
                    return
                tool_accumulator.input_chunks.append(partial_json)
            return
        if event_type == "content_block_stop":
            index = event.get("index")
            accumulator = text_blocks.pop(index, None) if isinstance(index, int) else None
            if accumulator is not None:
                position = len(text_messages)
                expected = (
                    expected_visible_messages[position]
                    if position < len(expected_visible_messages)
                    else None
                )
                text_messages.append(
                    TextEvidence(
                        matches_expected=expected is not None and accumulator.matches(expected),
                        cyrillic_letters=accumulator.cyrillic_letters,
                        latin_letters=accumulator.latin_letters,
                    )
                )
                event_order.append("visible")
            tool_accumulator = tool_blocks.pop(index, None) if isinstance(index, int) else None
            if tool_accumulator is not None:
                kind = tool_accumulator.direct_kind
                if kind is None:
                    try:
                        input_value = json.loads("".join(tool_accumulator.input_chunks))
                    except json.JSONDecodeError:
                        kind = ToolKind.OTHER
                    else:
                        command = (
                            input_value.get("command") if isinstance(input_value, dict) else None
                        )
                        kind = (
                            _classify_bash_command(command, expected_new_chat_uri)
                            if isinstance(command, str)
                            else ToolKind.OTHER
                        )
                calls.append(kind)
                event_order.append(kind.value)
                if tool_accumulator.result_key is not None:
                    outstanding[tool_accumulator.result_key] = kind

    try:
        while process.poll() is None or pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready = selector.select(0 if process.poll() is not None else remaining)
            if not ready:
                continue
            try:
                chunk = os.read(descriptor, MAX_STREAM_LINE_BYTES + 1)
            except BlockingIOError:
                continue
            if not chunk:
                if pending:
                    malformed = True
                break
            pending.extend(chunk)
            if len(pending) > MAX_STREAM_LINE_BYTES and b"\n" not in pending:
                malformed = True
                break
            while b"\n" in pending:
                line, _, remaining_bytes = pending.partition(b"\n")
                pending = bytearray(remaining_bytes)
                consume(bytes(line))
                if malformed:
                    break
            if malformed:
                break
    finally:
        selector.close()
        if process.poll() is None:
            _terminate(process)
    if text_blocks or tool_blocks:
        malformed = True
    return AgentEvidence(
        result_seen=result_seen,
        session_verified=session_verified,
        malformed=malformed,
        timed_out=timed_out,
        returncode=process.wait(),
        text_messages=tuple(text_messages),
        tool_calls=tuple(calls),
        successful_tool_results=tuple(result.kind for result in results if result.succeeded),
        tool_results=tuple(results),
        event_order=tuple(event_order),
    )


def _agent_command(
    executable: str,
    *,
    prompt: str,
    session: uuid.UUID,
    resume: bool,
) -> tuple[str, ...]:
    command = [
        executable,
        "-p",
        "--model",
        CLAUDE_SONNET_5_MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    command.extend(("--resume", str(session)) if resume else ("--session-id", str(session)))
    command.append(prompt)
    _assert_normal_browser_path(command)
    return tuple(command)


def _status_command(executable: str) -> tuple[str, ...]:
    command = (executable, "mcp", "get", "plugin:sensai:sensai")
    _assert_normal_browser_path(command)
    return command


def _auth_status_command(executable: str) -> tuple[str, ...]:
    return (executable, "auth", "status")


def _plugin_list_command(executable: str) -> tuple[str, ...]:
    return (executable, "plugin", "list", "--json")


class ProductionSensaiE2E:
    """One explicit production check using a local disposable Claude run."""

    def __init__(
        self,
        *,
        profile: Path,
        driver: ClaudeDriver | None = None,
        contract_loader: Callable[[], PublicReadmeContract] = fetch_public_readme_contract,
        executable_resolver: Callable[[], str] = resolve_installed_wsl_claude,
        operator_proof: OperatorProofVerifier | None = None,
    ) -> None:
        self._profile = profile
        self._driver = driver or SubprocessClaudeDriver()
        self._contract_loader = contract_loader
        self._executable_resolver = executable_resolver
        self._operator_proof = operator_proof or SshOperatorProofVerifier()

    def run(self) -> ProductionE2EReport:
        """Install, authenticate, consult Telegram, forget, then remove local state."""

        contract = self._contract_loader()
        executable = self._executable_resolver()
        installation_session = uuid.uuid4()
        telegram_session = uuid.uuid4()
        with create_fresh_run(self._profile) as run:
            return self._run_inside_fresh_profile(
                run,
                contract=contract,
                executable=executable,
                new_chat_uri="claude://code/new?"
                + urlencode({"q": contract.russian_new_chat_request}),
                installation_session=installation_session,
                telegram_session=telegram_session,
            )

    def _run_inside_fresh_profile(
        self,
        run: ClaudeE2ERun,
        *,
        contract: PublicReadmeContract,
        executable: str,
        new_chat_uri: str,
        installation_session: uuid.UUID,
        telegram_session: uuid.UUID,
    ) -> ProductionE2EReport:
        # An initial tool turn can reach OAuth before its stream has yielded a
        # classifiable Bash block.  Arm cleanup first and use that initial
        # session until a later Telegram session exists.
        cleanup_needed = True
        cleanup_session = installation_session
        primary_error: ProductionE2EError | None = None
        try:
            if not self._driver.claude_authenticated(
                _auth_status_command(executable),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
            ):
                raise ProductionE2EError("isolated_claude_auth_not_verified")
            installation = self._driver.run_agent(
                _agent_command(
                    executable,
                    prompt=contract.russian_install_prompt,
                    session=installation_session,
                    resume=False,
                ),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=INSTALL_TIMEOUT_SECONDS,
                expected_visible_messages=(
                    contract.russian_authorization_message,
                    contract.russian_ready_message,
                ),
                expected_session=installation_session,
                expected_new_chat_uri=new_chat_uri,
            )
            self._require_installation(installation)
            normal_login_started = installation.tool_calls.count(ToolKind.LOGIN) == 1
            normal_login_completed = installation.successful_tool_results.count(ToolKind.LOGIN) == 1
            if not normal_login_started:
                raise ProductionE2EError("normal_login_not_started")
            if not normal_login_completed:
                raise ProductionE2EError("normal_login_not_completed")
            if not self._driver.mcp_configuration_observed(
                _status_command(executable),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
            ):
                raise ProductionE2EError("sensai_endpoint_configuration_not_verified")
            if not self._driver.public_sensai_plugin_installed(
                _plugin_list_command(executable),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
            ):
                raise ProductionE2EError("public_sensai_plugin_not_verified")
            telegram_start = self._driver.run_agent(
                _agent_command(
                    executable,
                    prompt=contract.russian_new_chat_request,
                    session=telegram_session,
                    resume=False,
                ),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=TELEGRAM_START_TIMEOUT_SECONDS,
                expected_visible_messages=(),
                expected_session=telegram_session,
                expected_new_chat_uri=None,
            )
            cleanup_session = telegram_session
            self._require_tool_turn(
                telegram_start,
                ToolKind.TELL_SENSAI,
                "telegram_start",
                SensaiReplyKind.INITIAL_DISCOVERY,
            )
            telegram_continuation = self._driver.run_agent(
                _agent_command(
                    executable,
                    prompt=_TELEGRAM_FACTS,
                    session=telegram_session,
                    resume=True,
                ),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=TELEGRAM_CONTINUATION_TIMEOUT_SECONDS,
                expected_visible_messages=(),
                expected_session=telegram_session,
                expected_new_chat_uri=None,
            )
            self._require_tool_turn(
                telegram_continuation,
                ToolKind.TELL_SENSAI,
                "telegram_continuation",
                None,
            )
            response_sha256 = telegram_continuation.successful_reply_digest(ToolKind.TELL_SENSAI)
            if response_sha256 is None or not self._operator_proof.verifies_digest(response_sha256):
                raise ProductionE2EError("telegram_operator_proof_not_verified")
        except ProductionE2EError as error:
            primary_error = error
        finally:
            cleanup_error: ProductionE2EError | None = None
            cleanup_completed = False
            if cleanup_needed:
                try:
                    cleanup = self._driver.run_agent(
                        _agent_command(
                            executable,
                            prompt=_FORGET_ME_REQUEST,
                            session=cleanup_session,
                            resume=True,
                        ),
                        cwd=run.work,
                        environment=run.environment,
                        timeout_seconds=FORGET_ME_TIMEOUT_SECONDS,
                        expected_visible_messages=(),
                        expected_session=cleanup_session,
                        expected_new_chat_uri=None,
                    )
                    self._require_tool_turn(cleanup, ToolKind.FORGET_ME, "forget_me", None)
                    cleanup_completed = True
                except ProductionE2EError as error:
                    cleanup_error = error
            if primary_error is not None:
                if cleanup_error is not None:
                    primary_error.add_note(f"cleanup also failed: {cleanup_error}")
                raise primary_error
            if cleanup_error is not None:
                raise cleanup_error
        return ProductionE2EReport(
            installation_messages_exact=True,
            normal_login_started=True,
            normal_login_completed=True,
            sensai_connection_verified=True,
            telegram_started=True,
            telegram_continued=True,
            forget_me_completed=cleanup_completed,
        )

    @staticmethod
    def _require_installation(evidence: AgentEvidence) -> None:
        if evidence.timed_out:
            raise ProductionE2EError("installation_timed_out")
        if evidence.malformed or evidence.returncode != 0 or not evidence.result_seen:
            raise ProductionE2EError("installation_stream_invalid")
        if not evidence.session_verified:
            raise ProductionE2EError("installation_session_not_verified")
        if len(evidence.text_messages) != 2 or not all(
            message.matches_expected for message in evidence.text_messages
        ):
            raise ProductionE2EError("installation_messages_not_exact")
        if any(
            message.cyrillic_letters <= message.latin_letters for message in evidence.text_messages
        ):
            raise ProductionE2EError("installation_visible_message_not_russian")
        for required in (ToolKind.MARKETPLACE_ADD, ToolKind.PLUGIN_INSTALL):
            if not evidence.has_successful(required):
                raise ProductionE2EError(f"installation_{required}_not_observed")
        if evidence.tool_calls.count(ToolKind.NEW_CHAT_URI) != 1:
            raise ProductionE2EError("installation_new_chat_uri_not_observed")
        if ToolKind.FORBIDDEN_BROWSER_MODE in evidence.tool_calls:
            raise ProductionE2EError("installation_no_browser_forbidden")
        expected = (
            "visible",
            ToolKind.MARKETPLACE_ADD.value,
            ToolKind.PLUGIN_INSTALL.value,
            ToolKind.LOGIN.value,
            ToolKind.NEW_CHAT_URI.value,
            "visible",
        )
        if evidence.event_order != expected:
            raise ProductionE2EError("installation_event_order_invalid")

    @staticmethod
    def _require_tool_turn(
        evidence: AgentEvidence,
        kind: ToolKind,
        phase: str,
        expected_sensai_reply: SensaiReplyKind | None,
    ) -> None:
        if evidence.timed_out:
            raise ProductionE2EError(f"{phase}_timed_out")
        if evidence.malformed or evidence.returncode != 0 or not evidence.result_seen:
            raise ProductionE2EError(f"{phase}_stream_invalid")
        if not evidence.session_verified:
            raise ProductionE2EError(f"{phase}_session_not_verified")
        if not evidence.has_successful(kind):
            if phase == "forget_me" and kind not in evidence.tool_calls:
                raise ProductionE2EError("cleanup_not_authenticated")
            raise ProductionE2EError(f"{phase}_tool_result_invalid")
        if expected_sensai_reply is not None and not any(
            result.kind is ToolKind.TELL_SENSAI
            and result.succeeded
            and result.sensai_reply is expected_sensai_reply
            for result in evidence.tool_results
        ):
            raise ProductionE2EError(f"{phase}_reply_body_unavailable")
