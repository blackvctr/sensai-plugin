# PLUGIN-CLAUDE-LOCAL-INSTALL: Isolated Claude Code Installation

## Purpose

Prove that one exact release bundle built by the current release builder can be independently verified, installed through Claude Code's local marketplace flow, and discovered as an MCP-providing plugin without changing the user's real Claude profile.

## Acceptance

`sensai_plugin.claude_acceptance.installed_claude_plugin` is the stable public acceptance context. It yields the exact installed selector, version, MCP URL, and still-live isolated profile. The command-line script is only a wrapper around that API, and external acceptance tests must import the public module rather than a script's private helpers.

`scripts/test_claude_lifecycle.py --bundle <release-directory>` uses the locally installed Claude Code and performs one bounded lifecycle:

1. Copy the regular bundle files once into a private read-only snapshot, then invoke
   `scripts/verify_release.py` against that snapshot in a separate process. The snapshot binds its
   root and files to device, inode, mode, size, timestamp, and digest. Reject any replacement or
   mutation after verification, extraction, Claude installation, discovery, or before cleanup.
   The caller-provided bundle path itself must be a regular directory, not a symlink; this is checked
   before any path resolution or Claude command.
2. Extract the exact attested Claude marketplace archive once, rejecting unsafe or duplicate
   paths, size limit violations, non-regular entries, and any mismatch with release metadata.
3. Verify every plugin byte against `MANIFEST.sha256`, including the exact MCP attestation, then
   make every extracted file and directory read-only under a writable temporary parent.
4. Create a completely isolated temporary Claude profile, home, plugin cache, secure storage,
   temporary directory, and XDG roots.
5. Run the official commands `claude plugin marketplace add`,
   `claude plugin install sensai@sensai-local --scope user`, `claude plugin list --json`, and
   `claude mcp get plugin:sensai:sensai`.
6. Require the exact selector, release version, user scope, isolated install path, MCP namespace,
   transport, and release MCP URL. The advertised installed path is not trusted: its complete payload
   must be regular, contain no symlinks, and match the verified read-only marketplace payload
   byte-for-byte. Human-readable `claude mcp get` output is parsed as one exact plugin heading plus
   one exact `Type: http` and `URL: <release URL>` field; duplicate or suffix/prefix values fail.
7. Keep the isolated profile alive for the caller, then delete it and prove the real profile is
   unchanged. The guard watches only explicit Claude lifecycle surfaces: named settings files,
   plugin state files, the `sensai-local` marketplace and plugin-cache namespaces, the backup root,
   the home-level `.claude.json`, and Claude-specific XDG/secure-storage roots. Backup and cache
   roots are inspected shallowly; only the two `sensai-local` namespaces are traversed, with
   bounded depth, entry count, and file size. User history, projects, sessions, and unrelated
   cache subtrees are never recursively read, and a limit violation fails closed.

The lifecycle never rebuilds a release, installs an archive directly, modifies extracted release files, or uses the user's Claude profile. A tampered bundle fails before the first Claude command. Cleanup runs after a caller error and reports both the caller error and any cleanup failure. The temporary profile is rejected before any Claude command when it overlaps, is nested in, or contains any configured real Claude profile boundary, including the config root, plugin cache, secure storage, home-level configuration, XDG locations, and repository-local `.claude` state.

## Claim Boundary

This proves local marketplace installation and MCP configuration discovery. `claude mcp get` may report endpoint health, but this acceptance does not claim authentication, model behavior, or a successful MCP tool call.

Run from the plugin repository after building a bundle once:

```sh
uv run python scripts/test_claude_lifecycle.py --bundle /path/to/release
```

The durable automated real-CLI acceptance is:

```sh
uv run pytest -s -q tests/test_claude_release_lifecycle.py -k real_claude_cli
```
