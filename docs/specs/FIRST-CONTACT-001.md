# FIRST-CONTACT-001: Native Sensai installation and first consultation

## Purpose

Sensai is installed through the host's native plugin mechanism. The user's AI agent performs normal local setup. The person participates only where their account, consent, secret, payment, or an external side effect requires them.

The agent must make the flow understandable without turning it into a technical support script: before authorization or an external action, it briefly explains what will happen and why. It gives technical details when asked. It never asks the person to copy a password, authorization code, or token into the chat.

## Native installation

The public README is the human-facing starting point. The installing agent uses the native commands for its current host, not a generic skill installer or copied repository files.

Codex:

```bash
codex plugin marketplace add grayvectorblack/sensai-plugin
codex plugin add sensai@sensai
```

Claude Code:

```bash
claude plugin marketplace add grayvectorblack/sensai-plugin
claude plugin install sensai@sensai --scope user
```

If an applicable native command fails, the agent reports that Sensai could not be installed. It does not substitute an unofficial installation path.

## Authorization and first request

Before starting the host's authorization flow, the agent tells the person in their language that Sensai is being connected to this session and why. The person selects an account and approves the provider's consent screen; the agent handles the remaining host-native steps.

For Codex, the agent runs `codex mcp login sensai` and waits for the command's actual completion. It does not open, copy, or request an authorization URL, code, or token manually. Claude uses only its documented native mechanism; this specification does not invent an equivalent command.

After the plugin is loaded, the agent starts a consultation with `tell_sensai`, but does not send a technical launch command such as `Run Sensai` or `Запусти Sensai`. It sends only work facts and the actual request explicitly stated by the person; it does not add inferred or workspace-derived facts. If the launch command contained nothing else, it sends a neutral continuation instead. When the first MCP result is Sensai's exact fixed onboarding reply and the launch message included explicit facts or a request, the agent makes one fresh-ID follow-up with only that information. It does not make a third automatic call. The agent does not create, retain, infer, or send a conversation ID: Sensai keeps one shared consultation context for the authenticated person across their chats. An `Auth required` result in Codex is recovered by one native login attempt, then restarting Codex before one retry of the original request. The agent does not retry through the already-open client session. The agent states the outcome honestly and never claims browser navigation or access that the host did not confirm.

## Discovery and recommendations

Sensai first needs confirmed information about the person's role, common programs or sites, and recurring tasks. The user's agent asks the person for those facts; it must not infer them from a workspace, account label, installed software, or its own speculation.

Sensai may ask a necessary follow-up, recommend an available connector, or compose a useful scenario from the confirmed information. The number of options is its judgment, not a protocol rule. The user's agent relays every distinct option Sensai actually offers, including any recommendation, before asking the person to choose. It does not silently discard options or decide for the person.

## Boundaries

Sensai provides guidance. The user's agent performs local work through normal host tools and only reports results that it or the person has confirmed. It communicates with Sensai in concise English when useful and communicates with the person in the person's language. It does not send secrets, tokens, internal IDs, environment variables, or irrelevant transport details to Sensai.
