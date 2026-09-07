from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "plugin-src"
    shutil.copytree(REPOSITORY_ROOT / "plugin-src", destination)
    return destination
