"""Run the public Sensai installation acceptance from a local Claude profile.

The check ends after installation, normal Sensai login, and the published
second-chat URI attempt. It never begins a consultation or inspects server
internals.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sensai_plugin.claude_e2e_profile import ClaudeE2ERun, create_fresh_run
from sensai_plugin.installation_e2e_contract import (
    CLAUDE_LINUX_ACTIONS,
    CLAUDE_SONNET_5_MODEL,
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
PUBLIC_INSTALL_PROMPT = f"Установи Sensai {PUBLIC_README_URL}"
_E2E_WEBFETCH_PERMISSION = "WebFetch(domain:raw.githubusercontent.com)"
_PUBLIC_RAW_HOST = "raw.githubusercontent.com"
_PUBLIC_RAW_PREFIX = "/blackvctr/sensai-plugin/main/"
_OAUTH_ENTRY_HOSTS = frozenset({"black-vector.com", "accounts.google.com"})
_BROWSER_TOOL = Path("/mnt/c/gdrive/dev/.skills/use-windows-firefox/scripts/browser_tool_pw.py")
_SENSAI_PLUGIN_SELECTOR = "sensai@sensai"
_SENSAI_MCP_NAME = "plugin:sensai:sensai"
_SENSAI_MCP_URL = "https://black-vector.com/sensai/mcp"
_PUBLIC_METADATA_INVENTORY_URLS = frozenset(
    {
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/"
        ".claude-plugin/marketplace.json",
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/"
        "plugins/sensai/.claude-plugin/plugin.json",
        "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/plugins/sensai/.mcp.json",
    }
)
_SAFE_METADATA_CURL_FLAGS = frozenset(
    {
        "-s",
        "-S",
        "-L",
        "-f",
        "-fsSL",
        "-fsS",
        "-sSL",
        "--silent",
        "--show-error",
        "--location",
        "--fail",
    }
)
_INERT_ECHO = re.compile(
    r'echo(?: (?:"[=A-Za-z0-9._:/ -]{1,128}"|[=A-Za-z0-9._:/][=A-Za-z0-9._:/-]{0,127}))?'
)
_UNSAFE_SHELL_GRAMMAR = frozenset("\n\r\t`$\\|&<>(){}[]*!?")

_STATUS_FAILURE = re.compile(r"needs authentication|disconnected|\berror\b", re.IGNORECASE)
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LATIN = re.compile(r"[A-Za-z]")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_TERMINAL_REFERENCE = re.compile(r"(?:\bterminal\b|\b(?:bash|shell)\b|терминал)", re.IGNORECASE)


class ProductionE2EError(RuntimeError):
    """One safe category explaining an installation acceptance failure."""

    def __init__(
        self,
        phase: str,
        *,
        before_marketplace_receipt: PreMarketplaceFailureReceipt | None = None,
    ) -> None:
        super().__init__(phase)
        self.before_marketplace_receipt = before_marketplace_receipt


@dataclass(frozen=True, slots=True)
class InstallationScenario:
    """Fixed local acceptance boundary, never content supplied to Claude.

    The public README is an object Claude may read.  It must not also become
    the source of truth for what this test permits or expects: changing a
    candidate README must not silently widen the test's authority.
    """

    prompt: str
    plugin_selector: str
    mcp_name: str
    mcp_url: str
    new_chat_uri: str
    claude_linux_actions: tuple[tuple[str, ...], ...]


INSTALLATION_SCENARIO = InstallationScenario(
    prompt=PUBLIC_INSTALL_PROMPT,
    plugin_selector=_SENSAI_PLUGIN_SELECTOR,
    mcp_name=_SENSAI_MCP_NAME,
    mcp_url=_SENSAI_MCP_URL,
    new_chat_uri=CLAUDE_LINUX_ACTIONS[-1][1][1],
    claude_linux_actions=tuple(argv for _, argv in CLAUDE_LINUX_ACTIONS),
)


def _validate_expected_readme_sha256(value: str) -> str:
    if not _SHA256_HEX.fullmatch(value):
        raise ProductionE2EError("public_readme_sha256_invalid")
    return value


def fetch_public_readme_sha256(expected_sha256: str) -> str:
    """Fetch the public README and match it to one explicit candidate.

    The README is material Claude may read, not runner instructions.  The
    caller supplies the candidate digest so a comparison can safely test a
    non-published README without teaching the runner to parse its contents.
    """

    expected = _validate_expected_readme_sha256(expected_sha256)
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
    actual = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ProductionE2EError("public_readme_sha256_mismatch")
    return actual


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
    PUBLIC_README_FETCH = "public_readme_fetch"
    PUBLIC_METADATA_FETCH = "public_metadata_fetch"
    PUBLIC_METADATA_BASH = "public_metadata_bash"
    PUBLIC_METADATA_INVENTORY_BASH = "public_metadata_inventory_bash"
    PUBLIC_METADATA_COMPOUND_BASH = "public_metadata_compound_bash"
    FORBIDDEN_BROWSER_MODE = "forbidden_browser_mode"
    OTHER = "other"


class PermissionDecision(StrEnum):
    """One closed result from the installation permission boundary."""

    ALLOW = "allow"
    DENY = "deny"


class FirstTextKind(StrEnum):
    """A closed, redacted reading of Claude's first visible reply."""

    NONE = "none"
    TRUST_QUESTION = "trust_question"
    REFUSAL = "refusal"
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


class SdkExceptionKind(StrEnum):
    """Closed classes of an exception raised by the local SDK driver.

    The exception message and concrete third-party class are deliberately not
    retained.  A receipt must be useful for choosing the next diagnostic step
    without becoming a second copy of Claude, OAuth, or network output.
    """

    NONE = "none"
    OS = "os"
    VALUE = "value"
    RUNTIME = "runtime"
    OTHER = "other"


class SdkResultKind(StrEnum):
    """Closed reading of the structured terminal SDK result, when present."""

    NONE = "none"
    SUCCESS = "success"
    ERROR = "error"
    OTHER = "other"


class SdkResultCause(StrEnum):
    """Safe, closed reason for an error ``ResultMessage`` from the SDK.

    The SDK result also carries ``errors`` and ``result`` prose.  They are
    deliberately not read here: either field can contain Claude output,
    command text, URLs, or authentication details.  This enum is derived
    only from documented structured fields with a fixed allowlist.
    """

    NONE = "none"
    API_ERROR = "api_error"
    EXECUTION = "execution"
    BUDGET = "budget"
    STRUCTURED_OUTPUT_RETRIES = "structured_output_retries"
    TURN_LIMIT = "turn_limit"
    PERMISSION = "permission"
    INTERRUPTED = "interrupted"
    OTHER = "other"


class SdkCleanupKind(StrEnum):
    """Closed result of cleanup after a local SDK run."""

    NONE = "none"
    DISCONNECT_FAILED = "disconnect_failed"


class PreMarketplaceFailureKind(StrEnum):
    """Why a full E2E ended before adding the public marketplace."""

    TIMEOUT = "timeout"
    SDK_EXCEPTION = "sdk_exception"
    SDK_RESULT_ERROR = "sdk_result_error"
    MISSING_RESULT = "missing_result"
    COMPLETED_BEFORE_MARKETPLACE = "completed_before_marketplace"


@dataclass(frozen=True, slots=True)
class PreMarketplaceFailureReceipt:
    """Small redacted receipt for a full E2E that stops before marketplace add.

    Every field is a closed category.  In particular it contains no Claude
    text, tool input, URL, session identifier, or exception message.
    """

    kind: PreMarketplaceFailureKind
    stage: ExitStage
    sdk_exception: SdkExceptionKind
    sdk_result: SdkResultKind
    sdk_result_cause: SdkResultCause
    sdk_cleanup: SdkCleanupKind
    first_text: FirstTextKind
    first_tool_intent: ToolKind | None
    first_denied_tool_intent: ToolKind | None

    def machine_line(self) -> str:
        """Return the complete public receipt without formatting free text."""

        first_tool = self.first_tool_intent or "none"
        first_denied = self.first_denied_tool_intent or "none"
        return (
            "before_marketplace="
            f"kind:{self.kind},stage:{self.stage},sdk_exception:{self.sdk_exception},"
            f"sdk_result:{self.sdk_result},sdk_result_cause:{self.sdk_result_cause},"
            f"sdk_cleanup:{self.sdk_cleanup},"
            f"first_text:{self.first_text},"
            f"first_tool:{first_tool},first_denied:{first_denied}"
        )


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
_SDK_RESULT_SUBTYPES = {
    "error_during_execution": SdkResultCause.EXECUTION,
    "error_max_budget_usd": SdkResultCause.BUDGET,
    "error_max_structured_output_retries": SdkResultCause.STRUCTURED_OUTPUT_RETRIES,
    "error_max_turns": SdkResultCause.TURN_LIMIT,
    "error_permission": SdkResultCause.PERMISSION,
}
_SDK_RESULT_TERMINAL_REASONS = {
    "api_error": SdkResultCause.API_ERROR,
    "max_turns": SdkResultCause.TURN_LIMIT,
    "aborted_streaming": SdkResultCause.INTERRUPTED,
    "aborted_tools": SdkResultCause.INTERRUPTED,
}
# The SDK documents these HTTP statuses as a structured API-error field.  The
# receipt keeps the category, rather than the numeric status, so it cannot
# become an incidental network log.
_SDK_API_ERROR_STATUSES = frozenset(
    {400, 401, 403, 404, 408, 409, 413, 429, 500, 502, 503, 504, 529}
)


class ExitStage(StrEnum):
    BEFORE_MARKETPLACE = "before_marketplace"
    AFTER_MARKETPLACE_BEFORE_PLUGIN = "after_marketplace_before_plugin"
    AFTER_PLUGIN_BEFORE_LOGIN = "after_plugin_before_login"
    AFTER_LOGIN_BEFORE_NEW_CHAT = "after_login_before_new_chat"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TextEvidence:
    cyrillic_letters: int
    latin_letters: int
    contains_code_block: bool
    contains_terminal_reference: bool


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
    sensai_connection_verified: bool = False
    public_sensai_plugin_installed: bool = False
    tool_intents: tuple[ToolKind, ...] = ()
    denied_tool_intents: tuple[ToolKind, ...] = ()
    first_text_kind: FirstTextKind = FirstTextKind.NONE
    sdk_exception_kind: SdkExceptionKind = SdkExceptionKind.NONE
    sdk_result_kind: SdkResultKind = SdkResultKind.NONE
    sdk_result_cause: SdkResultCause = SdkResultCause.NONE
    sdk_cleanup_kind: SdkCleanupKind = SdkCleanupKind.NONE

    def has_successful(self, kind: ToolKind, *, exactly: int = 1) -> bool:
        return (
            self.tool_calls.count(kind) == exactly
            and self.successful_tool_results.count(kind) == exactly
        )


@dataclass(frozen=True, slots=True)
class ProductionE2EReport:
    localized_messages_accepted: bool
    normal_login_started: bool
    normal_login_completed: bool
    sensai_connection_verified: bool
    public_sensai_plugin_installed: bool
    new_chat_uri_attempted: bool

    @property
    def complete(self) -> bool:
        return all(
            (
                self.localized_messages_accepted,
                self.normal_login_started,
                self.normal_login_completed,
                self.sensai_connection_verified,
                self.public_sensai_plugin_installed,
                self.new_chat_uri_attempted,
            )
        )


@dataclass(frozen=True, slots=True)
class FirstComparisonReport:
    """Redacted evidence from the first safely stopped public README run."""

    public_readme_sha256: str
    first_text_kind: FirstTextKind
    first_tool_intent: ToolKind | None
    denied_tool_intents: tuple[ToolKind, ...]


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
    contains_code_block: bool = False
    contains_terminal_reference: bool = False
    _suffix: str = ""

    @classmethod
    def new(cls) -> _TextAccumulator:
        return cls(hashlib.sha256())

    def add(self, text: str) -> None:
        self.digest.update(text.encode("utf-8"))
        self.cyrillic_letters += len(_CYRILLIC.findall(text))
        self.latin_letters += len(_LATIN.findall(text))
        joined = self._suffix + text
        self.contains_code_block = self.contains_code_block or "```" in joined
        self.contains_terminal_reference = (
            self.contains_terminal_reference or _TERMINAL_REFERENCE.search(joined) is not None
        )
        # This is transient parser state, not recorded E2E evidence.
        self._suffix = joined[-16:]

    def evidence(self) -> TextEvidence:
        """Return only acceptance properties; never retain the visible text."""

        # This test accepts messages, not Markdown instructions or a request
        # that the person use a shell.  The text itself is intentionally not
        # written to a receipt.
        return TextEvidence(
            cyrillic_letters=self.cyrillic_letters,
            latin_letters=self.latin_letters,
            contains_code_block=self.contains_code_block,
            contains_terminal_reference=self.contains_terminal_reference,
        )


def _classify_first_text(text: str) -> FirstTextKind:
    """Keep a useful outcome category without retaining Claude's reply."""

    normalized = text.casefold()
    refusal_markers = (
        "не могу",
        "не буду",
        "отказыва",
        "can't",
        "cannot",
        "won't",
        "refus",
    )
    if any(marker in normalized for marker in refusal_markers):
        return FirstTextKind.REFUSAL
    trust_markers = ("довер", "trust", "подтверд", "confirm", "approval")
    if any(marker in normalized for marker in trust_markers):
        return FirstTextKind.TRUST_QUESTION
    return FirstTextKind.OTHER


def _classify_sdk_exception(error: Exception) -> SdkExceptionKind:
    """Keep only a stable local exception family, never its name or message."""

    if isinstance(error, OSError):
        return SdkExceptionKind.OS
    if isinstance(error, ValueError):
        return SdkExceptionKind.VALUE
    if isinstance(error, RuntimeError):
        return SdkExceptionKind.RUNTIME
    return SdkExceptionKind.OTHER


def _sdk_result_kind(message: object) -> SdkResultKind:
    """Read the documented result flag without retaining result content."""

    is_error = getattr(message, "is_error", None)
    if is_error is True:
        return SdkResultKind.ERROR
    if is_error is False:
        return SdkResultKind.SUCCESS
    return SdkResultKind.OTHER


def _sdk_result_cause(message: object) -> SdkResultCause:
    """Classify an SDK error result without retaining free-form error data.

    ``terminal_reason``, ``subtype`` and the documented API status are
    protocol fields.  ``errors`` and ``result`` are intentionally absent from
    this function: they are free-form text and must never enter a receipt.
    """

    if _sdk_result_kind(message) is not SdkResultKind.ERROR:
        return SdkResultCause.NONE
    reason = getattr(message, "terminal_reason", None)
    if isinstance(reason, str):
        known_reason = _SDK_RESULT_TERMINAL_REASONS.get(reason)
        if known_reason is not None:
            return known_reason
    subtype = getattr(message, "subtype", None)
    if isinstance(subtype, str):
        known_subtype = _SDK_RESULT_SUBTYPES.get(subtype)
        if known_subtype is not None:
            return known_subtype
    status = getattr(message, "api_error_status", None)
    if isinstance(status, int) and status in _SDK_API_ERROR_STATUSES:
        return SdkResultCause.API_ERROR
    return SdkResultCause.OTHER


def _pre_marketplace_failure_receipt(
    evidence: AgentEvidence,
) -> PreMarketplaceFailureReceipt | None:
    """Create a receipt only for a failed full run before marketplace add."""

    attempted_stage = _exit_stage((*evidence.tool_intents, *evidence.tool_calls))
    if attempted_stage is not ExitStage.BEFORE_MARKETPLACE:
        return None
    if evidence.timed_out:
        kind = PreMarketplaceFailureKind.TIMEOUT
    elif evidence.sdk_exception_kind is not SdkExceptionKind.NONE:
        kind = PreMarketplaceFailureKind.SDK_EXCEPTION
    elif evidence.sdk_result_kind is SdkResultKind.ERROR:
        kind = PreMarketplaceFailureKind.SDK_RESULT_ERROR
    elif not evidence.result_seen:
        kind = PreMarketplaceFailureKind.MISSING_RESULT
    elif evidence.returncode != 0 or evidence.sdk_result_kind is not SdkResultKind.SUCCESS:
        # A clean SDK result is expected to mark ``is_error=False``.  A future
        # result shape is still diagnosed as a closed pre-marketplace outcome.
        kind = PreMarketplaceFailureKind.COMPLETED_BEFORE_MARKETPLACE
    else:
        # The process returned a normal result but did not add the marketplace.
        kind = PreMarketplaceFailureKind.COMPLETED_BEFORE_MARKETPLACE
    return PreMarketplaceFailureReceipt(
        kind=kind,
        stage=attempted_stage,
        sdk_exception=evidence.sdk_exception_kind,
        sdk_result=evidence.sdk_result_kind,
        sdk_result_cause=evidence.sdk_result_cause,
        sdk_cleanup=evidence.sdk_cleanup_kind,
        first_text=evidence.first_text_kind,
        first_tool_intent=evidence.tool_intents[0] if evidence.tool_intents else None,
        first_denied_tool_intent=(
            evidence.denied_tool_intents[0] if evidence.denied_tool_intents else None
        ),
    )


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


def _bash_action_argv(command: object) -> tuple[str, ...] | None:
    """Normalize shell quoting while rejecting wrappers and command chaining."""

    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(tokens) == 5 and tokens[:3] == ["script", "-q", "-c"] and tokens[4] == "/dev/null":
        try:
            tokens = shlex.split(tokens[3], posix=True)
        except ValueError:
            return None
    return tuple(tokens)


def _classify_bash_command(command: str, expected_new_chat_uri: str | None) -> ToolKind:
    tokens = _bash_action_argv(command)
    if tokens is None:
        return ToolKind.OTHER
    if "--no-browser" in tokens:
        return ToolKind.FORBIDDEN_BROWSER_MODE
    if expected_new_chat_uri is not None and tokens == ("xdg-open", expected_new_chat_uri):
        return ToolKind.NEW_CHAT_URI
    if tokens == ("claude", "mcp", "login", "plugin:sensai:sensai"):
        return ToolKind.LOGIN
    if tokens == ("claude", "plugin", "marketplace", "add", "blackvctr/sensai-plugin"):
        return ToolKind.MARKETPLACE_ADD
    if tokens == ("claude", "plugin", "install", "sensai@sensai", "--scope", "user"):
        return ToolKind.PLUGIN_INSTALL
    return ToolKind.OTHER


def _is_public_raw_url(value: object) -> bool:
    """Accept a read-only URL from the public repository, without query data."""

    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == _PUBLIC_RAW_HOST
        and parsed.path.startswith(_PUBLIC_RAW_PREFIX)
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def _is_allowed_oauth_entry_url(value: object) -> bool:
    """The browser may follow redirects, but its first opened URL is fixed."""

    if not isinstance(value, str) or len(value.encode("utf-8")) > 16 * 1024:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _OAUTH_ENTRY_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    if parsed.hostname == "black-vector.com":
        return parsed.path.startswith("/sensai")
    return parsed.hostname == "accounts.google.com"


def _is_direct_public_curl(command: object) -> bool:
    """Allow only a direct, read-only curl of one public repository URL."""

    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not tokens or tokens[0] != "curl":
        return False
    options = {
        "-s",
        "-S",
        "-L",
        "-f",
        "-fsSL",
        "-fsS",
        "-sSL",
        "--silent",
        "--show-error",
        "--location",
        "--fail",
    }
    if len(tokens) < 2 or any(token not in options for token in tokens[1:-1]):
        return False
    return _is_public_raw_url(tokens[-1])


def _is_public_metadata_compound_bash(command: object) -> bool:
    """Recognize, but never permit, a compound public-metadata read.

    Claude previously proposed a shell sequence containing only ``echo`` and
    direct public ``curl`` calls.  It is not a model refusal, but this runner
    intentionally permits only one direct metadata read at a time.  Keeping a
    separate closed category prevents that policy boundary from masquerading
    as an unexplained installation failure.
    """

    if not isinstance(command, str) or ";" not in command:
        return False
    has_public_curl = False
    for part in command.split(";"):
        stripped = part.strip()
        if not stripped:
            continue
        if _is_direct_public_curl(stripped):
            has_public_curl = True
            continue
        try:
            tokens = shlex.split(stripped, posix=True)
        except ValueError:
            return False
        if not tokens or tokens[0] != "echo":
            return False
    return has_public_curl


def _is_safe_metadata_inventory_curl(part: str) -> str | None:
    """Return one exact public metadata URL for a closed curl invocation."""

    try:
        tokens = shlex.split(part, posix=True)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0] != "curl":
        return None
    options = tokens[1:-1]
    if len(options) != len(set(options)) or any(
        option not in _SAFE_METADATA_CURL_FLAGS for option in options
    ):
        return None
    url = tokens[-1]
    return url if url in _PUBLIC_METADATA_INVENTORY_URLS else None


def _is_safe_public_metadata_inventory_bash(command: object) -> bool:
    """Match one inert, read-only inventory of the three fixed public files.

    The shell grammar is deliberately small: three distinct fixed ``curl``
    reads; at most two inert ``echo`` labels before each read; semicolons only
    as separators.  It rejects shell expansion, redirection, pipes, operators,
    newlines, duplicate flags, duplicate URLs, and every other command.  This
    permits harmless spelling changes without treating an arbitrary shell
    script as a metadata read.
    """

    if (
        not isinstance(command, str)
        or not command
        or len(command.encode("utf-8")) > MAX_TOOL_INPUT_BYTES
        or any(character in command for character in _UNSAFE_SHELL_GRAMMAR)
    ):
        return False
    parts = [part.strip() for part in command.split(";")]
    if not parts or any(not part for part in parts):
        return False

    urls: list[str] = []
    pending_echoes = 0
    for part in parts:
        if _INERT_ECHO.fullmatch(part):
            # An echo after the final read would be an unnecessary fourth
            # command position; keep the accepted language finite.
            if len(urls) == len(_PUBLIC_METADATA_INVENTORY_URLS):
                return False
            pending_echoes += 1
            if pending_echoes > 2:
                return False
            continue
        url = _is_safe_metadata_inventory_curl(part)
        if url is None or url in urls:
            return False
        urls.append(url)
        pending_echoes = 0

    return (
        len(urls) == len(_PUBLIC_METADATA_INVENTORY_URLS)
        and set(urls) == _PUBLIC_METADATA_INVENTORY_URLS
    )


@dataclass(frozen=True, slots=True)
class InstallationPermission:
    """A redacted decision for one permission request from Claude."""

    decision: PermissionDecision
    intent: ToolKind
    action: ToolKind | None = None


class InstallationPermissionPolicy:
    """Permit public reads and the published Claude installation sequence."""

    def __init__(
        self,
        *,
        new_chat_uri: str,
        claude_linux_actions: tuple[tuple[str, ...], ...],
        first_comparison: bool = False,
    ) -> None:
        self._first_comparison = first_comparison
        self._new_chat_uri = new_chat_uri
        self._metadata_inventory_seen = False
        self._install_action_seen = False
        if len(claude_linux_actions) != 4:
            raise ValueError("installation action manifest is invalid")
        action_kinds = (
            ToolKind.MARKETPLACE_ADD,
            ToolKind.PLUGIN_INSTALL,
            ToolKind.LOGIN,
            ToolKind.NEW_CHAT_URI,
        )
        self._login_wrapper = claude_linux_actions[2]
        normalized: dict[tuple[str, ...], ToolKind] = {}
        for action, kind in zip(claude_linux_actions, action_kinds, strict=True):
            shell_action = shlex.join(action)
            argv = _bash_action_argv(shell_action)
            if argv is None or argv in normalized:
                raise ValueError("installation action manifest is invalid")
            normalized[argv] = kind
        if normalized.get(("xdg-open", new_chat_uri)) is not ToolKind.NEW_CHAT_URI:
            raise ValueError("installation action manifest is invalid")
        self._actions = normalized
        self._action_order = action_kinds
        self._next_action_index = 0

    def _intent(self, tool_name: str, tool_input: object) -> ToolKind:
        if not isinstance(tool_input, dict):
            return ToolKind.OTHER
        if tool_name == "WebFetch" and _is_public_raw_url(tool_input.get("url")):
            if tool_input.get("url") == PUBLIC_README_URL:
                return ToolKind.PUBLIC_README_FETCH
            return ToolKind.PUBLIC_METADATA_FETCH
        if tool_name != "Bash":
            return ToolKind.OTHER
        command = tool_input.get("command")
        if _is_direct_public_curl(command):
            return ToolKind.PUBLIC_METADATA_BASH
        if _is_safe_public_metadata_inventory_bash(command):
            return ToolKind.PUBLIC_METADATA_INVENTORY_BASH
        if _is_public_metadata_compound_bash(command):
            return ToolKind.PUBLIC_METADATA_COMPOUND_BASH
        argv = _bash_action_argv(command)
        if argv is None:
            return ToolKind.OTHER
        return self._actions.get(argv, ToolKind.OTHER)

    def decide(self, tool_name: str, tool_input: object) -> InstallationPermission:
        intent = self._intent(tool_name, tool_input)
        if intent is ToolKind.PUBLIC_README_FETCH:
            return InstallationPermission(PermissionDecision.ALLOW, intent)
        if intent is ToolKind.PUBLIC_METADATA_FETCH:
            return InstallationPermission(
                PermissionDecision.DENY if self._first_comparison else PermissionDecision.ALLOW,
                intent,
            )
        if not isinstance(tool_input, dict) or tool_name != "Bash":
            return InstallationPermission(PermissionDecision.DENY, intent)
        command = tool_input.get("command")
        if intent is ToolKind.PUBLIC_METADATA_BASH:
            if self._first_comparison:
                return InstallationPermission(PermissionDecision.DENY, intent)
            return InstallationPermission(PermissionDecision.ALLOW, intent)
        if intent is ToolKind.PUBLIC_METADATA_INVENTORY_BASH:
            if self._first_comparison or self._metadata_inventory_seen or self._install_action_seen:
                return InstallationPermission(PermissionDecision.DENY, intent)
            self._metadata_inventory_seen = True
            return InstallationPermission(PermissionDecision.ALLOW, intent)
        if intent is ToolKind.PUBLIC_METADATA_COMPOUND_BASH:
            return InstallationPermission(PermissionDecision.DENY, intent)
        argv = _bash_action_argv(command)
        kind = self._actions.get(argv, ToolKind.OTHER) if argv is not None else ToolKind.OTHER
        if kind is ToolKind.LOGIN:
            if not isinstance(command, str):
                return InstallationPermission(PermissionDecision.DENY, intent)
            try:
                wrapper = tuple(shlex.split(command, posix=True))
            except ValueError:
                return InstallationPermission(PermissionDecision.DENY, intent)
            if wrapper != self._login_wrapper:
                return InstallationPermission(PermissionDecision.DENY, intent)
        if self._first_comparison:
            return InstallationPermission(PermissionDecision.DENY, intent)
        if kind in {
            ToolKind.MARKETPLACE_ADD,
            ToolKind.PLUGIN_INSTALL,
            ToolKind.LOGIN,
            ToolKind.NEW_CHAT_URI,
        }:
            if (
                self._next_action_index >= len(self._action_order)
                or kind is not self._action_order[self._next_action_index]
            ):
                return InstallationPermission(PermissionDecision.DENY, intent)
            self._next_action_index += 1
            self._install_action_seen = True
            return InstallationPermission(PermissionDecision.ALLOW, intent, kind)
        return InstallationPermission(PermissionDecision.DENY, intent)


def _force_permission_request(tool_use_id: object, observed: set[str]) -> dict[str, object]:
    """Make the SDK ask our callback even when its own rules would allow a tool."""

    if isinstance(tool_use_id, str) and tool_use_id:
        observed.add(tool_use_id)
    return {
        "continue_": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
        },
    }


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
    # Kept in the driver protocol for compatibility with transcript-only
    # callers.  The production acceptance does not compare prose to values
    # supplied by a README parser.
    del expected_visible_messages
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
                texts.append(text.evidence())
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
        return _is_exact_public_sensai_mcp_status(status)

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


def _owned_run_child_absent(run_root: Path) -> bool:
    """Confirm that no process remains with a working directory in this run."""

    try:
        approved_root = run_root.resolve(strict=True)
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                process_directory = (entry / "cwd").resolve(strict=True)
            except OSError:
                continue
            if process_directory.is_relative_to(approved_root):
                return False
    except OSError:
        return False
    return True


@dataclass(slots=True)
class _OAuthBrowserHandoff:
    """A disposable xdg-open bridge for exactly one normal OAuth browser flow."""

    work: Path
    environment: dict[str, str]
    expected_new_chat_uri: str
    marker: Path

    @classmethod
    def create(
        cls, work: Path, environment: dict[str, str], expected_new_chat_uri: str
    ) -> _OAuthBrowserHandoff:
        if not _BROWSER_TOOL.is_file():
            raise ProductionE2EError("oauth_browser_tool_unavailable")
        marker = work / ".sensai-oauth-browser-opened"
        bridge_dir = work / ".sensai-xdg-open"
        bridge_dir.mkdir(mode=0o700)
        bridge = bridge_dir / "xdg-open"
        bridge.write_text(
            "#!" + sys.executable + "\n"
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "from urllib.parse import urlsplit\n"
            f"browser_tool = {str(_BROWSER_TOOL)!r}\n"
            f"marker = {str(marker)!r}\n"
            f"new_chat_uri = {expected_new_chat_uri!r}\n"
            "oauth_hosts = {'black-vector.com', 'accounts.google.com'}\n"
            "if len(sys.argv) != 2:\n"
            "    raise SystemExit(64)\n"
            "target = sys.argv[1]\n"
            "if target == new_chat_uri:\n"
            "    raise SystemExit(0)\n"
            "parsed = urlsplit(target)\n"
            "try:\n"
            "    port = parsed.port\n"
            "except ValueError:\n"
            "    raise SystemExit(64)\n"
            "if (parsed.scheme != 'https' or parsed.hostname not in oauth_hosts\n"
            "        or port not in (None, 443) or parsed.username or parsed.password):\n"
            "    raise SystemExit(64)\n"
            "if parsed.hostname == 'black-vector.com' and not parsed.path.startswith('/sensai'):\n"
            "    raise SystemExit(64)\n"
            "with open(marker, 'x', encoding='ascii') as opened:\n"
            "    opened.write('opened\\n')\n"
            "result = subprocess.run(\n"
            "    [sys.executable, browser_tool, 'open_oauth_url_stdin'],\n"
            "    input=target.encode('utf-8'),\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            "    env=os.environ.copy(),\n"
            "    check=False,\n"
            ")\n"
            "raise SystemExit(result.returncode)\n",
            encoding="utf-8",
        )
        bridge.chmod(0o700)
        browser_environment = dict(environment)
        browser_environment["PATH"] = str(bridge_dir) + os.pathsep + environment.get("PATH", "")
        browser_environment["CODEX_BROWSER_PROJECT_DIR"] = str(work.parent)
        browser_environment["CODEX_BROWSER_SESSION_ID"] = f"sensai-install-{uuid.uuid4()}"
        browser_environment["CODEX_BROWSER_HEADLESS"] = "1"
        browser_environment["CODEX_BROWSER_CLONE_MODE"] = "light"
        return cls(work, browser_environment, expected_new_chat_uri, marker)

    def cleanup(self) -> bool:
        """Release only the fresh skill-managed browser session if OAuth opened it."""

        if not self.marker.exists():
            return True
        try:
            completed = subprocess.run(
                [sys.executable, str(_BROWSER_TOOL), "quit"],
                cwd=self.work,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=MCP_STATUS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0


class SdkClaudeDriver(SubprocessClaudeDriver):
    """Use the documented Agent SDK callback instead of CLI shell allow rules."""

    def __init__(self, *, first_comparison: bool = False) -> None:
        self._first_comparison = first_comparison

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
        if expected_new_chat_uri is None or not command or command[-1] == "":
            raise ProductionE2EError("sdk_installation_arguments_invalid")
        try:
            return asyncio.run(
                self._run_agent_async(
                    executable=str(command[0]),
                    prompt=str(command[-1]),
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    expected_visible_messages=expected_visible_messages,
                    expected_session=expected_session,
                    expected_new_chat_uri=expected_new_chat_uri,
                )
            )
        except RuntimeError as error:
            if "asyncio.run() cannot be called" in str(error):
                raise ProductionE2EError("sdk_event_loop_unavailable") from error
            raise

    async def _run_agent_async(
        self,
        *,
        executable: str,
        prompt: str,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
        expected_visible_messages: Sequence[str],
        expected_session: uuid.UUID,
        expected_new_chat_uri: str,
    ) -> AgentEvidence:
        # The caller's scenario is an acceptance boundary.  It is deliberately
        # not copied into Claude's prompt or compared as generated prose.
        del expected_visible_messages
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                HookMatcher,
                PermissionResultAllow,
                PermissionResultDeny,
                ResultMessage,
                TextBlock,
                ToolResultBlock,
                ToolUseBlock,
                UserMessage,
            )
        except ImportError as error:
            raise ProductionE2EError("claude_agent_sdk_unavailable") from error

        policy = InstallationPermissionPolicy(
            new_chat_uri=expected_new_chat_uri,
            claude_linux_actions=INSTALLATION_SCENARIO.claude_linux_actions,
            first_comparison=self._first_comparison,
        )
        handoff = (
            None
            if self._first_comparison
            else _OAuthBrowserHandoff.create(cwd, environment, expected_new_chat_uri)
        )
        driver_environment = handoff.environment if handoff is not None else environment
        text_blocks: list[TextEvidence] = []
        calls: list[ToolKind] = []
        intents: list[ToolKind] = []
        denied_intents: list[ToolKind] = []
        results: list[ToolResultEvidence] = []
        event_order: list[str] = []
        outstanding: dict[str, ToolKind] = {}
        pretool_ids: set[str] = set()
        result_seen = False
        session_verified = False
        terminal_error = False
        timed_out = False
        connection_verified = False
        plugin_verified = False
        first_text_kind = FirstTextKind.NONE
        sdk_exception_kind = SdkExceptionKind.NONE
        sdk_result_kind = SdkResultKind.NONE
        sdk_result_cause = SdkResultCause.NONE
        sdk_cleanup_kind = SdkCleanupKind.NONE

        async def force_permission(
            _hook_input: Any, tool_use_id: str | None, _hook_context: Any
        ) -> Any:
            return _force_permission_request(tool_use_id, pretool_ids)

        async def decide(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
            nonlocal connection_verified, plugin_verified
            tool_use_id = getattr(context, "tool_use_id", None)
            if not isinstance(tool_use_id, str) or tool_use_id not in pretool_ids:
                denied_intents.append(ToolKind.OTHER)
                return PermissionResultDeny(
                    message="Installation permission gate was not observed."
                )
            decision = policy.decide(tool_name, tool_input)
            intents.append(decision.intent)
            if decision.decision is PermissionDecision.DENY:
                denied_intents.append(decision.intent)
                return PermissionResultDeny(message="Not part of the Sensai installation flow.")
            if decision.action is ToolKind.NEW_CHAT_URI:
                if not any(
                    result.kind is ToolKind.LOGIN and result.succeeded for result in results
                ):
                    return PermissionResultDeny(message="Sensai sign-in has not completed.")
                connection_verified = await asyncio.to_thread(
                    self.mcp_configuration_observed,
                    _status_command(executable),
                    cwd=cwd,
                    environment=driver_environment,
                    timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
                )
                plugin_verified = await asyncio.to_thread(
                    self.public_sensai_plugin_installed,
                    _plugin_list_command(executable),
                    cwd=cwd,
                    environment=driver_environment,
                    timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
                )
                if not connection_verified or not plugin_verified:
                    return PermissionResultDeny(message="Sensai is not ready for the next chat.")
            if decision.action is not None:
                calls.append(decision.action)
                event_order.append(decision.action.value)
                outstanding[tool_use_id] = decision.action
            return PermissionResultAllow()

        options = ClaudeAgentOptions(
            cli_path=executable,
            cwd=cwd,
            env=driver_environment,
            model=CLAUDE_SONNET_5_MODEL,
            tools=["WebFetch", "Bash"],
            allowed_tools=[],
            permission_mode="default",
            can_use_tool=decide,
            strict_mcp_config=True,
            mcp_servers={},
            setting_sources=[],
            skills=[],
            plugins=[],
            hooks={"PreToolUse": [HookMatcher(matcher="Bash|WebFetch", hooks=[force_permission])]},
            session_id=str(expected_session),
            max_turns=16,
            stderr=lambda _line: None,
        )
        client = ClaudeSDKClient(options=options)
        disconnected = False
        child_absent = False
        try:

            async def receive() -> None:
                nonlocal first_text_kind, result_seen, session_verified, terminal_error
                nonlocal sdk_result_kind, sdk_result_cause
                await client.connect()
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                if first_text_kind is FirstTextKind.NONE:
                                    first_text_kind = _classify_first_text(block.text)
                                accumulator = _TextAccumulator.new()
                                accumulator.add(block.text)
                                text_blocks.append(accumulator.evidence())
                                event_order.append("visible")
                            elif isinstance(block, ToolUseBlock):
                                # The callback above is the only source of an allowed action.
                                del block
                    elif isinstance(message, UserMessage) and isinstance(message.content, list):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                kind = outstanding.pop(block.tool_use_id, None)
                                if kind is not None:
                                    results.append(
                                        ToolResultEvidence(kind, block.is_error is not True)
                                    )
                    elif isinstance(message, ResultMessage):
                        result_seen = True
                        session_verified = message.session_id == str(expected_session)
                        terminal_error = message.is_error
                        sdk_result_kind = _sdk_result_kind(message)
                        sdk_result_cause = _sdk_result_cause(message)

            try:
                await asyncio.wait_for(receive(), timeout=timeout_seconds)
            except TimeoutError:
                timed_out = True
                with suppress(Exception):
                    await client.interrupt()
        except Exception as error:
            terminal_error = True
            sdk_exception_kind = _classify_sdk_exception(error)
        finally:
            if timed_out:
                with suppress(Exception):
                    await client.interrupt()
            try:
                await client.disconnect()
            except Exception:
                sdk_cleanup_kind = SdkCleanupKind.DISCONNECT_FAILED
            else:
                disconnected = True
            browser_cleaned = handoff is None or handoff.cleanup()
            child_absent = _owned_run_child_absent(cwd.parent)
            if not child_absent:
                raise ProductionE2EError("claude_sdk_child_remained")
            if not browser_cleaned:
                raise ProductionE2EError("oauth_browser_cleanup_failed")

        successful = tuple(item.kind for item in results if item.succeeded)
        completed_cleanly = (
            result_seen and not terminal_error and not timed_out and disconnected and child_absent
        )
        return AgentEvidence(
            result_seen=result_seen,
            session_verified=session_verified,
            malformed=False,
            unclosed_block=False,
            stream_limit_exceeded=False,
            timed_out=timed_out,
            returncode=0 if completed_cleanly else 1,
            text_messages=tuple(text_blocks),
            tool_calls=tuple(calls),
            successful_tool_results=successful,
            tool_results=tuple(results),
            event_order=tuple(event_order),
            record_kinds=(),
            exit_category=(
                ExitCategory.CLEAN
                if result_seen and not terminal_error
                else ExitCategory.NONZERO_UNCLASSIFIED
            ),
            exit_stage=_exit_stage(successful),
            terminal_result_kind=TerminalResultKind.NONE,
            terminal_error_count=0,
            stderr_seen=False,
            sensai_connection_verified=connection_verified,
            public_sensai_plugin_installed=plugin_verified,
            tool_intents=tuple(intents),
            denied_tool_intents=tuple(denied_intents),
            first_text_kind=first_text_kind,
            sdk_exception_kind=sdk_exception_kind,
            sdk_result_kind=sdk_result_kind,
            sdk_result_cause=sdk_result_cause,
            sdk_cleanup_kind=sdk_cleanup_kind,
        )


def _is_exact_public_sensai_inventory(entries: object) -> bool:
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        return False
    sensai = [item for item in entries if str(item.get("id", "")).startswith("sensai@")]
    return (
        len(sensai) == 1
        and sensai[0].get("id") == INSTALLATION_SCENARIO.plugin_selector
        and sensai[0].get("scope") == "user"
        and sensai[0].get("enabled") is True
        and sensai[0].get("mcpServers")
        == {"sensai": {"type": "http", "url": INSTALLATION_SCENARIO.mcp_url}}
    )


def _is_exact_public_sensai_mcp_status(status: str) -> bool:
    """Accept only one exact configured public Sensai MCP endpoint."""

    if _STATUS_FAILURE.search(status) is not None:
        return False
    lines = [line.strip() for line in status.splitlines() if line.strip()]
    if lines.count(f"{INSTALLATION_SCENARIO.mcp_name}:") != 1:
        return False
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator != ":" or key not in {"Type", "URL"}:
            continue
        normalized = value.strip()
        if not normalized or key in fields:
            return False
        fields[key] = normalized
    return fields == {"Type": "http", "URL": INSTALLATION_SCENARIO.mcp_url}


def _new_chat_bash_command(new_chat_uri: str) -> str:
    return f"xdg-open {shlex.quote(new_chat_uri)}"


def _agent_command(
    executable: str,
    *,
    prompt: str,
    session: uuid.UUID,
) -> tuple[str, ...]:
    # This tuple is a closed description for injected unit drivers.  The
    # production path below uses the Agent SDK callback rather than handing the
    # CLI a string allowlist of shell spellings.
    command = (
        executable,
        "-p",
        "--model",
        CLAUDE_SONNET_5_MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--restricted",
        "--tools",
        "WebFetch,Bash",
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
        expected_public_readme_sha256: str,
        driver: ClaudeDriver | None = None,
        public_readme_validator: Callable[[str], str] = fetch_public_readme_sha256,
        executable_resolver: Callable[[], str] = resolve_installed_wsl_claude,
        first_comparison: bool = False,
    ) -> None:
        self._profile = profile
        self._expected_public_readme_sha256 = _validate_expected_readme_sha256(
            expected_public_readme_sha256
        )
        self._first_comparison = first_comparison
        self._driver = driver or SdkClaudeDriver(first_comparison=first_comparison)
        self._public_readme_validator = public_readme_validator
        self._executable_resolver = executable_resolver

    def run(self) -> ProductionE2EReport:
        """Run the full public-candidate acceptance path.

        The public page is checked as one exact candidate before execution.
        The prompt passed to Claude remains a fixed test input below.
        """

        if self._first_comparison:
            raise ProductionE2EError("full_installation_not_available_in_first_comparison")
        self._public_readme_validator(self._expected_public_readme_sha256)
        executable = self._executable_resolver()
        with create_fresh_run(self._profile) as run:
            return self._run_inside_fresh_profile(run, executable, uuid.uuid4())

    def compare_first_response(self) -> FirstComparisonReport:
        """Observe the first public-README reaction before effects are allowed.

        Only an exact WebFetch of the README is permitted.  Metadata Bash and
        every other proposed action are denied in the SDK callback and retained
        only as closed categories in the returned receipt.
        """

        if not self._first_comparison:
            raise ProductionE2EError("first_comparison_mode_required")
        digest = self._public_readme_validator(self._expected_public_readme_sha256)
        executable = self._executable_resolver()
        with create_fresh_run(self._profile) as run:
            if not self._driver.claude_authenticated(
                _auth_status_command(executable),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
            ):
                raise ProductionE2EError("isolated_claude_auth_not_verified")
            session = uuid.uuid4()
            evidence = self._driver.run_agent(
                _agent_command(
                    executable,
                    prompt=INSTALLATION_SCENARIO.prompt,
                    session=session,
                ),
                cwd=run.work,
                environment=run.environment,
                timeout_seconds=INSTALL_TIMEOUT_SECONDS,
                expected_visible_messages=(),
                expected_session=session,
                expected_new_chat_uri=INSTALLATION_SCENARIO.new_chat_uri,
            )
        if evidence.timed_out:
            raise ProductionE2EError("first_comparison_timed_out")
        if evidence.stream_limit_exceeded or evidence.malformed or evidence.unclosed_block:
            raise ProductionE2EError("first_comparison_stream_invalid")
        return FirstComparisonReport(
            public_readme_sha256=digest,
            first_text_kind=evidence.first_text_kind,
            first_tool_intent=evidence.tool_intents[0] if evidence.tool_intents else None,
            denied_tool_intents=evidence.denied_tool_intents,
        )

    def _run_inside_fresh_profile(
        self, run: ClaudeE2ERun, executable: str, session: uuid.UUID
    ) -> ProductionE2EReport:
        if not self._driver.claude_authenticated(
            _auth_status_command(executable),
            cwd=run.work,
            environment=run.environment,
            timeout_seconds=MCP_STATUS_TIMEOUT_SECONDS,
        ):
            raise ProductionE2EError("isolated_claude_auth_not_verified")
        new_chat_uri = INSTALLATION_SCENARIO.new_chat_uri
        installation = self._driver.run_agent(
            _agent_command(
                executable,
                prompt=INSTALLATION_SCENARIO.prompt,
                session=session,
            ),
            cwd=run.work,
            environment=run.environment,
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            expected_visible_messages=(),
            expected_session=session,
            expected_new_chat_uri=new_chat_uri,
        )
        try:
            self._require_installation(installation)
        except ProductionE2EError as error:
            receipt = _pre_marketplace_failure_receipt(installation)
            if receipt is not None:
                raise ProductionE2EError(str(error), before_marketplace_receipt=receipt) from error
            raise
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
        # The public README requires localized prose, but does not prescribe
        # its exact Russian wording or a fixed number of sentences before
        # sign-in.  It does require an explanation before the normal Google
        # sign-in and one completion message after the prepared new chat.
        if not evidence.text_messages:
            raise ProductionE2EError("installation_visible_message_count_invalid")
        if any(item.cyrillic_letters <= item.latin_letters for item in evidence.text_messages):
            raise ProductionE2EError("installation_visible_message_not_russian")
        if any(item.contains_code_block for item in evidence.text_messages):
            raise ProductionE2EError("installation_visible_message_contains_code_block")
        if any(item.contains_terminal_reference for item in evidence.text_messages):
            raise ProductionE2EError("installation_visible_message_mentions_terminal")
        if evidence.denied_tool_intents:
            raise ProductionE2EError(
                f"installation_permission_denied_{evidence.denied_tool_intents[0]}"
            )
        for kind in (ToolKind.MARKETPLACE_ADD, ToolKind.PLUGIN_INSTALL, ToolKind.LOGIN):
            if not evidence.has_successful(kind):
                raise ProductionE2EError(f"installation_{kind}_not_observed")
        if evidence.tool_calls.count(ToolKind.NEW_CHAT_URI) != 1:
            raise ProductionE2EError("installation_new_chat_uri_not_observed")
        if not evidence.sensai_connection_verified:
            raise ProductionE2EError("sensai_endpoint_configuration_not_verified")
        if not evidence.public_sensai_plugin_installed:
            raise ProductionE2EError("public_sensai_plugin_not_verified")
        if ToolKind.FORBIDDEN_BROWSER_MODE in evidence.tool_calls:
            raise ProductionE2EError("installation_no_browser_forbidden")
        expected_actions = (
            ToolKind.MARKETPLACE_ADD.value,
            ToolKind.PLUGIN_INSTALL.value,
            ToolKind.LOGIN.value,
            ToolKind.NEW_CHAT_URI.value,
        )
        if tuple(item for item in evidence.event_order if item != "visible") != expected_actions:
            raise ProductionE2EError("installation_event_order_invalid")
        if len(evidence.text_messages) != evidence.event_order.count("visible"):
            raise ProductionE2EError("installation_visible_message_count_invalid")
        login_index = evidence.event_order.index(ToolKind.LOGIN.value)
        new_chat_index = evidence.event_order.index(ToolKind.NEW_CHAT_URI.value)
        if (
            evidence.event_order[:2]
            != (ToolKind.MARKETPLACE_ADD.value, ToolKind.PLUGIN_INSTALL.value)
            or "visible" not in evidence.event_order[2:login_index]
            or evidence.event_order[new_chat_index + 1 :] != ("visible",)
        ):
            raise ProductionE2EError("installation_event_order_invalid")
