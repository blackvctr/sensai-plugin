"""Run the public Sensai installation acceptance from a local Claude profile.

The check ends after installation, normal Sensai login, and the published
second-chat URI attempt. It never begins a consultation or inspects server
internals.
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
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
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
PROCESS_TERMINATION_GRACE_SECONDS = 3
MAX_STREAM_LINE_BYTES = 256 * 1024
MAX_STREAM_EVENTS = 128
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_TOOL_INPUT_BYTES = 32 * 1024
MAX_PUBLIC_README_BYTES = 2 * 1024 * 1024
PUBLIC_README_URL = "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md"

_STATUS_FAILURE = re.compile(r"needs authentication|disconnected|\berror\b", re.IGNORECASE)
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-z]")


class ProductionE2EError(RuntimeError):
    """One safe category explaining an installation acceptance failure."""


def fetch_public_readme_contract() -> PublicReadmeContract:
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
    LOGIN = "sensai_login"
    MARKETPLACE_ADD = "public_marketplace_add"
    PLUGIN_INSTALL = "public_plugin_install"
    NEW_CHAT_URI = "new_chat_uri"
    FORBIDDEN_BROWSER_MODE = "forbidden_browser_mode"
    OTHER = "other"


class ExitCategory(StrEnum):
    CLEAN = "clean"
    TOOL_RESULT_ERROR = "tool_result_error"
    TERMINAL_ERROR = "terminal_error"
    NONZERO_UNCLASSIFIED = "nonzero_unclassified"


class TerminalResultKind(StrEnum):
    NONE = "none"
    SUCCESS = "success"
    EXECUTION = "execution"
    BUDGET = "budget"
    STRUCTURED_OUTPUT_RETRIES = "structured_output_retries"
    TURN_LIMIT = "turn_limit"
    PERMISSION = "permission"
    API_ERROR = "api_error"
    OTHER = "other"


_TERMINAL_RESULT_SUBTYPES = {
    "success": TerminalResultKind.SUCCESS,
    "error_during_execution": TerminalResultKind.EXECUTION,
    "error_max_budget_usd": TerminalResultKind.BUDGET,
    "error_max_structured_output_retries": TerminalResultKind.STRUCTURED_OUTPUT_RETRIES,
    "error_max_turns": TerminalResultKind.TURN_LIMIT,
    "error_permission": TerminalResultKind.PERMISSION,
}
_TERMINAL_RESULT_REASONS = {"api_error": TerminalResultKind.API_ERROR}
_MAX_TERMINAL_ERROR_COUNT = 32


class ExitStage(StrEnum):
    BEFORE_MARKETPLACE = "before_marketplace"
    AFTER_MARKETPLACE_BEFORE_PLUGIN = "after_marketplace_before_plugin"
    AFTER_PLUGIN_BEFORE_LOGIN = "after_plugin_before_login"
    AFTER_LOGIN_BEFORE_NEW_CHAT = "after_login_before_new_chat"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TextEvidence:
    matches_expected: bool
    cyrillic_letters: int
    latin_letters: int


@dataclass(frozen=True, slots=True)
class ToolResultEvidence:
    kind: ToolKind
    succeeded: bool


@dataclass(frozen=True, slots=True)
class AgentEvidence:
    result_seen: bool
    session_verified: bool
    malformed: bool
    unclosed_block: bool
    stream_limit_exceeded: bool
    timed_out: bool
    returncode: int
    text_messages: tuple[TextEvidence, ...]
    tool_calls: tuple[ToolKind, ...]
    successful_tool_results: tuple[ToolKind, ...]
    tool_results: tuple[ToolResultEvidence, ...]
    event_order: tuple[str, ...]
    record_kinds: tuple[str, ...]
    exit_category: ExitCategory
    exit_stage: ExitStage
    terminal_result_kind: TerminalResultKind
    terminal_error_count: int
    stderr_seen: bool

    def has_successful(self, kind: ToolKind, *, exactly: int = 1) -> bool:
        return (
            self.tool_calls.count(kind) == exactly
            and self.successful_tool_results.count(kind) == exactly
        )


@dataclass(frozen=True, slots=True)
class ProductionE2EReport:
    installation_messages_exact: bool
    normal_login_started: bool
    normal_login_completed: bool
    sensai_connection_verified: bool
    public_sensai_plugin_installed: bool
    new_chat_uri_attempted: bool

    @property
    def complete(self) -> bool:
        return all(
            (
                self.installation_messages_exact,
                self.normal_login_started,
                self.normal_login_completed,
                self.sensai_connection_verified,
                self.public_sensai_plugin_installed,
                self.new_chat_uri_attempted,
            )
        )


class ClaudeDriver(Protocol):
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

    def claude_authenticated(
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


class _Digest(Protocol):
    def update(self, data: bytes) -> None: ...

    def digest(self) -> bytes: ...


@dataclass(slots=True)
class _TextAccumulator:
    digest: _Digest
    cyrillic_letters: int = 0
    latin_letters: int = 0

    @classmethod
    def new(cls) -> _TextAccumulator:
        return cls(hashlib.sha256())

    def add(self, text: str) -> None:
        self.digest.update(text.encode("utf-8"))
        self.cyrillic_letters += len(_CYRILLIC.findall(text))
        self.latin_letters += len(_LATIN.findall(text))

    def matches(self, expected: str) -> bool:
        expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
        return self.digest.digest() == expected_digest


@dataclass(slots=True)
class _ToolAccumulator:
    result_key: bytes | None
    input_chunks: list[str]


def _safe_tool_result_key(value: object) -> bytes | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        return None
    return hashlib.sha256(value.encode("utf-8")).digest()


def _terminal_result_summary(record: dict[str, object]) -> tuple[TerminalResultKind, int]:
    """Keep only a fixed terminal subtype and count, never its free-text details."""

    subtype = record.get("subtype")
    known = _TERMINAL_RESULT_SUBTYPES.get(subtype) if isinstance(subtype, str) else None
    reason = record.get("terminal_reason")
    known_reason = _TERMINAL_RESULT_REASONS.get(reason) if isinstance(reason, str) else None
    errors = record.get("errors")
    error_count = min(len(errors), _MAX_TERMINAL_ERROR_COUNT) if isinstance(errors, list) else 0
    if known_reason is not None:
        return known_reason, error_count
    if isinstance(reason, str):
        return TerminalResultKind.OTHER, error_count
    if record.get("is_error") is True:
        if known is not None and known is not TerminalResultKind.SUCCESS:
            return known, error_count
        return TerminalResultKind.OTHER, error_count
    if known is not None:
        return known, error_count
    if isinstance(subtype, str):
        return TerminalResultKind.OTHER, error_count
    return TerminalResultKind.NONE, error_count


def _assert_normal_browser_path(command: Sequence[str]) -> None:
    if "--no-browser" in command:
        raise ProductionE2EError("normal_login_path_required")


def _classify_bash_command(command: str, expected_new_chat_uri: str | None) -> ToolKind:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ToolKind.OTHER
    if "--no-browser" in tokens:
        return ToolKind.FORBIDDEN_BROWSER_MODE
    if (
        expected_new_chat_uri is not None
        and len(tokens) == 2
        and tokens[0] in {"xdg-open", "open"}
        and tokens[1] == expected_new_chat_uri
    ):
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


def _exit_stage(successful: Sequence[ToolKind]) -> ExitStage:
    if ToolKind.MARKETPLACE_ADD not in successful:
        return ExitStage.BEFORE_MARKETPLACE
    if ToolKind.PLUGIN_INSTALL not in successful:
        return ExitStage.AFTER_MARKETPLACE_BEFORE_PLUGIN
    if ToolKind.LOGIN not in successful:
        return ExitStage.AFTER_PLUGIN_BEFORE_LOGIN
    if ToolKind.NEW_CHAT_URI not in successful:
        return ExitStage.AFTER_LOGIN_BEFORE_NEW_CHAT
    return ExitStage.UNKNOWN


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
    expected_new_chat_uri: str | None,
) -> AgentEvidence:
    if process.stdout is None:
        raise ProductionE2EError("claude_stream_unavailable")
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ, "stdout")
    stderr = getattr(process, "stderr", None)
    if stderr is not None:
        os.set_blocking(stderr.fileno(), False)
        selector.register(stderr.fileno(), selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout_seconds
    pending = bytearray()
    total_bytes = 0
    event_count = 0
    malformed = False
    unclosed_block = False
    stream_limit_exceeded = False
    timed_out = False
    result_seen = False
    session_verified = False
    terminal_error = False
    terminal_result_kind = TerminalResultKind.NONE
    terminal_error_count = 0
    stderr_seen = False
    text_blocks: dict[int, _TextAccumulator] = {}
    tool_blocks: dict[int, _ToolAccumulator] = {}
    outstanding: dict[bytes, ToolKind] = {}
    texts: list[TextEvidence] = []
    calls: list[ToolKind] = []
    results: list[ToolResultEvidence] = []
    order: list[str] = []
    record_kinds: list[str] = []

    def record_kind(record: object) -> str:
        if not isinstance(record, dict):
            return "other"
        item_type = record.get("type")
        if isinstance(item_type, str) and item_type in {"system", "result", "user"}:
            return item_type
        event = record.get("event") if item_type == "stream_event" else None
        event_type = event.get("type") if isinstance(event, dict) else None
        if event_type in {"content_block_start", "content_block_delta", "content_block_stop"}:
            return f"stream:{event_type}"
        return "stream:other"

    def consume(line: bytes) -> None:
        nonlocal \
            event_count, \
            malformed, \
            result_seen, \
            session_verified, \
            stream_limit_exceeded, \
            terminal_error, \
            terminal_result_kind, \
            terminal_error_count
        if not line:
            return
        event_count += 1
        if event_count > MAX_STREAM_EVENTS:
            stream_limit_exceeded = True
            return
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed = True
            return
        record_kinds.append(record_kind(record))
        if _verify_session(record, expected_session):
            session_verified = True
        if isinstance(record, dict) and record.get("type") == "result":
            result_seen = True
            terminal_result_kind, terminal_error_count = _terminal_result_summary(record)
            terminal_error = terminal_result_kind not in {
                TerminalResultKind.NONE,
                TerminalResultKind.SUCCESS,
            }
        if isinstance(record, dict) and record.get("type") == "user":
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    key = _safe_tool_result_key(block.get("tool_use_id"))
                    kind = (
                        outstanding.pop(key, ToolKind.OTHER) if key is not None else ToolKind.OTHER
                    )
                    results.append(ToolResultEvidence(kind, block.get("is_error") is not True))
            return
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
            if block.get("type") == "text":
                text_blocks[index] = _TextAccumulator.new()
            elif block.get("type") == "tool_use":
                input_value = block.get("input")
                initial = (
                    [json.dumps(input_value)]
                    if isinstance(input_value, dict) and input_value
                    else []
                )
                tool_blocks[index] = _ToolAccumulator(
                    _safe_tool_result_key(block.get("id")), initial
                )
            return
        if event_type == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            if not isinstance(index, int) or not isinstance(delta, dict):
                return
            accumulator = text_blocks.get(index)
            if accumulator is not None and isinstance(delta.get("text"), str):
                accumulator.add(delta["text"])
            tool = tool_blocks.get(index)
            partial = delta.get("partial_json")
            if tool is not None and isinstance(partial, str):
                if (
                    sum(len(item) for item in tool.input_chunks) + len(partial)
                    > MAX_TOOL_INPUT_BYTES
                ):
                    malformed = True
                    return
                tool.input_chunks.append(partial)
            return
        if event_type == "content_block_stop":
            index = event.get("index")
            if not isinstance(index, int):
                malformed = True
                return
            text = text_blocks.pop(index, None)
            if text is not None:
                position = len(texts)
                expected = (
                    expected_visible_messages[position]
                    if position < len(expected_visible_messages)
                    else None
                )
                texts.append(
                    TextEvidence(
                        matches_expected=expected is not None and text.matches(expected),
                        cyrillic_letters=text.cyrillic_letters,
                        latin_letters=text.latin_letters,
                    )
                )
                order.append("visible")
            tool = tool_blocks.pop(index, None)
            if tool is not None:
                try:
                    value = json.loads("".join(tool.input_chunks))
                except json.JSONDecodeError:
                    kind = ToolKind.OTHER
                else:
                    command = value.get("command") if isinstance(value, dict) else None
                    kind = (
                        _classify_bash_command(command, expected_new_chat_uri)
                        if isinstance(command, str)
                        else ToolKind.OTHER
                    )
                calls.append(kind)
                order.append(kind.value)
                if tool.result_key is not None:
                    outstanding[tool.result_key] = kind

    try:
        while process.poll() is None or pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready = selector.select(0 if process.poll() is not None else remaining)
            if not ready:
                continue
            key, _ = ready[0]
            try:
                chunk = os.read(key.fd, MAX_STREAM_LINE_BYTES + 1)
            except BlockingIOError:
                continue
            if key.data == "stderr":
                if chunk:
                    stderr_seen = True
                else:
                    selector.unregister(key.fileobj)
                continue
            if not chunk:
                if pending:
                    malformed = True
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_STREAM_BYTES:
                stream_limit_exceeded = True
                break
            pending.extend(chunk)
            if len(pending) > MAX_STREAM_LINE_BYTES and b"\n" not in pending:
                malformed = True
                break
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
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
        unclosed_block = True
    successful = tuple(item.kind for item in results if item.succeeded)
    if any(not item.succeeded for item in results):
        exit_category = ExitCategory.TOOL_RESULT_ERROR
    elif terminal_error:
        exit_category = ExitCategory.TERMINAL_ERROR
    elif process.returncode not in {None, 0}:
        exit_category = ExitCategory.NONZERO_UNCLASSIFIED
    else:
        exit_category = ExitCategory.CLEAN
    return AgentEvidence(
        result_seen=result_seen,
        session_verified=session_verified,
        malformed=malformed,
        unclosed_block=unclosed_block,
        stream_limit_exceeded=stream_limit_exceeded,
        timed_out=timed_out,
        returncode=process.wait(),
        text_messages=tuple(texts),
        tool_calls=tuple(calls),
        successful_tool_results=successful,
        tool_results=tuple(results),
        event_order=tuple(order),
        record_kinds=tuple(record_kinds),
        exit_category=exit_category,
        exit_stage=_exit_stage(successful),
        terminal_result_kind=terminal_result_kind,
        terminal_error_count=terminal_error_count,
        stderr_seen=stderr_seen,
    )


class SubprocessClaudeDriver:
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
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
            status = completed.stdout.decode("utf-8", errors="strict")
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
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        return False
    sensai = [item for item in entries if str(item.get("id", "")).startswith("sensai@")]
    return (
        len(sensai) == 1
        and sensai[0].get("id") == "sensai@sensai"
        and sensai[0].get("scope") == "user"
        and sensai[0].get("enabled") is True
    )


def _agent_command(executable: str, *, prompt: str, session: uuid.UUID) -> tuple[str, ...]:
    command = (
        executable,
        "-p",
        "--model",
        CLAUDE_SONNET_5_MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--session-id",
        str(session),
        prompt,
    )
    _assert_normal_browser_path(command)
    return command


def _status_command(executable: str) -> tuple[str, ...]:
    return executable, "mcp", "get", "plugin:sensai:sensai"


def _auth_status_command(executable: str) -> tuple[str, ...]:
    return executable, "auth", "status"


def _plugin_list_command(executable: str) -> tuple[str, ...]:
    return executable, "plugin", "list", "--json"


class ProductionSensaiE2E:
    """One explicit local check of the public Sensai installation path."""

    def __init__(
        self,
        *,
        profile: Path,
        driver: ClaudeDriver | None = None,
        contract_loader: Callable[[], PublicReadmeContract] = fetch_public_readme_contract,
        executable_resolver: Callable[[], str] = resolve_installed_wsl_claude,
    ) -> None:
        self._profile = profile
        self._driver = driver or SubprocessClaudeDriver()
        self._contract_loader = contract_loader
        self._executable_resolver = executable_resolver

    def run(self) -> ProductionE2EReport:
        contract = self._contract_loader()
        executable = self._executable_resolver()
        with create_fresh_run(self._profile) as run:
            return self._run_inside_fresh_profile(run, contract, executable, uuid.uuid4())

    def _run_inside_fresh_profile(
        self, run: ClaudeE2ERun, contract: PublicReadmeContract, executable: str, session: uuid.UUID
    ) -> ProductionE2EReport:
        if not self._driver.claude_authenticated(
            _auth_status_command(executable),
            cwd=run.work,
            environment=run.environment,
            timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
        ):
            raise ProductionE2EError("isolated_claude_auth_not_verified")
        new_chat_uri = "claude://code/new?" + urlencode({"q": contract.russian_new_chat_request})
        installation = self._driver.run_agent(
            _agent_command(executable, prompt=contract.russian_install_prompt, session=session),
            cwd=run.work,
            environment=run.environment,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            expected_visible_messages=(
                contract.russian_authorization_message,
                contract.russian_ready_message,
            ),
            expected_session=session,
            expected_new_chat_uri=new_chat_uri,
        )
        self._require_installation(installation)
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
        return ProductionE2EReport(True, True, True, True, True, True)

    @staticmethod
    def _require_installation(evidence: AgentEvidence) -> None:
        if evidence.timed_out:
            raise ProductionE2EError("installation_timed_out")
        if evidence.stream_limit_exceeded:
            raise ProductionE2EError("installation_stream_limit_exceeded")
        if evidence.malformed:
            raise ProductionE2EError("installation_stream_malformed")
        if evidence.unclosed_block:
            raise ProductionE2EError("installation_stream_unclosed_block")
        if evidence.returncode != 0:
            category = (
                f"terminal_{evidence.terminal_result_kind}"
                if evidence.exit_category is ExitCategory.TERMINAL_ERROR
                else evidence.exit_category
            )
            raise ProductionE2EError(
                f"installation_claude_exit_{category}_at_{evidence.exit_stage}"
            )
        if not evidence.result_seen:
            raise ProductionE2EError("installation_terminal_result_missing")
        if not evidence.session_verified:
            raise ProductionE2EError("installation_session_not_verified")
        if len(evidence.text_messages) != 2 or not all(
            item.matches_expected for item in evidence.text_messages
        ):
            raise ProductionE2EError("installation_messages_not_exact")
        if any(item.cyrillic_letters <= item.latin_letters for item in evidence.text_messages):
            raise ProductionE2EError("installation_visible_message_not_russian")
        for kind in (ToolKind.MARKETPLACE_ADD, ToolKind.PLUGIN_INSTALL, ToolKind.LOGIN):
            if not evidence.has_successful(kind):
                raise ProductionE2EError(f"installation_{kind}_not_observed")
        if evidence.tool_calls.count(ToolKind.NEW_CHAT_URI) != 1:
            raise ProductionE2EError("installation_new_chat_uri_not_observed")
        if ToolKind.FORBIDDEN_BROWSER_MODE in evidence.tool_calls:
            raise ProductionE2EError("installation_no_browser_forbidden")
        if evidence.event_order != (
            "visible",
            ToolKind.MARKETPLACE_ADD.value,
            ToolKind.PLUGIN_INSTALL.value,
            ToolKind.LOGIN.value,
            ToolKind.NEW_CHAT_URI.value,
            "visible",
        ):
            raise ProductionE2EError("installation_event_order_invalid")
