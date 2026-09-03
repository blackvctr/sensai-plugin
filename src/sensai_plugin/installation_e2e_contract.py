"""Deterministic acceptance rules for a recorded Claude installation run.

The eventual E2E runner is responsible for running Claude, observing the
browser, and producing the small transcript below.  This module deliberately
does none of those things: it turns that transcript into reproducible pass or
fail evidence without retaining credentials or conversation text outside the
two visible Claude messages needed for the check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from unicodedata import category, name
from urllib.parse import parse_qsl, urlsplit

PUBLIC_RUSSIAN_INSTALL_PROMPT = (
    "Установи Sensai https://raw.githubusercontent.com/blackvctr/"
    "sensai-plugin/main/README.md"
)
REQUIRED_CLAUDE_MODEL = "claude-sonnet-5"

VisibleMessagePhase = Literal["authorization", "ready"]


@dataclass(frozen=True, slots=True)
class ClaudeVisibleMessage:
    """One complete Claude message shown to the person during installation.

    ``phase`` is supplied by the runner from the observed installation stage;
    it is not inferred from the prose.  This keeps the evaluator deterministic
    and avoids pretending that a word search understands a person's request.
    """

    phase: VisibleMessagePhase
    text: str


@dataclass(frozen=True, slots=True)
class GoogleLoginStarted:
    """The real Google consent flow became visible in the local browser."""


@dataclass(frozen=True, slots=True)
class GoogleLoginCompleted:
    """The same Google consent flow returned successfully to Claude."""


@dataclass(frozen=True, slots=True)
class SensaiConnectionObserved:
    """Claude's installed Sensai connection was queried after Google consent."""

    connected: bool


@dataclass(frozen=True, slots=True)
class ClaudeNewChatUriAttempt:
    """Claude asked the local system to open the next ordinary conversation."""

    uri: str


type InstallationEvent = (
    ClaudeVisibleMessage
    | GoogleLoginStarted
    | GoogleLoginCompleted
    | SensaiConnectionObserved
    | ClaudeNewChatUriAttempt
)


@dataclass(frozen=True, slots=True)
class InstallationE2ETranscript:
    """Closed, redacted evidence from one installation attempt.

    The public request is recorded exactly because it is the entry point the
    person copies from README.  No browser profile, OAuth token, command
    output, or hidden Claude reasoning belongs in this transcript.
    """

    public_prompt: str
    model: str
    events: tuple[InstallationEvent, ...]


@dataclass(frozen=True, slots=True)
class InstallationE2EReport:
    """Deterministic result with machine-readable failure categories."""

    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def evaluate_installation_e2e(transcript: InstallationE2ETranscript) -> InstallationE2EReport:
    """Validate the observable contract for the production installation E2E.

    The decisive event sequence is intentionally short and closed:

    1. one Russian authorization message;
    2. one real Google login start and completion;
    3. a successful Sensai connection observation;
    4. one Russian ready message; and
    5. one well-formed Russian ``claude://code/new`` URI attempt.

    It does not prescribe either Russian sentence.  README specifies their
    purpose, not exact prose, so forcing a sentence here would make the test
    brittle for no product reason.
    """

    failures: list[str] = []
    if transcript.public_prompt != PUBLIC_RUSSIAN_INSTALL_PROMPT:
        failures.append("public_prompt_not_exact")
    if transcript.model != REQUIRED_CLAUDE_MODEL:
        failures.append("wrong_claude_model")

    expected_event_types = (
        ClaudeVisibleMessage,
        GoogleLoginStarted,
        GoogleLoginCompleted,
        SensaiConnectionObserved,
        ClaudeVisibleMessage,
        ClaudeNewChatUriAttempt,
    )
    actual_event_types = tuple(type(event) for event in transcript.events)
    if actual_event_types != expected_event_types:
        failures.append("unsafe_event_order")
        _record_additional_event_failures(transcript.events, failures)
        return InstallationE2EReport(tuple(failures))

    authorization, _, _, connection, ready, chat_uri = transcript.events
    assert isinstance(authorization, ClaudeVisibleMessage)
    assert isinstance(connection, SensaiConnectionObserved)
    assert isinstance(ready, ClaudeVisibleMessage)
    assert isinstance(chat_uri, ClaudeNewChatUriAttempt)

    if authorization.phase != "authorization" or ready.phase != "ready":
        failures.append("unsafe_event_order")
    for message in (authorization, ready):
        if not _is_predominantly_cyrillic(message.text):
            failures.append(f"{message.phase}_message_not_russian")
    if not connection.connected:
        failures.append("sensai_connection_not_verified")
    if not _is_valid_russian_new_chat_uri(chat_uri.uri):
        failures.append("wrong_new_chat_uri")

    return InstallationE2EReport(tuple(failures))


def _record_additional_event_failures(
    events: tuple[InstallationEvent, ...], failures: list[str]
) -> None:
    """Preserve concrete missing/duplicate evidence beside an order failure."""

    starts = sum(isinstance(event, GoogleLoginStarted) for event in events)
    completions = sum(isinstance(event, GoogleLoginCompleted) for event in events)
    if starts != 1:
        failures.append("google_login_start_count_invalid")
    if completions != 1:
        failures.append("google_login_completion_count_invalid")
    connections = [event for event in events if isinstance(event, SensaiConnectionObserved)]
    if len(connections) != 1 or not connections[0].connected:
        failures.append("sensai_connection_not_verified")
    uri_attempts = [event for event in events if isinstance(event, ClaudeNewChatUriAttempt)]
    if len(uri_attempts) != 1 or not _is_valid_russian_new_chat_uri(uri_attempts[0].uri):
        failures.append("wrong_new_chat_uri")


def _is_predominantly_cyrillic(text: str) -> bool:
    """Classify visible language from Unicode letter code points alone.

    URLs and product names count as Latin when Claude writes them in a visible
    message.  The acceptance therefore requires an actual Russian sentence,
    rather than looking for a convenient Russian word among English prose.
    """

    cyrillic = 0
    latin = 0
    for character in text:
        if not category(character).startswith("L"):
            continue
        unicode_name = name(character, "")
        if "CYRILLIC" in unicode_name:
            cyrillic += 1
        elif "LATIN" in unicode_name:
            latin += 1
    return cyrillic > latin and cyrillic > 0


def _is_valid_russian_new_chat_uri(uri: str) -> bool:
    """Accept one ordinary Russian Claude new-chat request, never a command."""

    parsed = urlsplit(uri)
    if (
        parsed.scheme != "claude"
        or parsed.netloc != "code"
        or parsed.path != "/new"
        or parsed.fragment
    ):
        return False
    try:
        parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    if len(parameters) != 1 or parameters[0][0] != "q":
        return False
    request = parameters[0][1].strip()
    return bool(request) and not request.startswith("/") and _is_predominantly_cyrillic(request)
