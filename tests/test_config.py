"""Loading and validating the mount table.

A bad config stops the server at startup, so every rejection here has to name
the mount and what to fix -- the operator reads it in a log, not a client.
"""

import pytest

from files_mcp.config import TEMP_PREFIX, ConfigError, load_config
from tests.conftest import write_config


def mount(path, name="council", mode="ro", **extra):
    return {"name": name, "path": str(path), "mode": mode, **extra}


def test_loads_mounts_and_limits(tree):
    config = load_config(write_config(tree, {
        "mounts": [mount(tree / "council")],
        "limits": {"max_files_per_read": 3},
    }))

    assert list(config.mounts) == ["council"]
    assert config.mounts["council"].root == (tree / "council").resolve()
    assert config.limits.max_files_per_read == 3
    assert config.limits.max_read_bytes == 1_048_576


def test_tilde_is_expanded(tree, monkeypatch):
    monkeypatch.setenv("HOME", str(tree))
    config = load_config(write_config(tree, {"mounts": [mount("~/council")]}))

    assert config.mounts["council"].root == (tree / "council").resolve()


def test_root_is_resolved_through_symlinks(tree):
    """Containment is checked against the real root, so the root must be real."""
    (tree / "alias").symlink_to(tree / "council")
    config = load_config(write_config(tree, {"mounts": [mount(tree / "alias")]}))

    assert config.mounts["council"].root == (tree / "council").resolve()


def test_global_and_mount_excludes_merge(tree):
    config = load_config(write_config(tree, {
        "mounts": [mount(tree / "council", exclude=[".git"])],
        "exclude": [".DS_Store"],
    }))
    council = config.mounts["council"]

    assert council.is_excluded(".git")
    assert council.is_excluded(".DS_Store")
    assert not council.is_excluded("council.md")


def test_temp_files_are_always_excluded(tree):
    config = load_config(write_config(tree, {"mounts": [mount(tree / "council")]}))

    assert config.mounts["council"].is_excluded(TEMP_PREFIX + "x1y2")


def test_config_path_comes_from_the_environment(tree, monkeypatch):
    path = write_config(tree, {"mounts": [mount(tree / "council")]})
    monkeypatch.setenv("FILES_MCP_CONFIG", str(path))

    assert "council" in load_config().mounts


@pytest.mark.parametrize(
    "body, message",
    [
        ({"mounts": []}, "at least one mount"),
        ({"mounts": [{"name": "Council", "path": ".", "mode": "ro"}]}, "lowercase slugs"),
        ({"mounts": [{"name": "a/b", "path": ".", "mode": "ro"}]}, "lowercase slugs"),
        ({"mounts": [{"name": "", "path": ".", "mode": "ro"}]}, "lowercase slugs"),
        ({"mounts": [{"name": "x", "path": ".", "mode": "write"}]}, "'ro' or 'rw'"),
        ({"mounts": [{"name": "x", "mode": "ro"}]}, "no path"),
        ({"mounts": [{"name": "x", "path": "/no/such/dir", "mode": "ro"}]}, "does not exist"),
        ({"mounts": [{"name": "x", "path": ".", "mode": "ro"}], "limits": {"max_read_bytes": 0}},
         "positive integer"),
        ({"mounts": [{"name": "x", "path": ".", "mode": "ro"}], "limits": {"max_reads": 5}},
         "Unknown limits"),
        ({"mounts": [{"name": "x", "path": ".", "mode": "ro"}], "exclude": ".git"},
         "list of glob patterns"),
    ],
)
def test_bad_configs_are_refused(tree, body, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tree, body))


def test_a_missing_path_names_the_mount(tree):
    with pytest.raises(ConfigError, match="'notes'"):
        load_config(write_config(tree, {"mounts": [mount(tree / "nope", name="notes")]}))


def test_a_file_is_not_a_mount(tree):
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(write_config(tree, {"mounts": [mount(tree / "council" / "council.md")]}))


def test_duplicate_names_are_refused(tree):
    with pytest.raises(ConfigError, match="more than once"):
        load_config(write_config(tree, {
            "mounts": [mount(tree / "council"), mount(tree / "other")],
        }))


def test_a_missing_config_says_where_to_put_one(tree):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tree / "absent.yaml")
