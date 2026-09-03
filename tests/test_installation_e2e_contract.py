from __future__ import annotations

from urllib.parse import urlencode

from sensai_plugin.installation_e2e_contract import (
    README_PATH,
    REQUIRED_CLAUDE_MODEL,
    ClaudeNewChatUriAttempt,
    ClaudeVisibleMessage,
    GoogleLoginCompleted,
    GoogleLoginStarted,
    InstallationTranscript,
    PublicReadmeContract,
    SensaiConnectionObserved,
    _public_contract_from_markdown,
    evaluate_installation_transcript,
)


def _public_contract() -> PublicReadmeContract:
    return _public_contract_from_markdown(README_PATH.read_text(encoding="utf-8"))


def _valid_transcript() -> InstallationTranscript:
    contract = _public_contract()
    return InstallationTranscript(
        public_prompt=contract.russian_install_prompt,
        model=REQUIRED_CLAUDE_MODEL,
        events=(
            ClaudeVisibleMessage(
                text="Я установлю Sensai сам. Выберите Google-аккаунт в открывшемся окне.",
            ),
            GoogleLoginStarted(),
            GoogleLoginCompleted(),
            SensaiConnectionObserved(connected=True),
            ClaudeVisibleMessage(
                text="Sensai готов. Открываю новый разговор для рабочей консультации.",
            ),
            ClaudeNewChatUriAttempt(
                uri="claude://code/new?" + urlencode({"q": contract.russian_new_chat_request}),
            ),
        ),
    )


def test_reads_the_current_public_russian_prompt_and_new_chat_request() -> None:
    contract = _public_contract()

    assert contract.russian_install_prompt.startswith("Установи Sensai ")
    assert "\n" not in contract.russian_install_prompt
    assert contract.russian_new_chat_request.startswith(
        "Проконсультируйся с Sensai."  # noqa: RUF001 - exact public Russian request
    )


def test_ignores_an_unrelated_russian_prompt_before_the_human_installation_section() -> None:
    markdown = README_PATH.read_text(encoding="utf-8")
    unrelated_prefix = "Russian:\n\n```text\nНе та строка\n```\n\n"  # noqa: RUF001 - fixture

    assert _public_contract_from_markdown(unrelated_prefix + markdown) == _public_contract()


def test_ignores_an_unrelated_russian_link_in_the_chatgpt_desktop_section() -> None:
    markdown = README_PATH.read_text(encoding="utf-8")
    unrelated_link = (
        "### ChatGPT Desktop\n\n"
        "- Russian: [Другой разговор](claude://code/new?q=%D0%94%D1%80%D1%83%D0%B3%D0%BE%D0%B9)\n"
    )
    altered = markdown.replace("### ChatGPT Desktop\n", unrelated_link, 1)

    assert _public_contract_from_markdown(altered) == _public_contract()


def test_structurally_complete_transcript_reports_the_current_readme_content_gap() -> None:
    report = evaluate_installation_transcript(_valid_transcript())

    assert report.failures == ("readme_canonical_visible_messages_missing",)


def test_rejects_a_stale_or_extended_public_prompt() -> None:
    transcript = _valid_transcript()
    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=(
                transcript.public_prompt
                + "\nСначала ответь по-русски."  # noqa: RUF001 - stale Russian second line
            ),
            model=transcript.model,
            events=transcript.events,
        )
    )

    assert report.failures == (
        "public_prompt_not_exact",
        "readme_canonical_visible_messages_missing",
    )


def test_rejects_any_model_other_than_claude_sonnet_5() -> None:
    transcript = _valid_transcript()
    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model="sonnet",
            events=transcript.events,
        )
    )

    assert report.failures == (
        "wrong_claude_model",
        "readme_canonical_visible_messages_missing",
    )


def test_rejects_english_visible_message_by_unicode_letters() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[0] = ClaudeVisibleMessage(
        text="I will install Sensai myself. Select your Google account.",
    )

    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == (
        "visible_message_not_russian",
        "readme_canonical_visible_messages_missing",
    )


def test_rejects_duplicate_google_login_even_when_everything_else_is_observed() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events.insert(2, GoogleLoginStarted())

    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == (
        "unsafe_event_order",
        "google_login_start_count_invalid",
    )


def test_rejects_a_third_user_directed_message() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events.insert(1, ClaudeVisibleMessage(text="Подождите, пожалуйста."))

    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == ("unsafe_event_order",)


def test_rejects_missing_or_unsuccessful_sensai_connection() -> None:
    transcript = _valid_transcript()
    events = tuple(
        event for event in transcript.events if not isinstance(event, SensaiConnectionObserved)
    )

    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=events,
        )
    )

    assert report.failures == ("unsafe_event_order", "sensai_connection_not_verified")


def test_rejects_a_different_russian_new_chat_request() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[-1] = ClaudeNewChatUriAttempt(
        uri="claude://code/new?"
        + urlencode(
            {"q": "Открой новый разговор с Sensai."}  # noqa: RUF001 - alternate Russian request
        ),
    )

    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == (
        "wrong_new_chat_uri",
        "readme_canonical_visible_messages_missing",
    )


def test_rejects_invisible_leading_or_trailing_whitespace_in_new_chat_request() -> None:
    transcript = _valid_transcript()
    request = _public_contract().russian_new_chat_request

    for changed_request in (f" {request}", f"{request} "):
        events = list(transcript.events)
        events[-1] = ClaudeNewChatUriAttempt(
            uri="claude://code/new?" + urlencode({"q": changed_request}),
        )
        report = evaluate_installation_transcript(
            InstallationTranscript(
                public_prompt=transcript.public_prompt,
                model=transcript.model,
                events=tuple(events),
            )
        )

        assert report.failures == (
            "wrong_new_chat_uri",
            "readme_canonical_visible_messages_missing",
        )


def test_rejects_a_malformed_claude_new_chat_uri_without_crashing() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[-1] = ClaudeNewChatUriAttempt(uri="claude://code/new?q")

    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == (
        "wrong_new_chat_uri",
        "readme_canonical_visible_messages_missing",
    )


def test_rejects_a_new_chat_uri_before_the_verified_connection() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[-2], events[-1] = events[-1], events[-2]

    report = evaluate_installation_transcript(
        InstallationTranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == ("unsafe_event_order",)
