# files-mcp

An MCP server that gives remote clients (Claude on the web, iPad and iPhone,
Claude Code, and anything else that speaks MCP) controlled access to a few
configured folders on one machine.

Think Docker volume mounts: a config file names each folder, says where it
really lives, and whether it is read-only or read-write. Clients see only the
mount names. Nothing else on the machine is reachable, and the server cannot
run commands.

**Status: phase 2: reads and writes.** See [docs/spec.md](docs/spec.md) for the full
design and the phase plan.

## Tools

| Tool | What it does |
| --- | --- |
| `list_mounts` | The mounts, and whether each is read-only |
| `list_directory` | A folder's entries with type, size and modified time; optionally recursive; paginated |
| `read_files` | Up to 25 text files in one call, each with a content `version`; `head` or `tail` for the first or last lines |
| `glob` | File paths matching a pattern like `notes/*/2026-*.md` or `notes/**/*.md`; paginated |
| `search_text` | Matching lines (path and line number) under a folder; case-insensitive substring or regex, optional file-name filter; paginated |
| `get_file_info` | Type, size, created and modified times, and the content `version` |
| `write_file` | Create a file, or replace one; replacing needs `if_version` from a recent read. Atomic |
| `edit_file` | Exact find-and-replace edits, all or nothing; returns a diff; `dry_run` to preview |
| `append_file` | Add to the end of a file, creating it if needed |
| `create_directory` | Create a folder and its parents |
| `move_file` | Move or rename within or between writable mounts; never overwrites |
| `delete_file` | Delete one file or one empty folder; never recursive |

Every path starts with a mount name, like `notes/2026/october.md`.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/USERNAME/files-mcp.git
cd files-mcp
uv sync
mkdir -p ~/.files-mcp
cp config.example.yaml ~/.files-mcp/config.yaml   # then edit the mounts
```

### Claude Code, over stdio

```bash
claude mcp add files -- uv run --directory /Users/USERNAME/Code/files-mcp files-mcp
```

### Over HTTP

```bash
FILES_MCP_TRANSPORT=http uv run files-mcp     # 127.0.0.1:18794/mcp
```

For a remote connector, put it behind a tunnel with password OAuth
(`FILES_MCP_AUTH=password`). See
[docs/deployment-macos.md](docs/deployment-macos.md), and
[.env.example](.env.example) for every setting.

## Safety

- Paths are resolved, symlinks included, and must stay inside their mount.
  `..` is refused.
- Excluded names (from the config, plus the server's own temp files) are
  invisible: not listed, not readable, and reported as not found.
- Only UTF-8 text is read or written. Per-file size and per-call file-count
  limits come from the config.
- Read-only mounts refuse every write. Replacing or editing a file checks a
  content version, so a stale client cannot overwrite a newer change.
- Every write-tool call, refusals included, is logged to
  `~/.files-mcp/audit.log`: time, tool, mount-relative paths, OAuth client,
  and result. Never file contents.
- `/health` is unauthenticated and reports only mount names, modes and whether
  each is readable. It never reports host paths or file names.

## License

MIT
