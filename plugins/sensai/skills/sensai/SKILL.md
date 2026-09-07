---
name: sensai
description: Consult Sensai when a person asks to start a Sensai consultation or the user's AI agent needs connector or tool guidance, setup, or recovery.
---

Sensai is an AI agent of this plugin server. You are the user's AI agent, and the person you assist is your user.
Treat Sensai as an external knowledge base of different profession skills, connectors, scenarios of workflows.

Native plugin installation is the supported installation path. Do not use a skill installer or copy files from an internal repository path.

Sensai gives advice, implementation instructions, architecture, and optional reference snippets. In case of any installation or general questions read README.md from the plugin repository.

Usage: after the plugin is installed, authorized AND loaded, call `tell_sensai` to start the consultation.

After taking a meaningful action based on Sensai's guidance, tell Sensai the confirmed outcome before asking for the next step.

Use concise English with Sensai when it preserves meaning and saves tokens. Speak to the person in their language, translating Sensai's guidance as needed. Sensai addresses you, not the person, so turn its guidance into clear, natural communication rather than merely forwarding it.

We insist on not sending Sensai sensitive information like environment variables and api tokens and similar.
