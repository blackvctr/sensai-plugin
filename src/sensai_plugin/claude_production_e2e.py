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
import signal
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sensai_plugin.claude_e2e_profile import ClaudeE2ERun, create_fresh_run
from sensai_plugin.installation_e2e_contract import (
    CLAUDE_SONNET_5_MODEL,
    PublicReadmeContract,
    _load_public_readme_contract,
)

INSTALL_TIMEOUT_SECONDS = 300
MCP_STATUS_TIMEOUT_SECONDS = 20
TELEGRAM_START_TIMEOUT_SECONDS = 120
TELEGRAM_CONTINUATION_TIMEOUT_SECONDS = 120
FORGET_ME_TIMEOUT_SECONDS = 60
PROCESS_TERMINATION_GRACE_SECONDS = 3
MAX_STREAM_LINE_BYTES = 256 * 1024
MAX_STREAM_EVENTS = 128

_STATUS_FAILURE = re.compile(r"needs authentication|disconnected|\berror\b", re.IGNORECASE)
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-z]")

# These are deliberately ordinary Russian facts, not a fake tool result.
_TELEGRAM_FACTS = (
    "Я маркетолог. Регулярно отвечаю клиентам в Telegram и хочу быстрее "
    "готовить ответы на повторяющиеся вопросы."
)
_FORGET_ME_REQUEST = "Заверши проверку: вызови инструмент Sensai forget_me ровно один раз."


class ProductionE2EError(RuntimeError):
    """The safe production acceptance report contains one failed phase."""


class ToolKind(StrEnum):
    """Only tool facts the runner may retain after a stream is consumed."""

    LOGIN = "sensai_login"
    TELL_SENSAI = "tell_sensai"
    FORGET_ME = "forget_me"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class TextEvidence:
    """A completed Claude text block without its contents or digest."""

    matches_expected: bool
    cyrillic_letters: int
    latin_letters: int


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

    def has_successful(self, kind: ToolKind, *, exactly: int = 1) -> bool:
        return (
            self.tool_calls.count(kind) == exactly
            and self.successful_tool_results.count(kind) == exactly
        )


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
    ) -> AgentEvidence: ...

    def mcp_connected(
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
        )

    def mcp_connected(
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
            and "Status:" in status
            and _STATUS_FAILURE.search(status) is None
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
class _LoginCommandScanner:
    """Recognize the fixed login subcommand across JSON chunks without storing them."""

    token_index: int = 0
    character_index: int = 0
    waiting_for_space: bool = False
    matched: bool = False

    _TOKENS = ("mcp", "login", "plugin:sensai:sensai")

    def add(self, fragment: str) -> None:
        for character in fragment.lower():
            self._add_character(character)

    def _add_character(self, character: str) -> None:
        if self.matched:
            return
        if self.waiting_for_space:
            if character.isspace():
                self.waiting_for_space = False
                self.character_index = 0
                return
            self._restart(character)
            return
        token = self._TOKENS[self.token_index]
        if character == token[self.character_index]:
            self.character_index += 1
            if self.character_index == len(token):
                if self.token_index == len(self._TOKENS) - 1:
                    self.matched = True
                else:
                    self.token_index += 1
                    self.waiting_for_space = True
            return
        self._restart(character)

    def _restart(self, character: str) -> None:
        self.token_index = 0
        self.waiting_for_space = False
        self.character_index = 1 if character == self._TOKENS[0][0] else 0


@dataclass(slots=True)
class _ToolAccumulator:
    """One tool-use block reduced to a kind once its JSON input is complete."""

    direct_kind: ToolKind | None
    login_scanner: _LoginCommandScanner

    def kind(self) -> ToolKind:
        if self.direct_kind is not None:
            return self.direct_kind
        return ToolKind.LOGIN if self.login_scanner.matched else ToolKind.OTHER


def _direct_tool_kind(block: object) -> ToolKind | None:
    if not isinstance(block, dict):
        return None
    name = block.get("name")
    if isinstance(name, str) and name.endswith("forget_me"):
        return ToolKind.FORGET_ME
    if isinstance(name, str) and name.endswith("tell_sensai"):
        return ToolKind.TELL_SENSAI
    return None


def _new_tool_accumulator(block: object) -> _ToolAccumulator:
    scanner = _LoginCommandScanner()
    input_value = block.get("input") if isinstance(block, dict) else None
    command = input_value.get("command") if isinstance(input_value, dict) else None
    if isinstance(command, str):
        scanner.add(command)
    return _ToolAccumulator(_direct_tool_kind(block), scanner)


def _tool_result_successes(record: object, outstanding: deque[ToolKind]) -> list[ToolKind]:
    """Reduce tool result blocks to success/failure categories without contents."""

    if not isinstance(record, dict) or record.get("type") != "user":
        return []
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    successes: list[ToolKind] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        kind = outstanding.popleft() if outstanding else ToolKind.OTHER
        if block.get("is_error") is not True:
            successes.append(kind)
    return successes


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
    successes: list[ToolKind] = []
    outstanding: deque[ToolKind] = deque()
    result_seen = session_verified = malformed = timed_out = False
    event_count = 0
    deadline = time.monotonic() + timeout_seconds

    def consume(line: bytes) -> None:
        nonlocal event_count, malformed, result_seen, session_verified
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
        successes.extend(_tool_result_successes(record, outstanding))
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
                tool_accumulator.login_scanner.add(partial_json)
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
            tool_accumulator = tool_blocks.pop(index, None) if isinstance(index, int) else None
            if tool_accumulator is not None:
                kind = tool_accumulator.kind()
                calls.append(kind)
                outstanding.append(kind)

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
    return AgentEvidence(
        result_seen=result_seen,
        session_verified=session_verified,
        malformed=malformed,
        timed_out=timed_out,
        returncode=process.wait(),
        text_messages=tuple(text_messages),
        tool_calls=tuple(calls),
        successful_tool_results=tuple(successes),
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


class ProductionSensaiE2E:
    """One explicit production check using a local disposable Claude run."""

    def __init__(
        self,
        *,
        profile: Path,
        claude_executable: str = "claude",
        driver: ClaudeDriver | None = None,
    ) -> None:
        if not claude_executable or any(character.isspace() for character in claude_executable):
            raise ProductionE2EError("invalid_claude_executable")
        self._profile = profile
        self._executable = claude_executable
        self._driver = driver or SubprocessClaudeDriver()

    def run(self) -> ProductionE2EReport:
        """Install, authenticate, consult Telegram, forget, then remove local state."""

        contract = _load_public_readme_contract()
        installation_session = uuid.uuid4()
        telegram_session = uuid.uuid4()
        with create_fresh_run(self._profile) as run:
            return self._run_inside_fresh_profile(
                run,
                contract=contract,
                installation_session=installation_session,
                telegram_session=telegram_session,
            )

    def _run_inside_fresh_profile(
        self,
        run: ClaudeE2ERun,
        *,
        contract: PublicReadmeContract,
        installation_session: uuid.UUID,
        telegram_session: uuid.UUID,
    ) -> ProductionE2EReport:
        cleanup_needed = False
        primary_error: ProductionE2EError | None = None
        try:
            installation = self._driver.run_agent(
                _agent_command(
                    self._executable,
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
            )
            self._require_installation(installation)
            normal_login_started = installation.tool_calls.count(ToolKind.LOGIN) == 1
            normal_login_completed = installation.successful_tool_results.count(ToolKind.LOGIN) == 1
            if not normal_login_started:
                raise ProductionE2EError("normal_login_not_started")
            if not normal_login_completed:
                raise ProductionE2EError("normal_login_not_completed")
            if not self._driver.mcp_connected(
                _status_command(self._executable),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
            ):
                raise ProductionE2EError("sensai_connection_not_verified")
            cleanup_needed = True

            telegram_start = self._driver.run_agent(
                _agent_command(
                    self._executable,
                    prompt=contract.russian_new_chat_request,
                    session=telegram_session,
                    resume=False,
                ),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=TELEGRAM_START_TIMEOUT_SECONDS,
                expected_visible_messages=(),
                expected_session=telegram_session,
            )
            self._require_tool_turn(telegram_start, ToolKind.TELL_SENSAI, "telegram_start")
            telegram_continuation = self._driver.run_agent(
                _agent_command(
                    self._executable,
                    prompt=_TELEGRAM_FACTS,
                    session=telegram_session,
                    resume=True,
                ),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=TELEGRAM_CONTINUATION_TIMEOUT_SECONDS,
                expected_visible_messages=(),
                expected_session=telegram_session,
            )
            self._require_tool_turn(
                telegram_continuation,
                ToolKind.TELL_SENSAI,
                "telegram_continuation",
            )
        except ProductionE2EError as error:
            primary_error = error
        finally:
            cleanup_error: ProductionE2EError | None = None
            cleanup_completed = False
            if cleanup_needed:
                try:
                    cleanup = self._driver.run_agent(
                        _agent_command(
                            self._executable,
                            prompt=_FORGET_ME_REQUEST,
                            session=telegram_session,
                            resume=True,
                        ),
                        cwd=run.work,
                        environment=run.environment,
                        timeout_seconds=FORGET_ME_TIMEOUT_SECONDS,
                        expected_visible_messages=(),
                        expected_session=telegram_session,
                    )
                    self._require_tool_turn(cleanup, ToolKind.FORGET_ME, "forget_me")
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

    @staticmethod
    def _require_tool_turn(evidence: AgentEvidence, kind: ToolKind, phase: str) -> None:
        if evidence.timed_out:
            raise ProductionE2EError(f"{phase}_timed_out")
        if evidence.malformed or evidence.returncode != 0 or not evidence.result_seen:
            raise ProductionE2EError(f"{phase}_stream_invalid")
        if not evidence.session_verified:
            raise ProductionE2EError(f"{phase}_session_not_verified")
        if not evidence.has_successful(kind):
            raise ProductionE2EError(f"{phase}_tool_result_invalid")
