# Sensai Plugin

Sensai is an AI agent that advises another AI agent. It helps the user's agent choose useful work
scenarios, implement them, set up connectors locally, and recover from setup problems. Sensai does
not connect to external accounts or run code on the user's computer.

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

Here and below, speak to the person in their language and make every step convenient for them. Unless
you clearly know otherwise, assume the person is a nontechnical specialist: explain what is happening
in plain language and adapt your terminology to their level. Treat the person's time as especially
valuable: don't ask them to perform manually anything you can do yourself. Avoid technical terms and
unnecessary interface clicks. In both products, the person should normally only need to approve Google
sign-in and then restart the app or follow a link to another tab; do everything else yourself.

### ChatGPT Desktop

```bash
codex plugin marketplace add grayvectorblack/sensai-plugin
codex plugin add sensai@sensai
```

Before starting Google sign-in, explain that this lets Sensai remember the work context the person
chooses to share, such as their role and recurring tasks, so its guidance remains relevant. Tell the
person that an ordinary Google window will open, where they choose an account and confirm access.

Run `codex mcp login sensai` once as one long-running terminal operation. Don't open the authorization
URL manually. When the terminal tool yields a running session or handle, preserve it and poll that same
handle every 5-10 seconds until the command actually exits or reports its native five-minute timeout.
An empty poll or a poll with no new output still means the operation is running; it is not completion.
Don't launch another login, close the process, end the turn, or return a final response while that
session is alive. Only a real process exit decides whether login succeeded or reached its native
timeout. Retry only after that operation reports a true timeout. The person handles only Google's
browser account selection and consent; don't ask them to run a command, copy a URL, code, token, or
credential, or report that they are done.

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

Before starting Google sign-in, explain that this lets Sensai remember the work context the person
chooses to share, such as their role and recurring tasks, so its guidance remains relevant. Tell the
person that an ordinary Google window will open, where they choose an account and confirm access.

Open `/mcp`, select Sensai, and complete its Google sign-in. Don't ask the person to run a command,
copy a URL, code, token, or credential, or report that they are done. The person handles only Google's
browser account selection and consent.

After sign-in succeeds, run `/reload-plugins` once and invoke Sensai in the same session. Tell the
person in their language that Sensai is ready and has started. Don't ask them to greet Sensai manually
or start another chat unless Claude Desktop says the reload could not be applied. Don't include MCP,
commands, paths, plugin versions, or transport details in the person-facing status update.

#### Known problems

1. Do not use `/plugin` in the app chat. This is a known limitation
   ([anthropics/claude-code#42142](https://github.com/anthropics/claude-code/issues/42142)), not an
   installation failure. Install plugins through the real `claude` CLI. First locate it on `PATH`:
   `which claude` or `command -v claude` in bash/zsh; `Get-Command claude` or `where.exe claude` in
   PowerShell.

2. Do not execute a `claude` file from the app's internal directories. It is a service file, not the
   host CLI. For example, macOS stores a Linux build at
   `~/Library/Application Support/Claude/claude-code-vm/<version>/claude`, which produces `exec format
   error`. Treat app-directory files outside `PATH` as service-only. Verify the file and host formats:
   `file <path>` and `uname -sm` on Unix; inspect the extension and `$env:PROCESSOR_ARCHITECTURE` on
   Windows. A format mismatch is not fixed by sandbox changes or `sudo`; retain the raw check output.

3. If `claude` is absent from `PATH`, first explain to the human in their language that automating the
   Sensai plugin installation requires installing the official Claude CLI, a separate Anthropic program
   that changes their user profile. Request the human's explicit approval. Only after approval, run
   `curl -fsSL https://claude.ai/install.sh | bash` on macOS or Linux, or
   `irm https://claude.ai/install.ps1 | iex` in PowerShell on Windows, and report the result. It
   installs in `~/.local/bin` or the user profile on Windows. If a sandbox blocks required external
   writes, request the human's explicit confirmation for elevated execution; only after confirmation,
   use the host-supported approval mechanism. Recheck `PATH` in the same session before continuing
   the plugin installation.

4. Do not edit `installed_plugins.json`. `settings.json` entries such as `extraKnownMarketplaces` and
   `enabledPlugins` register a marketplace but do not install a plugin; the skill will not appear and
   `plugin list` remains empty. These files are under `~/.claude` or `%USERPROFILE%\.claude` on Windows.
