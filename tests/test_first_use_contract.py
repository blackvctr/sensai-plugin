from __future__ import annotations

from pathlib import Path

from sensai_plugin.package_builder import build_packages

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = REPOSITORY_ROOT / "payload-src/shared/skills/sensai/SKILL.md"
PACKAGED_SKILL = REPOSITORY_ROOT / "plugins/sensai/skills/sensai/SKILL.md"


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


def _read_section(readme: str, heading: str, next_heading: str) -> str:
    return readme.split(heading, maxsplit=1)[1].split(next_heading, maxsplit=1)[0]


def test_each_desktop_path_explains_why_sensai_sign_in_is_needed() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    chatgpt_path = _read_section(readme, "### ChatGPT Desktop", "### Claude Desktop")
    claude_path = _read_section(readme, "### Claude Desktop", "#### Known problems")

    _assert_auth_explanation(chatgpt_path)
    _assert_auth_explanation(claude_path)


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
