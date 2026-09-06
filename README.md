# Sensai Plugin

Sensai is an AI agent created by [Sergey Skripko](https://linkedin.com/in/grayskripko) that advises another AI agent. It helps the user's agent identify and set up a useful connector or built-in tool for the person's current work, recover from setup problems, and, when genuinely useful, combine those tools into a workflow. Sensai does not connect to external accounts or run code on the user's computer.

Sensai may return advice, architecture, detailed implementation instructions, and non-executed reference snippets. The user's own AI agent writes and reviews any resulting code, installs its dependencies, runs it, and verifies it locally through the normal controls of ChatGPT Desktop or Claude Desktop. Connector setup also happens locally.

## Installation (if you are human)

1. You need [ChatGPT Desktop](https://chatgpt.com/download/) with an OpenAI subscription, or [Claude Desktop](https://claude.ai/download) with an Anthropic subscription.
2. In ChatGPT Desktop, make sure you are on the Codex tab. In Claude Desktop, make sure you are on the Code tab.
3. Use a strong model, especially while installing the plugin. At the time of writing, use GPT-5.6 Terra rather than Luna; in Claude Desktop, use Opus rather than Sonnet.
4. Start a new chat with this instruction:

```text
Установи плагин Sensai из marketplace blackvctr/sensai-plugin. После установки открой новый чат и отправь /sensai:sensai.
```

## Installation after explicit request (AI agent part)

Here and below, make every step convenient for the person. English may be used for messages to Sensai to save tokens, unless the person needs to see the message. Unless you clearly know otherwise, assume the person is a nontechnical specialist: explain what is happening in plain language and adapt your terminology to their level. Treat the person's time as especially valuable: don't ask them to perform manually actions you can do yourself, though you can inform the person if it makes sense. Avoid technical terms and unnecessary interface clicks. Leave the person only actions that need their own account choice or approval. Nothing in this process should require them to know what `PATH` is.

### General steps

1. Write every message to the person in the language they wrote to you. If they wrote in Russian, you answer in Russian.
2. Install the plugin yourself.
3. Before Google sign-in, briefly explain that it lets Sensai understand who it is speaking with and continue the conversation with relevant context, so the consultation carries over into new chats. Say that an ordinary Google window will open, where they choose an account and confirm access.
4. After that explanation, run the host-specific login command below once. The command opens the sign-in page in the person's default browser itself; you don't open, copy, or hand over the URL. Wait for the command to report actual completion; don't start a second login while one is running. The person only chooses their Google account and approves access. You run the command and confirm completion; authorization URLs, codes, tokens, and credentials stay in the provider and browser flow and never enter the conversation or another tool.
5. After authorization, start Sensai using the host-specific steps below.

### ChatGPT Desktop

```bash
codex plugin marketplace add blackvctr/sensai-plugin
codex plugin add sensai@sensai
codex mcp login sensai
```

After sign-in succeeds, give the person one complete localized sentence. For English, use exactly:

```markdown
The Sensai plugin is installed. To start using it, open a [new chat](codex://new?prompt=Run%20%5B%40sensai%5D%28plugin%3A%2F%2Fsensai%40sensai%29.). Then press Enter to send the prepared message.
```

For another language, translate both the visible sentence and `Run` inside the link before URL-encoding the prompt. Keep `[@sensai](plugin://sensai@sensai)` unchanged. The link only fills the new-chat composer; it does not send the message.

### Claude Desktop

```bash
claude plugin marketplace add blackvctr/sensai-plugin
claude plugin install sensai@sensai --scope user
```

Sign-in needs a terminal on its input, and your Bash tool has none: run bare, `claude mcp login` stops with `stdin isn't a terminal`. Wrap it for the shell you are running in instead:

```bash
script -q /dev/null claude mcp login plugin:sensai:sensai                     # macOS
script -q -c "claude mcp login plugin:sensai:sensai" /dev/null                # Linux
start "" /min cmd /c claude mcp login plugin:sensai:sensai                    # Windows, CMD
Start-Process cmd -ArgumentList '/c','claude mcp login plugin:sensai:sensai' -WorkingDirectory 'C:\' -WindowStyle Minimized    # Windows, PowerShell
```

The argument order differs between macOS and Linux because they ship different `script` implementations. Windows needs two lines because it ships two shells: in PowerShell `start` is an alias for `Start-Process`, which reads the empty `""` title as the program to run, so the CMD line fails there. Pick by shell, not by operating system. `-WorkingDirectory 'C:\'` matters when your shell sits on a path Windows cannot use as a working directory, such as a WSL mount; without it the launch dies with `Windows cannot find '\\'` and writes nothing to any log. On macOS and Linux no window appears at all; on Windows a minimized console opens and closes itself. Either way the person only chooses their Google account in the browser, and the command finishes on its own.

Confirm the result with `claude mcp get plugin:sensai:sensai`. Before sign-in it reports `Status: ! Needs authentication`; after a successful sign-in that line changes. Treat the changed status as the signal to continue, not the login command's own exit.

Then open the new session yourself, again using the line for the shell you are running in:

```bash
start "" "claude://code/new?q=%2Fsensai%3Asensai"         # Windows, CMD
Start-Process 'claude://code/new?q=%2Fsensai%3Asensai'    # Windows, PowerShell
open "claude://code/new?q=%2Fsensai%3Asensai"             # macOS
xdg-open "claude://code/new?q=%2Fsensai%3Asensai"         # Linux
```

A new Claude Code session opens with `/sensai:sensai` already in its composer.

Then tell the person, in one complete localized sentence, that Sensai is installed and has just opened in a new chat, and that they only need to press Enter there if the prepared message has not been sent on its own.

If the command fails, or no new session appears, tell the person instead to open a new chat and send `/sensai:sensai` there.

#### Known problems

Recover from these yourself and explain the reason to the person in their language. Ask for their explicit consent in two cases only: before installing the Claude CLI, and before using elevated or sandbox-disabling permission.

1. Do not use `/plugin` in the app chat. This is a known limitation
   ([anthropics/claude-code#42142](https://github.com/anthropics/claude-code/issues/42142)), not an
   installation failure. Install plugins through the real `claude` CLI. First locate it on `PATH`:
   `which claude` or `command -v claude` in bash/zsh; `Get-Command claude` or `where.exe claude` in
   PowerShell.

2. Do not execute a `claude` file found inside the app's internal directories. It is not the host CLI;
   use the `claude` command found on `PATH` instead.

3. If `claude` is absent from `PATH`, first explain to the human in their language that automating the
   Sensai plugin installation requires installing the official Claude CLI, a separate Anthropic program.
   Request the human's explicit approval. Only after approval, install it with the first method that
   fits their system, and report the result:

   - Windows: `winget install Anthropic.ClaudeCode`
   - macOS: `brew install --cask claude-code`
   - macOS without Homebrew, or Linux: `curl -fsSL https://claude.ai/install.sh | bash`
   - Windows without WinGet: `irm https://claude.ai/install.ps1 | iex` in PowerShell, or
     `curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd` in CMD

   Prefer a package manager where one exists: it is signed, it reverses cleanly, and it asks less of
   the person than approving a script downloaded from the internet. The CLI installs into
   `~/.local/bin`, or the user profile on Windows. If a sandbox blocks required external writes,
   request the human's explicit confirmation for elevated execution; only after confirmation, use the
   host-supported approval mechanism. Recheck `PATH` in the same session before continuing the plugin
   installation.

4. Do not edit Claude's internal plugin configuration files manually. Use the CLI commands above to
   register the marketplace and install the plugin.

5. Two separate sign-ins exist here, and only one of them belongs to this installation. Sensai's
   Google sign-in is the one you need, and `claude mcp login plugin:sensai:sensai` performs it. The
   CLI's own Anthropic account sign-in is unrelated: marketplace registration and plugin installation
   both succeed while `claude auth status` reports `"loggedIn": false`, so `claude auth login` is not
   part of this flow. When `claude mcp get plugin:sensai:sensai` reports `Needs authentication`, it is
   Sensai asking, not the CLI.

6. If sign-in fails with `Client ID '<id>' not found`, run `claude mcp logout plugin:sensai:sensai`
   and repeat the sign-in command; it then succeeds. Handle this silently and continue: the person
   needs a second trip through the Google window, nothing more.
