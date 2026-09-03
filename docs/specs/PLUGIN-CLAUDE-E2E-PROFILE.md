# PLUGIN-CLAUDE-E2E-PROFILE: Local Claude authorization baseline

## Purpose

The production Sensai end-to-end check runs from the person's local machine.
It needs a separate, persistent Claude profile so that each test starts cleanly
without asking the person to sign in to Claude again. It never transfers Claude
authorization, browser data, or Google data to a server.

## Persistent contents

The provisioner accepts one explicit Claude credential file and verifies that it
contains a Claude login. It writes only that login into its own profile. It does
not copy Claude settings, conversation history, plugins, marketplace state,
browser data, or MCP/Sensai authorization.

At provisioning time, protected local metadata records a one-way fingerprint of
the exact minimal login record. Before every fresh test run, the fingerprint
must still match. A different but structurally valid login record is therefore
rejected rather than silently becoming the identity used by the test.

The target profile must be outside the development directory, separate from the
chosen source Claude profile, and must not already exist. The only accepted
location is under the local Linux home share directory, for example
`~/.local/share/sensai-claude-e2e`; Windows-mounted paths such as `/mnt/c/...`
are not accepted. Provisioning checks that every private directory actually has
mode 0700 and every private file actually has mode 0600. It therefore never
claims protection merely because it requested a permission change.

Provisioning cannot overwrite either an existing test profile or a working
Claude profile. Its directory is private to the local user; the credential and
manifest files are readable only by that user. A profile-local lock prevents two
provision or test operations from sharing its state.

The command is explicit. `--dry-run` validates the requested source and target
without creating any file. `--detect-source` means exactly the credential file
under the currently configured `CLAUDE_CONFIG_DIR` (or its standard default).
If that file is absent or invalid, the command stops; it does not search another
Claude profile. `--source-credentials` names a concrete alternative.

```sh
uv run python scripts/provision_claude_e2e_profile.py \
  --profile "$HOME/.local/share/sensai-claude-e2e" \
  --detect-source --dry-run
```

After the dry run has been reviewed, the same command with `--provision` creates
the profile. Provisioning itself does not start Claude, open a browser, install
a plugin, or contact Sensai.

## One test run

`create_fresh_run` creates a new child directory under the persistent profile,
copies the one Claude-login record into it, and supplies all of these isolated
locations to the future Claude process:

- `HOME`
- `CLAUDE_CONFIG_DIR`
- `CLAUDE_SECURESTORAGE_CONFIG_DIR`
- `CLAUDE_CODE_PLUGIN_CACHE_DIR`
- `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_DATA_HOME`
- `TMPDIR`, `TMP`, and `TEMP`

The run selects `claude-sonnet-5`. It includes an empty private `work`
directory. Every future Claude subprocess must use that exact directory as its
current working directory; it must not run from this repository or from the
persistent profile. The run starts with no plugin, MCP, Sensai, cache, or prior
conversation state. Its directory is removed after the caller exits, including
after a failed test. The persistent baseline remains only for the next new run.

## Boundaries

This component prepares local state only. It does not prove that the copied
Claude login works, that Google consent succeeds, that Sensai is reachable, or
that installation works. The later production E2E owns those facts and must use
the public plugin and production Sensai server.
