"""The mount table: which folders exist, where they really live, and their limits.

Read once at startup from ``~/.files-mcp/config.yaml`` (or ``FILES_MCP_CONFIG``).
There is no way to change it at runtime -- no MCP Roots, no tool -- so this file
is the whole security boundary. Anything wrong with it stops the server from
starting rather than being worked around.

Error messages here may name host paths. They only ever reach the operator's
log at startup, never a client.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".files-mcp" / "config.yaml"

# Atomic writes stage into files with this prefix. They are always excluded, so
# a half-written file is never listed, read, or mistaken for real content.
TEMP_PREFIX = ".files-mcp-tmp-"

MOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(Exception):
    """The config cannot be used. The message names what to fix."""


@dataclass(frozen=True)
class Limits:
    max_read_bytes: int = 1_048_576
    max_files_per_read: int = 25
    max_write_bytes: int = 1_048_576


@dataclass(frozen=True)
class Mount:
    name: str
    # Resolved with realpath at load time. Every resolved client path must sit
    # inside this, so it has to be the real location, not a symlink to it.
    root: Path
    mode: str
    exclude: tuple[str, ...]

    @property
    def writable(self) -> bool:
        return self.mode == "rw"

    def is_excluded(self, name: str) -> bool:
        """Whether one path segment is hidden in this mount."""
        if name.startswith(TEMP_PREFIX):
            return True
        name = unicodedata.normalize("NFC", name)
        return any(fnmatchcase(name, pattern) for pattern in self.exclude)


@dataclass(frozen=True)
class Config:
    mounts: dict[str, Mount]
    limits: Limits


def config_path() -> Path:
    override = os.environ.get("FILES_MCP_CONFIG", "").strip()
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ConfigError(
            f"No config at {path}. Copy config.example.yaml there, or set FILES_MCP_CONFIG."
        ) from None
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read config at {path}: {exc}") from None

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a YAML mapping with a 'mounts' list.")

    shared_exclude = _patterns(raw.get("exclude", []), "the top-level exclude list")

    entries = raw.get("mounts")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path} must list at least one mount under 'mounts'.")

    mounts: dict[str, Mount] = {}
    for index, entry in enumerate(entries):
        mount = _mount(entry, index, shared_exclude)
        if mount.name in mounts:
            raise ConfigError(f"Mount name {mount.name!r} is used more than once.")
        mounts[mount.name] = mount

    return Config(mounts=mounts, limits=_limits(raw.get("limits", {})))


def _mount(entry: object, index: int, shared_exclude: tuple[str, ...]) -> Mount:
    if not isinstance(entry, dict):
        raise ConfigError(f"Mount #{index + 1} must be a mapping with name, path, and mode.")

    name = entry.get("name")
    if not isinstance(name, str) or not MOUNT_NAME.match(name):
        raise ConfigError(
            f"Mount #{index + 1} has name {name!r}. Names are lowercase slugs: letters, "
            f"digits, '-' and '_', starting with a letter or digit, and no '/'."
        )

    mode = entry.get("mode")
    if mode not in ("ro", "rw"):
        raise ConfigError(f"Mount {name!r} has mode {mode!r}; it must be 'ro' or 'rw'.")

    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(f"Mount {name!r} has no path.")
    root = Path(os.path.realpath(Path(raw_path).expanduser()))
    if not root.is_dir():
        raise ConfigError(f"Mount {name!r} path {raw_path} does not exist or is not a directory.")

    exclude = shared_exclude + _patterns(entry.get("exclude", []), f"mount {name!r}'s exclude")
    return Mount(name=name, root=root, mode=mode, exclude=exclude)


def _patterns(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise ConfigError(f"{where} must be a list of glob patterns.")
    return tuple(unicodedata.normalize("NFC", p) for p in value)


def _limits(value: object) -> Limits:
    if not isinstance(value, dict):
        raise ConfigError("'limits' must be a mapping.")
    defaults = Limits()
    kwargs = {}
    for key in ("max_read_bytes", "max_files_per_read", "max_write_bytes"):
        number = value.get(key, getattr(defaults, key))
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ConfigError(f"limits.{key} must be a positive integer.")
        kwargs[key] = number
    unknown = set(value) - set(kwargs)
    if unknown:
        raise ConfigError(f"Unknown limits: {', '.join(sorted(unknown))}.")
    return Limits(**kwargs)
