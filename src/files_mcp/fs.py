"""Blocking filesystem work: listing folders and reading text.

Everything here is synchronous and is called from a worker thread with a
timeout (see ``server._blocking``). A cloud-synced folder can hold placeholder
files whose bytes are not on disk yet, and reading one blocks until the sync
client fetches it -- possibly forever. The timeout is what turns that into an
error instead of a hung request.

Nothing here returns a host path. Entries carry mount-relative paths.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import sys
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Iterator

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


def allow_dataless_downloads() -> bool:
    """Let this process download cloud placeholders when it reads them. macOS only.

    A file in a File Provider folder (Dropbox, iCloud Drive) can be "dataless":
    listed, with a size, but with no bytes on disk until something reads it.
    launchd starts its jobs with the policy that allows that download switched
    off, so under a LaunchAgent every such read fails at once with EDEADLK
    ("Resource deadlock avoided"), while the same read from a terminal works.
    Measured on the host: policy 1 (off) by default, and reads succeed once it
    is set to 2 (on).

    Returns whether the policy is now on. Elsewhere there is nothing to do.
    """
    if sys.platform != "darwin":
        return False
    # From <sys/resource.h>.
    iopol_type_vfs_materialize_dataless_files = 3
    iopol_scope_process = 0
    iopol_materialize_dataless_files_on = 2
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.setiopolicy_np(
        iopol_type_vfs_materialize_dataless_files,
        iopol_scope_process,
        iopol_materialize_dataless_files_on,
    )
    return result == 0


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
    _require_folder(target)
    depth = None if recursive else 1
    return [entry for _, entry in walk(target, max_depth=depth)]


def walk(target: Resolved, max_depth: int | None = None) -> Iterator[tuple[Path, dict]]:
    """Yield ``(real path, entry)`` for everything visible under a folder, in path order.

    The real path is for reading and must never reach a client; the entry is
    what a client sees. ``max_depth`` 1 is the folder's immediate contents;
    None is everything below it. The folder itself is not checked here --
    callers do that, so each can word its own error.
    """
    yield from _walk(target.mount, target.real, target.display, max_depth, top=True)


def _require_folder(target: Resolved) -> None:
    if not target.real.exists():
        raise _missing(target.display)
    if not target.real.is_dir():
        raise FileError(f"{target.display} is a file, not a folder. Use read_files to read it.")


def _walk(
    mount: Mount, folder: Path, display: str, max_depth: int | None, top: bool = False
) -> Iterator[tuple[Path, dict]]:
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
            yield real, {"name": nfc(name), "path": path, "type": "dir",
                         "modified": _timestamp(info.st_mtime)}
            deeper = None if max_depth is None else max_depth - 1
            if (deeper is None or deeper > 0) and not is_link:
                yield from _walk(mount, real, path, deeper)
        elif stat.S_ISREG(info.st_mode):
            yield real, {"name": nfc(name), "path": path, "type": "file",
                         "size": info.st_size, "modified": _timestamp(info.st_mtime)}
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


# ----------------------------------------------------------------------
# Glob
# ----------------------------------------------------------------------

WILDCARDS = frozenset("*?[")


def has_wildcard(segment: str) -> bool:
    return any(c in WILDCARDS for c in segment)


def match_segments(pattern: list[str], parts: list[str]) -> bool:
    """Match path segments against pattern segments.

    Each segment matches with ``fnmatch`` rules (``*``, ``?``, ``[...]``),
    never across a ``/``. A ``**`` segment matches zero or more whole
    segments. Case-sensitive, against names as stored on disk.
    """
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(match_segments(pattern[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], pattern[0]) and match_segments(
        pattern[1:], parts[1:]
    )


def glob_files(base: Resolved | None, rest: list[str]) -> list[dict]:
    """Files under ``base`` whose path below it matches ``rest``, in path order.

    ``base`` is the pattern's literal prefix, already resolved; None means it
    does not exist (or is excluded, which must look the same), so nothing
    matches. With no wildcard segments at all, ``rest`` is empty and the
    pattern names one file.
    """
    if base is None or not base.real.exists():
        return []
    if not rest:
        if base.real.is_file():
            info = base.real.stat()
            return [{"name": base.display.rsplit("/", 1)[-1], "path": base.display,
                     "type": "file", "size": info.st_size,
                     "modified": _timestamp(info.st_mtime)}]
        return []
    if not base.real.is_dir():
        return []

    # Without **, nothing deeper than the pattern can match, so don't walk it.
    depth = None if "**" in rest else len(rest)
    prefix = len(base.display.split("/"))
    return [
        entry
        for _, entry in walk(base, max_depth=depth)
        if entry["type"] == "file" and match_segments(rest, entry["path"].split("/")[prefix:])
    ]


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------

# A matching line is returned whole up to this, then cut. Minified files and
# data dumps can put megabytes on one line.
MAX_LINE_CHARS = 500


def search(
    target: Resolved,
    matches: Callable[[str], bool],
    file_glob: list[str] | None,
    limits: Limits,
) -> dict:
    """Every matching line in the text files at or under ``target``.

    ``matches`` gets each line, NFC-normalized. ``file_glob`` filters files by
    their path below ``target`` with ``match_segments`` rules; a one-segment
    pattern such as ``*.md`` matches the file name at any depth.

    Files over ``max_read_bytes`` and files that are not UTF-8 text are
    skipped and counted rather than failing the search.
    """
    if not target.real.exists():
        raise _missing(target.display)

    if target.real.is_file():
        candidates = [(target.real, target.display)]
    else:
        prefix = len(target.display.split("/"))
        candidates = []
        for real, entry in walk(target):
            if entry["type"] != "file":
                continue
            parts = entry["path"].split("/")[prefix:]
            if file_glob is not None:
                if len(file_glob) == 1:
                    if not fnmatchcase(parts[-1], file_glob[0]):
                        continue
                elif not match_segments(file_glob, parts):
                    continue
            candidates.append((real, entry["path"]))

    hits: list[dict] = []
    skipped_large = skipped_binary = 0
    for real, display in candidates:
        try:
            if real.stat().st_size > limits.max_read_bytes:
                skipped_large += 1
                continue
            data = real.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:SNIFF_BYTES]:
            skipped_binary += 1
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped_binary += 1
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            line = nfc(line)
            if matches(line):
                if len(line) > MAX_LINE_CHARS:
                    line = line[:MAX_LINE_CHARS] + "…"
                hits.append({"path": display, "line": number, "text": line})

    return {
        "matches": hits,
        "files_searched": len(candidates) - skipped_large - skipped_binary,
        "skipped_large": skipped_large,
        "skipped_binary": skipped_binary,
    }


# ----------------------------------------------------------------------
# File info
# ----------------------------------------------------------------------


def file_info(target: Resolved) -> dict:
    """Type, size, times, and (for a file) the content version.

    The version is the same whole-file hash ``read_text`` returns, streamed so
    a file of any size can be checked without reading it into memory.
    """
    if not target.real.exists():
        raise _missing(target.display)
    info = target.real.stat()
    # st_birthtime exists on macOS and the BSDs; Linux does not expose it here.
    created = getattr(info, "st_birthtime", None)
    result = {
        "path": target.display,
        "type": "dir" if target.real.is_dir() else "file",
        "created": _timestamp(created) if created is not None else None,
        "modified": _timestamp(info.st_mtime),
    }
    if result["type"] == "file":
        digest = hashlib.sha256()
        with open(target.real, "rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
        result["size"] = info.st_size
        result["version"] = digest.hexdigest()[:16]
    return result
