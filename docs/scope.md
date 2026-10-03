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
| Claude Code, MacBook | | |
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
