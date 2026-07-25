# Sensai Plugin

Sensai is an AI agent that advises another AI agent. It helps the user's agent choose useful work
scenarios, implement them, set up connectors locally, and recover from setup problems. Sensai does
not connect to external accounts or run code on the user's computer.

Sensai may return advice, architecture, detailed implementation instructions, and non-executed
reference snippets. The user's own AI agent writes and reviews any resulting code, installs its
dependencies, runs it, and verifies it locally through the normal controls of ChatGPT Desktop or
Claude Desktop.
Connector setup also happens locally. The person completes any authorization or consent screen.

## Installation (if you are human)

1. You need [ChatGPT Desktop](https://chatgpt.com/download/) with an OpenAI subscription, or [Claude Desktop](https://claude.ai/download) with an Anthropic subscription.
2. In ChatGPT Desktop, make sure you are on the Codex tab. In Claude Desktop, make sure you are on the Code tab.
3. Start a new chat with this instruction:

```text
Install Sensai https://github.com/grayvectorblack/sensai-plugin
```

### ChatGPT Desktop

To apply the change fully, the agent may suggest starting a new chat or restarting the app.

### Claude Desktop

To apply the change fully, the agent may suggest starting a new chat or restarting the app.

## Installation (AI agent part)

Here and below, speak to the person in their language and make every step convenient for them. Unless
you clearly know otherwise, assume the person is a nontechnical specialist: explain what is happening
in plain language and adapt your terminology to their level.

### ChatGPT Desktop (Codex tab)

```bash
codex plugin marketplace add grayvectorblack/sensai-plugin
codex plugin add sensai@sensai
```

Before starting Google sign-in, explain that this connects Sensai to the current ChatGPT Desktop
session, so Sensai can start consulting in that session. Tell the person that an ordinary Google window
will open, where they choose an account and confirm access.

Run `codex mcp login sensai` once as one long-running terminal operation. Don't open the authorization
URL manually. When the terminal tool yields a running session or handle, preserve it and poll that same
handle every 5-10 seconds until the command actually exits or reports its native five-minute timeout.
An empty poll or a poll with no new output still means the operation is running; it is not completion.
Don't launch another login, close the process, end the turn, or return a final response while that
session is alive. Only a real process exit decides whether login succeeded or reached its native
timeout. Retry only after that operation reports a true timeout. The person handles only Google's
browser account selection and consent; don't ask them to run a command, copy a URL, code, token, or
credential, or report that they are done.

After sign-in succeeds, inform the person in their language: "The Sensai plugin is installed. To start
using it, open a [new chat]. Then press Enter to send the prepared message." Make `new chat` an actual
`codex://new?prompt=<URL-encoded prompt>` link. Its prompt must be the person's-language equivalent of:

```text
Run [@sensai](plugin://sensai@sensai).
```

The link only fills the new-chat composer; it does not send the message. Don't ask the person to greet
Sensai manually. Don't include MCP, commands, paths, plugin versions, or transport details in the
person-facing status update.

### Claude Desktop (Code tab)

```bash
claude plugin marketplace add grayvectorblack/sensai-plugin
claude plugin install sensai@sensai --scope user
```

Before starting Google sign-in, explain that this connects Sensai to the current Claude Desktop
session, so Sensai can start consulting in that session. Tell the person that an ordinary Google window
will open, where they choose an account and confirm access.

Open `/mcp`, select Sensai, and complete its Google sign-in. Don't ask the person to run a command,
copy a URL, code, token, or credential, or report that they are done. The person handles only Google's
browser account selection and consent.

After sign-in succeeds, run `/reload-plugins` once and invoke Sensai in the same session. Tell the
person in their language that Sensai is ready and has started. Don't ask them to greet Sensai manually
or start another chat unless Claude Desktop says the reload could not be applied. Don't include MCP,
commands, paths, plugin versions, or transport details in the person-facing status update.
