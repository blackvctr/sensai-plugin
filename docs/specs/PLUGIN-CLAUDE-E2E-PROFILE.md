# PLUGIN-CLAUDE-E2E-PROFILE: Local Claude authorization baseline

## Purpose

The production Sensai end-to-end check runs from the person's local machine.
It needs a separate, persistent Claude profile so that each test starts cleanly
without asking the person to sign in to Claude again. It never transfers Claude
authorization, browser data, or Google data to a server.

## Persistent contents

The normal provisioner accepts one explicit private Linux Claude credential file
and verifies that it contains the two minimal Claude auth records: `claudeAiOauth`
and `oauthAccount`. Their files must belong to the local user, have mode `0600`,
contain no symlink in their paths, and be outside mounted and development
directories. It writes only those two records into its own profile. It does not
copy other Claude settings, conversation history, plugins, marketplace state,
browser data, or MCP/Sensai authorization.

The current working Claude profile can be a shared or mounted copy which cannot
meet that contract. `--trust-current-credentials-once` is the one explicit
exception: it reads exactly the currently configured Claude credential file once
after the person has approved the migration, reduces it to the Claude login in
memory, combines it with only `oauthAccount` from the current private main
Claude config, and writes a new private baseline. It accepts no arbitrary source
path, cannot be used for a dry run, and never refreshes the baseline later. This
is a deliberate trust event, not a claim that the shared source is protected.
When the current credentials directory is explicitly overridden, that override
selects only `.credentials.json`; `oauthAccount` still comes from the approved
main file in the local home directory.
It still rejects a symlink in the configured source path: the configured path
must point directly to the already approved current directory before migration.

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
Claude profile. `--source-credentials` names a concrete credential alternative
and must be paired with its concrete `--source-account-config` file.

```sh
uv run python scripts/provision_claude_e2e_profile.py \
  --profile "$HOME/.local/share/sensai-claude-e2e" \
  --detect-source --dry-run
```

After the dry run has been reviewed, the same command with `--provision` creates
the profile. Provisioning itself does not start Claude, open a browser, install
a plugin, or contact Sensai.

For the explicitly approved one-time migration from the configured current
profile, use a new target and no source-path argument:

```sh
uv run python scripts/provision_claude_e2e_profile.py \
  --profile "$HOME/.local/share/sensai-claude-e2e" \
  --trust-current-credentials-once --provision
```

If the baseline must later be replaced, create a new separately named profile
through another explicit migration. The runner never rereads the old source.

## One test run

`create_fresh_run` creates a new child directory under the persistent profile,
copies the two minimal Claude auth records into its isolated configuration, and
supplies all of these isolated locations to the future Claude process:

- `claudeAiOauth` is written only to
  `CLAUDE_SECURESTORAGE_CONFIG_DIR/.credentials.json`;
- `oauthAccount` is written only to `CLAUDE_CONFIG_DIR/.claude.json`.

- `HOME`
- `CLAUDE_CONFIG_DIR`
- `CLAUDE_SECURESTORAGE_CONFIG_DIR`
- `CLAUDE_CODE_PLUGIN_CACHE_DIR`
- `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_DATA_HOME`
- `TMPDIR`, `TMP`, and `TEMP`

Controlled installation acceptance fixes `claude-sonnet-5` as local test policy.
The neutral public README does not recommend a model. The run includes an empty private `work`
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

## Controlled installation acceptance

`claude_production_e2e.py` is a local controlled-acceptance test, not an
interpreter for the public README. Its fixed local policy limits Claude to four
allowed actions and defines the canonical new-chat URI. The test validates the
observed action order after the run. Beyond normal Claude Code behavior, it adds
no prompt to the exact public Russian installation request.

The test keeps only in-memory facts about the two visible messages: their
non-whitespace length, Cyrillic and Latin letter counts, and a boolean for
Markdown code. This safety filter rejects code-shaped replies. It is not a
semantic proof that a reply is helpful, complete, or truthful. No visible
message text is written to the report or retained after stream parsing.
