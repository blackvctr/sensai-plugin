"""Fixed values owned by the local Claude installation acceptance.

The public README explains installation to people. It is deliberately not a
machine-readable test contract: the production E2E keeps its expectations in
code and only verifies the exact public README bytes it exercises.
"""

from __future__ import annotations

from urllib.parse import quote

# This is the one model identifier used by every Claude compatibility check.
CLAUDE_SONNET_5_MODEL = "claude-sonnet-5"

RUSSIAN_NEW_CHAT_REQUEST = (
    "Проконсультируйся с Sensai. Сначала задай мне вопросы о моей работе, "  # noqa: RUF001
    "обычных программах и повторяющихся задачах."
)
RUSSIAN_NEW_CHAT_URI = "claude://code/new?q=" + quote(RUSSIAN_NEW_CHAT_REQUEST, safe="")

CLAUDE_LINUX_ACTIONS = (
    ("marketplace_add", ("claude", "plugin", "marketplace", "add", "blackvctr/sensai-plugin")),
    ("plugin_install", ("claude", "plugin", "install", "sensai@sensai", "--scope", "user")),
    ("sensai_login", ("script", "-q", "-c", "claude mcp login plugin:sensai:sensai", "/dev/null")),
    ("new_chat", ("xdg-open", RUSSIAN_NEW_CHAT_URI)),
)
