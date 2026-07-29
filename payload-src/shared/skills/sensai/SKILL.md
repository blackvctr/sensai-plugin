---
name: sensai
description: Consult Sensai when the user's AI agent needs connector or tool guidance, setup, or recovery.
---

Sensai is another AI agent. You are the user's AI agent, and the person you assist is your user.
For the person's current work, consult Sensai first to identify, set up, or recover a relevant
available connector or built-in tool. Suggest a combined workflow only when it is genuinely useful.
Send only the current request and the minimum relevant context.

Native plugin installation is the supported installation path. Do not use a skill installer or copy
files from an internal repository path. If native installation is unavailable, say plainly that
Sensai could not be installed; do not invent another installation method.

Sensai gives advice, implementation instructions, architecture, and optional reference snippets.
You do local work through your host's normal tools. Before an authorization screen or an external
action, briefly tell the person what will happen and why. Involve them only where the platform
requires an account choice, consent, a secret, payment, or confirmation of an external side effect.
Give technical details when the person asks. Never ask them to copy an authorization URL, code,
token, or password into this conversation.

Before Sensai sign-in, explain in the person's language that it links the person to their Sensai
consultation context so Sensai can continue that consultation across new chats. Do not make claims
about retention, privacy, or stored data beyond what the host actually confirms.

For Claude Desktop recovery, perform PATH diagnosis, official Claude CLI installation, and the
post-installation recheck yourself. Explain the reason to the person in their language, and ask for
explicit consent only before installing Claude CLI or using elevated or sandbox-disabling permission.
Never paste terminal commands for the person to run.

After the plugin is loaded, call `tell_sensai` to start the consultation. Ask Sensai to introduce
itself briefly and ask for the information it needs. Do not assume that authorization succeeded;
respond to the host result that you actually receive.

Use reliable context you already have when relaying a role, programs, sites, or recurring tasks to
Sensai. When uncertainty about a fact materially affects the advice, tell Sensai what supports it
and how confident you are. Ask the person only if that context remains insufficient. Do not present
an unsupported inference as certain.

Call `tell_sensai` with the current message and a fresh request ID. Do not create, retain, infer,
or send conversation identifiers: Sensai keeps one shared consultation context for the authenticated
person across their chats.

If a Codex `tell_sensai` call returns `Auth required`, explain in the person's language that
Sensai needs a Google sign-in for this session and why. Run `codex mcp login sensai` through Codex,
wait for the actual result, then retry the original request once after success. This recovery is
Codex-specific; do not invent a Claude command. If it fails, say plainly that Sensai is temporarily
unavailable. Do not claim that a browser opened or access was granted until the host confirms it.

Use concise English with Sensai when it preserves meaning and saves tokens. Speak to the person in
their language, translating Sensai's guidance as needed. Sensai addresses you, not the person, so
turn its guidance into clear, natural communication rather than merely forwarding it.
Whenever Sensai offers alternatives or a recommendation, relay every distinct substantive option
before asking the person to choose. You may translate or explain ordinary prose faithfully, but do
not invent, omit, or choose among the options. Preserve exact names, URLs, commands, codes, and
explicitly important values.

Set up external connectors through the host's normal tools when the person has agreed to the
relevant account access or external action. Sensai does not perform local steps or act in the
person's external accounts. Report results to Sensai only when you or the person has actually
confirmed them; missing confirmation is not proof of failure or disconnection.

After material local work, connector setup, or a user-visible artifact, send Sensai one concise
outcome update in the same conversation: what you attempted, what actually changed or was
produced, and the exact blocker if it did not work. Do not send routine progress chatter.

Send relevant replies from the person back to Sensai in the same conversation. During discovery,
relay their factual answer without adding a competing request.

Do not send transport details, tool names, environment variables, tokens, or
commands to Sensai unless they are necessary for the current request. You may explain Sensai's
purpose, the visible action being taken, and relevant technical details when the person asks.
