"""Deterministic evaluator for an observed Claude installation transcript.

This is deliberately not an E2E test.  A future runner must actually run
Claude, observe the local browser, and record the closed event sequence below.
This module only turns that observed sequence into reproducible pass/fail
evidence.  It never launches Claude, reads a browser profile, starts OAuth, or
contacts Sensai.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unicodedata import category, name
from urllib.parse import parse_qsl, urlsplit

REQUIRED_CLAUDE_MODEL = "claude-sonnet-5"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPOSITORY_ROOT / "README.md"


@dataclass(frozen=True, slots=True)
class PublicReadmeContract:
    """The two executable Russian values published in the current README."""

    russian_install_prompt: str
    russian_new_chat_request: str


@dataclass(frozen=True, slots=True)
class ClaudeVisibleMessage:
    """One complete Claude message shown to the person during installation.

    The position in the closed event sequence gives it its role.  The runner
    cannot turn an arbitrary message into an authorization or readiness
    message simply by attaching a label.
    """

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
class InstallationTranscript:
    """Closed, redacted evidence from one installation attempt.

    The public request is recorded exactly because it is the entry point the
    person copies from README.  No browser profile, OAuth token, command
    output, or hidden Claude reasoning belongs in this transcript.
    """

    public_prompt: str
    model: str
    events: tuple[InstallationEvent, ...]


@dataclass(frozen=True, slots=True)
class InstallationTranscriptReport:
    """Deterministic result with machine-readable failure categories."""

    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def evaluate_installation_transcript(
    transcript: InstallationTranscript,
) -> InstallationTranscriptReport:
    """Validate the observable contract that a real E2E runner must provide.

    The decisive event sequence is intentionally short and closed:

    1. one user-directed message before Google consent;
    2. one real Google login start and completion;
    3. a successful Sensai connection observation;
    4. one user-directed ready message; and
    5. one ``claude://code/new`` URI whose request exactly matches README.

    Current README gives the purposes of the two messages but not their exact
    Russian text.  Unicode letters can prove that a message is predominantly
    Russian; they cannot prove that arbitrary Russian prose avoids manual
    terminal/software instructions.  Until README publishes both canonical
    messages, this evaluator deliberately reports that missing contract rather
    than claiming content safety from a word search.
    """

    failures: list[str] = []
    public_contract = _load_public_readme_contract()
    if transcript.public_prompt != public_contract.russian_install_prompt:
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
        _record_additional_event_failures(
            transcript.events,
            failures,
            russian_new_chat_request=public_contract.russian_new_chat_request,
        )
        return InstallationTranscriptReport(tuple(failures))

    authorization, _, _, connection, ready, chat_uri = transcript.events
    assert isinstance(authorization, ClaudeVisibleMessage)
    assert isinstance(connection, SensaiConnectionObserved)
    assert isinstance(ready, ClaudeVisibleMessage)
    assert isinstance(chat_uri, ClaudeNewChatUriAttempt)

    for message in (authorization, ready):
        if not _is_predominantly_cyrillic(message.text):
            failures.append("visible_message_not_russian")
    if not connection.connected:
        failures.append("sensai_connection_not_verified")
    if not _is_valid_new_chat_uri(chat_uri.uri, public_contract.russian_new_chat_request):
        failures.append("wrong_new_chat_uri")
    failures.append("readme_canonical_visible_messages_missing")

    return InstallationTranscriptReport(tuple(failures))


def _record_additional_event_failures(
    events: tuple[InstallationEvent, ...], failures: list[str], *, russian_new_chat_request: str
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
    if len(uri_attempts) != 1 or not _is_valid_new_chat_uri(
        uri_attempts[0].uri, russian_new_chat_request
    ):
        failures.append("wrong_new_chat_uri")


def _load_public_readme_contract() -> PublicReadmeContract:
    """Read the public Russian entry points instead of maintaining copies."""

    return _public_contract_from_markdown(README_PATH.read_text(encoding="utf-8"))


def _public_contract_from_markdown(markdown: str) -> PublicReadmeContract:
    """Extract exactly the Russian install prompt and new-chat request from README."""

    lines = markdown.splitlines()
    try:
        russian_heading = lines.index("Russian:")
    except ValueError as error:
        raise ValueError("README has no Russian installation prompt heading") from error

    prompt_fence = russian_heading + 1
    while prompt_fence < len(lines) and not lines[prompt_fence].strip():
        prompt_fence += 1
    if prompt_fence >= len(lines) or lines[prompt_fence] != "```text":
        raise ValueError("README Russian installation prompt is not a text code block")
    prompt_end = prompt_fence + 1
    while prompt_end < len(lines) and lines[prompt_end] != "```":
        prompt_end += 1
    prompt_lines = lines[prompt_fence + 1 : prompt_end]
    if prompt_end == len(lines) or len(prompt_lines) != 1 or not prompt_lines[0].strip():
        raise ValueError("README Russian installation prompt must be exactly one nonempty line")

    russian_link = next(
        (line for line in lines if line.startswith("- Russian: [")),
        None,
    )
    if russian_link is None:
        raise ValueError("README has no Russian Claude new-chat link")
    link_body = russian_link.removeprefix("- Russian: [")
    visible_request, separator, uri_tail = link_body.partition("](")
    if not separator or not uri_tail.endswith(")"):
        raise ValueError("README Russian Claude new-chat link is malformed")
    uri_request = _new_chat_request(uri_tail[:-1])
    if uri_request is None or visible_request != uri_request:
        raise ValueError("README Russian Claude new-chat link does not encode its visible request")

    return PublicReadmeContract(
        russian_install_prompt=prompt_lines[0],
        russian_new_chat_request=uri_request,
    )


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


def _is_valid_new_chat_uri(uri: str, expected_request: str) -> bool:
    """Accept only the exact ordinary Russian request published in README."""

    request = _new_chat_request(uri)
    return (
        request is not None
        and request == expected_request
        and not request.startswith("/")
        and _is_predominantly_cyrillic(request)
    )


def _new_chat_request(uri: str) -> str | None:
    """Decode one syntactically valid Claude new-chat URI."""

    parsed = urlsplit(uri)
    if (
        parsed.scheme != "claude"
        or parsed.netloc != "code"
        or parsed.path != "/new"
        or parsed.fragment
    ):
        return None
    try:
        parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if len(parameters) != 1 or parameters[0][0] != "q":
        return None
    request = parameters[0][1].strip()
    return request or None
