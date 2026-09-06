from __future__ import annotations

from pathlib import Path

from sensai_plugin.claude_first_reply import _SCENARIO_PROMPTS, FirstReplyScenario
from sensai_plugin.package_builder import BuiltPackages, plugin_version
from sensai_plugin.package_builder import build_packages as _build_packages

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


def test_public_readme_is_human_installation_guidance_not_a_test_manifest() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Installation after explicit request (AI agent part)" in readme
    assert "#### Known problems" in readme
    assert "## Installation manifest" not in readme
    assert '"schema": "sensai-install-v2"' not in readme
    assert "```json" not in readme


def test_public_copy_paste_prompts_and_fixed_e2e_input_remain_exact() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    russian_prompt = (
        "Установи плагин Sensai из marketplace blackvctr/sensai-plugin. "
        "После установки открой новый чат и отправь /sensai:sensai."
    )
    assert f"```text\n{russian_prompt}\n```" in readme
    assert _SCENARIO_PROMPTS[FirstReplyScenario.URL_BOOTSTRAP] == russian_prompt


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
