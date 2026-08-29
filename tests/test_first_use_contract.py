from __future__ import annotations

from pathlib import Path

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


def _assert_auth_explanation(text: str) -> None:
    normalized = " ".join(text.lower().split())
    assert "sign-in" in normalized
    assert "consultation context" in normalized
    assert "continue" in normalized
    assert "new chats" in normalized
    assert "identifies the person's sensai workspace" not in normalized


def _assert_context_evidence_behavior(text: str) -> None:
    """Assert the three discovery decisions, without coupling to a sentence."""

    normalized = " ".join(text.lower().split())
    assert "reliable context" in normalized
    assert "uncertainty" in normalized
    assert "what supports it" in normalized
    assert "how confident" in normalized
    assert "only if" in normalized and "remains insufficient" in normalized
    assert "unsupported inference" in normalized and "as certain" in normalized


def _assert_connector_first_positioning(text: str) -> None:
    """Require an actionable connector/tool first step and optional workflows."""

    normalized = " ".join(text.lower().split())
    connector_position = normalized.index("connector")
    tool_position = normalized.index("built-in tool")
    workflow_position = normalized.index("combined workflow")

    assert connector_position < workflow_position
    assert tool_position < workflow_position
    assert "only when it is genuinely useful" in normalized
    assert "fixed number of scenarios" not in normalized


def _assert_explicit_connector_guidance_request(text: str) -> None:
    """Only a human-confirmed named setup request reaches the server as intent."""

    normalized = " ".join(text.lower().split())
    assert "guidance_request" in normalized
    assert "subject" in normalized
    assert "named connector" in normalized
    assert "setup" in normalized
    assert "activation" in normalized
    assert "authorization" in normalized
    assert "first verification" in normalized
    assert "explicitly confirmed" in normalized
    assert "first mentioning or recommending a connector is not confirmation" in normalized
    assert "discovery" in normalized


def _assert_initial_sensai_call(text: str) -> None:
    normalized = " ".join(text.lower().split())
    assert "call `tell_sensai` to start the consultation" in normalized
    assert "consultation_start" not in normalized
    assert "call `tell_sensai` with the current message" not in normalized
    assert "never send the person's technical sensai launch command" in normalized
    assert "run sensai" in normalized
    assert "запусти sensai" in normalized
    assert "explicitly stated work facts" in normalized
    assert "exactly once more with a new request id" in normalized
    assert "do not make a third automatic call" in normalized


def _assert_codex_auth_recovery_restarts_before_retry(text: str) -> None:
    """Codex login does not refresh an already-open MCP client session."""

    normalized = " ".join(text.lower().split())
    login_position = normalized.index("codex mcp login sensai")
    restart_position = normalized.index("codex needs to be restarted")
    retry_position = normalized.index("before one retry of the original request")

    assert login_position < restart_position < retry_position
    assert "do not retry through the already-open client session" in normalized
    assert "reconnect or reload" not in normalized
    assert "start a new codex chat or session" not in normalized
    assert "codex-specific" in normalized
    assert "do not invent a claude command" in normalized


def _assert_claude_recovery_audience(text: str) -> None:
    """Keep terminal recovery with the agent, not the person it assists."""

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


def test_built_codex_and_claude_payloads_share_the_auth_explanation(tmp_path: Path) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    source = SOURCE_SKILL.read_text(encoding="utf-8")
    codex = (built.codex / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
    claude = (built.claude / "skills/sensai/SKILL.md").read_text(encoding="utf-8")

    assert codex == claude == source
    _assert_auth_explanation(codex)


def test_built_payloads_calibrate_context_evidence_before_questioning_a_person(
    tmp_path: Path,
) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    for payload in (built.codex, built.claude):
        skill = (payload / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
        _assert_context_evidence_behavior(skill)


def test_built_payloads_put_connectors_before_optional_workflows(tmp_path: Path) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    for payload in (built.codex, built.claude):
        skill = (payload / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
        _assert_connector_first_positioning(skill)


def test_built_payloads_send_explicit_named_connector_guidance_requests(tmp_path: Path) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    for payload in (built.codex, built.claude):
        skill = (payload / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
        _assert_explicit_connector_guidance_request(skill)


def test_built_payloads_start_sensai_without_a_client_control_flag(tmp_path: Path) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    for payload in (built.codex, built.claude):
        skill = (payload / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
        _assert_initial_sensai_call(skill)


def test_built_payloads_restart_codex_before_auth_recovery_retry(tmp_path: Path) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    for payload in (built.codex, built.claude):
        skill = (payload / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
        _assert_codex_auth_recovery_restarts_before_retry(skill)


def test_claude_recovery_keeps_terminal_work_with_the_agents(tmp_path: Path) -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    recovery = _read_section(readme, "#### Known problems", "")
    _assert_claude_recovery_audience(recovery)

    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )
    for payload in (built.codex, built.claude):
        skill = (payload / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
        _assert_claude_recovery_audience(skill)
