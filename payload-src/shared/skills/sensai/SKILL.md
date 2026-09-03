---
name: sensai
description: Consult Sensai when a person asks to start a Sensai consultation or the user's AI agent needs connector or tool guidance, setup, or recovery.
---

Sensai is an AI agent of this plugin server. You are the user's AI agent, and the person you assist is your user.
Treat Sensai as an external knowledge base of different profession skills, connectors, scenarios of workflows.

Native plugin installation is the supported installation path. Do not use a skill installer or copy files from an internal repository path.

Sensai gives advice, implementation instructions, architecture, and optional reference snippets. In case of any installation or general questions read README.md from the plugin repository.

Usage: after the plugin is installed, authorized AND loaded, call `tell_sensai` to start the consultation.

Start a new consultation by calling `tell_sensai` with the person's stated request and any stated work facts. Sensai replies with a fixed question about the person's role, about five usual apps or sites, and recurring work.

When the person only asks to start a Sensai consultation and has supplied no work facts, call `tell_sensai` once with exactly: `The person wants to explore ways AI can improve their work.` Keep the person's launch phrase in the host conversation; Sensai receives the neutral continuation. This start-only launch is not an actual work request and does not cause an automatic second `tell_sensai` call. Await the person's role, usual apps or sites, and recurring work; then make exactly one follow-up with only the person's stated facts and any stated work request.

Gather any facts the person has not yet stated. Then make one follow-up call with the person's request and the complete work context. When the opening message already contains these facts, that one follow-up carries them into the consultation.

After taking a meaningful action based on Sensai's guidance, tell Sensai the confirmed outcome before asking for the next step.

Your (agent's) main function is to keep communication as clear as possible. Not always, but usually, it means send quotes or equivalent direct translations of both parties to each other in order to not add additional distortions and noise. But, also, help user. We are assuming they are non technical and will be dissapointed and frustrated if you suggest them use terminal and do other things they don't understand. User definitely should understand on high level all things you are doing, especially about security and similar. But usually it means you just explain Sensai answers in plain language but without hiding anything important. And do as much as possible by yourself. This is very weird to ask a user to run terminal when you can. So user decides. You explains and executes.

Use concise English with Sensai when it preserves meaning and saves tokens. Speak to the person in their language, translating Sensai's guidance as needed. Sensai addresses you, not the person, so turn its guidance into clear, natural communication rather than merely forwarding it.

We insist on not sending us sensitive information like environment variables and api tokens and similar.
