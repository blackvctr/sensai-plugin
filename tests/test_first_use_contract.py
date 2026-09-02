from __future__ import annotations

from pathlib import Path

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


def test_readme_requires_russian_first_install_reply_before_general_steps() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    installation_start = "## Installation after explicit request (AI agent part)"
    pre_general_steps = _read_section(readme, installation_start, "### General steps")
    normalized = " ".join(pre_general_steps.split())

    first_reply = normalized.index("first visible reply")
    tool_call = normalized.index("calling any tool")
    assert tool_call < first_reply
    assert "if they write `Установи Sensai ...`, reply in Russian" in normalized
    assert "install Sensai yourself" in normalized
    assert "do not ask them to use a terminal" in normalized


def test_public_copy_paste_prompts_make_the_first_reply_safe_and_match_the_harness() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    russian_prompt = (
        "Установи Sensai https://raw.githubusercontent.com/blackvctr/"
        "sensai-plugin/main/README.md\n"
        "Перед любым действием сначала коротко ответь по-русски, что установкой "
        "займёшься ты. Всё сделай сам, без моих действий. Затем продолжай установку."
    )
    english_prompt = (
        "Install Sensai https://raw.githubusercontent.com/blackvctr/"
        "sensai-plugin/main/README.md\n"
        "Before doing anything, briefly reply in English that you will handle the installation. "
        "Do not ask me to do anything. Then continue the installation."
    )

    assert f"```text\n{russian_prompt}\n```" in readme
    assert f"```text\n{english_prompt}\n```" in readme
    assert _SCENARIO_PROMPTS[FirstReplyScenario.URL_BOOTSTRAP] == russian_prompt


def test_readme_requires_explicit_install_request_and_keeps_credentials_out_of_chat() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Installation after explicit request (AI agent part)" in readme
    assert "tokens, and credentials stay in the provider and browser flow" in readme
    assert "never enter the conversation or another tool" in readme


def test_readme_labels_windows_commands_by_shell_and_covers_powershell() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    claude_path = _read_section(readme, "### Claude Desktop", "#### Known problems")

    assert "# Windows\n" not in claude_path
    assert "# Windows, CMD" in claude_path
    assert "# Windows, PowerShell" in claude_path
    assert (
        "Start-Process cmd -ArgumentList '/c','claude mcp login plugin:sensai:sensai' "
        "-WorkingDirectory 'C:\\' -WindowStyle Minimized" in claude_path
    )
    assert "Start-Process 'claude://code/new?q=%2Fsensai%3Asensai'" in claude_path


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
