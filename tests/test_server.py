"""Tool behavior against a real mount tree.

What is pinned: that nothing outside a mount is reachable through any tool,
that one bad path in a batch does not sink the rest, the pagination contract,
and that errors tell the model what to do next.
"""

import asyncio

import pytest

from files_mcp import fs, server
from tests.conftest import MAX_READ_BYTES, text_of


# ----------------------------------------------------------------------
# list_mounts
# ----------------------------------------------------------------------


async def test_list_mounts_names_each_with_its_mode(config):
    result = await server.list_mounts()

    assert result.structured_content["mounts"] == [
        {"name": "council", "mode": "ro"},
        {"name": "other", "mode": "rw"},
    ]
    assert "council (read-only)" in text_of(result)
    assert "start with the mount name" in text_of(result)


async def test_list_mounts_never_shows_host_paths(tree, config):
    result = await server.list_mounts()

    assert str(tree) not in text_of(result)
    assert str(tree) not in str(result.structured_content)


# ----------------------------------------------------------------------
# list_directory
# ----------------------------------------------------------------------


async def test_list_directory_shows_visible_entries(config):
    result = await server.list_directory(path="council")
    names = [item["name"] for item in result.structured_content["items"]]

    # Not here: .git and .DS_Store (excluded), the temp file, and the three
    # links that point outside the mount or at something excluded.
    assert names == [
        "advisors", "big.txt", "binary.bin", "café.md", "council.md", "link-advisors",
    ]


async def test_list_directory_reports_type_size_and_modified(config):
    result = await server.list_directory(path="council")
    items = {item["name"]: item for item in result.structured_content["items"]}

    assert items["advisors"]["type"] == "dir"
    assert items["link-advisors"]["type"] == "dir"
    assert items["council.md"]["type"] == "file"
    assert items["council.md"]["size"] == len("# Council\n\nThe whole council.\n")
    assert items["council.md"]["path"] == "council/council.md"
    assert "T" in items["council.md"]["modified"]


async def test_list_directory_returns_nfc_names(config):
    result = await server.list_directory(path="council")

    assert "café.md" in [item["name"] for item in result.structured_content["items"]]


async def test_list_directory_recursive_walks_the_tree(config):
    result = await server.list_directory(path="council/advisors", recursive=True)
    paths = [item["path"] for item in result.structured_content["items"]]

    assert paths == [
        "council/advisors/strategy",
        "council/advisors/strategy/memory",
        "council/advisors/strategy/memory/_now.md",
        "council/advisors/strategy/persona.md",
    ]


async def test_recursive_listing_does_not_descend_symlinked_folders(config):
    """Otherwise a link could loop the walk, or list one folder twice."""
    result = await server.list_directory(path="council", recursive=True)
    paths = [item["path"] for item in result.structured_content["items"]]

    assert "council/link-advisors" in paths
    assert not any(p.startswith("council/link-advisors/") for p in paths)
    assert not any("/.git" in p for p in paths)


async def test_list_directory_of_a_file_points_to_read_files(config):
    result = await server.list_directory(path="council/council.md")

    assert "read_files" in result.structured_content["error"]


async def test_list_directory_of_a_missing_folder_is_not_found(config):
    result = await server.list_directory(path="council/nowhere")

    assert "was not found" in result.structured_content["error"]


async def test_empty_folder_says_so(tree, config):
    (tree / "council" / "empty").mkdir()
    result = await server.list_directory(path="council/empty")

    assert text_of(result) == "council/empty is empty."
    assert result.structured_content["total"] == 0


# ----------------------------------------------------------------------
# Pagination
# ----------------------------------------------------------------------


@pytest.fixture
def many(tree, config):
    folder = tree / "other" / "many"
    folder.mkdir()
    for i in range(1342):
        (folder / f"f{i:04d}.md").write_text("x")
    return folder


async def test_first_page_reports_the_total(many):
    result = await server.list_directory(path="other/many", limit=200)

    assert text_of(result).startswith("Showing 1-200 of 1,342")
    assert result.structured_content["count"] == 200
    assert result.structured_content["total"] == 1342
    assert result.structured_content["offset"] == 0
    assert result.structured_content["limit"] == 200


async def test_later_page_continues_where_the_last_stopped(many):
    result = await server.list_directory(path="other/many", limit=200, offset=1200)

    assert text_of(result).startswith("Showing 1,201-1,342 of 1,342")
    assert result.structured_content["items"][0]["name"] == "f1200.md"
    assert result.structured_content["count"] == 142


async def test_a_complete_page_has_no_showing_line(config):
    result = await server.list_directory(path="other")

    assert not text_of(result).startswith("Showing")
    assert result.structured_content["total"] == result.structured_content["count"] == 1


async def test_offset_past_the_end_says_so(many):
    result = await server.list_directory(path="other/many", offset=5000)

    assert "past the end" in text_of(result)
    assert result.structured_content["total"] == 1342


@pytest.mark.parametrize("limit, offset", [(0, 0), (-1, 0), (10, -1)])
async def test_bad_pagination_is_refused(config, limit, offset):
    result = await server.list_directory(path="council", limit=limit, offset=offset)

    assert "error" in result.structured_content


# ----------------------------------------------------------------------
# read_files
# ----------------------------------------------------------------------


async def test_read_files_returns_several_files_in_one_call(config):
    result = await server.read_files(
        paths=["council/council.md", "council/advisors/strategy/persona.md"]
    )
    files = result.structured_content["files"]

    assert [f["content"] for f in files] == ["# Council\n\nThe whole council.\n", "# Strategy\n"]
    assert result.structured_content["errors"] == 0
    assert "=== council/council.md (version " in text_of(result)


async def test_version_is_a_short_content_hash(config):
    result = await server.read_files(paths=["council/council.md"])

    version = result.structured_content["files"][0]["version"]
    assert version == fs.version_of(b"# Council\n\nThe whole council.\n")
    assert len(version) == 16


async def test_one_bad_path_does_not_stop_the_rest(config):
    result = await server.read_files(
        paths=["council/council.md", "council/missing.md", "other/readme.md"]
    )
    files = result.structured_content["files"]

    assert files[0]["content"].startswith("# Council")
    assert "was not found" in files[1]["error"]
    assert files[2]["content"] == "other mount\n"
    assert result.structured_content["errors"] == 1
    assert "Read 2 of 3 file(s). 1 failed" in text_of(result)


async def test_binary_files_are_an_error_not_bytes(config):
    result = await server.read_files(paths=["council/binary.bin"])

    assert "not UTF-8 text" in result.structured_content["files"][0]["error"]


async def test_reading_a_folder_points_to_list_directory(config):
    result = await server.read_files(paths=["council/advisors"])

    assert "list_directory" in result.structured_content["files"][0]["error"]


async def test_nfc_path_reads_nfd_file(config):
    result = await server.read_files(paths=["council/café.md"])

    assert result.structured_content["files"][0]["content"] == "accented\n"


async def test_head_returns_the_first_lines(config):
    result = await server.read_files(paths=["council/council.md"], head=1)

    assert result.structured_content["files"][0]["content"] == "# Council\n"
    assert "first 1 lines" in text_of(result)


async def test_tail_returns_the_last_lines(config):
    result = await server.read_files(paths=["council/council.md"], tail=1)

    assert result.structured_content["files"][0]["content"] == "The whole council.\n"


async def test_head_and_tail_are_exclusive(config):
    result = await server.read_files(paths=["council/council.md"], head=1, tail=1)

    assert "not both" in result.structured_content["error"]


@pytest.mark.parametrize("arg", [{"head": 0}, {"tail": -2}])
async def test_line_counts_must_be_positive(config, arg):
    result = await server.read_files(paths=["council/council.md"], **arg)

    assert "positive" in result.structured_content["error"]


async def test_too_many_paths_is_refused(config):
    result = await server.read_files(paths=["council/council.md"] * 6)

    assert "at most 5 paths" in result.structured_content["error"]


async def test_no_paths_is_refused(config):
    result = await server.read_files(paths=[])

    assert "at least one path" in result.structured_content["error"]


async def test_a_large_file_needs_head_or_tail(tree, config):
    result = await server.read_files(paths=["council/big.txt"])
    error = result.structured_content["files"][0]["error"]

    assert "over the 4,096-byte read limit" in error
    assert "head or tail" in error


async def test_head_of_a_large_file_reads_within_the_limit(tree, config):
    result = await server.read_files(paths=["council/big.txt"], head=3)
    item = result.structured_content["files"][0]

    assert item["content"] == "line 00001\nline 00002\nline 00003\n"
    # The version is the whole file's, since it is what a write will check.
    assert item["version"] == fs.version_of((tree / "council/big.txt").read_bytes())


async def test_tail_of_a_large_file_reads_within_the_limit(tree, config):
    result = await server.read_files(paths=["council/big.txt"], tail=2)
    item = result.structured_content["files"][0]

    assert item["content"] == "line 01999\nline 02000\n"
    assert item["version"] == fs.version_of((tree / "council/big.txt").read_bytes())


async def test_head_of_a_large_file_stops_at_the_limit(config):
    result = await server.read_files(paths=["council/big.txt"], head=100_000)
    item = result.structured_content["files"][0]

    assert len(item["content"].encode()) <= MAX_READ_BYTES
    assert item["content"].endswith("\n")
    assert item["total_lines"] is None


async def test_a_slow_read_fails_instead_of_hanging(config, monkeypatch):
    """An online-only cloud placeholder can block a read until it downloads."""
    monkeypatch.setattr(server, "FS_TIMEOUT_SECONDS", 0.05)

    def stuck(*args):
        import time

        time.sleep(0.5)

    monkeypatch.setattr(fs, "read_text", stuck)
    result = await asyncio.wait_for(server.read_files(paths=["council/council.md"]), 2)

    assert "may not be downloaded" in result.structured_content["files"][0]["error"]


async def test_a_permission_failure_is_reported_not_raised(config, monkeypatch):
    def denied(*args):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(fs, "read_text", denied)
    result = await server.read_files(paths=["council/council.md"])

    assert "Operation not permitted" in result.structured_content["files"][0]["error"]


# ----------------------------------------------------------------------
# Escapes, for every tool
# ----------------------------------------------------------------------

ESCAPES = [
    ("council/../outside/secret.txt", "not allowed"),
    ("/etc/passwd", "no mount named"),
    ("", "list_mounts"),
    ("council/link-out", "outside the 'council' mount"),
    ("council/link-secret", "outside the 'council' mount"),
    ("council/.git", "was not found"),
    ("council/.git/config", "was not found"),
    ("council/link-git", "was not found"),
]


def _error_of(result):
    structured = result.structured_content
    if "files" in structured:
        return structured["files"][0]["error"]
    return structured["error"]


@pytest.mark.parametrize("path, message", ESCAPES)
async def test_list_directory_refuses_escapes(tree, config, path, message):
    result = await server.list_directory(path=path)

    assert message in _error_of(result)
    assert "top secret" not in text_of(result)
    assert str(tree) not in text_of(result)


@pytest.mark.parametrize("path, message", ESCAPES)
async def test_read_files_refuses_escapes(tree, config, path, message):
    result = await server.read_files(paths=[path])

    assert message in _error_of(result)
    assert "top secret" not in text_of(result)
    assert str(tree) not in text_of(result)


@pytest.mark.parametrize("path, message", ESCAPES)
async def test_search_text_refuses_escapes(tree, config, path, message):
    result = await server.search_text(query="secret", path=path)

    assert message in _error_of(result)
    assert "top secret" not in text_of(result)
    assert str(tree) not in text_of(result)


@pytest.mark.parametrize("path, message", ESCAPES)
async def test_get_file_info_refuses_escapes(tree, config, path, message):
    result = await server.get_file_info(path=path)

    assert message in _error_of(result)
    assert str(tree) not in text_of(result)
