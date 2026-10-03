# Running as a background service on macOS

How to host the HTTP server on an unattended Mac and reach it from a Claude
connector over a Cloudflare Tunnel. This is the sibling of weather-mcp's and
notes-mcp's deployment docs, and most of it is the same; the part that is not
is privacy controls, below.

Real values for a specific host (its name, the public hostname, the port it
was given) belong in the gitignored `docs/local/`, not here.

## Config first

The server refuses to start without a valid mount table. Copy
`config.example.yaml` to `~/.files-mcp/config.yaml` and edit it. Each mount's
path must exist and be a folder, or startup fails with a message naming the
mount. The config is read once; restart the service after changing it.

## Privacy controls are the risky part

Reading files in most of the home folder is protected, and a LaunchAgent cannot
answer a privacy prompt. When one is pending, `open()` blocks with nothing in
the log. Running the same command over SSH works, which is misleading: your
shell inherits an approval the LaunchAgent does not have.

### Full Disk Access

Grant it to the **resolved** interpreter, not the venv symlink:

```bash
readlink -f .venv/bin/python
```

`uv` shares one interpreter across every project on the same Python version, so
if other uv-managed servers on the host already have Full Disk Access, this
probably resolves to a binary that is already granted, and there is nothing to
do. The same sharing means a uv Python upgrade moves the binary and silently
voids the grant for every such server at once. `/health` reports the resolved
path (with `~` for the home directory), and `scripts/healthcheck.sh` fails
when it differs from `EXPECTED_PYTHON`.

To grant it: System Settings → Privacy & Security → Full Disk Access → `+`,
then `Cmd+Shift+G` to type the path. Clicking Allow on a popup is often not
enough. uv's interpreters are ad-hoc signed with an empty identifier, so an
approval bound to a signing identity does not stick, but an explicit entry
recorded against the path does.

### Cloud-synced folders (File Provider)

A folder under `~/Library/CloudStorage/` (Dropbox, iCloud Drive, and others,
often reached through a symlink such as `~/Dropbox`) is a File Provider
location. macOS can treat File Provider access as a permission separate from
Full Disk Access. If it does, reads fail with `Operation not permitted`, and
`/health` reports that mount as `readable: false`.

Three other File Provider behaviors are handled in the server:

- **launchd forbids downloading placeholders.** A file that is online-only is
  "dataless": `ls -lO` shows the `dataless` flag, and it has a size but no
  bytes on disk. Reading it normally makes the sync client download it, but
  launchd starts its jobs with that download switched off (the
  `IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES` I/O policy), so under a
  LaunchAgent every such read fails immediately with `Resource deadlock
  avoided` (EDEADLK). Listing still works, and the same read from a terminal
  or over SSH succeeds, which makes this look like a permissions problem. It
  is not one. The server switches the policy on at startup with
  `setiopolicy_np`.
- **Slow downloads.** With downloads allowed, a read blocks until the file
  arrives. Every filesystem call runs in a worker thread under a 20-second
  timeout, so a slow download returns an error saying to try again rather
  than hanging the request. Keeping mounted folders available offline in the
  sync client makes this rare.
- **Decomposed filenames.** Names can be stored in NFD. The server compares
  and returns names in NFC.

## Invoke the interpreter directly, not `uv run`

`uv run` spawns the interpreter as a child, so launchd ends up supervising the
wrapper, and killing the job leaves the real server holding the port. Point
`ProgramArguments` at the venv's interpreter.

## Template

Save as `~/Library/LaunchAgents/com.example.files-mcp.plist`, replacing the
placeholders. It contains a secret, so `chmod 600` it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.files-mcp</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/Code/files-mcp/.venv/bin/python</string>
    <string>-m</string>
    <string>files_mcp</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/Code/files-mcp</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>FILES_MCP_TRANSPORT</key><string>http</string>
    <key>FILES_MCP_HOST</key><string>127.0.0.1</string>
    <key>FILES_MCP_PORT</key><string>18794</string>
    <key>FILES_MCP_AUTH</key><string>password</string>
    <key>FILES_MCP_PASSWORD</key><string>REPLACE-WITH-A-LONG-RANDOM-VALUE</string>
    <key>FILES_MCP_BASE_URL</key><string>https://files.example.com</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USERNAME/.files-mcp/server.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.files-mcp/server.log</string>
</dict>
</plist>
```

```bash
chmod 600 ~/Library/LaunchAgents/com.example.files-mcp.plist
launchctl load ~/Library/LaunchAgents/com.example.files-mcp.plist
curl -s http://127.0.0.1:18794/health
```

`RunAtLoad` only helps after a reboot if the host logs in automatically: a
LaunchAgent starts with the user session.

## Ingress

Bind to loopback and add an ingress entry to the tunnel the host already runs:

```yaml
ingress:
  - hostname: files.example.com
    service: http://127.0.0.1:18794
  - service: http_status:404
```

A `service:` must be a key of the same list item as its `hostname`. Written as
a separate `- service:` entry it becomes a catch-all, and the tunnel refuses to
start on its next boot, taking every hostname down with it. A running tunnel
keeps its old config in memory, so this stays invisible until a restart.
Validate after every edit:

```bash
cloudflared --config ~/.cloudflared/config.yml tunnel ingress validate
```

**No Cloudflare Access application in front of the hostname.** Access intercepts
the OAuth callbacks and breaks the connector handshake.

## Monitoring

`scripts/healthcheck.sh` polls `/health` and reports to a dead-man's-switch;
`scripts/self-update.sh` pulls the tracked branch and restarts the service when
it moves. They read `~/.files-mcp/check.env` and `~/.files-mcp/update.env`; both
must be `chmod 600`, since a ping URL is a capability.

```
*/10 * * * * /Users/USERNAME/Code/files-mcp/scripts/healthcheck.sh >> /Users/USERNAME/.files-mcp/check.log 2>&1
*/15 * * * * /Users/USERNAME/Code/files-mcp/scripts/self-update.sh >> /Users/USERNAME/.files-mcp/update.log 2>&1
```

Use a **distinct** healthchecks.io UUID per service, so one service's pings
cannot mask another's silence.

The health check fails when the server does not answer, when any mount cannot
be listed (almost always a missing or voided privacy grant), and when the
interpreter has moved away from `EXPECTED_PYTHON`.

## Cutover order

The issuer is baked into each connector's registration, so:

1. Set the final `FILES_MCP_BASE_URL`.
2. Start HTTP mode on the host.
3. Verify over the tunnel from off-network.
4. Add the connector in Claude and authorize once.

MCP Inspector passing is not sufficient evidence. Test against Claude directly.
