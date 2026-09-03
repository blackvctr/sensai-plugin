"""Bounded real-Claude acceptance for Sensai's first visible reply.

The check uses the already configured Claude profile. It never logs in or
changes a profile setting. Its private hooks retain only three booleans and
deny every attempted tool before execution; the real stream independently
proves the order in which text and tool requests appeared.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from sensai_plugin.installation_e2e_contract import CLAUDE_SONNET_5_MODEL

CLAUDE_TIMEOUT_SECONDS = 45
CLAUDE_TERMINATION_GRACE_SECONDS = 2
MAX_STREAM_LINE_BYTES = 256 * 1024
MAX_EVENT_ORDER_ENTRIES = 4
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-z]")
_TERMINAL_LEXEME = re.compile(
    r"(?:\bterminal\b|\bcommand\s+line\b|\bshell\b|\bbash\b|\bpowershell\b|"
    r"\bcmd(?:\.exe)?\b|\bclaude\s+(?:plugin|mcp)\b|"
    r"терминал|командн(?:ая|ой)\s+строк|консол[ьи])",
    re.IGNORECASE,
)
_CODE_BLOCK = re.compile(r"```")


class FirstReplyScenario(StrEnum):
    """The historical link-based first-contact case under acceptance."""

    URL_BOOTSTRAP = "url_bootstrap"


# This is the Russian public copy-paste installation request from README.
# Keeping it literal makes the real first-reply check cover that exact path.
_CANONICAL_RUSSIAN_INSTALL_PROMPT = (
    "Установи Sensai https://raw.githubusercontent.com/blackvctr/"
    "sensai-plugin/main/README.md"
)

_SCENARIO_PROMPTS: dict[FirstReplyScenario, str] = {
    FirstReplyScenario.URL_BOOTSTRAP: _CANONICAL_RUSSIAN_INSTALL_PROMPT,
}

ResultCategory = Literal[
    "completed",
    "cli_failed",
    "timed_out",
    "malformed_stream",
    "stream_evidence_missing",
    "hook_evidence_missing",
]
EventCategory = Literal["assistant_reply", "tool_attempt", "result"]


@dataclass(frozen=True, slots=True)
class ClaudeFirstReplyAcceptance:
    """Only redacted observations from one real Claude invocation."""

    scenario: FirstReplyScenario
    event_order: tuple[EventCategory, ...]
    first_reply_captured: bool
    cyrillic_present: bool
    cyrillic_preponderates: bool
    terminal_lexeme_present: bool
    code_block_present: bool
    tool_requested: bool
    tool_gate_reached: bool
    result: ResultCategory
    timed_out: bool

    @property
    def passed(self) -> bool:
        """Require a safe Russian first response to the canonical request."""

        if self.result != "completed" or not self.first_reply_captured:
            return False
        if not self.event_order or self.event_order[0] != "assistant_reply":
            return False
        return (
            self.cyrillic_present
            and self.cyrillic_preponderates
            and not self.terminal_lexeme_present
            and not self.code_block_present
        )

    def safe_json(self) -> str:
        """Return the intentionally small report suitable for a test log."""

        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class ClaudeFirstReplyError(RuntimeError):
    """The harness could not run within its closed evidence model."""


def _is_canonical_russian_install_prompt(prompt: str) -> bool:
    """Reject stale or changed harness input before it reaches Claude."""

    return prompt == _CANONICAL_RUSSIAN_INSTALL_PROMPT


def _prompt_for(scenario: FirstReplyScenario) -> str:
    prompt = _SCENARIO_PROMPTS[scenario]
    if not _is_canonical_russian_install_prompt(prompt):
        raise ClaudeFirstReplyError("first-reply scenario input is not the canonical README prompt")
    return prompt


def _hook_program() -> str:
    """Return a hook that writes only safe boolean observations.

    The MessageDisplay path looks at exactly its ``delta`` field. It does not
    recurse through arbitrary hook data and never stores a character of the
    reply. PreToolUse records the attempted tool and denies it.
    """

    return r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["SENSAI_CLAUDE_FIRST_REPLY_STATE"])

def state():
    try:
        loaded = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or set(loaded) != {
        "seen_nonblank_text", "tool_attempted", "tool_before_text"
    }:
        return None
    if not all(isinstance(loaded[key], bool) for key in loaded):
        return None
    return {
        "seen_nonblank_text": loaded.get("seen_nonblank_text") is True,
        "tool_attempted": loaded.get("tool_attempted") is True,
        "tool_before_text": loaded.get("tool_before_text") is True,
    }

def save(value):
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, STATE)

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    payload = {}

observation = state()
if sys.argv[1] == "message-display":
    if observation is None:
        raise SystemExit(0)
    delta = payload.get("delta") if isinstance(payload, dict) else None
    if isinstance(delta, str) and delta.strip():
        observation["seen_nonblank_text"] = True
    try:
        save(observation)
    except OSError:
        pass
    raise SystemExit(0)

if sys.argv[1] != "pre-tool-use":
    raise SystemExit(2)
def deny():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "first-reply acceptance blocks tools"
    }}))
    raise SystemExit(0)

# A missing or unwritable state file must never become permission to execute.
# The later harness result is simply hook_evidence_missing, with no retry.
if observation is None:
    deny()
observation["tool_attempted"] = True
if not observation["seen_nonblank_text"]:
    observation["tool_before_text"] = True
try:
    save(observation)
except OSError:
    deny()
deny()
'''


@contextmanager
def _temporary_hooks(temporary_root: Path) -> Iterator[tuple[Path, Path]]:
    """Create owned hooks and a three-boolean state file, then erase them."""

    root = Path(tempfile.mkdtemp(prefix="sensai-claude-first-reply-", dir=temporary_root))
    root.chmod(0o700)
    hook = root / "block_tools.py"
    state = root / "hook-state.json"
    settings = root / "settings.json"
    run_error: BaseException | None = None
    try:
        hook.write_text(_hook_program(), encoding="utf-8")
        hook.chmod(0o700)
        state.write_text(
            json.dumps(
                {
                    "seen_nonblank_text": False,
                    "tool_attempted": False,
                    "tool_before_text": False,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        state.chmod(0o600)
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook))}"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "MessageDisplay": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{command} message-display",
                                    }
                                ],
                            }
                        ],
                        "PreToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{command} pre-tool-use",
                                    }
                                ],
                            }
                        ],
                    }
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        settings.chmod(0o600)
        yield settings, state
    except BaseException as error:
        run_error = error
        raise
    finally:
        try:
            shutil.rmtree(root)
        except OSError as cleanup_error:
            if run_error is not None:
                run_error.add_note("temporary first-reply cleanup also failed")
            else:
                raise ClaudeFirstReplyError(
                    "temporary first-reply cleanup failed"
                ) from cleanup_error


def _read_state(state: Path) -> tuple[bool, bool, bool] | None:
    try:
        loaded = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    expected = {"seen_nonblank_text", "tool_attempted", "tool_before_text"}
    if not isinstance(loaded, dict) or set(loaded) != expected:
        return None
    if not all(isinstance(loaded[key], bool) for key in expected):
        return None
    return (
        loaded["seen_nonblank_text"],
        loaded["tool_attempted"],
        loaded["tool_before_text"],
    )


def _stream_event(event: object) -> dict[str, object] | None:
    if not isinstance(event, dict) or event.get("type") != "stream_event":
        return None
    nested = event.get("event")
    return nested if isinstance(nested, dict) else None


def _stream_content_start(event: dict[str, object]) -> tuple[int, str] | None:
    if event.get("type") != "content_block_start":
        return None
    index = event.get("index")
    block = event.get("content_block")
    if not isinstance(index, int) or not isinstance(block, dict):
        return None
    block_type = block.get("type")
    return (index, block_type) if isinstance(block_type, str) else None


def _stream_text_delta(event: dict[str, object], index: int) -> str | None:
    if event.get("type") != "content_block_delta" or event.get("index") != index:
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


def _stream_text_stop(event: dict[str, object], index: int) -> bool:
    return event.get("type") == "content_block_stop" and event.get("index") == index


def _event_is_result(event: object) -> bool:
    return isinstance(event, dict) and event.get("type") == "result"


@dataclass
class _FirstReplyFlags:
    cyrillic: int = 0
    latin: int = 0
    terminal_lexeme: bool = False
    code_block: bool = False
    _tail: str = ""

    def add(self, delta: str) -> None:
        self.cyrillic += len(_CYRILLIC.findall(delta))
        self.latin += len(_LATIN.findall(delta))
        joined = self._tail + delta
        self.terminal_lexeme = self.terminal_lexeme or _TERMINAL_LEXEME.search(joined) is not None
        self.code_block = self.code_block or _CODE_BLOCK.search(joined) is not None
        self._tail = joined[-64:]

    def result(self) -> tuple[bool, bool, bool, bool]:
        return (
            self.cyrillic > 0,
            self.cyrillic > self.latin,
            self.terminal_lexeme,
            self.code_block,
        )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=CLAUDE_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _read_stream(
    process: subprocess.Popen[bytes], *, timeout_seconds: float
) -> tuple[
    tuple[EventCategory, ...],
    tuple[bool, bool, bool, bool],
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
]:
    """Read stream-json without returning text, URLs, IDs, or error output."""

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector.register(descriptor, selectors.EVENT_READ)
    order: list[EventCategory] = []
    first_text_index: int | None = None
    first_reply_captured = False
    flags = _FirstReplyFlags()
    result_seen = False
    first_text_complete = False
    tool_before_first_text_complete = False
    malformed = False
    timed_out = False
    ended_after_result = False
    ended_after_first_text = False
    end_of_stream = False
    pending = bytearray()
    deadline = time.monotonic() + timeout_seconds

    def consume(line: bytes) -> bool:
        nonlocal first_text_index, first_reply_captured, first_text_complete
        nonlocal malformed, result_seen, tool_before_first_text_complete
        if len(line) > MAX_STREAM_LINE_BYTES:
            malformed = True
            return True
        try:
            outer = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed = True
            return True
        if nested := _stream_event(outer):
            if started := _stream_content_start(nested):
                index, block_type = started
                if block_type == "tool_use":
                    order.append("tool_attempt")
                    tool_before_first_text_complete = not first_text_complete
                elif block_type == "text" and first_text_index is None:
                    first_text_index = index
            if first_text_index is not None and (
                delta := _stream_text_delta(nested, first_text_index)
            ):
                if not first_reply_captured and delta.strip():
                    first_reply_captured = True
                    order.append("assistant_reply")
                if first_reply_captured:
                    flags.add(delta)
            if first_text_index is not None and _stream_text_stop(nested, first_text_index):
                first_text_complete = True
                return True
        if _event_is_result(outer):
            result_seen = True
            order.append("result")
            return True
        return False

    try:
        while True:
            if first_text_complete:
                ended_after_first_text = True
                _terminate(process)
                break
            if result_seen:
                ended_after_result = True
                _terminate(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate(process)
                break
            ready = selector.select(0 if process.poll() is not None else remaining)
            if not ready:
                if process.poll() is not None:
                    if pending:
                        malformed = True
                    break
                timed_out = True
                _terminate(process)
                break
            while True:
                try:
                    chunk = os.read(descriptor, MAX_STREAM_LINE_BYTES + 1)
                except BlockingIOError:
                    break
                if not chunk:
                    end_of_stream = True
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
                    if consume(bytes(line)):
                        break
                if malformed or result_seen or first_text_complete:
                    break
            if malformed or first_text_complete or end_of_stream:
                if first_text_complete:
                    ended_after_first_text = True
                _terminate(process)
                break
    finally:
        selector.close()
        if process.poll() is None:
            _terminate(process)
    return (
        tuple(order[:MAX_EVENT_ORDER_ENTRIES]),
        flags.result(),
        first_reply_captured,
        malformed,
        timed_out,
        ended_after_result,
        ended_after_first_text,
        tool_before_first_text_complete,
    )


def run_real_claude_first_reply(
    *,
    scenario: FirstReplyScenario,
    claude_executable: str = "claude",
    cwd: Path | None = None,
    timeout_seconds: float = CLAUDE_TIMEOUT_SECONDS,
    temporary_root: Path | None = None,
) -> ClaudeFirstReplyAcceptance:
    """Run one bounded `claude -p` check with the actual configured profile.

    The only prompt is a reviewed natural-Russian scenario owned by this
    module. It is not returned, written to a temporary file, or logged.
    """

    if timeout_seconds <= 0:
        raise ClaudeFirstReplyError("first-reply timeout must be positive")
    prompt = _prompt_for(scenario)
    working_directory = (cwd or Path.cwd()).resolve(strict=True)
    root = (temporary_root or Path(tempfile.gettempdir())).resolve(strict=True)
    if not root.is_dir():
        raise ClaudeFirstReplyError("first-reply temporary root is not a directory")
    environment = os.environ.copy()
    with _temporary_hooks(root) as (settings, state):
        environment["SENSAI_CLAUDE_FIRST_REPLY_STATE"] = str(state)
        process = subprocess.Popen(
            [
                claude_executable,
                "-p",
                "--model",
                CLAUDE_SONNET_5_MODEL,
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--settings",
                str(settings),
                prompt,
            ],
            cwd=working_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        (
            order,
            flags,
            first_reply_captured,
            malformed,
            timed_out,
            ended_after_result,
            ended_after_first_text,
            tool_before_first_text_complete,
        ) = _read_stream(process, timeout_seconds=timeout_seconds)
        returncode = process.wait()
        observed = _read_state(state)
    result: ResultCategory
    if observed is None:
        result = "hook_evidence_missing"
        seen_nonblank_text = tool_attempted = tool_before_text = False
    else:
        seen_nonblank_text, tool_attempted, tool_before_text = observed
        if timed_out:
            result = "timed_out"
        elif malformed:
            result = "malformed_stream"
        elif returncode != 0 and not (ended_after_result or ended_after_first_text):
            result = "cli_failed"
        elif not ended_after_first_text or not first_reply_captured:
            result = "stream_evidence_missing"
        else:
            result = "completed"
    model_tool_requested = "tool_attempt" in order
    if (
        tool_before_text
        or tool_before_first_text_complete
        or (tool_attempted and (not order or order[0] != "assistant_reply"))
        or (model_tool_requested != tool_attempted and not ended_after_first_text)
    ) and result == "completed":
        result = "stream_evidence_missing"
    if seen_nonblank_text != first_reply_captured and result == "completed":
        result = "hook_evidence_missing"
    return ClaudeFirstReplyAcceptance(
        scenario=scenario,
        event_order=order,
        first_reply_captured=first_reply_captured,
        cyrillic_present=flags[0],
        cyrillic_preponderates=flags[1],
        terminal_lexeme_present=flags[2],
        code_block_present=flags[3],
        tool_requested=model_tool_requested,
        tool_gate_reached=tool_attempted,
        result=result,
        timed_out=timed_out,
    )
