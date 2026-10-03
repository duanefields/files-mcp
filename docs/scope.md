# Scope and results

What was tested, on which surface, and what happened. Phase 1 does not start
until every phase 0 gate below passes. Host-specific detail stays in
`docs/local/`.

## Decision

Phase 0: **go**, 2026-10-03, on the owner's call. Gates 1 and 3 passed;
gate 2 (reboot) was waived and gate 4 (iPad timing) was not measured. Phase 1
started the same day.

## Phase 0, step 0a: local

| Check | Result | Notes |
| --- | --- | --- |
| Unit tests | Pass | 107 tests, offline, macOS, Python 3.12.12. Ruff clean, secret scan clean. |
| stdio | Pass | Scripted FastMCP client against the real council folder (read-only, through the `~/Dropbox` symlink into File Provider storage) on the development Mac. All three tools; `read_files` of `council.md` plus `persona.md` with two bad paths in the same call: 2 ms. |
| HTTP, no auth | Pass | Same script over streamable HTTP on loopback: 4 ms. `/health` reports the mount readable and the interpreter as `~/...`, with no host paths. |
| HTTP, password auth | Pass | Unauthenticated `POST /mcp` gets 401; OAuth metadata advertises `files:manage`; `/health` stays public. The full login flow is exercised in step 0b by a real connector. |
| Claude Code, interactive | Not yet | |

The development Mac's own shell already has access to its Dropbox folder, so
none of the above says anything about a LaunchAgent's privacy grants. That is
what step 0b is for.

## Phase 0, step 0b: production

Deployed 2026-10-03 as a LaunchAgent on the host Mac behind the existing
tunnel, with password OAuth. The connector-free checks below were run with a
scripted client over the public URL: dynamic registration, the password login
(a wrong password gets 401), PKCE token exchange, then MCP calls with the
bearer token.

**Found on first deploy: launchd forbids downloading online-only files.** The
mount listed fine, but every read failed with EDEADLK ("Resource deadlock
avoided"). The files were dataless Dropbox placeholders, and launchd starts
jobs with the dataless-file I/O policy off. A throwaway launchd job using the
server's own interpreter confirmed it: policy 1 (off) by default and the read
fails; after `setiopolicy_np` sets it to on, the same file reads and Dropbox
downloads it. The server now does that at startup. It was not a privacy
grant: Full Disk Access on the shared uv interpreter was enough, and no File
Provider prompt appeared.

### Gate 1: `read_files` returns `council/council.md` and `council/advisors/strategy/persona.md` in one call

| Surface | Result | Setup quirks |
| --- | --- | --- |
| claude.ai, iPad | Not tested | Assumed to match the iPhone app: same claude.ai connector, same account. |
| Claude iPhone app | Pass | Same test prompt as the desktop app; no setup quirks reported. |
| Cowork, MacBook | Pass | Works through the connector. In practice Cowork should prefer its local folder; see the routing note in docs/spec.md. |
| Claude Code, MacBook | Pass | Connected over HTTP. In practice Claude Code should prefer the local folder; see the routing note in docs/spec.md. |
| Claude desktop app | Pass | Connector added through claude.ai. list_mounts, one-call read of both files, a full strategy-advisor load, an excluded path (`council/.git/config`) returning the server's "not found", and `council/../council.md` refused by the server with "'..' is not allowed in paths". |
| Scripted client, public URL | Pass | Both files in one call, about 300 ms from the development Mac. |

### Gate 2: works after the host reboots, with nobody at the keyboard

Waived by the owner on 2026-10-03; the host was not rebooted for this test.
The LaunchAgent uses the same `RunAtLoad` and `KeepAlive` setup as the other
servers on the host, which do come back after a reboot, but this server
has not been seen to.

### Gate 3: an online-only placeholder returns content or fails clearly, without hanging

Pass. `_index.md` was dataless on the host; reading it in a `read_files`
batch returned its content and Dropbox materialized it (the batch of three
took 750 ms). A download that outlasts 20 seconds returns an error saying to
try again; that path is covered by a test, not yet seen live.

### Gate 4: loading an advisor on the iPad is faster than through the Dropbox connector

| Path | Calls | Time |
| --- | --- | --- |
| Dropbox connector | | |
| files-mcp `read_files` | 1 | |

## Phase 1: glob, search_text, get_file_info

Built 2026-10-03. 168 offline tests (escapes for every tool, excluded
prefixes indistinguishable from missing ones, `**`, NFC/NFD in patterns and
in file text, skipped large and binary files, line pagination). Checked over
stdio against the real council folder on the development Mac: all three
tools behave as specified.

Choices the spec left open:

- `glob` returns files only, matches case-sensitively, and supports `**` for
  any number of folders. The first segment must be a literal mount name.
  If the part before the first wildcard does not exist, the reply is the
  standard not-found error (identical for an excluded folder), so a mistyped
  folder is not mistaken for an empty result. Changed after the first
  connector test, where a model pointed out the ambiguity.
- `search_text`'s `glob` filter with one segment (`*.md`) matches the file
  name at any depth; with a `/` it matches the path below the searched
  folder. Lines longer than 500 characters are cut. Default `limit` is 100.
  The whole search runs under a 60-second timeout.
- `get_file_info` on a folder returns type and times, no size or version.
  `created` is null on platforms that do not expose a birth time.

## Phase 2: writes and the audit log

Built 2026-10-03. 282 offline tests. Every write tool is tested against
read-only refusal and every escape (`..`, an unknown mount, symlinks out,
excluded and temp names), each asserting the whole tree is byte-for-byte
unchanged afterward.

Choices the spec left open:

- `write_file` refuses a version for a file that no longer exists, rather
  than silently creating it. Replacing keeps the old file's permissions.
- `edit_file` checks the version (when given) against the file as it is now,
  applies edits sequentially, writes nothing if any edit fails, and skips the
  write entirely when the edits change nothing. Dry runs are not audited.
- `append_file` returns the new version, and refuses a file whose first 8 KB
  contains a NUL byte.
- `move_file` creates missing destination folders, allows a case-only rename
  on a case-insensitive disk, refuses to move a folder into itself, and falls
  back to copy-and-delete across volumes.
- `delete_file` treats a folder holding only `.DS_Store` as empty. Any other
  contents, visible or not, make it non-empty.
- Neither `move_file` nor `delete_file` acts on a symlink itself, or on a
  mount's top folder.
- Write tools run one at a time inside the server.

Deployed to the host Mac 2026-10-03, with the council mount switched to
`rw`. Checked over the public URL with the full OAuth login, in a throwaway
folder that was removed afterward: create, refused overwrite without a
version, refused edit with a stale version, edit with the current version,
append (newline added), read back, move into a new subfolder, refused delete
of a non-empty folder, file and folder deletes, and a refused write into
`.git` (not found). Each call took 80–120 ms; the first, about 300 ms. The
audit log recorded all eleven write-tool calls with the OAuth client ID and
no contents.
