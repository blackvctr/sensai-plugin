"""Neutral public README contract and observable installation facts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sensai_plugin.controlled_installation_policy import (
    CONTROLLED_NEW_CHAT_URI,
    message_facts,
    message_facts_meet_contract,
)
from sensai_plugin.package_builder import plugin_version

CLAUDE_SONNET_5_MODEL = "claude-sonnet-5"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPOSITORY_ROOT / "README.md"
NEUTRAL_IDENTITY = (
    "Sensai is a local plugin for a person's AI assistant. It helps choose a useful connector "
    "or built-in tool for current work."
)
PUBLIC_README_URL = "https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md"
PUBLIC_PUBLISHER = "Black Vector"
PUBLIC_REPOSITORY = "https://github.com/blackvctr/sensai-plugin"
PUBLIC_PLUGIN_ID = "sensai"
PUBLIC_PLUGIN_VERSION = plugin_version(REPOSITORY_ROOT)
PUBLIC_README_TEMPLATE = f"""# Sensai

{NEUTRAL_IDENTITY}

Publisher: {PUBLIC_PUBLISHER}
Repository: {PUBLIC_REPOSITORY}
Plugin: {PUBLIC_PLUGIN_ID}
Version: {PUBLIC_PLUGIN_VERSION}

## Installation

Russian:

```text
Установи Sensai {PUBLIC_README_URL}
```

English:

```text
Install Sensai {PUBLIC_README_URL}
```
"""


@dataclass(frozen=True, slots=True)
class PublicReadmeContract:
    neutral_identity: str
    russian_install_prompt: str


@dataclass(frozen=True, slots=True)
class ClaudeVisibleMessage:
    text: str


@dataclass(frozen=True, slots=True)
class SensaiLoginStarted:
    pass


@dataclass(frozen=True, slots=True)
class SensaiLoginCompleted:
    pass


@dataclass(frozen=True, slots=True)
class SensaiConnectionObserved:
    connected: bool


@dataclass(frozen=True, slots=True)
class ClaudeNewChatUriAttempt:
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
    public_prompt: str
    model: str
    events: tuple[InstallationEvent, ...]


@dataclass(frozen=True, slots=True)
class InstallationTranscriptReport:
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def evaluate_installation_transcript(
    transcript: InstallationTranscript,
) -> InstallationTranscriptReport:
    contract = _load_public_readme_contract()
    failures: list[str] = []
    if transcript.public_prompt != contract.russian_install_prompt:
        failures.append("public_prompt_not_exact")
    if transcript.model != CLAUDE_SONNET_5_MODEL:
        failures.append("wrong_claude_model")
    expected_types = (
        ClaudeVisibleMessage,
        SensaiLoginStarted,
        SensaiLoginCompleted,
        ClaudeNewChatUriAttempt,
        ClaudeVisibleMessage,
        SensaiConnectionObserved,
    )
    if tuple(type(event) for event in transcript.events) != expected_types:
        failures.append("unsafe_event_order")
        _record_event_facts(transcript.events, failures)
        return InstallationTranscriptReport(tuple(failures))
    first, _, _, chat, second, connection = transcript.events
    assert isinstance(first, ClaudeVisibleMessage)
    assert isinstance(second, ClaudeVisibleMessage)
    assert isinstance(chat, ClaudeNewChatUriAttempt)
    assert isinstance(connection, SensaiConnectionObserved)
    if not all(message_facts_meet_contract(message_facts(item.text)) for item in (first, second)):
        failures.append("visible_message_not_russian")
    if not connection.connected:
        failures.append("sensai_connection_not_verified")
    if not _is_valid_new_chat_uri(chat.uri):
        failures.append("wrong_new_chat_uri")
    return InstallationTranscriptReport(tuple(failures))


def _record_event_facts(events: tuple[InstallationEvent, ...], failures: list[str]) -> None:
    if sum(isinstance(event, SensaiLoginStarted) for event in events) != 1:
        failures.append("google_login_start_count_invalid")
    if sum(isinstance(event, SensaiLoginCompleted) for event in events) != 1:
        failures.append("google_login_completion_count_invalid")
    connections = [event for event in events if isinstance(event, SensaiConnectionObserved)]
    if len(connections) != 1 or not connections[0].connected:
        failures.append("sensai_connection_not_verified")
    messages = [event for event in events if isinstance(event, ClaudeVisibleMessage)]
    if len(messages) != 2 or not all(
        message_facts_meet_contract(message_facts(item.text)) for item in messages
    ):
        failures.append("visible_message_not_russian")
    uris = [event for event in events if isinstance(event, ClaudeNewChatUriAttempt)]
    if len(uris) != 1 or not _is_valid_new_chat_uri(uris[0].uri):
        failures.append("wrong_new_chat_uri")


def _load_public_readme_contract() -> PublicReadmeContract:
    return _public_contract_from_markdown(README_PATH.read_text(encoding="utf-8"))


def _public_contract_from_markdown(markdown: str) -> PublicReadmeContract:
    if markdown != PUBLIC_README_TEMPLATE:
        raise ValueError("README differs from the neutral public template")
    return PublicReadmeContract(
        neutral_identity=NEUTRAL_IDENTITY,
        russian_install_prompt=f"Установи Sensai {PUBLIC_README_URL}",
    )


def _is_valid_new_chat_uri(uri: str) -> bool:
    return uri == CONTROLLED_NEW_CHAT_URI
