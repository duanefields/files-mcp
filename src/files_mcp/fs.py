"""Blocking filesystem work: listing folders and reading text.

Everything here is synchronous and is called from a worker thread with a
timeout (see ``server._blocking``). A cloud-synced folder can hold placeholder
files whose bytes are not on disk yet, and reading one blocks until the sync
client fetches it -- possibly forever. The timeout is what turns that into an
error instead of a hung request.

Nothing here returns a host path. Entries carry mount-relative paths.
"""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path

from .config import Limits, Mount
from .paths import Resolved, nfc, not_found

CHUNK = 65_536
# Enough of a file to tell text from binary without reading all of it.
SNIFF_BYTES = 8_192


class FileError(Exception):
    """A file that exists but cannot be returned. The message says why."""


def version_of(data: bytes) -> str:
    """The content version: the first 16 hex characters of the SHA-256."""
    return hashlib.sha256(data).hexdigest()[:16]


def _timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------


def list_entries(target: Resolved, recursive: bool) -> list[dict]:
    """Every visible entry under a folder, sorted by path.

    Symlinks are listed as what they point at, provided that is inside the
    mount and not excluded; links that escape are left out, the same as an
    excluded name. A recursive walk never descends through a symlinked folder,
    so a link cannot loop the walk or list one folder twice.
    """
    if not target.real.exists():
        raise _missing(target.display)
    if not target.real.is_dir():
        raise FileError(f"{target.display} is a file, not a folder. Use read_files to read it.")

    entries: list[dict] = []
    _walk(target.mount, target.real, target.display, recursive, entries, top=True)
    return entries


def _walk(
    mount: Mount, folder: Path, display: str, recursive: bool, out: list[dict], top: bool = False
) -> None:
    try:
        names = sorted(os.listdir(folder), key=nfc)
    except OSError as exc:
        # The folder asked for has to say why it failed. A subfolder deep in a
        # recursive walk is skipped, so one unreadable corner does not sink it.
        if top:
            raise FileError(f"{display} could not be listed ({exc.strerror}).") from None
        return

    for name in names:
        if mount.is_excluded(name):
            continue
        real = folder / name
        try:
            info = os.lstat(real)
            is_link = stat.S_ISLNK(info.st_mode)
            if is_link:
                resolved = Path(os.path.realpath(real))
                if resolved != mount.root and not resolved.is_relative_to(mount.root):
                    continue
                if any(mount.is_excluded(p) for p in resolved.relative_to(mount.root).parts):
                    continue
                info = os.stat(resolved)
        except OSError:
            # Vanished between listdir and stat, or a dangling link.
            continue

        path = f"{display}/{nfc(name)}"
        if stat.S_ISDIR(info.st_mode):
            out.append({"name": nfc(name), "path": path, "type": "dir",
                        "modified": _timestamp(info.st_mtime)})
            if recursive and not is_link:
                _walk(mount, real, path, recursive, out)
        elif stat.S_ISREG(info.st_mode):
            out.append({"name": nfc(name), "path": path, "type": "file",
                        "size": info.st_size, "modified": _timestamp(info.st_mtime)})
        # Sockets, FIFOs and devices are not files anyone means to read.


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def read_text(target: Resolved, limits: Limits, head: int | None, tail: int | None) -> dict:
    """Read one file as UTF-8 text, optionally only its first or last lines.

    ``version`` is always the hash of the *whole* file, even when only part of
    it is returned, because it is what a later write checks against.

    A file over ``max_read_bytes`` can still be read with ``head`` or ``tail``:
    the whole file is streamed through the hash, but only one limit's worth of
    bytes at the relevant end is kept.
    """
    if not target.real.exists():
        raise _missing(target.display)
    if target.real.is_dir():
        raise FileError(
            f"{target.display} is a folder. Use list_directory to see what's in it."
        )

    size = target.real.stat().st_size
    limit = limits.max_read_bytes
    if size > limit and head is None and tail is None:
        raise FileError(
            f"{target.display} is {size:,} bytes, over the {limit:,}-byte read limit. "
            f"Pass head or tail to read its first or last lines."
        )

    if size <= limit:
        data = target.real.read_bytes()
        version = version_of(data)
        window, clipped = data, False
    else:
        version, window = _stream(target.real, limit, from_end=tail is not None)
        clipped = True

    text = _decode(window, target.display)
    lines = text.splitlines(keepends=True)
    total_lines = None if clipped else len(lines)
    if head is not None:
        lines = lines[:head]
    elif tail is not None:
        lines = lines[-tail:]

    return {
        "path": target.display,
        "content": "".join(lines),
        "version": version,
        "size": size,
        "lines_returned": len(lines),
        # Unknown when only part of a large file was examined.
        "total_lines": total_lines,
    }


def _stream(real: Path, keep: int, from_end: bool) -> tuple[str, bytes]:
    """Hash the whole file and keep ``keep`` bytes from one end, cut at a line break."""
    digest = hashlib.sha256()
    kept = bytearray()
    with open(real, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            if from_end:
                kept += chunk
                if len(kept) > keep:
                    del kept[: len(kept) - keep]
            elif len(kept) < keep:
                kept += chunk[: keep - len(kept)]

    # The window starts or ends mid-file, so the partial line at the cut is
    # dropped: half a line is worse than none, and it may split a UTF-8 character.
    if from_end:
        cut = kept.find(b"\n")
        window = bytes(kept[cut + 1:]) if cut != -1 else b""
    else:
        cut = kept.rfind(b"\n")
        window = bytes(kept[: cut + 1]) if cut != -1 else b""
    return digest.hexdigest()[:16], window


def _decode(data: bytes, display: str) -> str:
    binary = FileError(
        f"{display} is not UTF-8 text. This server only reads text files; binary "
        f"and media files are not supported."
    )
    if b"\x00" in data[:SNIFF_BYTES]:
        raise binary
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise binary from None


def _missing(display: str) -> FileError:
    # Same wording as an excluded path, deliberately.
    return FileError(str(not_found(display)))
