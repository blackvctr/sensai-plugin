# PLUGIN-PACKAGE-001: Deterministic Plugin Payloads

## Purpose

Build two public, independently installable Sensai plugin payloads from one reviewed source tree: one for Codex and one for Claude. Packaging must be reproducible and fail closed when an unreviewed file appears.

## Boundaries

This specification covers source layout, generated file layout, deterministic hashing, strict source inclusion, and payload isolation. It does not implement the builder, run Codex or Claude CLIs, publish a marketplace, contact the MCP server, or test authentication.

## Source Layout

Only these source files are eligible for payload generation:

```text
payload-src/
  shared/
    .mcp.json
    skills/sensai/SKILL.md
  codex/.codex-plugin/plugin.json
  claude/.claude-plugin/plugin.json
```

The builder code lives under `src/sensai_plugin/`; tests and documentation remain repository-only. The four paths above form an exact allowlist, not a glob. Every path must be a regular file reached through regular directories. A missing file, extra file or directory content, symlink, or path that resolves outside `payload-src/` is an error until the allowlist and this specification are deliberately changed.

## Generated Contract

Each build replaces, rather than merges into, its destination and creates:

```text
packages/
  codex/sensai/
    .codex-plugin/plugin.json
    .mcp.json
    skills/sensai/SKILL.md
    MANIFEST.sha256
  claude/sensai/
    .claude-plugin/plugin.json
    .mcp.json
    skills/sensai/SKILL.md
    MANIFEST.sha256
```

The platform manifest is copied only to its platform payload. The shared MCP configuration and human instructions are byte-identical in both payloads and originate only from `payload-src/shared`. `MANIFEST.sha256` contains every other regular payload file, sorted by POSIX relative path, as `<lowercase sha256><two spaces><relative path>\n`. It does not list itself. No generated content contains timestamps, source paths, random values, host-specific values, or machine-dependent line endings.

## Requirements

- **PLUGIN-PACKAGE-001-R01 Reproducibility:** Two builds from unchanged source produce identical
  relative file lists, identical bytes for every file, and valid identical SHA-256 manifests.
- **PLUGIN-PACKAGE-001-R02 Closed allowlist:** Unexpected regular files and directories, whether
  empty or containing files, raise `UnexpectedSourceFileError`; missing required files raise
  `MissingRequiredSourceFileError`;
  source symlinks, symlink substitution, and path escape raise `UnsafeSourceError`. Nothing is
  copied or silently ignored.
- **PLUGIN-PACKAGE-001-R03 Platform contracts:** The Codex payload contains
  `.codex-plugin/plugin.json`; the Claude payload contains `.claude-plugin/plugin.json`; each
  contains the same single Sensai `SKILL.md` and `.mcp.json` bytes. Those bytes exactly equal their
  respective files under `payload-src/shared`; platform manifests likewise derive byte-for-byte
  from their platform source.
- **PLUGIN-PACKAGE-001-R04 Public-safe payloads:** Payloads contain only regular files rooted below
  their own package directory. They contain no symlink, path escape, local absolute path, secret-like
  filename or content, private server import or reference, tests, build tooling, cache, development
  metadata, or unresolved `../` reference. Each unsafe source category is rejected independently.
- **PLUGIN-PACKAGE-001-R05 Independent roots:** Either platform directory can be relocated and
  validated from its own root. Every path referenced by its plugin manifest resolves inside that
  root and no installed file refers back to `payload-src`, repository build code, or the other
  platform payload.

## Failure Rules

The builder must fail before publishing either payload when source validation, copying, hashing, or post-build isolation validation fails. Partial output is not an accepted build. There is no fallback that drops unknown files, rewrites unsafe references, or reuses stale output.

## Evidence Map

| Requirement | Executable evidence |
| --- | --- |
| R01 | `test_plugin_package_001_r01_builds_are_byte_reproducible` |
| R02 | `test_plugin_package_001_r02_rejects_unexpected_source_entries`; `test_plugin_package_001_r02_rejects_missing_or_substituted_required_source` |
| R03 | `test_plugin_package_001_r03_platform_contracts_derive_shared_runtime_material` |
| R04 | `test_plugin_package_001_r04_rejects_unsafe_allowlisted_content`; `test_plugin_package_001_r04_rejects_secret_or_development_source_names` |
| R05 | `test_plugin_package_001_r05_each_payload_is_a_self_contained_root` |

## Contract Sources

- OpenAI Codex plugin root: `.codex-plugin/plugin.json`, root-level `skills/`, and root-level
  `.mcp.json`.
- Claude plugin root: `.claude-plugin/plugin.json` with all components, including `skills/` and
  `.mcp.json`, at the plugin root rather than inside `.claude-plugin/`.
- Claude installs plugins into a separate cache, so a package cannot rely on parent or source-tree
  paths.

Official references:

- <https://help.openai.com/en/articles/20001256-plugins-in-codex>
- <https://code.claude.com/docs/en/plugins>
- <https://code.claude.com/docs/en/plugins-reference>
- <https://code.claude.com/docs/en/plugin-marketplaces>
