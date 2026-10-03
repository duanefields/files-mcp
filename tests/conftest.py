"""Suite-wide fixtures: a real mount tree built in tmp_path, and a clean server.

Everything runs against real files on the local disk -- no mocking of the
filesystem -- because the rules being tested (symlinks, exclusion, Unicode
normalization) are exactly the ones a mock would get wrong.

The tree, under ``tmp_path``::

    outside/secret.txt              never reachable
    council/                        mount "council", ro
      council.md
      advisors/strategy/persona.md
      advisors/strategy/memory/_now.md
      .git/config                   excluded by the mount
      .DS_Store                     excluded globally
      .files-mcp-tmp-abc            always excluded
      binary.bin
      big.txt                       over the read limit
      café.md                       stored with an NFD name
      link-out -> ../outside        escapes
      link-secret -> ../outside/secret.txt
      link-git -> .git/config       points at an excluded file
      link-advisors -> advisors     stays inside
    other/                          mount "other", rw
      readme.md
      link-out -> ../outside        escapes
      link-secret -> ../outside/secret.txt
"""

import os
import unicodedata

import pytest
import yaml

from files_mcp import server
from files_mcp.config import load_config

FILES_ENV = [
    "FILES_MCP_CONFIG",
    "FILES_MCP_TRANSPORT",
    "FILES_MCP_HOST",
    "FILES_MCP_PORT",
    "FILES_MCP_STATELESS",
    "FILES_MCP_AUTH",
    "FILES_MCP_PASSWORD",
    "FILES_MCP_BASE_URL",
    "FILES_MCP_STATE_DIR",
]

# Small, so the over-the-limit path is cheap to exercise.
MAX_READ_BYTES = 4096

NFD_NAME = unicodedata.normalize("NFD", "café.md")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    for name in FILES_ENV:
        monkeypatch.delenv(name, raising=False)
    # The audit log lives in the state dir; it must never be the real one.
    monkeypatch.setenv("FILES_MCP_STATE_DIR", str(tmp_path / "state"))
    server._config = None
    server._health_cache = None
    yield
    server._config = None
    server._health_cache = None


@pytest.fixture
def tree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret\n")

    council = tmp_path / "council"
    (council / "advisors" / "strategy" / "memory").mkdir(parents=True)
    (council / "council.md").write_text("# Council\n\nThe whole council.\n")
    (council / "advisors" / "strategy" / "persona.md").write_text("# Strategy\n")
    (council / "advisors" / "strategy" / "memory" / "_now.md").write_text("now\n")
    (council / ".git").mkdir()
    (council / ".git" / "config").write_text("[core]\n")
    (council / ".DS_Store").write_bytes(b"\x00\x01")
    (council / ".files-mcp-tmp-abc").write_text("half written")
    (council / "binary.bin").write_bytes(b"PK\x03\x04\x00\x00binary")
    (council / "big.txt").write_text(
        "".join(f"line {i:05d}\n" for i in range(1, 2001))  # 22,000 bytes
    )
    (council / NFD_NAME).write_text("accented\n")
    os.symlink("../outside", council / "link-out")
    os.symlink("../outside/secret.txt", council / "link-secret")
    os.symlink(".git/config", council / "link-git")
    os.symlink("advisors", council / "link-advisors")

    other = tmp_path / "other"
    other.mkdir()
    (other / "readme.md").write_text("other mount\n")
    os.symlink("../outside", other / "link-out")
    os.symlink("../outside/secret.txt", other / "link-secret")

    return tmp_path


def write_config(tmp_path, body: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


@pytest.fixture
def config(tree):
    path = write_config(
        tree,
        {
            "mounts": [
                {"name": "council", "path": str(tree / "council"), "mode": "ro",
                 "exclude": [".git"]},
                {"name": "other", "path": str(tree / "other"), "mode": "rw"},
            ],
            "exclude": [".DS_Store"],
            "limits": {"max_read_bytes": MAX_READ_BYTES, "max_files_per_read": 5},
        },
    )
    loaded = load_config(path)
    server._config = loaded
    return loaded


def text_of(result):
    content = result.content
    if isinstance(content, list):
        return "\n".join(getattr(block, "text", str(block)) for block in content)
    return content
