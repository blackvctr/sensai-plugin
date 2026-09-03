from __future__ import annotations

from sensai_plugin.installation_e2e_contract import (
    PUBLIC_RUSSIAN_INSTALL_PROMPT,
    REQUIRED_CLAUDE_MODEL,
    ClaudeNewChatUriAttempt,
    ClaudeVisibleMessage,
    GoogleLoginCompleted,
    GoogleLoginStarted,
    InstallationE2ETranscript,
    SensaiConnectionObserved,
    evaluate_installation_e2e,
)


def _valid_transcript() -> InstallationE2ETranscript:
    return InstallationE2ETranscript(
        public_prompt=PUBLIC_RUSSIAN_INSTALL_PROMPT,
        model=REQUIRED_CLAUDE_MODEL,
        events=(
            ClaudeVisibleMessage(
                phase="authorization",
                text="Я установлю Sensai сам. Выберите Google-аккаунт в открывшемся окне.",
            ),
            GoogleLoginStarted(),
            GoogleLoginCompleted(),
            SensaiConnectionObserved(connected=True),
            ClaudeVisibleMessage(
                phase="ready",
                text="Sensai готов. Открываю новый разговор для рабочей консультации.",
            ),
            ClaudeNewChatUriAttempt(
                uri="claude://code/new?q=%D0%9F%D1%80%D0%BE%D0%BA%D0%BE%D0%BD%D1%81%D1%83%D0%BB%D1%8C%D1%82%D0%B8%D1%80%D1%83%D0%B9%D1%81%D1%8F%20%D1%81%20Sensai",
            ),
        ),
    )


def test_accepts_one_complete_russian_installation_path() -> None:
    assert evaluate_installation_e2e(_valid_transcript()).passed


def test_rejects_a_stale_or_extended_public_prompt() -> None:
    transcript = _valid_transcript()
    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=(
                transcript.public_prompt
                + "\nСначала ответь по-русски."  # noqa: RUF001 - stale Russian second line
            ),
            model=transcript.model,
            events=transcript.events,
        )
    )

    assert report.failures == ("public_prompt_not_exact",)


def test_rejects_any_model_other_than_claude_sonnet_5() -> None:
    transcript = _valid_transcript()
    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=transcript.public_prompt,
            model="sonnet",
            events=transcript.events,
        )
    )

    assert report.failures == ("wrong_claude_model",)


def test_rejects_english_visible_message_by_unicode_letters() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[0] = ClaudeVisibleMessage(
        phase="authorization",
        text="I will install Sensai myself. Select your Google account.",
    )

    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == ("authorization_message_not_russian",)


def test_rejects_duplicate_google_login_even_when_everything_else_is_good() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events.insert(2, GoogleLoginStarted())

    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == (
        "unsafe_event_order",
        "google_login_start_count_invalid",
    )


def test_rejects_missing_or_unsuccessful_sensai_connection() -> None:
    transcript = _valid_transcript()
    events = tuple(
        event for event in transcript.events if not isinstance(event, SensaiConnectionObserved)
    )

    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=events,
        )
    )

    assert report.failures == ("unsafe_event_order", "sensai_connection_not_verified")


def test_rejects_a_wrong_or_english_claude_new_chat_uri() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[-1] = ClaudeNewChatUriAttempt(uri="claude://code/new?q=Consult%20Sensai")

    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == ("wrong_new_chat_uri",)


def test_rejects_a_malformed_claude_new_chat_uri_without_crashing() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[-1] = ClaudeNewChatUriAttempt(uri="claude://code/new?q")

    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == ("wrong_new_chat_uri",)


def test_rejects_a_new_chat_uri_before_the_verified_connection() -> None:
    transcript = _valid_transcript()
    events = list(transcript.events)
    events[-2], events[-1] = events[-1], events[-2]

    report = evaluate_installation_e2e(
        InstallationE2ETranscript(
            public_prompt=transcript.public_prompt,
            model=transcript.model,
            events=tuple(events),
        )
    )

    assert report.failures == ("unsafe_event_order",)
