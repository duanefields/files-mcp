"""An MCP server for file access to a few configured folders."""

import os

# FastMCP's startup banner asks PyPI whether a newer FastMCP exists, so simply
# starting the server makes an outbound request nobody asked for. Set before
# importing FastMCP: its settings are a pydantic-settings object built at import
# time, so a later assignment has no effect. setdefault, so an operator who
# wants the check back can still set it in the environment -- but note the
# update setting is a literal, and only "off" disables it; "false" raises a
# ValidationError on import.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")

from .server import mcp  # noqa: E402

__all__ = ["mcp"]
