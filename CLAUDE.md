# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code
in this repository.

## This repository is public

Assume every tracked file, and the git history, is world-readable forever. No
secrets, and no real hostnames, domain names, tunnel IDs, machine names, or
home paths in code, tests, docs, or commit messages. Use `files.example.com`,
"the host Mac", and `/Users/USERNAME`. Real deployment values live in the
gitignored `docs/local/`.

Run `./scripts/scan-secrets.sh` before every push. It greps tracked files for
personal-data shapes and for every literal in `docs/local/forbidden.txt`. CI
runs the shape patterns only, since the forbidden list is not in the repo.

`config.yaml` is never committed; `config.example.yaml` is the template.

## Commands

### Development Setup

```bash
uv sync
cp config.example.yaml ~/.files-mcp/config.yaml   # then edit the mounts

# Run the MCP server (stdio transport, default)
uv run files-mcp

# Run with HTTP transport
FILES_MCP_TRANSPORT=http uv run files-mcp

# A different config file
FILES_MCP_CONFIG=/path/to/config.yaml uv run files-mcp
```

### Testing

```bash
uv sync --extra test
uv run pytest
uv run pytest tests/test_paths.py
uv run pytest -k "escape"
```

The suite is entirely offline. Every test builds its mounts in `tmp_path`
(see `tests/conftest.py` for the tree) and never touches a real folder.

### Linting

```bash
uvx ruff@0.16.4 check src tests
```

The rule set is narrow on purpose (`E9`, `F`, `B`): bugs, not style. Pin the
version here and in `.github/workflows/ci.yml` together.

### CI

`.github/workflows/ci.yml` runs on push to `main`, on every pull request, and
weekly. Three jobs: `test` (macOS and Linux × Python 3.12 and 3.13), `lint`
(ruff plus the secret scan), and `audit` (`pip-audit` over the lockfile).

## Architecture Overview

An MCP server that gives remote clients file access to a few configured
folders ("mounts") on one machine, like Docker volume mounts. Clients see only
mount names; host paths never appear in a tool result, an error, or `/health`.

`docs/spec.md` is the design, with the phase plan. Phases 0 and 1 are built,
all read-only: `list_mounts`, `list_directory`, `read_files`, `glob`,
`search_text`, `get_file_info`. Phase 2 adds writes and the audit log.
`docs/scope.md` records what was tested where.

1. **src/files_mcp/config.py**: loads and validates `config.yaml`. Mount roots
   are resolved with `realpath` at load time. A bad config is a startup failure,
   never a runtime workaround.
2. **src/files_mcp/paths.py**: `resolve()` is the **only** place a client
   string becomes a host path. Every tool goes through it. Changes here need a
   test in `tests/test_paths.py` and the per-tool escape tests in
   `tests/test_server.py`.
3. **src/files_mcp/fs.py**: blocking filesystem work (listing, reading,
   glob matching, search, file info, versions). Synchronous, called from a
   worker thread. `walk()` is the one directory walker; glob and search are
   built on it so they inherit its exclusion and symlink rules. Do not add a
   second walker.
4. **src/files_mcp/server.py**: the tools, `/health`, and transport selection
   in `main()`.
5. **src/files_mcp/auth.py**: password-guarded OAuth 2.1 provider, copied from
   weather-mcp. Domain-independent apart from the scope (`files:manage`), the
   env prefix, and the login page title. **Keep it that way**, so a fix in one
   project can be carried to the others by reading a diff.

## Key Implementation Details

- FastMCP 3.x. Tools are bare `@mcp.tool` on `async def`; the docstrings are
  written **at the model**, not at a developer.
- Tools return `ToolResult` with text and `structured_content`. Errors return
  `_error_result` and are never raised. Every message says what to do next.
- Paginated tools follow imessage-mcp: `_validate_pagination`, a "Showing
  1-200 of 1,342" first line when there is more, and `{items, count, total,
  offset, limit}`.
- Transport env vars are read inside `main()`, not at import time. The config
  is loaded there too, so a bad one stops startup.
- `stateless_http` defaults to `True`; remote clients dial from a pool of
  addresses and a stateful session wedges.

## Path rules (do not weaken)

- The first segment is a mount name. A leading `/` is ignored.
- `..` is refused outright, not normalized.
- Symlinks are resolved, and the result must sit inside the mount's resolved
  root. A link that escapes is refused even for reads, and left out of listings.
- Excluded names, including anything a symlink resolves to, are reported as
  **not found**, never "excluded". `.files-mcp-tmp-*` is always excluded.
- Names are compared and returned in NFC. On a filesystem that does not
  normalize (Linux CI), `_locate` falls back to an NFC scan of the parent.
- No case handling of our own; APFS is case-insensitive already. Glob
  patterns match case-sensitively against names as stored.
- `glob` resolves the pattern's literal prefix with `resolve()`. A missing or
  excluded prefix (`paths.NotFound`) is zero matches, not an error, so the
  two stay indistinguishable; an escaping prefix is still an error.

## Things that bite on macOS

- **Cloud-synced placeholders block reads.** Reading an online-only file waits
  for the download. Every filesystem call goes through `server._blocking`,
  with a `FS_TIMEOUT_SECONDS` timeout, so a request returns an error rather than
  hanging. Do not add a filesystem call outside it.
- **launchd forbids downloading placeholders.** A LaunchAgent starts with the
  dataless-file I/O policy off, so reading an online-only file fails at once
  with EDEADLK ("Resource deadlock avoided") even though a terminal can read
  it. `main()` calls `fs.allow_dataless_downloads()` to switch it on. Do not
  remove it; without it the server cannot read most of a Dropbox mount.
- **Privacy grants.** A LaunchAgent cannot answer a prompt. Full Disk Access is
  bound to the resolved interpreter path, which a uv Python upgrade moves.
  `/health` reports it; see `docs/deployment-macos.md`.
- **`version`** is the first 16 hex characters of the SHA-256 of the *whole*
  file, even for `head`/`tail` reads, because phase 2 writes check against it.
