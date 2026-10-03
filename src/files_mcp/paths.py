"""Turning a client's ``<mount>/<relative path>`` into a real path, safely.

Every tool goes through ``resolve``. It is the only place a client string
becomes a host path, so the rules live here and nowhere else:

- The first segment names a mount. Nothing outside a mount is addressable.
- ``..`` is refused outright rather than normalized away. There is no
  legitimate reason for a client to climb, and refusing is easier to reason
  about than proving a normalization cannot escape.
- Symlinks are resolved and the result must still sit inside the mount's own
  resolved root. A link that points out is refused, even for reads.
- Excluded names are reported as not found, never as excluded, so a client
  cannot probe for them.
- Names are compared in NFC. macOS can store decomposed (NFD) names, and a
  client typing "café" sends the composed form.

Messages raised from here go to clients, so they carry mount-relative paths
only, never a host path.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Mount


class PathError(Exception):
    """A path the client cannot use. The message says what to do instead."""


class NotFound(PathError):
    """Missing or excluded -- deliberately one class, so the two cannot be told apart."""


@dataclass(frozen=True)
class Resolved:
    mount: Mount
    # The real, symlink-free host path. Never shown to a client.
    real: Path
    # What the client sees: "<mount>/<relative path>", NFC, or just "<mount>".
    display: str


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def split(path: str) -> tuple[str, list[str]]:
    """Split a client path into a mount name and relative segments.

    A leading ``/`` is allowed and ignored, as are empty and ``.`` segments.
    """
    if "\x00" in path:
        raise PathError("Paths cannot contain NUL characters.")
    parts = [p for p in nfc(path).split("/") if p not in ("", ".")]
    if not parts:
        raise PathError(
            "A path must start with a mount name, like 'council/notes.md'. "
            "Use list_mounts to see what's available."
        )
    if ".." in parts:
        raise PathError(
            f"'..' is not allowed in paths ({path!r}). Write the path from the mount "
            f"name down, like 'council/notes.md'."
        )
    return parts[0], parts[1:]


def resolve(config: Config, path: str) -> Resolved:
    """Resolve a client path to a real path inside its mount, or raise PathError.

    The target need not exist; callers decide whether a missing file is an error.
    """
    name, segments = split(path)
    mount = config.mounts.get(name)
    if mount is None:
        raise PathError(
            f"There is no mount named {name!r}. Every path starts with a mount name; "
            f"use list_mounts to see what's available."
        )

    display = "/".join([name, *segments])
    if any(mount.is_excluded(segment) for segment in segments):
        raise not_found(display)

    real = Path(os.path.realpath(_locate(mount.root, segments)))
    if real != mount.root and not real.is_relative_to(mount.root):
        raise PathError(
            f"{display} points outside the {name!r} mount, so it can't be used. "
            f"Only files inside a mount are reachable."
        )
    # A link inside the mount can still point at something excluded, such as
    # .git/config; that has to be just as invisible as naming it directly.
    if any(mount.is_excluded(part) for part in real.relative_to(mount.root).parts):
        raise not_found(display)

    return Resolved(mount=mount, real=real, display=display)


def _locate(root: Path, segments: list[str]) -> Path:
    """Join segments onto root, matching each by NFC when the exact name is absent.

    APFS already treats NFC and NFD names as the same file, so on the host this
    almost never scans. Other filesystems do not, and without the fallback an
    NFD name on disk is unreachable by the composed name a client types.
    """
    current = root
    for segment in segments:
        candidate = current / segment
        if not os.path.lexists(candidate):
            try:
                names = os.listdir(current)
            except OSError:
                names = []
            match = next((n for n in names if nfc(n) == segment), None)
            if match is not None:
                candidate = current / match
        current = candidate
    return current


def not_found(display: str) -> NotFound:
    """The not-found error, shared so a missing file and a hidden one read the same."""
    return NotFound(
        f"{display} was not found. Use list_directory on its folder to see what's there."
    )
