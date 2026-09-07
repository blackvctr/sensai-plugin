# Installed plugin surface

The published Sensai plugin contains only its platform manifests, one MCP configuration, its skill, and a checksum manifest. It does not ship local hooks, executable files, shell commands, or additional MCP servers.

The MCP configuration registers one remote HTTP MCP server:

```text
https://black-vector.com/sensai/mcp
```

The MCP server is an external service. Installing the plugin does not perform Google sign-in; a host may ask the person for consent before any later authorization flow.

Repository development files, including root scripts and `.githooks`, are not included in the installed plugin artifact.
