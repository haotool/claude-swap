"""Usage-cache serialization and per-slot freshness for claude-swap.

Owns the pure codec layer for usage cache rows: round-trip success dicts and
``oauth.UsageFetchError`` values to/from on-disk form, merge a fetch failure
with a trusted prior row, and decide whether a row is within the per-slot TTL.
No I/O and no ``ClaudeAccountSwitcher`` coupling — the switcher's usage-cache
orchestration (which slots to fetch, locking, the thread pool) stays in
``switcher``.
"""

from __future__ import annotations

import time
from typing import Any

from claude_swap import oauth

_USAGE_CACHE_TTL = 15


def _usage_error_to_cache(error_value: oauth.UsageFetchError) -> dict[str, Any]:
    return {
        "_type": "usage_fetch_error",
        "reason": error_value.reason,
        "status_code": error_value.status_code,
        "message": error_value.message,
        "retry_after": error_value.retry_after,
        "_cached_at": time.time(),
    }


def _usage_from_cache(value: object) -> object:
    if isinstance(value, dict) and value.get("_type") == "usage_fetch_error":
        return oauth.UsageFetchError(
            reason=str(value.get("reason") or "unknown"),
            status_code=value.get("status_code"),
            message=str(value.get("message") or ""),
            retry_after=value.get("retry_after"),
        )
    return value


def _usage_to_cache(value: object) -> object:
    if isinstance(value, oauth.UsageFetchError):
        return _usage_error_to_cache(value)
    if isinstance(value, dict):
        stamped = dict(value)
        stamped["_cached_at"] = time.time()
        return stamped
    return value


def _is_usage_dict(value: object) -> bool:
    return isinstance(value, dict) and value.get("_type") != "usage_fetch_error"


def _merge_usage_with_previous(
    current: object,
    previous: object,
) -> tuple[object, oauth.UsageFetchError | None]:
    previous = _usage_from_cache(previous)
    if (current is None or isinstance(current, oauth.UsageFetchError)) and _is_usage_dict(previous):
        return previous, current if isinstance(current, oauth.UsageFetchError) else None
    return current, None


def _persist_usage_cache_entry(
    existing: dict[str, Any],
    key: str,
    current: object,
    previous: object,
) -> None:
    """Write one cache row without re-stamping stale data after fetch failures."""
    prev_trusted = previous if isinstance(previous, dict) and _is_usage_dict(previous) else None
    if isinstance(current, oauth.UsageFetchError):
        if prev_trusted is not None:
            # Keep showing the trusted prior usage, but do not drop a server
            # Retry-After: stamp it as a side field so monitor backoff can honour
            # it even while a fresh-enough usage row masks the active error.
            # Always rewrite the side field from the *current* error so a stale
            # Retry-After from an earlier 429 cannot survive a later non-429
            # failure (which would drive the monitor with a wrong backoff).
            row = dict(prev_trusted)
            row.pop("_last_rate_limit", None)
            if current.reason == "rate_limited" and current.retry_after is not None:
                row["_last_rate_limit"] = {
                    "retry_after": current.retry_after,
                    "at": time.time(),
                }
            existing[key] = row
        else:
            existing[key] = _usage_to_cache(current)
    elif current is None:
        existing[key] = prev_trusted
    elif isinstance(current, str):
        existing[key] = current
    elif _is_usage_dict(current):
        existing[key] = _usage_to_cache(current)


def _usage_slot_trusted(
    entry: object,
    now: float,
) -> bool:
    """True when a single usage cache row is within the per-slot TTL.

    Requires a per-row ``_cached_at`` stamp. Legacy rows without one are
    treated as untrusted so unrelated cache writes cannot extend their TTL.
    """
    if not isinstance(entry, dict):
        return False
    cached_at = entry.get("_cached_at")
    if not isinstance(cached_at, (int, float)) or float(cached_at) <= 0:
        return False
    return now - float(cached_at) < _USAGE_CACHE_TTL


def extract_retry_after(entry: object, now: float) -> int | None:
    """Remaining server Retry-After (seconds) stamped on a masked-429 row, else None.

    Single source for reading the ``_last_rate_limit`` side field that
    ``_persist_usage_cache_entry`` writes when a trusted usage row masks an
    active rate-limit error. The stored ``at`` timestamp is decayed against
    *now* so the monitor backs off only for the time the server window has
    left — a window that has already elapsed yields ``None`` rather than an
    over-long backoff. Callers (``switcher.get_active_usage_retry_after``) read
    through here instead of poking the private key directly.

    Fails closed: the write path always stamps a positive ``at``, so a missing
    or invalid one means a corrupt/legacy row and returns ``None`` rather than
    an undecayed (potentially stale) backoff.
    """
    if not isinstance(entry, dict):
        return None
    last_rate_limit = entry.get("_last_rate_limit")
    if not isinstance(last_rate_limit, dict):
        return None
    retry_after = oauth.parse_retry_after_seconds(last_rate_limit.get("retry_after"))
    if retry_after is None:
        return None
    at = last_rate_limit.get("at")
    if not isinstance(at, (int, float)) or at <= 0:
        return None
    remaining = retry_after - (now - float(at))
    if remaining <= 0:
        return None
    return int(remaining)
