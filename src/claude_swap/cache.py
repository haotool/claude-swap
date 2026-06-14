"""Simple file-based cache utilities for claude-swap."""

from __future__ import annotations

import json
import time
from pathlib import Path

from claude_swap.paths import get_backup_root

CACHE_DIR = get_backup_root() / "cache"

MISSING = object()


def read_cache(path: Path, ttl: float, default=MISSING):
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


def read_cache_data(path: Path, default=MISSING):
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


def read_cache_with_timestamp(path: Path) -> tuple[dict | None, float | None]:
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


def write_cache(path: Path, data) -> None:
    """Write data to a cache file with a timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"timestamp": time.time(), "data": data}),
        encoding="utf-8",
    )
