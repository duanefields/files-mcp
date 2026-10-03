"""Transport selection, startup config checks, and the health endpoint.

Nothing here starts a server; `mcp.run` is replaced so that main() can be
observed deciding what to do.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from files_mcp import fs, server
from tests.conftest import write_config


@pytest.fixture
def config_env(tree, monkeypatch):
    path = write_config(tree, {
        "mounts": [{"name": "council", "path": str(tree / "council"), "mode": "ro"}],
    })
    monkeypatch.setenv("FILES_MCP_CONFIG", str(path))
    return path


def test_stdio_is_the_default(config_env):
    with patch.object(server.mcp, "run") as run:
        server.main()

    run.assert_called_once_with()


def test_a_bad_config_stops_startup(tree, monkeypatch, caplog):
    path = write_config(tree, {
        "mounts": [{"name": "council", "path": str(tree / "nope"), "mode": "ro"}],
    })
    monkeypatch.setenv("FILES_MCP_CONFIG", str(path))

    with patch.object(server.mcp, "run") as run, pytest.raises(SystemExit):
        server.main()

    run.assert_not_called()
    assert "'council'" in caplog.text


def test_http_binds_loopback_on_the_files_port(config_env, monkeypatch):
    monkeypatch.setenv("FILES_MCP_TRANSPORT", "http")

    with patch.object(server.mcp, "run") as run:
        server.main()

    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 18794
    assert run.call_args.kwargs["stateless_http"] is True


def test_sessions_can_be_restored(config_env, monkeypatch):
    monkeypatch.setenv("FILES_MCP_TRANSPORT", "http")
    monkeypatch.setenv("FILES_MCP_STATELESS", "false")

    with patch.object(server.mcp, "run") as run:
        server.main()

    assert run.call_args.kwargs["stateless_http"] is False


def test_an_open_port_without_auth_warns(config_env, monkeypatch, caplog):
    monkeypatch.setenv("FILES_MCP_TRANSPORT", "http")
    monkeypatch.setenv("FILES_MCP_HOST", "0.0.0.0")

    with patch.object(server.mcp, "run"):
        server.main()

    assert "read every mounted folder" in caplog.text


def test_localhost_without_auth_is_silent(config_env, monkeypatch, caplog):
    monkeypatch.setenv("FILES_MCP_TRANSPORT", "http")

    with patch.object(server.mcp, "run"):
        server.main()

    assert "read every mounted folder" not in caplog.text


def test_password_auth_uses_the_files_scope(config_env, tree, monkeypatch):
    monkeypatch.setenv("FILES_MCP_TRANSPORT", "http")
    monkeypatch.setenv("FILES_MCP_AUTH", "password")
    monkeypatch.setenv("FILES_MCP_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("FILES_MCP_BASE_URL", "https://files.example.com")
    monkeypatch.setenv("FILES_MCP_STATE_DIR", str(tree / "state"))

    with patch.object(server.mcp, "run"):
        server.main()

    assert server.mcp.auth.required_scopes == ["files:manage"]
    server.mcp.auth = None


async def test_health_reports_mounts_without_host_paths(tree, config):
    body = json.loads((await server.health(MagicMock())).body)

    assert body["status"] == "ok"
    assert body["mounts"] == [
        {"name": "council", "mode": "ro", "readable": True},
        {"name": "other", "mode": "rw", "readable": True},
    ]
    assert str(tree) not in json.dumps(body)
    assert "council.md" not in json.dumps(body)


async def test_health_hides_the_home_directory(config):
    import os

    body = json.loads((await server.health(MagicMock())).body)

    assert os.path.expanduser("~") not in body["python"]


async def test_health_degrades_when_a_mount_is_unreadable(tree, config, monkeypatch):
    real_listdir = server.os.listdir

    def listdir(path):
        if str(path).endswith("council"):
            raise PermissionError(1, "Operation not permitted")
        return real_listdir(path)

    monkeypatch.setattr(server.os, "listdir", listdir)
    body = json.loads((await server.health(MagicMock())).body)

    assert body["status"] == "degraded"
    assert body["mounts"][0]["readable"] is False
    assert body["mounts"][1]["readable"] is True


async def test_health_is_cached(config, monkeypatch):
    """Public and unauthenticated: a poll loop must not touch the disk every hit."""
    calls = []
    real_listdir = server.os.listdir
    monkeypatch.setattr(server.os, "listdir", lambda p: calls.append(p) or real_listdir(p))

    for _ in range(5):
        await server.health(MagicMock())

    roots = {m.root for m in config.mounts.values()}
    assert len([c for c in calls if c in roots]) == 2  # once per mount


def test_startup_allows_cloud_placeholder_downloads(config_env, monkeypatch):
    """Under launchd the policy starts off, and every online-only read fails."""
    monkeypatch.setattr(server.sys, "platform", "darwin")
    allow = MagicMock(return_value=True)
    monkeypatch.setattr(server.fs, "allow_dataless_downloads", allow)

    with patch.object(server.mcp, "run"):
        server.main()

    allow.assert_called_once_with()


def test_a_failed_policy_change_warns(config_env, monkeypatch, caplog):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.fs, "allow_dataless_downloads", lambda: False)

    with patch.object(server.mcp, "run"):
        server.main()

    assert "Resource deadlock avoided" in caplog.text


@pytest.mark.skipif(sys.platform != "darwin", reason="setiopolicy_np is macOS only")
def test_dataless_policy_is_really_set():
    import ctypes

    assert fs.allow_dataless_downloads() is True
    libc = ctypes.CDLL(None)
    assert libc.getiopolicy_np(3, 0) == 2  # materialize dataless files: on


def test_dataless_policy_is_a_no_op_off_macos(monkeypatch):
    monkeypatch.setattr(fs.sys, "platform", "linux")

    assert fs.allow_dataless_downloads() is False
