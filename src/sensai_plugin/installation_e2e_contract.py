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

# This is the one model identifier used by every Claude compatibility check.
# Keep the value here because this module owns the observed installation contract.
CLAUDE_SONNET_5_MODEL = "claude-sonnet-5"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPOSITORY_ROOT / "README.md"


@dataclass(frozen=True, slots=True)
class PublicReadmeContract:
    """Russian installation values that the public README makes exact."""

    russian_install_prompt: str
    russian_authorization_message: str
    russian_new_chat_request: str
    russian_ready_message: str


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

    1. the exact authorization message before any Google consent;
    2. one real Google login start and completion;
    3. a successful Sensai connection observation;
    4. one ``claude://code/new`` URI whose request exactly matches README; and
    5. the exact ready message after that attempt.

    The URI event proves only that Claude asked the local system to open the
    published link. It deliberately does not claim that Claude Desktop made a
    new window visible.
    """

    failures: list[str] = []
    public_contract = _load_public_readme_contract()
    if transcript.public_prompt != public_contract.russian_install_prompt:
        failures.append("public_prompt_not_exact")
    if transcript.model != CLAUDE_SONNET_5_MODEL:
        failures.append("wrong_claude_model")

    expected_event_types = (
        ClaudeVisibleMessage,
        GoogleLoginStarted,
        GoogleLoginCompleted,
        SensaiConnectionObserved,
        ClaudeNewChatUriAttempt,
        ClaudeVisibleMessage,
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

    authorization, _, _, connection, chat_uri, ready = transcript.events
    assert isinstance(authorization, ClaudeVisibleMessage)
    assert isinstance(connection, SensaiConnectionObserved)
    assert isinstance(ready, ClaudeVisibleMessage)
    assert isinstance(chat_uri, ClaudeNewChatUriAttempt)

    if authorization.text != public_contract.russian_authorization_message:
        failures.append("authorization_message_not_exact")
    if ready.text != public_contract.russian_ready_message:
        failures.append("ready_message_not_exact")
    for message in (authorization, ready):
        if not _is_predominantly_cyrillic(message.text):
            failures.append("visible_message_not_russian")
    if not connection.connected:
        failures.append("sensai_connection_not_verified")
    if not _is_valid_new_chat_uri(chat_uri.uri, public_contract.russian_new_chat_request):
        failures.append("wrong_new_chat_uri")
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
    human_installation = _markdown_section(lines, "## Installation (if you are human)")
    russian_heading = _line_index(human_installation, "Russian:")

    prompt_fence = russian_heading + 1
    while prompt_fence < len(human_installation) and not human_installation[prompt_fence].strip():
        prompt_fence += 1
    if prompt_fence >= len(human_installation) or human_installation[prompt_fence] != "```text":
        raise ValueError("README Russian installation prompt is not a text code block")
    prompt_end = prompt_fence + 1
    while prompt_end < len(human_installation) and human_installation[prompt_end] != "```":
        prompt_end += 1
    prompt_lines = human_installation[prompt_fence + 1 : prompt_end]
    if (
        prompt_end == len(human_installation)
        or len(prompt_lines) != 1
        or not prompt_lines[0].strip()
    ):
        raise ValueError("README Russian installation prompt must be exactly one nonempty line")

    agent_installation = _markdown_section(
        lines,
        "## Installation after explicit request (AI agent part)",
    )
    claude_desktop = _markdown_subsection(agent_installation, "### Claude Desktop")
    russian_link = next(
        (line for line in claude_desktop if line.startswith("- Russian: [")),
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

    canonical_messages = _markdown_subsubsection(
        claude_desktop,
        "#### Exact Russian messages for a successful Claude installation",
    )
    authorization_message = _marked_text_block(
        canonical_messages,
        "**First visible message — before any action and Google sign-in:**",
    )
    ready_message = _marked_text_block(
        canonical_messages,
        (
            "**Second visible message — after Sensai is connected and after "
            "attempting to open the new-chat link above:**"
        ),
    )

    return PublicReadmeContract(
        russian_install_prompt=prompt_lines[0],
        russian_authorization_message=authorization_message,
        russian_new_chat_request=uri_request,
        russian_ready_message=ready_message,
    )


def _markdown_section(lines: list[str], heading: str) -> list[str]:
    """Return one level-two Markdown section, excluding neighboring sections."""

    start = _line_index(lines, heading)
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return lines[start + 1 : end]


def _markdown_subsection(lines: list[str], heading: str) -> list[str]:
    """Return one level-three Markdown subsection, excluding sibling hosts."""

    start = _line_index(lines, heading)
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("### ")),
        len(lines),
    )
    return lines[start + 1 : end]


def _markdown_subsubsection(lines: list[str], heading: str) -> list[str]:
    """Return one level-four Markdown subsection, excluding later siblings."""

    start = _line_index(lines, heading)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith(("## ", "### ", "#### "))
        ),
        len(lines),
    )
    return lines[start + 1 : end]


def _marked_text_block(lines: list[str], marker: str) -> str:
    """Read one exact one-line text block following a readable README marker."""

    marker_index = _line_index(lines, marker)
    fence = marker_index + 1
    while fence < len(lines) and not lines[fence].strip():
        fence += 1
    if fence >= len(lines) or lines[fence] != "```text":
        raise ValueError(f"README marker has no text block: {marker}")
    end = fence + 1
    while end < len(lines) and lines[end] != "```":
        end += 1
    block_lines = lines[fence + 1 : end]
    if end == len(lines) or len(block_lines) != 1 or not block_lines[0].strip():
        raise ValueError(f"README marker must have one nonempty line: {marker}")
    return block_lines[0]


def _line_index(lines: list[str], expected: str) -> int:
    """Locate one exact structural heading with a clear source error."""

    try:
        return lines.index(expected)
    except ValueError as error:
        raise ValueError(f"README has no expected heading: {expected}") from error


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
    request = parameters[0][1]
    return request or None
