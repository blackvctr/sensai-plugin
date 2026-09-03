from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from sensai_plugin.claude_first_reply import (
    _SCENARIO_PROMPTS,
    FirstReplyScenario,
)
from sensai_plugin.package_builder import (
    BuiltPackages,
    plugin_version,
)
from sensai_plugin.package_builder import (
    build_packages as _build_packages,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = REPOSITORY_ROOT / "payload-src/shared/skills/sensai/SKILL.md"
PACKAGED_SKILL = REPOSITORY_ROOT / "plugins/sensai/skills/sensai/SKILL.md"


def build_packages(*, source_root: Path, output_root: Path) -> BuiltPackages:
    return _build_packages(
        source_root=source_root,
        output_root=output_root,
        version=plugin_version(REPOSITORY_ROOT),
    )


def test_public_payload_is_built_from_the_single_skill_source() -> None:
    assert PACKAGED_SKILL.read_bytes() == SOURCE_SKILL.read_bytes()


def _assert_post_install_consultation_contract(text: str) -> None:
    """Keep the concise skill focused on the conversation after installation."""

    normalized = " ".join(text.lower().split())

    first_call = normalized.index("tell_sensai")
    assert all(
        normalized.index(readiness) < first_call
        for readiness in ("installed", "authorized", "loaded")
    )

    # First call: the person's stated request and facts are the consultation input.
    assert "stated request" in normalized
    assert "stated work facts" in normalized

    # Sensai deliberately opens with this fixed discovery question.  One complete
    # follow-up must carry facts already present in the person's opening message.
    assert "fixed question" in normalized
    assert "role" in normalized
    assert "apps" in normalized
    assert "sites" in normalized
    assert "recurring" in normalized
    assert "one follow-up" in normalized
    assert "person's request" in normalized
    assert "complete work context" in normalized
    assert "opening message" in normalized
    assert "these facts" in normalized

    # The next recommendation needs an observed result, not a guess.
    assert "meaningful action" in normalized
    assert "confirmed outcome" in normalized

    # Sensai may receive concise English, but the person receives their language.
    assert "speak to the person" in normalized
    assert "their language" in normalized
    assert "send quotes or equivalent direct translations" in normalized

    # Sensitive information stays outside the consultation.
    assert "sensitive information" in normalized
    assert "environment variables" in normalized
    assert "api tokens" in normalized

    # The post-installation skill no longer owns setup intent or host-specific
    # recovery.  Those rules belonged to the old, much larger skill.
    assert "guidance_request" not in normalized
    assert "codex mcp login" not in normalized
    assert "claude mcp login" not in normalized
    assert "official claude cli" not in normalized


def _assert_claude_recovery_audience(text: str) -> None:
    """README recovery keeps terminal work with the agent, not its user."""

    normalized = " ".join(text.lower().split())
    assert "path" in normalized
    assert "official claude cli" in normalized
    assert "install" in normalized
    assert "recheck" in normalized
    assert "yourself" in normalized or "your own session" in normalized
    assert "explicit consent" in normalized
    assert "elevated" in normalized or "sandbox-disabling" in normalized
    assert (
        "recover from these yourself" in normalized
        or (
            "perform path diagnosis, official claude cli installation, and the "
            "post-installation recheck yourself"
        )
        in normalized
    )
    assert "ask the person to run" not in normalized
    assert "tell the person to run" not in normalized


def _read_section(readme: str, heading: str, next_heading: str) -> str:
    section = readme.split(heading, maxsplit=1)[1]
    return section if not next_heading else section.split(next_heading, maxsplit=1)[0]


def test_readme_has_one_general_auth_explanation_and_native_login_per_host() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    general_steps = _read_section(readme, "### General steps", "### ChatGPT Desktop")
    chatgpt_path = _read_section(readme, "### ChatGPT Desktop", "### Claude Desktop")
    claude_path = _read_section(readme, "### Claude Desktop", "#### Known problems")

    general = " ".join(general_steps.lower().split())
    assert "google sign-in" in general
    assert "relevant context" in general
    assert "new chats" in general
    assert "chooses their google account" in general
    assert "codex mcp login sensai" in chatgpt_path
    assert "claude mcp login plugin:sensai:sensai" in claude_path


def test_readme_conditions_russian_replies_on_the_person_using_russian() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    general_steps = _read_section(readme, "### General steps", "### ChatGPT Desktop")
    normalized = " ".join(general_steps.split())

    assert "If they wrote in Russian, you answer in Russian." in normalized
    assert ". They wrote in Russian, you answer in Russian." not in normalized


def test_readme_documents_the_two_exact_russian_claude_messages_in_order() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    claude_section = _read_section(readme, "### Claude Desktop", "#### Known problems")
    first_marker = "**First visible message — before any action and Google sign-in:**"
    second_marker = (
        "**Second visible message — after Sensai is connected and after "
        "attempting to open the new-chat link above:**"
    )
    first_message = (
        "Я сам установлю Sensai. Сейчас откроется обычное окно Google: выберите свой "
        "аккаунт и подтвердите доступ, чтобы Sensai мог продолжать этот рабочий разговор "
        "в следующих чатах."
    )
    second_message = (
        "Sensai установлен. Я попытался открыть новый разговор с подготовленным сообщением. "  # noqa: RUF001 - exact published Russian message
        "Если он появился, нажмите Enter."
    )

    # These literals deliberately do not use the README parser.  A parser and
    # its fixture could otherwise drift together while the published promises
    # change or move to the wrong stage of the installation.
    assert "Before calling any tool or taking an installation step" in readme
    assert "use exactly the two Russian messages" in readme
    assert "in the stated order" in readme
    assert "use the second exact Russian message below" in claude_section
    assert claude_section.index(first_marker) < claude_section.index(second_marker)
    assert f"{first_marker}\n\n```text\n{first_message}\n```" in claude_section
    assert f"{second_marker}\n\n```text\n{second_message}\n```" in claude_section

    first_message_index = claude_section.index(first_message)
    login_index = claude_section.index("claude mcp login plugin:sensai:sensai")
    new_chat_index = claude_section.index("Then open a new Claude Code session")
    second_message_index = claude_section.index(second_message)
    assert first_message_index < login_index < new_chat_index < second_message_index


def test_public_copy_paste_prompts_remain_one_line_and_russian_matches_the_harness() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    russian_prompt = (
        "Установи Sensai https://raw.githubusercontent.com/blackvctr/"
        "sensai-plugin/main/README.md"
    )
    english_prompt = (
        "Install Sensai https://raw.githubusercontent.com/blackvctr/"
        "sensai-plugin/main/README.md"
    )

    assert f"```text\n{russian_prompt}\n```" in readme
    assert f"```\n{english_prompt}\n```" in readme
    assert _SCENARIO_PROMPTS[FirstReplyScenario.URL_BOOTSTRAP] == russian_prompt


def test_readme_requires_explicit_install_request_and_keeps_credentials_out_of_chat() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Installation after explicit request (AI agent part)" in readme
    assert "tokens, and credentials stay in the provider and browser flow" in readme
    assert "never enter the conversation or another tool" in readme


def test_readme_uses_a_waited_hidden_powershell_claude_login() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    claude_path = _read_section(readme, "### Claude Desktop", "#### Known problems")
    login_path = _read_section(
        claude_path,
        "Sign-in needs a terminal on its input",
        "Then open the new session yourself",
    )

    assert "# Windows\n" not in login_path
    assert "# Windows, PowerShell" in login_path
    assert (
        "$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')"
        in login_path
    )
    assert "if ($userPath) { $env:Path = \"$userPath;$env:Path\" }" in login_path
    assert (
        "$claude = (Get-Command claude.exe -CommandType Application -ErrorAction Stop).Source"
        in login_path
    )
    assert "C:\\Users\\" not in login_path
    assert (
        "$command = '\"\"{0}\" mcp login plugin:sensai:sensai\"' -f $claude"
        in login_path
    )
    assert (
        "$login = Start-Process -FilePath $env:ComSpec" in login_path
    )
    assert "@('/d', '/s', '/c', $command)" in login_path
    assert "-WorkingDirectory $env:USERPROFILE" in login_path
    assert "-WindowStyle Hidden -Wait -PassThru" in login_path
    assert "if ($login.ExitCode -ne 0)" in login_path
    assert "start \"\" /min" not in login_path
    assert "Start-Process cmd -ArgumentList" not in login_path
    assert "--no-browser" not in login_path
    assert "Minimized" not in login_path
    assert "& $claude mcp get plugin:sensai:sensai" in login_path
    assert login_path.index("$userPath =") < login_path.index("Get-Command claude")
    assert login_path.index("-Wait -PassThru") < login_path.index(
        "if ($login.ExitCode -ne 0)"
    ) < login_path.index(
        "& $claude mcp get plugin:sensai:sensai"
    )
    assert "The person only chooses their Google account in the browser." in login_path


def test_claude_launch_links_start_a_localized_ordinary_consultation() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    claude_path = _read_section(readme, "### Claude Desktop", "#### Known problems")
    launch_path = _read_section(
        claude_path,
        "Then open a new Claude Code session with the ordinary request",
        "#### Known problems",
    )
    russian_request = (
        "Проконсультируйся с Sensai. Сначала задай мне вопросы о моей работе, "  # noqa: RUF001 - exact public Russian launch text
        "обычных программах и повторяющихся задачах."
    )
    english_request = "Consult Sensai. First ask me about my work, usual apps, and recurring tasks."

    encoded_requests = [
        encoded
        for encoded in launch_path.split("(claude://code/new?q=")[1:]
        for encoded in [encoded.split(")", maxsplit=1)[0]]
    ]
    decoded_requests = {unquote(encoded) for encoded in encoded_requests}

    assert russian_request in decoded_requests
    assert english_request in decoded_requests
    assert all(request.strip() and not request.startswith("/") for request in decoded_requests)
    assert "/sensai:sensai" not in launch_path
    assert russian_request in launch_path
    assert english_request in launch_path


def test_sensai_skill_description_covers_a_person_starting_consultation() -> None:
    for skill in (SOURCE_SKILL, PACKAGED_SKILL):
        text = skill.read_text(encoding="utf-8")
        frontmatter = text.split("---", maxsplit=2)[1]
        assert "person asks to start a Sensai consultation" in frontmatter
        assert (
            "call `tell_sensai` once with exactly: `The person wants to explore ways AI "
            "can improve their work.`" in text
        )
        assert "Keep the person's launch phrase in the host conversation" in text
        assert (
            "This start-only launch is not an actual work request and does not cause an "
            "automatic second `tell_sensai` call." in text
        )
        assert "Await the person's role, usual apps or sites, and recurring work" in text
        assert (
            "then make exactly one follow-up with only the person's stated facts and any "
            "stated work request." in text
        )


def test_first_contact_limits_the_general_follow_up_to_non_start_only_launches() -> None:
    first_contact = (REPOSITORY_ROOT / "docs/specs/FIRST-CONTACT-001.md").read_text(
        encoding="utf-8"
    )

    assert (
        "Outside the start-only case, when the first MCP result is Sensai's exact fixed "
        "onboarding reply" in first_contact
    )


def test_built_payloads_keep_the_concise_post_install_consultation_contract(
    tmp_path: Path,
) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    source = SOURCE_SKILL.read_text(encoding="utf-8")
    codex = (built.codex / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
    claude = (built.claude / "skills/sensai/SKILL.md").read_text(encoding="utf-8")

    assert codex == claude == source
    _assert_post_install_consultation_contract(codex)


def test_claude_recovery_keeps_terminal_work_with_the_agents() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    recovery = _read_section(readme, "#### Known problems", "")
    _assert_claude_recovery_audience(recovery)
