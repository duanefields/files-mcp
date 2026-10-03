"""Blocking write operations: write, edit, append, create folder, move, delete.

Like ``fs``, everything here is synchronous and runs in a worker thread under
a timeout. Every target has already been through ``paths.resolve``, so it is
inside its mount and not excluded; this module adds the write rules:

- Nothing changes on an ``ro`` mount.
- Overwriting needs the version from a recent read (``if_version``), so a
  stale client cannot discard someone else's newer edit. That includes Dropbox
  syncing a change in from another machine.
- Whole-file writes are atomic: a temp file in the same folder, fsync, then
  rename. Temp names start with ``TEMP_PREFIX``, which is always excluded.
- A mount's root can't be moved or deleted, and a symlink is never moved or
  deleted through -- that would act on whatever it points at.
- Delete is never recursive.
"""

from __future__ import annotations

import difflib
import errno
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .config import TEMP_PREFIX, Limits
from .fs import CHUNK, SNIFF_BYTES, FileError, version_of
from .paths import Resolved, not_found

# The process umask, read once at import while nothing else is running: it can
# only be read by setting it, which is not thread-safe later. New files get
# the same permissions an ordinary file created here would.
_UMASK = os.umask(0)
os.umask(_UMASK)
NEW_FILE_MODE = 0o666 & ~_UMASK


def _writable(target: Resolved) -> None:
    if not target.mount.writable:
        raise FileError(
            f"The {target.mount.name!r} mount is read-only, so {target.display} can't be "
            f"changed. Use list_mounts to see which mounts can be written."
        )


def _not_root(target: Resolved, verb: str) -> None:
    if target.real == target.mount.root:
        raise FileError(f"{target.display} is a mount's top folder and can't be {verb}.")


def _not_a_link(target: Resolved, verb: str) -> None:
    if target.located.is_symlink():
        raise FileError(
            f"{target.display} is a symbolic link. This server won't {verb} links, "
            f"because that would act on what the link points to."
        )


def _encode(content: str, limits: Limits, display: str) -> bytes:
    data = content.encode("utf-8")
    if len(data) > limits.max_write_bytes:
        raise FileError(
            f"That content is {len(data):,} bytes, over the {limits.max_write_bytes:,}-byte "
            f"write limit for {display}. Split it into smaller files, or append in parts."
        )
    return data


def _hash_file(real: Path) -> str:
    digest = hashlib.sha256()
    with open(real, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _check_version(target: Resolved, if_version: str | None) -> str:
    """The file's current version, after checking it against ``if_version`` if given."""
    current = _hash_file(target.real)
    if if_version is not None and if_version != current:
        raise FileError(
            f"{target.display} has changed since it was read (your version {if_version}, "
            f"current version {current}). Read it again and redo the change against the "
            f"current text, so the other change isn't lost."
        )
    return current


def _atomic_write(real: Path, data: bytes, mode: int) -> None:
    """Write via a temp file in the same folder, fsync, then rename over the target."""
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=real.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Make the rename itself durable. Best effort: not every filesystem
    # allows fsync on a directory.
    try:
        dir_fd = os.open(real.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# ----------------------------------------------------------------------
# write_file
# ----------------------------------------------------------------------


def write_file(target: Resolved, content: str, if_version: str | None, limits: Limits) -> dict:
    _writable(target)
    data = _encode(content, limits, target.display)

    if target.real.exists():
        if target.real.is_dir():
            raise FileError(f"{target.display} is a folder, not a file.")
        if if_version is None:
            raise FileError(
                f"{target.display} already exists. To replace it, read it first and pass "
                f"its version as if_version. For a small change, edit_file is usually "
                f"better; to add to the end, use append_file."
            )
        previous = _check_version(target, if_version)
        mode = target.real.stat().st_mode & 0o7777
        created = False
    else:
        if if_version is not None:
            raise FileError(
                f"{target.display} no longer exists, so it can't match version {if_version}; "
                f"it may have been moved or deleted. Check with list_directory, or pass no "
                f"if_version to create it."
            )
        previous = None
        mode = NEW_FILE_MODE
        created = True

    _atomic_write(target.real, data, mode)
    return {
        "path": target.display,
        "created": created,
        "size": len(data),
        "version": version_of(data),
        "previous_version": previous,
    }


# ----------------------------------------------------------------------
# edit_file
# ----------------------------------------------------------------------


def edit_file(
    target: Resolved,
    edits: list[tuple[str, str]],
    if_version: str | None,
    dry_run: bool,
    limits: Limits,
) -> dict:
    """Apply exact-match replacements in order; all of them, or none.

    Each edit applies to the text as the previous edits left it. Each
    ``old_text`` must occur exactly once at that point; otherwise nothing is
    written and the error names the edit and how many matches it found.
    """
    _writable(target)
    if not target.real.exists():
        raise FileError(str(not_found(target.display)))
    if target.real.is_dir():
        raise FileError(f"{target.display} is a folder, not a file.")
    if not edits:
        raise FileError("Pass at least one edit.")

    size = target.real.stat().st_size
    if size > limits.max_read_bytes:
        raise FileError(
            f"{target.display} is {size:,} bytes, over the {limits.max_read_bytes:,}-byte "
            f"limit for editing."
        )
    raw = target.real.read_bytes()
    previous = version_of(raw)
    if if_version is not None and if_version != previous:
        raise FileError(
            f"{target.display} has changed since it was read (your version {if_version}, "
            f"current version {previous}). Read it again and redo the edits against the "
            f"current text, so the other change isn't lost."
        )
    if b"\x00" in raw[:SNIFF_BYTES]:
        raise FileError(f"{target.display} is not UTF-8 text, so it can't be edited here.")
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FileError(f"{target.display} is not UTF-8 text, so it can't be edited here.") from None

    text = original
    for number, (old, new) in enumerate(edits, start=1):
        label = f"Edit {number} of {len(edits)}"
        if not old:
            raise FileError(f"{label} has an empty old_text. Nothing was changed.")
        count = text.count(old)
        if count != 1:
            hint = (
                "Read the file again and copy the text exactly, including whitespace and "
                "line breaks."
                if count == 0
                else "Include more of the surrounding text so it matches only once."
            )
            raise FileError(
                f"{label}: old_text matches {count} times in {target.display}; it must match "
                f"exactly once. Nothing was changed. {hint}"
            )
        text = text.replace(old, new, 1)

    data = _encode(text, limits, target.display)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=target.display,
            tofile=target.display,
        )
    )
    if not dry_run and text != original:
        _atomic_write(target.real, data, target.real.stat().st_mode & 0o7777)

    return {
        "path": target.display,
        "diff": diff,
        "dry_run": dry_run,
        "changed": text != original,
        "version": version_of(data),
        "previous_version": previous,
    }


# ----------------------------------------------------------------------
# append_file
# ----------------------------------------------------------------------


def append_file(target: Resolved, content: str, limits: Limits) -> dict:
    """Append, adding a newline first if the file doesn't end with one.

    Not atomic, and doesn't need to be: an append can't discard anyone's
    change, which is why it takes no version.
    """
    _writable(target)
    data = _encode(content, limits, target.display)

    created = not target.real.exists()
    if created:
        target.real.parent.mkdir(parents=True, exist_ok=True)
        prefix = b""
    else:
        if target.real.is_dir():
            raise FileError(f"{target.display} is a folder, not a file.")
        with open(target.real, "rb") as handle:
            if b"\x00" in handle.read(SNIFF_BYTES):
                raise FileError(
                    f"{target.display} is not UTF-8 text, so it can't be appended to here."
                )
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                prefix = b""
            else:
                handle.seek(-1, os.SEEK_END)
                prefix = b"" if handle.read(1) == b"\n" else b"\n"

    fd = os.open(target.real, os.O_WRONLY | os.O_APPEND | os.O_CREAT, NEW_FILE_MODE)
    with os.fdopen(fd, "ab") as handle:
        handle.write(prefix + data)
        handle.flush()
        os.fsync(handle.fileno())

    return {
        "path": target.display,
        "created": created,
        "size": target.real.stat().st_size,
        "version": _hash_file(target.real),
    }


# ----------------------------------------------------------------------
# create_directory
# ----------------------------------------------------------------------


def create_directory(target: Resolved) -> dict:
    _writable(target)
    if target.real.exists():
        if not target.real.is_dir():
            raise FileError(f"{target.display} already exists as a file.")
        return {"path": target.display, "created": False}
    target.real.mkdir(parents=True)
    return {"path": target.display, "created": True}


# ----------------------------------------------------------------------
# move_file
# ----------------------------------------------------------------------


def move(source: Resolved, destination: Resolved) -> dict:
    """Move or rename a file or folder, within or across writable mounts."""
    _writable(source)
    _writable(destination)
    if not os.path.lexists(source.located):
        raise FileError(str(not_found(source.display)))
    _not_root(source, "moved")
    _not_a_link(source, "move")
    _not_root(destination, "replaced")

    # The destination's own name, as the client typed it, under its real parent.
    leaf = destination.display.rsplit("/", 1)[-1]
    final = destination.real.parent / leaf

    if os.path.lexists(destination.located):
        # A case-only rename on a case-insensitive disk finds "itself" at the
        # destination. That is a rename, not a collision.
        same = os.path.exists(final) and os.path.samefile(source.real, final)
        if not same:
            raise FileError(
                f"{destination.display} already exists. Move to a name that's free, or "
                f"delete the existing one first."
            )

    if source.real.is_dir() and (
        final == source.real or final.is_relative_to(source.real)
    ):
        raise FileError(f"{source.display} can't be moved inside itself.")

    kind = "dir" if source.real.is_dir() else "file"
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source.real, final)
    except OSError as exc:
        if exc.errno != errno.EXDEV:  # the mounts are on different volumes
            raise
        shutil.move(source.real, final)
    return {"source": source.display, "destination": destination.display, "type": kind}


# ----------------------------------------------------------------------
# delete_file
# ----------------------------------------------------------------------

# macOS drops these into folders on its own. A folder holding nothing else
# looks empty to every client, so it should delete like one.
DISPOSABLE = {".DS_Store"}


def delete(target: Resolved) -> dict:
    """Delete one file, or one empty folder. Never recursive."""
    _writable(target)
    if not os.path.lexists(target.located):
        raise FileError(str(not_found(target.display)))
    _not_root(target, "deleted")
    _not_a_link(target, "delete")

    if target.real.is_dir():
        names = os.listdir(target.real)
        if any(name not in DISPOSABLE for name in names):
            raise FileError(
                f"{target.display} isn't empty, so it can't be deleted. This server only "
                f"deletes empty folders; delete or move what's in it first."
            )
        for name in names:
            os.unlink(target.real / name)
        os.rmdir(target.real)
        kind = "dir"
    else:
        os.unlink(target.real)
        kind = "file"
    return {"path": target.display, "type": kind}
