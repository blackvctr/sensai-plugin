"""Fixed values owned by the local Claude installation acceptance.

The public README explains installation to people. It is deliberately not a
machine-readable test contract: the production E2E keeps its expectations in
code and only verifies the exact public README bytes it exercises.
"""

from __future__ import annotations

from urllib.parse import quote

# This is the one model identifier used by every Claude compatibility check.
CLAUDE_SONNET_5_MODEL = "claude-sonnet-5"

# This is the exact prepared request in the published August README.  The
# installation acceptance is intentionally anchored to the public instruction,
# rather than to a later consultation scenario.
CLAUDE_NEW_CHAT_REQUEST = "/sensai:sensai"
CLAUDE_NEW_CHAT_URI = "claude://code/new?q=" + quote(CLAUDE_NEW_CHAT_REQUEST, safe="")

CLAUDE_LINUX_ACTIONS = (
    ("marketplace_add", ("claude", "plugin", "marketplace", "add", "blackvctr/sensai-plugin")),
    ("plugin_install", ("claude", "plugin", "install", "sensai@sensai", "--scope", "user")),
    ("sensai_login", ("script", "-q", "-c", "claude mcp login plugin:sensai:sensai", "/dev/null")),
    ("new_chat", ("xdg-open", CLAUDE_NEW_CHAT_URI)),
)
