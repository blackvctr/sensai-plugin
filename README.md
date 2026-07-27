# Sensai Plugin

Sensai is an AI agent that advises another AI agent. It helps the user's agent identify and set up a
useful connector or built-in tool for the person's current work, recover from setup problems, and,
when genuinely useful, combine those tools into a workflow. Sensai does not connect to external
accounts or run code on the user's computer.

Sensai may return advice, architecture, detailed implementation instructions, and non-executed
reference snippets. The user's own AI agent writes and reviews any resulting code, installs its
dependencies, runs it, and verifies it locally through the normal controls of ChatGPT Desktop or
Claude Desktop.
Connector setup also happens locally.

## Installation (if you are human)

1. You need [ChatGPT Desktop](https://chatgpt.com/download/) with an OpenAI subscription, or [Claude Desktop](https://claude.ai/download) with an Anthropic subscription.
2. In ChatGPT Desktop, make sure you are on the Codex tab. In Claude Desktop, make sure you are on the Code tab.
3. Use a strong model, especially while installing the plugin. At the time of writing, use GPT-5.6 Terra rather than Luna; in Claude Desktop, use Opus rather than Sonnet.
4. Start a new chat with this instruction:

```text
Install Sensai https://github.com/grayvectorblack/sensai-plugin
```

## Installation (AI agent part)

Here and below, speak to the person in their language and make every step convenient for them. English
may be used for messages to Sensai to save tokens, unless the person needs to see the message. Unless
you clearly know otherwise, assume the person is a nontechnical specialist: explain what is happening
in plain language and adapt your terminology to their level. Treat the person's time as especially
valuable: don't ask them to perform manually anything you can do yourself. Avoid technical terms and
unnecessary interface clicks. Take care of every step you can; leave the person only actions that need
their own account choice or approval.

### ChatGPT Desktop

```bash
codex plugin marketplace add grayvectorblack/sensai-plugin
codex plugin add sensai@sensai
```

Before starting Google sign-in, explain in the person's language that sign-in links the person to
their Sensai consultation context, so Sensai can continue that consultation across new chats. Tell
the person that an ordinary Google window will open, where they choose an account and confirm access.

Run `codex mcp login sensai` once. Don't open or copy the authorization URL manually. Wait for the
command to report actual completion, and do not start another login while it is still running. The
person handles only Google's browser account selection and consent; don't ask them to run a command,
copy a URL, code, token, or credential, or report that they are done.

After sign-in succeeds, give the person one complete localized sentence. For English, use exactly:

```markdown
The Sensai plugin is installed. To start using it, open a [new chat](codex://new?prompt=Run%20%5B%40sensai%5D%28plugin%3A%2F%2Fsensai%40sensai%29.). Then press Enter to send the prepared message.
```

For another language, translate both the visible sentence and `Run` inside the link before URL-encoding
the prompt. Keep `[@sensai](plugin://sensai@sensai)` unchanged. The link only fills the new-chat
composer; it does not send the message. Don't include MCP, commands, paths, plugin versions, or
transport details in the person-facing status update.

### Claude Desktop

```bash
claude plugin marketplace add grayvectorblack/sensai-plugin
claude plugin install sensai@sensai --scope user
```

Before starting Google sign-in, explain in the person's language that sign-in links the person to
their Sensai consultation context, so Sensai can continue that consultation across new chats. Tell
the person that an ordinary Google window will open, where they choose an account and confirm access.

Start Sensai sign-in and explain it. Only the person chooses their Google account and approves access;
wait for host-confirmed completion before continuing. Don't ask the person to run a command,
copy a URL, code, token, or credential, or report that they are done. The person handles only Google's
browser account selection and consent.

After sign-in succeeds, run `/reload-plugins` once and invoke Sensai in the same session. Tell the
person in their language that Sensai is ready and has started. Don't ask them to greet Sensai manually
or start another chat unless Claude Desktop says the reload could not be applied. Don't include MCP,
commands, paths, plugin versions, or transport details in the person-facing status update.

#### Known problems

Perform this recovery yourself: diagnose `PATH`, install the official Claude CLI when needed, and
recheck it in your own session. Explain the reason to the person in their language, and ask for
their explicit consent only before installing Claude CLI or using elevated or sandbox-disabling
permission. Never paste terminal commands for the person to run.

1. Do not use `/plugin` in the app chat. This is a known limitation
   ([anthropics/claude-code#42142](https://github.com/anthropics/claude-code/issues/42142)), not an
   installation failure. Install plugins through the real `claude` CLI. First locate it on `PATH`:
   `which claude` or `command -v claude` in bash/zsh; `Get-Command claude` or `where.exe claude` in
   PowerShell.

2. Do not execute a `claude` file found inside the app's internal directories. It is not the host CLI;
   use the `claude` command found on `PATH` instead.

3. If `claude` is absent from `PATH`, first explain to the human in their language that automating the
   Sensai plugin installation requires installing the official Claude CLI, a separate Anthropic program.
   Request the human's explicit approval. Only after approval, run
   `curl -fsSL https://claude.ai/install.sh | bash` on macOS or Linux, or
   `irm https://claude.ai/install.ps1 | iex` in PowerShell on Windows, and report the result. It
   installs in `~/.local/bin` or the user profile on Windows. If a sandbox blocks required external
   writes, request the human's explicit confirmation for elevated execution; only after confirmation,
   use the host-supported approval mechanism. Recheck `PATH` in the same session before continuing
   the plugin installation.

4. Do not edit Claude's internal plugin configuration files manually. Use the CLI commands above to
   register the marketplace and install the plugin.
