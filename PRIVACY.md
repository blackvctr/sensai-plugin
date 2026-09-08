# Privacy

## Consultation data

Sensai receives only the consultation messages explicitly sent to its MCP tool. It does not automatically receive the host workspace, files, terminal output, or the rest of a host session. After the initial onboarding response, Sensai processes consultation messages through its configured OpenAI model runtime. Do not send passwords, private keys, API keys, access tokens, or other secrets in consultation messages.

Sensai keeps live consultation and runtime data needed to continue a consultation. Request traces are metadata-only, but runtime conversation storage can contain consultation messages and responses.

## Google sign-in

Google sign-in requests the `openid` and `email` scopes only. It does not request Gmail, Drive, Calendar, or other Google service scopes. Sensai validates the Google identity and uses a stable Google subject to associate the Sensai account and its data.

## External services

The installed plugin configures one remote MCP endpoint: `https://black-vector.com/sensai/mcp`. Consultation messages are sent to Sensai over that connection. The configured OpenAI model runtime processes consultation messages after onboarding.

## Delete my data

The `forget_me` MCP tool deletes the authenticated person's live Sensai profile, consultation/runtime data, and Sensai OAuth authorization data. It does not promise deletion from backups, service logs, Google, OpenAI, pending anonymous OAuth attempts, or data recreated after the deletion.
