"""Usage-cache serialization and per-slot freshness for claude-swap.

Owns the pure codec layer for usage cache rows: round-trip success dicts and
``oauth.UsageFetchError`` values to/from on-disk form, merge a fetch failure
with a trusted prior row, and decide whether a row is within the per-slot TTL.
No I/O and no ``ClaudeAccountSwitcher`` coupling — the switcher's usage-cache
orchestration (which slots to fetch, locking, the thread pool) stays in
``switcher``. ``switcher`` re-exports these names for existing callers.
"""

from __future__ import annotations

import time

from claude_swap import oauth

_USAGE_CACHE_TTL = 15


def _usage_error_to_cache(error_value: oauth.UsageFetchError) -> dict:
    return {
        "_type": "usage_fetch_error",
        "reason": error_value.reason,
        "status_code": error_value.status_code,
        "message": error_value.message,
        "retry_after": error_value.retry_after,
    }


def _usage_from_cache(value):
    if isinstance(value, dict) and value.get("_type") == "usage_fetch_error":
        return oauth.UsageFetchError(
            reason=str(value.get("reason") or "unknown"),
            status_code=value.get("status_code"),
            message=str(value.get("message") or ""),
            retry_after=value.get("retry_after"),
        )
    return value


def _usage_to_cache(value):
    if isinstance(value, oauth.UsageFetchError):
        return _usage_error_to_cache(value)
    if isinstance(value, dict):
        stamped = dict(value)
        stamped["_cached_at"] = time.time()
        return stamped
    return value


def _is_usage_dict(value) -> bool:
    return isinstance(value, dict) and value.get("_type") != "usage_fetch_error"


def _merge_usage_with_previous(current, previous):
    previous = _usage_from_cache(previous)
    if (current is None or isinstance(current, oauth.UsageFetchError)) and _is_usage_dict(previous):
        return previous, current
    return current, None


def _persist_usage_cache_entry(
    existing: dict,
    key: str,
    current,
    previous,
) -> None:
    """Write one cache row without re-stamping stale data after fetch failures."""
    prev_trusted = previous if isinstance(previous, dict) and _is_usage_dict(previous) else None
    if isinstance(current, oauth.UsageFetchError):
        existing[key] = prev_trusted if prev_trusted is not None else _usage_to_cache(current)
    elif current is None:
        existing[key] = prev_trusted
    elif isinstance(current, str):
        existing[key] = current
    elif _is_usage_dict(current):
        existing[key] = _usage_to_cache(current)


def _usage_slot_trusted(
    entry: dict,
    now: float,
    file_timestamp: float | None = None,
) -> bool:
    """True when a single usage cache row is within the per-slot TTL.

    Legacy rows without ``_cached_at`` inherit the wrapper file timestamp so
    pre-007 caches remain trusted until the file TTL expires.
    """
    if not isinstance(entry, dict):
        return False
    cached_at = entry.get("_cached_at")
    if isinstance(cached_at, (int, float)) and float(cached_at) > 0:
        resolved = float(cached_at)
    elif file_timestamp is not None and file_timestamp > 0:
        resolved = file_timestamp
    else:
        return False
    return now - resolved < _USAGE_CACHE_TTL
