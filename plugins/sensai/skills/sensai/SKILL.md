---
name: sensai
description: Consult Sensai when the user's AI agent needs guidance on useful work, setup, or recovery.
---

Sensai is another AI agent. You are the user's AI agent, and the person you assist is your user.
Consult Sensai when you need help choosing, setting up, or recovering useful agent workflows. Send
only the current request and the minimum relevant context.

Native plugin installation is the supported installation path. Do not use a skill installer or copy
files from an internal repository path. If native installation is unavailable, say plainly that
Sensai could not be installed; do not invent another installation method.

Sensai gives advice, implementation instructions, architecture, and optional reference snippets.
You do local work through your host's normal tools. Before an authorization screen or an external
action, briefly tell the person what will happen and why. Involve them only where the platform
requires an account choice, consent, a secret, payment, or confirmation of an external side effect.
Give technical details when the person asks. Never ask them to copy an authorization URL, code,
token, or password into this conversation.

After the plugin is loaded, call `tell_sensai` to start the consultation. Ask Sensai to introduce
itself briefly and ask for the information it needs. Do not assume that authorization succeeded;
respond to the host result that you actually receive.

Before relaying a role, programs, sites, or recurring tasks to Sensai, ask the person directly.
Do not treat workspace contents, account labels, installed tools, or your own guesses as facts
about them. You may offer a few example roles or tasks to make the question easier. Relay only
facts the person confirms.

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

Set up external connectors through the host's normal tools when the person has agreed to the
relevant account access or external action. Sensai does not perform local steps or act in the
person's external accounts. Report results to Sensai only when you or the person has actually
confirmed them; missing confirmation is not proof of failure or disconnection.

After material local work, connector setup, or a user-visible artifact, send Sensai one concise
outcome update in the same conversation: what you attempted, what actually changed or was
produced, and the exact blocker if it did not work. Do not send routine progress chatter.

Send relevant replies from the person back to Sensai in the same conversation. During discovery,
relay their factual answer without adding a competing request. Let Sensai decide whether to ask a
necessary follow-up, recommend a connector, or compose a useful scenario.

When Sensai returns options and a recommendation, present every distinct option and the
recommendation before asking the person to choose. Preserve the person's language and each option's
concise meaning. Do not choose on the person's behalf.

Do not send transport details, tool names, environment variables, tokens, or
commands to Sensai unless they are necessary for the current request. You may explain Sensai's
purpose, the visible action being taken, and relevant technical details when the person asks.
