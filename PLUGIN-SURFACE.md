# Installed plugin surface

The published Sensai plugin contains only its platform manifests, one MCP configuration, its skill, and a checksum manifest. It does not ship local hooks, executable files, shell commands, or additional MCP servers.

The MCP configuration registers one remote HTTP MCP server:

```text
https://black-vector.com/sensai/mcp
```

The MCP server is an external service. Installing the plugin does not perform Google sign-in; a host may ask the person for consent before any later authorization flow.

## MCP security boundary

Before authorization, the MCP endpoint rejects requests and publishes OAuth protected-resource metadata for exactly `https://black-vector.com/sensai/mcp`. The public metadata names one authorization server, `https://black-vector.com/sensai/oauth`, and one MCP scope, `sensai:use`.

The authorization server supports authorization-code and refresh-token grants with PKCE S256. Google sign-in requests only the `openid` and `email` scopes; it does not request Gmail, Drive, Calendar, or other Google service scopes.

Authorization protects access to the MCP endpoint. It does not mean that consultation messages are local: authorized consultation messages explicitly sent through the MCP tool are sent to Sensai and processed as described in [PRIVACY.md](PRIVACY.md). The plugin does not automatically send the host workspace, files, terminal output, or the rest of a host session.

Repository development files, including root scripts and `.githooks`, are not included in the installed plugin artifact.
