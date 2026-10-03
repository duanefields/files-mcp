# Scope and results

What was tested, on which surface, and what happened. Phase 1 does not start
until every phase 0 gate below passes. Host-specific detail stays in
`docs/local/`.

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

### Gate 1: `read_files` returns `council/council.md` and `council/advisors/strategy/persona.md` in one call

| Surface | Result | Setup quirks |
| --- | --- | --- |
| claude.ai, iPad | | |
| Claude iPhone app | | |
| Cowork, MacBook | | |
| Claude Code, MacBook | | |

### Gate 2: works after the host reboots, with nobody at the keyboard

### Gate 3: an online-only placeholder returns content or fails clearly, without hanging

### Gate 4: loading an advisor on the iPad is faster than through the Dropbox connector

| Path | Calls | Time |
| --- | --- | --- |
| Dropbox connector | | |
| files-mcp `read_files` | 1 | |
