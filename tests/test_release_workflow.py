from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"


def test_release_workflow_builds_and_attests_only_version_tags() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "tags:" in workflow
    assert '"v*"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: read" in workflow
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in workflow
    assert "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e" in workflow
    assert "uv sync --locked" in workflow
    assert "scripts/build_release.py" in workflow
    assert "scripts/verify_release.py" in workflow
    assert "SHA256SUMS" in workflow
    assert "sha256sum --check SHA256SUMS" in workflow
    assert "actions/attest-build-provenance" in workflow
    assert "actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be" in workflow
    assert "sensai-*-claude-marketplace.zip" in workflow
    assert "sensai-*-codex-marketplace.zip" in workflow
    assert "actions/upload-artifact" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    assert "gh release create" not in workflow
    assert "action-gh-release" not in workflow
