"""The audit log: one line per write-tool call, never file contents.

Every call to write_file, edit_file, append_file, create_directory,
move_file and delete_file appends one JSON line to ``audit.log`` in the state
directory (``FILES_MCP_STATE_DIR``, default ``~/.files-mcp``): when, which
tool, the mount-relative path(s) the client passed, which OAuth client
called, and whether it worked. Refusals are logged too -- a stream of
read-only or version-mismatch refusals is worth seeing.

Contents never go in, and neither do host paths. A ``dry_run`` edit changes
nothing and is not logged.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastmcp.server.dependencies import get_access_token

logger = logging.getLogger(__name__)


def log_path() -> Path:
    state_dir = os.environ.get("FILES_MCP_STATE_DIR", "").strip()
    base = Path(state_dir).expanduser() if state_dir else Path.home() / ".files-mcp"
    return base / "audit.log"


def _client_id() -> str:
    """The OAuth client behind this call, or "local" over stdio and unauthenticated HTTP."""
    try:
        token = get_access_token()
    except Exception:  # no request context at all
        token = None
    return token.client_id if token is not None else "local"


def record(tool: str, paths: list[str], result: str) -> None:
    """Append one line. A failure to log is reported, but never undoes or fails the call."""
    line = json.dumps(
        {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "paths": paths,
            "client": _client_id(),
            "result": result,
        },
        ensure_ascii=False,
    )
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 on creation: client IDs and paths are nobody else's business.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        logger.warning("Could not write the audit log at %s: %s", path, exc)
