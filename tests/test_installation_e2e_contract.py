from __future__ import annotations

import pytest

from sensai_plugin.controlled_installation_policy import CONTROLLED_NEW_CHAT_URI
from sensai_plugin.installation_e2e_contract import (
    CLAUDE_SONNET_5_MODEL,
    NEUTRAL_IDENTITY,
    PUBLIC_README_TEMPLATE,
    README_PATH,
    ClaudeNewChatUriAttempt,
    ClaudeVisibleMessage,
    InstallationTranscript,
    PublicReadmeContract,
    SensaiConnectionObserved,
    SensaiLoginCompleted,
    SensaiLoginStarted,
    _public_contract_from_markdown,
    evaluate_installation_transcript,
)


def _public_contract() -> PublicReadmeContract:
    return _public_contract_from_markdown(README_PATH.read_text(encoding="utf-8"))


def _new_chat_uri() -> str:
    return CONTROLLED_NEW_CHAT_URI


def _valid_transcript() -> InstallationTranscript:
    return InstallationTranscript(
        public_prompt=_public_contract().russian_install_prompt,
        model=CLAUDE_SONNET_5_MODEL,
        events=(
            ClaudeVisibleMessage(text="Сейчас установлю Sensai."),
            SensaiLoginStarted(),
            SensaiLoginCompleted(),
            ClaudeNewChatUriAttempt(uri=_new_chat_uri()),
            ClaudeVisibleMessage(text="Подключение готово."),
            SensaiConnectionObserved(connected=True),
        ),
    )


def test_reads_neutral_identity_and_exact_public_russian_prompt() -> None:
    contract = _public_contract()

    assert contract.neutral_identity == NEUTRAL_IDENTITY
    assert contract.russian_install_prompt.startswith("Установи Sensai ")
    assert "\n" not in contract.russian_install_prompt


def test_public_readme_is_the_exact_neutral_template() -> None:
    markdown = README_PATH.read_text(encoding="utf-8")

    assert markdown == PUBLIC_README_TEMPLATE
    assert "Publisher: Black Vector" in markdown
    assert "Repository: https://github.com/blackvctr/sensai-plugin" in markdown
    assert "Plugin: sensai" in markdown
    assert "Version: 0.2.13" in markdown


def test_rejects_any_extra_public_readme_content() -> None:
    with pytest.raises(ValueError):
        _public_contract_from_markdown(PUBLIC_README_TEMPLATE + "\nExtra content.\n")


@pytest.mark.parametrize(
    "replacement",
    (
        "Another product identity.",
        "Sensai is a local plugin.\n\n```text\nУстанови Sensai https://example.test/readme\n```",  # noqa: RUF001
    ),
)
def test_rejects_a_readme_without_neutral_identity_or_public_prompt(replacement: str) -> None:
    markdown = README_PATH.read_text(encoding="utf-8")
    if replacement.startswith("Another"):
        markdown = markdown.replace(NEUTRAL_IDENTITY, replacement)
    else:
        markdown = replacement

    with pytest.raises(ValueError):
        _public_contract_from_markdown(markdown)


def test_rejects_prompt_outside_its_one_line_text_block() -> None:
    markdown = README_PATH.read_text(encoding="utf-8").replace(
        "```text\nУстанови",  # noqa: RUF001
        "```\nУстанови",  # noqa: RUF001
    )

    with pytest.raises(ValueError):
        _public_contract_from_markdown(markdown)


def test_accepts_observed_russian_installation_path_without_forced_replies() -> None:
    report = evaluate_installation_transcript(_valid_transcript())

    assert report.passed


def test_rejects_a_stale_or_extended_public_prompt() -> None:
    transcript = _valid_transcript()
    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt + "\nДополнение",  # noqa: RUF001
            model=transcript.model,
            events=transcript.events,
        )
    )

    assert report.failures == ("public_prompt_not_exact",)


def test_rejects_another_model() -> None:
    transcript = _valid_transcript()
    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model="sonnet",
            events=transcript.events,
        )
    )

    assert report.failures == ("wrong_claude_model",)


def test_rejects_english_visible_message_by_unicode_letters() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[0] = ClaudeVisibleMessage(text="I will install Sensai now.")

    report = evaluate_installation_transcript(
        InstallationTranscript(transcript.public_prompt, transcript.model, tuple(events))
    )

    assert report.failures == ("visible_message_not_russian",)


def test_rejects_duplicate_login_and_wrong_event_order() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events.insert(2, SensaiLoginStarted())

    report = evaluate_installation_transcript(
        InstallationTranscript(transcript.public_prompt, transcript.model, tuple(events))
    )

    assert report.failures == ("unsafe_event_order", "google_login_start_count_invalid")


def test_rejects_unverified_connection() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[-1] = SensaiConnectionObserved(connected=False)

    report = evaluate_installation_transcript(
        InstallationTranscript(transcript.public_prompt, transcript.model, tuple(events))
    )

    assert report.failures == ("sensai_connection_not_verified",)


@pytest.mark.parametrize(
    "uri",
    (
        "claude://code/new?q",
        "claude://code/new?q=English",
        "claude://code/new?q=%D0%A2%D0%B5%D1%81%D1%82",
        "https://x",
    ),
)
def test_rejects_any_new_chat_uri_other_than_local_canonical_uri(uri: str) -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[3] = ClaudeNewChatUriAttempt(uri=uri)

    report = evaluate_installation_transcript(
        InstallationTranscript(transcript.public_prompt, transcript.model, tuple(events))
    )

    assert report.failures == ("wrong_new_chat_uri",)


def test_rejects_markdown_code_in_a_visible_message() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[0] = ClaudeVisibleMessage(text="`Готово`.")

    report = evaluate_installation_transcript(
        InstallationTranscript(transcript.public_prompt, transcript.model, tuple(events))
    )

    assert report.failures == ("visible_message_not_russian",)
