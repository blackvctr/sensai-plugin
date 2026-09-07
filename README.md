# Sensai Plugin

Sensai is a plugin for Claude Code and Codex. It connects the host agent to Black Vector's remote consultation service for work-related questions. Sensai can provide information about supported tools and integrations.

Publisher: [Black Vector](https://black-vector.com/)

Privacy: [PRIVACY.md](PRIVACY.md)

## Plugin surface

The installed plugin contains no local hooks or executables. It connects to one remote MCP server: `https://black-vector.com/sensai/mcp`. See [PRIVACY.md](PRIVACY.md) for the data and Google sign-in boundary.

## Installation (if you are human)

1. You need [ChatGPT Desktop](https://chatgpt.com/download/) with an OpenAI subscription, or [Claude Desktop](https://claude.ai/download) with an Anthropic subscription.
2. In ChatGPT Desktop, make sure you are on the Codex tab. In Claude Desktop, make sure you are on the Code tab.
3. Use your AI client's standard plugin-installation controls to add the public marketplace `blackvctr/sensai-plugin`. Review and confirm any marketplace, plugin, or account-access prompt shown by the client.
