from __future__ import annotations

from pathlib import Path

from sensai_plugin.claude_first_reply import _SCENARIO_PROMPTS, FirstReplyScenario
from sensai_plugin.installation_e2e_contract import _public_contract_from_markdown
from sensai_plugin.package_builder import BuiltPackages, build_packages as _build_packages, plugin_version

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


def test_public_readme_uses_a_concise_strict_installation_manifest() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    contract = _public_contract_from_markdown(readme)

    assert "## Installation manifest" in readme
    assert '"schema": "sensai-install-v1"' in readme
    assert "Installation after explicit request (AI agent part)" not in readme
    assert "#### Known problems" not in readme
    assert "curl -fsSL" not in readme
    assert "sandbox-disabling" not in readme
    assert "elevated execution" not in readme
    assert contract.russian_authorization_message.startswith("Я сам установлю Sensai.")
    assert contract.russian_ready_message.startswith("Sensai установлен.")


def test_public_copy_paste_prompts_and_manifest_request_remain_exact() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    contract = _public_contract_from_markdown(readme)
    russian_prompt = "Установи Sensai https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md"
    english_prompt = "Install Sensai https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md"

    assert f"```text\n{russian_prompt}\n```" in readme
    assert f"```text\n{english_prompt}\n```" in readme
    assert _SCENARIO_PROMPTS[FirstReplyScenario.URL_BOOTSTRAP] == russian_prompt
    assert contract.russian_new_chat_request.startswith("Проконсультируйся с Sensai.")


def test_manifest_keeps_only_typed_local_installation_values() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    for value in (
        '"repository": "blackvctr/sensai-plugin"',
        '"plugin": "sensai@sensai"',
        '"scope": "user"',
        '"server": "plugin:sensai:sensai"',
        '"terminal": "linux-script"',
        '"server": "sensai"',
    ):
        assert value in readme
    assert "claude plugin marketplace add" not in readme
    assert "claude mcp login" not in readme


def test_sensai_skill_description_covers_a_person_starting_consultation() -> None:
    for skill in (SOURCE_SKILL, PACKAGED_SKILL):
        text = skill.read_text(encoding="utf-8")
        frontmatter = text.split("---", maxsplit=2)[1]
        assert "person asks to start a Sensai consultation" in frontmatter
        assert "Keep the person's launch phrase in the host conversation" in text
        assert "Await the person's role, usual apps or sites, and recurring work" in text


def test_built_payloads_keep_the_concise_post_install_consultation_contract(tmp_path: Path) -> None:
    built = build_packages(
        source_root=REPOSITORY_ROOT / "payload-src",
        output_root=tmp_path / "packages",
    )

    source = SOURCE_SKILL.read_text(encoding="utf-8")
    codex = (built.codex / "skills/sensai/SKILL.md").read_text(encoding="utf-8")
    claude = (built.claude / "skills/sensai/SKILL.md").read_text(encoding="utf-8")

    assert codex == claude == source
    normalized = " ".join(codex.lower().split())
    assert "stated work facts" in normalized
    assert "sensitive information" in normalized
    assert "claude mcp login" not in normalized
