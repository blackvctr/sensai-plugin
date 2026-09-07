# Sensai Plugin

Sensai is an AI agent that advises another AI agent. It helps the user's agent identify and set up a useful connector or built-in tool for the person's current work, recover from setup problems, and, when genuinely useful, combine those tools into a workflow. Sensai does not connect to external accounts or run code on the user's computer.

Publisher: [Black Vector](https://black-vector.com/)

Sensai may return advice, architecture, detailed implementation instructions, and non-executed reference snippets. The user's own AI agent writes and reviews any resulting code, installs its dependencies, runs it, and verifies it locally through the normal controls of ChatGPT Desktop or Claude Desktop. Connector setup also happens locally.

## Installation (if you are human)

1. You need [ChatGPT Desktop](https://chatgpt.com/download/) with an OpenAI subscription, or [Claude Desktop](https://claude.ai/download) with an Anthropic subscription.
2. In ChatGPT Desktop, make sure you are on the Codex tab. In Claude Desktop, make sure you are on the Code tab.
3. Use a strong model, especially while installing the plugin. At the time of writing, use GPT-5.6 Terra rather than Luna; in Claude Desktop, use Opus rather than Sonnet.
4. Start a new chat with this instruction:

```text
Установи плагин Sensai из marketplace blackvctr/sensai-plugin. После установки открой новый чат и отправь /sensai:sensai.
```

