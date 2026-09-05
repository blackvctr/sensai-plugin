"""Deterministic evaluator for an observed Claude installation transcript.

This is deliberately not an E2E test.  A future runner must actually run
Claude, observe the local browser, and record the closed event sequence below.
This module only turns that observed sequence into reproducible pass/fail
evidence.  It never launches Claude, reads a browser profile, starts OAuth, or
contacts Sensai.
"""

from __future__ import annotations

import json
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
class SensaiLoginStarted:
    """Claude started its ordinary ``mcp login`` command for Sensai.

    This is deliberately an observation about the command and its protocol,
    not a claim about a particular browser window.  The production runner
    never reads, records, or interprets a browser screen, an OAuth URL, or an
    authorization code.
    """


@dataclass(frozen=True, slots=True)
class SensaiLoginCompleted:
    """The ordinary Sensai ``mcp login`` command completed successfully.

    A later ``mcp get`` status observation is still required: a successful
    command exit by itself is not evidence that the connection is usable.
    """


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
    | SensaiLoginStarted
    | SensaiLoginCompleted
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

    1. the exact authorization message before the normal Sensai login;
    2. one normal Sensai login command start and completion;
    3. one ``claude://code/new`` URI whose request exactly matches README;
    4. the exact ready message after that attempt; and
    5. a later safe endpoint-configuration observation.  The completed normal
       login and the first successful ``tell_sensai`` call prove service use;
       this later status check does not pretend to have happened before URI.

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
        SensaiLoginStarted,
        SensaiLoginCompleted,
        ClaudeNewChatUriAttempt,
        ClaudeVisibleMessage,
        SensaiConnectionObserved,
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

    authorization, _, _, chat_uri, ready, connection = transcript.events
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

    starts = sum(isinstance(event, SensaiLoginStarted) for event in events)
    completions = sum(isinstance(event, SensaiLoginCompleted) for event in events)
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

    manifest = _installation_manifest(lines)
    hosts = _manifest_object(manifest.get("hosts"), {"claude_desktop", "chatgpt_desktop"}, "hosts")
    claude = _manifest_object(hosts["claude_desktop"], {"russian"}, "claude_desktop")
    russian = _manifest_object(claude["russian"], {"visible_messages", "steps"}, "claude_desktop.russian")
    messages = _manifest_list(russian["visible_messages"], 2, "visible_messages")
    phases = ("before_google_sign_in", "after_new_chat_attempt")
    visible_messages: list[str] = []
    for item, phase in zip(messages, phases, strict=True):
        message = _manifest_object(item, {"phase", "text"}, "visible_message")
        if message["phase"] != phase or not isinstance(message["text"], str) or not message["text"]:
            raise ValueError("README visible message manifest is invalid")
        visible_messages.append(message["text"])

    claude_steps = _manifest_steps(
        russian["steps"],
        (
            ("marketplace_add", "claude plugin marketplace add blackvctr/sensai-plugin"),
            ("plugin_install", "claude plugin install sensai@sensai --scope user"),
            ("sensai_login", 'script -q -c "claude mcp login plugin:sensai:sensai" /dev/null'),
        ),
        "claude_desktop.russian.steps",
        extra_steps=1,
    )
    new_chat = _manifest_object(claude_steps[-1], {"kind", "request", "uri"}, "new_chat_uri")
    if new_chat["kind"] != "new_chat_uri" or not isinstance(new_chat["request"], str) or not isinstance(new_chat["uri"], str):
        raise ValueError("README new-chat manifest is invalid")
    uri_request = _new_chat_request(new_chat["uri"])
    if uri_request is None or uri_request != new_chat["request"]:
        raise ValueError("README new-chat manifest URI does not match its request")

    chatgpt = _manifest_object(hosts["chatgpt_desktop"], {"steps"}, "chatgpt_desktop")
    _manifest_steps(
        chatgpt["steps"],
        (
            ("marketplace_add", "codex plugin marketplace add blackvctr/sensai-plugin"),
            ("plugin_install", "codex plugin add sensai@sensai"),
            ("sensai_login", "codex mcp login sensai"),
        ),
        "chatgpt_desktop.steps",
    )

    return PublicReadmeContract(
        russian_install_prompt=prompt_lines[0],
        russian_authorization_message=visible_messages[0],
        russian_new_chat_request=uri_request,
        russian_ready_message=visible_messages[1],
    )


def _installation_manifest(lines: list[str]) -> dict[str, object]:
    section = _markdown_section(lines, "## Installation manifest")
    fence_indexes = [index for index, line in enumerate(section) if line == "```json"]
    if len(fence_indexes) != 1:
        raise ValueError("README installation manifest must contain exactly one JSON block")
    start = fence_indexes[0] + 1
    end = next((index for index in range(start, len(section)) if section[index] == "```"), None)
    if end is None or any(line == "```json" for line in section[start:end]):
        raise ValueError("README installation manifest JSON block is malformed")
    try:
        value = json.loads("\n".join(section[start:end]), object_pairs_hook=_strict_json_object)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("README installation manifest is not valid strict JSON") from error
    manifest = _manifest_object(value, {"schema", "hosts"}, "manifest")
    if manifest["schema"] != "sensai-install-v1":
        raise ValueError("README installation manifest schema is invalid")
    return manifest


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _manifest_object(value: object, expected_keys: set[str], location: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError(f"README manifest object is invalid: {location}")
    return value


def _manifest_list(value: object, length: int, location: str) -> list[object]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"README manifest list is invalid: {location}")
    return value


def _manifest_steps(
    value: object, expected: tuple[tuple[str, str], ...], location: str, *, extra_steps: int = 0
) -> list[object]:
    steps = _manifest_list(value, len(expected) + extra_steps, location)
    for item, (kind, command) in zip(steps[: len(expected)], expected, strict=True):
        step = _manifest_object(item, {"kind", "command"}, location)
        if step["kind"] != kind or step["command"] != command:
            raise ValueError(f"README manifest step is invalid: {location}")
    return steps


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
