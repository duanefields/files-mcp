"""Phase 2: the write tools, the version rules, atomicity, and the audit log.

The fixture's "council" mount is read-only and "other" is read-write. What is
pinned, beyond each tool doing its job:

- Nothing outside a mount, excluded, or behind an escaping link can be
  created, changed, moved, or deleted, through any write tool.
- A read-only mount refuses every write.
- Overwrites need a matching version, and edits are all-or-nothing.
- No temp file is ever left behind.
- Every call is audited, refusals included, and contents never reach the log.
"""

import json
import os

import pytest

from files_mcp import audit, fs, server, write
from files_mcp.config import TEMP_PREFIX, load_config
from files_mcp.server import Edit
from tests.conftest import text_of, write_config


def error_of(result):
    return result.structured_content["error"]


def version(path):
    return fs.version_of(path.read_bytes())


def temp_files(folder):
    return [p for p in folder.rglob("*") if p.name.startswith(TEMP_PREFIX)]


def audit_lines():
    path = audit.log_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def other(tree, config):
    return tree / "other"


# ----------------------------------------------------------------------
# write_file
# ----------------------------------------------------------------------


async def test_write_creates_a_file_and_its_folders(other):
    result = await server.write_file(path="other/a/b/new.md", content="hello\n")

    assert (other / "a/b/new.md").read_text() == "hello\n"
    assert result.structured_content["created"] is True
    assert result.structured_content["version"] == version(other / "a/b/new.md")
    assert "Created other/a/b/new.md" in text_of(result)


async def test_overwrite_without_a_version_is_refused(other):
    result = await server.write_file(path="other/readme.md", content="clobbered")

    assert "read it first" in error_of(result)
    assert (other / "readme.md").read_text() == "other mount\n"


async def test_overwrite_with_a_stale_version_names_the_current_one(other):
    current = version(other / "readme.md")
    result = await server.write_file(
        path="other/readme.md", content="clobbered", if_version="0000000000000000"
    )

    assert "has changed since it was read" in error_of(result)
    assert current in error_of(result)
    assert (other / "readme.md").read_text() == "other mount\n"


async def test_overwrite_with_the_current_version_replaces_the_file(other):
    read = await server.read_files(paths=["other/readme.md"])
    current = read.structured_content["files"][0]["version"]

    result = await server.write_file(path="other/readme.md", content="new\n", if_version=current)

    assert (other / "readme.md").read_text() == "new\n"
    assert result.structured_content["previous_version"] == current
    assert result.structured_content["created"] is False


async def test_a_version_for_a_file_that_is_gone_is_refused(other):
    result = await server.write_file(path="other/gone.md", content="x", if_version="abc")

    assert "no longer exists" in error_of(result)
    assert not (other / "gone.md").exists()


async def test_writing_over_a_folder_is_refused(other):
    (other / "folder").mkdir()
    result = await server.write_file(path="other/folder", content="x", if_version="abc")

    assert "is a folder" in error_of(result)


async def test_write_limit_is_enforced_before_writing(other):
    result = await server.write_file(path="other/big.md", content="x" * 1_048_577)

    assert "over the 1,048,576-byte write limit" in error_of(result)
    assert not (other / "big.md").exists()


async def test_overwrite_keeps_the_file_permissions(other):
    os.chmod(other / "readme.md", 0o600)
    await server.write_file(
        path="other/readme.md", content="x", if_version=version(other / "readme.md")
    )

    assert (other / "readme.md").stat().st_mode & 0o777 == 0o600


async def test_writes_leave_no_temp_files(other):
    await server.write_file(path="other/new.md", content="one")
    await server.write_file(path="other/new.md", content="two", if_version=version(other / "new.md"))

    assert temp_files(other) == []


async def test_a_failed_rename_leaves_the_original_and_no_temp_file(other, monkeypatch):
    def broken(src, dst):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(write.os, "replace", broken)
    result = await server.write_file(
        path="other/readme.md", content="new", if_version=version(other / "readme.md")
    )

    assert "Input/output error" in error_of(result)
    assert (other / "readme.md").read_text() == "other mount\n"
    assert temp_files(other) == []


async def test_writing_through_an_inside_link_changes_the_target(other):
    os.symlink("readme.md", other / "alias.md")
    await server.write_file(
        path="other/alias.md", content="via link\n", if_version=version(other / "readme.md")
    )

    assert (other / "readme.md").read_text() == "via link\n"
    assert (other / "alias.md").is_symlink()


# ----------------------------------------------------------------------
# edit_file
# ----------------------------------------------------------------------


@pytest.fixture
def notes(other):
    path = other / "notes.md"
    path.write_text("# Notes\n\n- [ ] call Sam\n- [ ] buy milk\n")
    return path


async def test_edit_replaces_exactly_once_and_returns_a_diff(notes):
    result = await server.edit_file(
        path="other/notes.md", edits=[Edit(old_text="- [ ] call Sam", new_text="- [x] call Sam")]
    )

    assert notes.read_text() == "# Notes\n\n- [x] call Sam\n- [ ] buy milk\n"
    diff = result.structured_content["diff"]
    assert "-- [ ] call Sam" in diff and "+- [x] call Sam" in diff
    assert result.structured_content["version"] == version(notes)


async def test_edits_apply_in_order(notes):
    await server.edit_file(path="other/notes.md", edits=[
        Edit(old_text="call Sam", new_text="call Alex"),
        Edit(old_text="call Alex", new_text="email Alex"),
    ])

    assert "- [ ] email Alex" in notes.read_text()


async def test_zero_matches_fails_and_names_the_edit(notes):
    before = notes.read_text()
    result = await server.edit_file(path="other/notes.md", edits=[
        Edit(old_text="call Sam", new_text="call Alex"),
        Edit(old_text="walk the dog", new_text="x"),
    ])

    assert "Edit 2 of 2: old_text matches 0 times" in error_of(result)
    assert notes.read_text() == before  # the first edit was not applied either


async def test_two_matches_fails_and_says_to_add_context(notes):
    result = await server.edit_file(path="other/notes.md", edits=[Edit(old_text="- [ ]", new_text="x")])

    assert "Edit 1 of 1: old_text matches 2 times" in error_of(result)
    assert "surrounding text" in error_of(result)


async def test_whitespace_is_not_normalized(notes):
    result = await server.edit_file(
        path="other/notes.md", edits=[Edit(old_text="-  [ ] call Sam", new_text="x")]
    )

    assert "matches 0 times" in error_of(result)


async def test_edit_with_a_stale_version_is_refused(notes):
    result = await server.edit_file(
        path="other/notes.md",
        edits=[Edit(old_text="call Sam", new_text="call Alex")],
        if_version="0000000000000000",
    )

    assert "has changed since it was read" in error_of(result)
    assert "call Sam" in notes.read_text()


async def test_edit_with_the_current_version_applies(notes):
    await server.edit_file(
        path="other/notes.md",
        edits=[Edit(old_text="call Sam", new_text="call Alex")],
        if_version=version(notes),
    )

    assert "call Alex" in notes.read_text()


async def test_dry_run_writes_nothing(notes):
    before = notes.read_text()
    result = await server.edit_file(
        path="other/notes.md", edits=[Edit(old_text="call Sam", new_text="call Alex")], dry_run=True
    )

    assert notes.read_text() == before
    assert "+- [ ] call Alex" in result.structured_content["diff"]
    assert text_of(result).startswith("Dry run: nothing was written.")
    assert temp_files(notes.parent) == []


async def test_empty_old_text_is_refused(notes):
    result = await server.edit_file(path="other/notes.md", edits=[Edit(old_text="", new_text="x")])

    assert "empty old_text" in error_of(result)


async def test_editing_a_missing_file_is_not_found(other):
    result = await server.edit_file(path="other/missing.md", edits=[Edit(old_text="a", new_text="b")])

    assert "was not found" in error_of(result)


async def test_editing_a_binary_file_is_refused(other):
    (other / "data.bin").write_bytes(b"\x00\x01abc")
    result = await server.edit_file(path="other/data.bin", edits=[Edit(old_text="abc", new_text="x")])

    assert "not UTF-8 text" in error_of(result)


# ----------------------------------------------------------------------
# append_file
# ----------------------------------------------------------------------


async def test_append_adds_a_newline_when_the_file_lacks_one(other):
    (other / "log.md").write_text("first")
    await server.append_file(path="other/log.md", content="second\n")

    assert (other / "log.md").read_text() == "first\nsecond\n"


async def test_append_adds_no_extra_newline(other):
    (other / "log.md").write_text("first\n")
    result = await server.append_file(path="other/log.md", content="second\n")

    assert (other / "log.md").read_text() == "first\nsecond\n"
    assert result.structured_content["version"] == version(other / "log.md")


async def test_append_creates_the_file_and_folders(other):
    result = await server.append_file(path="other/logs/2026/oct.md", content="entry\n")

    assert (other / "logs/2026/oct.md").read_text() == "entry\n"
    assert result.structured_content["created"] is True


async def test_append_to_an_empty_file_adds_no_newline(other):
    (other / "empty.md").write_text("")
    await server.append_file(path="other/empty.md", content="x")

    assert (other / "empty.md").read_text() == "x"


async def test_append_to_a_binary_file_is_refused(other):
    (other / "data.bin").write_bytes(b"\x00\x01")
    result = await server.append_file(path="other/data.bin", content="x")

    assert "not UTF-8 text" in error_of(result)
    assert (other / "data.bin").read_bytes() == b"\x00\x01"


# ----------------------------------------------------------------------
# create_directory
# ----------------------------------------------------------------------


async def test_create_directory_makes_parents(other):
    result = await server.create_directory(path="other/a/b/c")

    assert (other / "a/b/c").is_dir()
    assert result.structured_content["created"] is True


async def test_create_directory_that_exists_succeeds(other):
    (other / "here").mkdir()
    result = await server.create_directory(path="other/here")

    assert result.structured_content["created"] is False
    assert "already exists" in text_of(result)


async def test_create_directory_over_a_file_is_refused(other):
    result = await server.create_directory(path="other/readme.md")

    assert "already exists as a file" in error_of(result)


# ----------------------------------------------------------------------
# move_file
# ----------------------------------------------------------------------


async def test_move_renames_a_file(other):
    result = await server.move_file(source="other/readme.md", destination="other/intro.md")

    assert (other / "intro.md").read_text() == "other mount\n"
    assert not (other / "readme.md").exists()
    assert "Moved file other/readme.md to other/intro.md" in text_of(result)


async def test_move_creates_destination_folders(other):
    await server.move_file(source="other/readme.md", destination="other/archive/2026/readme.md")

    assert (other / "archive/2026/readme.md").exists()


async def test_move_refuses_to_overwrite(other):
    (other / "taken.md").write_text("keep me")
    result = await server.move_file(source="other/readme.md", destination="other/taken.md")

    assert "already exists" in error_of(result)
    assert (other / "taken.md").read_text() == "keep me"
    assert (other / "readme.md").exists()


async def test_move_a_folder(other):
    (other / "folder").mkdir()
    (other / "folder/a.md").write_text("a")
    await server.move_file(source="other/folder", destination="other/renamed")

    assert (other / "renamed/a.md").read_text() == "a"


async def test_move_a_folder_inside_itself_is_refused(other):
    (other / "folder").mkdir()
    result = await server.move_file(source="other/folder", destination="other/folder/sub")

    assert "inside itself" in error_of(result)


async def test_case_only_rename(other):
    await server.move_file(source="other/readme.md", destination="other/README.md")

    names = os.listdir(other)
    assert "README.md" in names and "readme.md" not in names


async def test_mount_root_cannot_be_moved(other):
    result = await server.move_file(source="other", destination="other/x")

    assert "top folder" in error_of(result)


async def test_symlinks_are_not_moved(other):
    os.symlink("readme.md", other / "alias.md")
    result = await server.move_file(source="other/alias.md", destination="other/moved.md")

    assert "symbolic link" in error_of(result)
    assert (other / "readme.md").exists()


async def test_move_across_writable_mounts(tree):
    (tree / "spare").mkdir()
    server._config = load_config(write_config(tree, {"mounts": [
        {"name": "other", "path": str(tree / "other"), "mode": "rw"},
        {"name": "spare", "path": str(tree / "spare"), "mode": "rw"},
    ]}))

    result = await server.move_file(source="other/readme.md", destination="spare/readme.md")

    assert (tree / "spare/readme.md").read_text() == "other mount\n"
    assert not (tree / "other/readme.md").exists()
    assert "error" not in result.structured_content


async def test_move_into_a_read_only_mount_is_refused(other, tree):
    result = await server.move_file(source="other/readme.md", destination="council/readme.md")

    assert "read-only" in error_of(result)
    assert (other / "readme.md").exists()
    assert not (tree / "council/readme.md").exists()


# ----------------------------------------------------------------------
# delete_file
# ----------------------------------------------------------------------


async def test_delete_a_file_says_no_copy_is_kept(other):
    result = await server.delete_file(path="other/readme.md")

    assert not (other / "readme.md").exists()
    assert "keeps no copy" in text_of(result)


async def test_delete_an_empty_folder(other):
    (other / "empty").mkdir()
    await server.delete_file(path="other/empty")

    assert not (other / "empty").exists()


async def test_a_folder_holding_only_ds_store_counts_as_empty(other):
    (other / "empty").mkdir()
    (other / "empty/.DS_Store").write_bytes(b"\x00")
    await server.delete_file(path="other/empty")

    assert not (other / "empty").exists()


async def test_delete_refuses_a_folder_with_contents(other):
    (other / "full").mkdir()
    (other / "full/a.md").write_text("a")
    result = await server.delete_file(path="other/full")

    assert "isn't empty" in error_of(result)
    assert (other / "full/a.md").exists()


async def test_delete_refuses_a_folder_with_only_hidden_contents(other):
    """Never recursive, even for files the client cannot see."""
    (other / "repo").mkdir()
    (other / "repo/.files-mcp-tmp-x").write_text("x")
    result = await server.delete_file(path="other/repo")

    assert "isn't empty" in error_of(result)


async def test_mount_root_cannot_be_deleted(other):
    result = await server.delete_file(path="other")

    assert "top folder" in error_of(result)


async def test_delete_missing_is_not_found(other):
    result = await server.delete_file(path="other/missing.md")

    assert "was not found" in error_of(result)


async def test_symlinks_are_not_deleted(other):
    os.symlink("readme.md", other / "alias.md")
    result = await server.delete_file(path="other/alias.md")

    assert "symbolic link" in error_of(result)
    assert (other / "readme.md").exists() and (other / "alias.md").is_symlink()


# ----------------------------------------------------------------------
# Read-only mounts and escapes, for every write tool
# ----------------------------------------------------------------------


def write_calls(path):
    return {
        "write_file": lambda: server.write_file(path=path, content="x"),
        "edit_file": lambda: server.edit_file(path=path, edits=[Edit(old_text="C", new_text="x")]),
        "append_file": lambda: server.append_file(path=path, content="x"),
        "create_directory": lambda: server.create_directory(path=path),
        "delete_file": lambda: server.delete_file(path=path),
        "move_from": lambda: server.move_file(source=path, destination="other/moved.md"),
        "move_to": lambda: server.move_file(source="other/readme.md", destination=path),
    }


def snapshot(root):
    """Every path and file's bytes under root, except the audit log's state dir."""
    return sorted(
        (str(p.relative_to(root)), p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        for p in root.rglob("*")
        if p.relative_to(root).parts[0] != "state"
    )


@pytest.mark.parametrize("tool", list(write_calls("x")))
async def test_read_only_mount_refuses_every_write(tree, config, tool):
    before = snapshot(tree)
    path = "council/council.md" if tool != "move_to" else "council/new.md"

    result = await write_calls(path)[tool]()

    assert "read-only" in error_of(result)
    assert snapshot(tree) == before


ESCAPES_RW = [
    ("other/../outside/new.md", "not allowed"),
    ("/etc/new.md", "no mount named"),
    ("", "list_mounts"),
    ("other/link-out/new.md", "outside the 'other' mount"),
    ("other/link-secret", "outside the 'other' mount"),
    ("other/.DS_Store", "was not found"),
    ("other/.files-mcp-tmp-x", "was not found"),
]


@pytest.mark.parametrize("tool", list(write_calls("x")))
@pytest.mark.parametrize("path, message", ESCAPES_RW)
async def test_writes_refuse_escapes(tree, config, tool, path, message):
    before = snapshot(tree)

    result = await write_calls(path)[tool]()

    assert message in error_of(result)
    assert str(tree) not in text_of(result)
    assert snapshot(tree) == before
    assert (tree / "outside/secret.txt").read_text() == "top secret\n"


# ----------------------------------------------------------------------
# Audit log
# ----------------------------------------------------------------------


async def test_every_write_is_audited_without_contents(other):
    await server.write_file(path="other/diary.md", content="my private thoughts")
    await server.append_file(path="other/diary.md", content="more private thoughts")

    lines = audit_lines()
    assert [(line["tool"], line["paths"], line["result"]) for line in lines] == [
        ("write_file", ["other/diary.md"], "ok"),
        ("append_file", ["other/diary.md"], "ok"),
    ]
    assert lines[0]["client"] == "local"
    assert "T" in lines[0]["time"]
    assert "private" not in audit.log_path().read_text()


async def test_refusals_are_audited(config):
    await server.write_file(path="council/new.md", content="x")
    await server.write_file(path="other/../outside/x", content="x")

    results = [line["result"] for line in audit_lines()]
    assert results[0].startswith("refused: The 'council' mount is read-only")
    assert results[1].startswith("refused: '..' is not allowed")


async def test_moves_audit_both_paths(other):
    await server.move_file(source="other/readme.md", destination="other/intro.md")

    assert audit_lines()[0]["paths"] == ["other/readme.md", "other/intro.md"]


async def test_dry_runs_are_not_audited(notes):
    await server.edit_file(
        path="other/notes.md", edits=[Edit(old_text="call Sam", new_text="x")], dry_run=True
    )

    assert audit_lines() == []


async def test_the_audit_log_is_private(other):
    await server.write_file(path="other/new.md", content="x")

    assert audit.log_path().stat().st_mode & 0o777 == 0o600


async def test_audit_names_the_oauth_client(other, monkeypatch):
    class Token:
        client_id = "client-123"

    monkeypatch.setattr(audit, "get_access_token", lambda: Token())
    await server.write_file(path="other/new.md", content="x")

    assert audit_lines()[0]["client"] == "client-123"


async def test_an_nfc_path_writes_to_the_existing_nfd_file(other):
    """Otherwise an accented name typed by a client would create a near-duplicate."""
    import unicodedata

    nfd = unicodedata.normalize("NFD", "café.md")
    (other / nfd).write_text("old\n")

    refused = await server.write_file(path="other/café.md", content="new\n")
    assert "already exists" in error_of(refused)

    await server.write_file(path="other/café.md", content="new\n", if_version=version(other / nfd))
    cafes = [n for n in os.listdir(other) if unicodedata.normalize("NFC", n) == "café.md"]
    assert len(cafes) == 1
    assert (other / cafes[0]).read_text() == "new\n"
