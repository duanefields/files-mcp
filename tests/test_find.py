"""Phase 1: glob, search_text, and get_file_info, against the real mount tree.

The same things are pinned as for the phase 0 tools: nothing outside a mount
or excluded is reachable, results are paginated honestly, and errors say what
to do next. Escapes for search_text and get_file_info are also covered in
test_server.py alongside the other tools.
"""

import sys
import unicodedata

import pytest

from files_mcp import fs, server
from tests.conftest import NFD_NAME, text_of


def paths_of(result):
    return [item["path"] for item in result.structured_content["items"]]


# ----------------------------------------------------------------------
# glob
# ----------------------------------------------------------------------


async def test_glob_matches_within_one_folder_level(config):
    result = await server.glob(pattern="council/advisors/*/persona.md")

    assert paths_of(result) == ["council/advisors/strategy/persona.md"]
    assert "council/advisors/strategy/persona.md" in text_of(result)


async def test_star_does_not_cross_folders(config):
    result = await server.glob(pattern="council/*.md")

    assert paths_of(result) == ["council/café.md", "council/council.md"]


async def test_double_star_matches_any_depth(config):
    result = await server.glob(pattern="council/**/*.md")

    assert paths_of(result) == [
        "council/advisors/strategy/memory/_now.md",
        "council/advisors/strategy/persona.md",
        "council/café.md",
        "council/council.md",
    ]


async def test_double_star_matches_zero_folders(config):
    result = await server.glob(pattern="council/**/council.md")

    assert paths_of(result) == ["council/council.md"]


async def test_glob_returns_files_only(config):
    result = await server.glob(pattern="council/advisors/*")

    assert paths_of(result) == []


async def test_glob_skips_excluded_and_escaping_entries(config):
    result = await server.glob(pattern="council/*")

    assert paths_of(result) == [
        "council/big.txt", "council/binary.bin", "council/café.md", "council/council.md",
    ]


async def test_glob_without_wildcards_names_one_file(config):
    assert paths_of(await server.glob(pattern="council/council.md")) == ["council/council.md"]
    assert paths_of(await server.glob(pattern="council/missing.md")) == []


async def test_excluded_prefix_looks_exactly_like_a_missing_one(config):
    excluded = await server.glob(pattern="council/.git/*")
    missing = await server.glob(pattern="council/nowhere/*")

    assert text_of(excluded) == "No files match council/.git/*."
    assert text_of(missing) == "No files match council/nowhere/*."
    assert excluded.structured_content["total"] == missing.structured_content["total"] == 0


async def test_glob_matching_is_case_sensitive(config):
    assert paths_of(await server.glob(pattern="council/C*.md")) == []


async def test_glob_matches_nfd_names_by_nfc_pattern(config):
    assert paths_of(await server.glob(pattern="council/caf?.md")) == ["council/café.md"]
    assert paths_of(await server.glob(pattern="council/" + NFD_NAME)) == ["council/café.md"]


async def test_glob_does_not_walk_through_symlinked_folders(config):
    result = await server.glob(pattern="council/**/persona.md")

    assert paths_of(result) == ["council/advisors/strategy/persona.md"]


async def test_glob_paginates(tree, config):
    folder = tree / "other" / "many"
    folder.mkdir()
    for i in range(1342):
        (folder / f"f{i:04d}.md").write_text("x")

    result = await server.glob(pattern="other/many/*.md")

    assert text_of(result).startswith("Showing 1-200 of 1,342")
    assert result.structured_content["total"] == 1342


@pytest.mark.parametrize(
    "pattern, message",
    [
        ("*/council.md", "must start with a mount name"),
        ("**/council.md", "must start with a mount name"),
        ("council/../outside/*", "not allowed"),
        ("nope/*.md", "no mount named"),
        ("council/link-out/*", "outside the 'council' mount"),
        ("", "list_mounts"),
    ],
)
async def test_glob_refuses_bad_patterns(tree, config, pattern, message):
    result = await server.glob(pattern=pattern)

    assert message in result.structured_content["error"]
    assert str(tree) not in text_of(result)


async def test_glob_never_reaches_outside_through_a_link(config):
    result = await server.glob(pattern="council/link-*")

    assert paths_of(result) == []


# ----------------------------------------------------------------------
# search_text
# ----------------------------------------------------------------------


def hits_of(result):
    return [(h["path"], h["line"], h["text"]) for h in result.structured_content["items"]]


async def test_search_is_case_insensitive_substring(config):
    result = await server.search_text(query="COUNCIL", path="council")

    assert hits_of(result) == [
        ("council/council.md", 1, "# Council"),
        ("council/council.md", 3, "The whole council."),
    ]
    assert "council/council.md:1: # Council" in text_of(result)


async def test_search_narrows_to_a_folder(config):
    result = await server.search_text(query="strategy", path="council/advisors")

    assert hits_of(result) == [("council/advisors/strategy/persona.md", 1, "# Strategy")]


async def test_search_a_single_file(config):
    result = await server.search_text(query="whole", path="council/council.md")

    assert hits_of(result) == [("council/council.md", 3, "The whole council.")]


async def test_search_glob_with_one_segment_matches_names_at_any_depth(tree, config):
    (tree / "council" / "notes.txt").write_text("strategy notes\n")
    result = await server.search_text(query="strategy", path="council", glob="*.md")

    assert [h[0] for h in hits_of(result)] == ["council/advisors/strategy/persona.md"]


async def test_search_glob_with_folders_matches_the_relative_path(config):
    result = await server.search_text(query="now", path="council", glob="advisors/**/_now.md")

    assert hits_of(result) == [("council/advisors/strategy/memory/_now.md", 1, "now")]


async def test_search_regex(config):
    result = await server.search_text(query=r"^#\s+\w+$", path="council", regex=True)

    assert [h[2] for h in hits_of(result)] == ["# Strategy", "# Council"]


async def test_invalid_regex_says_how_to_fix_it(config):
    result = await server.search_text(query="(unclosed", path="council", regex=True)

    assert "not a valid regular expression" in result.structured_content["error"]
    assert "regex=false" in result.structured_content["error"]


async def test_search_matches_nfd_text_with_an_nfc_query(tree, config):
    (tree / "other" / "dessert.md").write_text(unicodedata.normalize("NFD", "Crème brûlée\n"))
    result = await server.search_text(query="crème", path="other")

    assert hits_of(result) == [("other/dessert.md", 1, "Crème brûlée")]


async def test_search_skips_large_and_binary_files_and_says_so(config):
    result = await server.search_text(query="line 00001", path="council")

    assert hits_of(result) == []
    assert result.structured_content["skipped_large"] == 1
    assert result.structured_content["skipped_binary"] == 1
    assert "over the read limit were skipped" in text_of(result)
    assert "non-text file(s) were skipped" in text_of(result)


async def test_search_never_reads_outside_or_excluded_files(config):
    assert hits_of(await server.search_text(query="top secret", path="council")) == []
    assert hits_of(await server.search_text(query="[core]", path="council")) == []


async def test_search_paginates_lines(tree, config):
    (tree / "other" / "log.md").write_text("".join(f"entry {i}\n" for i in range(250)))
    result = await server.search_text(query="entry", path="other")

    assert text_of(result).startswith("Showing 1-100 of 250")
    assert result.structured_content["total"] == 250
    nxt = await server.search_text(query="entry", path="other", offset=200)
    assert hits_of(nxt)[0] == ("other/log.md", 201, "entry 200")
    assert nxt.structured_content["count"] == 50


async def test_long_lines_are_cut(tree, config):
    (tree / "other" / "wide.md").write_text("needle " + "x" * 2000 + "\n")
    result = await server.search_text(query="needle", path="other")

    line = hits_of(result)[0][2]
    assert len(line) == fs.MAX_LINE_CHARS + 1
    assert line.endswith("…")


@pytest.mark.parametrize("arg, message", [
    ({"query": ""}, "must not be empty"),
    ({"glob": "../*.md"}, "not a usable filter"),
    ({"glob": "/"}, "not a usable filter"),
    ({"limit": 0}, "limit must be a positive integer"),
])
async def test_search_refuses_bad_arguments(config, arg, message):
    result = await server.search_text(**({"query": "x", "path": "council"} | arg))

    assert message in result.structured_content["error"]


async def test_no_matches_says_how_much_was_searched(config):
    result = await server.search_text(query="zebra", path="other")

    assert text_of(result) == "No lines in other match 'zebra'. 1 file(s) searched."


# ----------------------------------------------------------------------
# get_file_info
# ----------------------------------------------------------------------


async def test_file_info_reports_size_times_and_the_read_version(config):
    info = (await server.get_file_info(path="council/council.md")).structured_content
    read = (await server.read_files(paths=["council/council.md"])).structured_content

    assert info["type"] == "file"
    assert info["size"] == read["files"][0]["size"]
    assert info["version"] == read["files"][0]["version"]
    assert "T" in info["modified"]


@pytest.mark.skipif(sys.platform != "darwin", reason="birth time is not exposed on Linux")
async def test_file_info_reports_created_on_macos(config):
    info = (await server.get_file_info(path="council/council.md")).structured_content

    assert "T" in info["created"]


async def test_file_info_versions_files_over_the_read_limit(tree, config):
    info = (await server.get_file_info(path="council/big.txt")).structured_content

    assert info["version"] == fs.version_of((tree / "council/big.txt").read_bytes())


async def test_folder_info_has_no_version(config):
    result = await server.get_file_info(path="council/advisors")

    assert result.structured_content["type"] == "dir"
    assert "version" not in result.structured_content
    assert "(folder)" in text_of(result)


async def test_file_info_of_a_missing_file_is_not_found(config):
    result = await server.get_file_info(path="council/missing.md")

    assert "was not found" in result.structured_content["error"]
