"""Simple file-based cache utilities for claude-swap."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from claude_swap.paths import get_backup_root

CACHE_DIR = get_backup_root() / "cache"

MISSING = object()


def read_cache(path: Path, ttl: float, default: object = MISSING) -> object:
    """Read cached JSON data if the file exists and is within TTL.

    Returns the stored 'data' value, or *default* if missing/expired/invalid.
    When *default* is not provided, returns the ``MISSING`` sentinel so
    callers can distinguish "no cache" from a cached ``None`` value.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - raw["timestamp"] < ttl:
            return raw["data"]
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
    ):
        pass
    return default


def read_cache_data(path: Path, default: object = MISSING) -> object:
    """Read cached JSON data without enforcing TTL."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw["data"]
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
    ):
        return default


def read_cache_with_timestamp(path: Path) -> tuple[dict[str, Any] | None, float | None]:
    """Read cached JSON data and the wrapper file timestamp."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = raw["data"]
        ts = raw["timestamp"]
        if isinstance(data, dict) and isinstance(ts, (int, float)):
            return data, float(ts)
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        pass
    return None, None


def write_cache(path: Path, data: object) -> None:
    """Write data to a cache file with a timestamp.

    Atomic: writes to a same-directory temp file, ``os.replace`` swaps it
    into place, then chmods to 0o600 on POSIX so the cache is not world-
    readable. Readers tolerate a missing/corrupt file by returning the
    default — this function eliminates the *source* of corruption so that
    path is exercised only for genuinely-absent caches.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"timestamp": time.time(), "data": data})
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(path))
        if sys.platform != "win32":
            os.chmod(str(path), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
