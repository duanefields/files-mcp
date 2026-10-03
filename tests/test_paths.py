"""Path resolution: the one place a client string becomes a host path.

Escape attempts are also covered per tool in test_server.py; this file pins the
rules themselves.
"""

import pytest

from files_mcp.paths import PathError, resolve
from tests.conftest import NFD_NAME


def test_mount_relative_path_resolves(tree, config):
    target = resolve(config, "council/advisors/strategy/persona.md")

    assert target.real == (tree / "council/advisors/strategy/persona.md").resolve()
    assert target.display == "council/advisors/strategy/persona.md"
    assert target.mount.name == "council"


def test_leading_slash_and_dot_segments_are_ignored(config):
    assert resolve(config, "/council/./council.md").display == "council/council.md"


def test_bare_mount_name_is_its_root(tree, config):
    target = resolve(config, "council")

    assert target.real == (tree / "council").resolve()
    assert target.display == "council"


def test_missing_files_still_resolve(config):
    """Whether a missing file is an error is the caller's decision."""
    assert resolve(config, "council/new.md").display == "council/new.md"


@pytest.mark.parametrize("path", ["", "/", "./"])
def test_empty_mount_name_is_refused(config, path):
    with pytest.raises(PathError, match="list_mounts"):
        resolve(config, path)


@pytest.mark.parametrize(
    "path", ["council/../outside/secret.txt", "council/advisors/../../other", "../council"]
)
def test_dot_dot_is_refused(config, path):
    with pytest.raises(PathError, match=r"'\.\.' is not allowed"):
        resolve(config, path)


def test_absolute_host_path_is_not_a_mount(tree, config):
    with pytest.raises(PathError, match="no mount named"):
        resolve(config, str(tree / "outside" / "secret.txt"))


def test_unknown_mount_is_refused(config):
    with pytest.raises(PathError, match="no mount named 'nope'"):
        resolve(config, "nope/file.md")


@pytest.mark.parametrize("path", ["council/link-out/secret.txt", "council/link-secret"])
def test_symlink_out_of_the_mount_is_refused(config, path):
    with pytest.raises(PathError, match="outside the 'council' mount"):
        resolve(config, path)


def test_symlink_inside_the_mount_is_followed(tree, config):
    target = resolve(config, "council/link-advisors/strategy/persona.md")

    assert target.real == (tree / "council/advisors/strategy/persona.md").resolve()


@pytest.mark.parametrize(
    "path", ["council/.git/config", "council/.git", "council/.DS_Store", "council/link-git"]
)
def test_excluded_names_read_as_not_found(config, path):
    with pytest.raises(PathError, match="was not found") as caught:
        resolve(config, path)

    assert "exclude" not in str(caught.value)


def test_temp_files_read_as_not_found(config):
    with pytest.raises(PathError, match="was not found"):
        resolve(config, "council/.files-mcp-tmp-abc")


def test_nul_is_refused(config):
    with pytest.raises(PathError, match="NUL"):
        resolve(config, "council/a\x00b")


def test_nfc_path_finds_nfd_file(tree, config):
    """A client types the composed form; macOS may have stored the decomposed one."""
    target = resolve(config, "council/café.md")

    assert target.real.read_text() == "accented\n"
    assert target.display == "council/café.md"


def test_nfd_path_finds_nfd_file(config):
    target = resolve(config, "council/" + NFD_NAME)

    assert target.real.read_text() == "accented\n"
    assert target.display == "council/café.md"


def test_errors_never_carry_host_paths(tree, config):
    for path in ["council/link-out/secret.txt", "council/../x", "nope/x", "council/.git"]:
        with pytest.raises(PathError) as caught:
            resolve(config, path)
        assert str(tree) not in str(caught.value)
