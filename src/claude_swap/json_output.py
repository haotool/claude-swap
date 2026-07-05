"""Serialization helpers for ``--json`` structured output.

Centralizes the schema-v1 shapes so ``--list``/``--status``/``--switch`` agree on
field names (camelCase, matching the export envelope in transfer.py) and on how the
internal usage dict is projected to JSON. Callers build payloads here; the CLI does
the single ``json.dumps`` (see cli.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from claude_swap.usage_store import UsageEntry as StoreUsageEntry

# Bump only on a breaking change to any payload shape. Scripts key off this.
SCHEMA_VERSION = 1

# Sentinel entries the usage collectors yield in place of a usage dict. Kept
# here (the serialization hub) so the human renderer and the JSON projection
# agree instead of scattering raw strings. Human-facing wording for these
# lives in ``list_reporter._SENTINEL_NOTES``.
USAGE_NO_CREDENTIALS = "no credentials"
USAGE_TOKEN_EXPIRED = "token expired"
# API-key (``/login`` managed key) accounts have no subscription quota; usage is
# reported as this sentinel instead of being fetched from the OAuth usage API.
USAGE_API_KEY = "api key"
# The active account's macOS Keychain was unreadable (locked / denied / timeout)
# with no plaintext fallback — distinct from a genuinely empty slot, so the user
# isn't misled into an unnecessary re-login.
USAGE_KEYCHAIN_UNAVAILABLE = "keychain unavailable"

# The decision-grade usage value a store entry projects to: a usage dict, a
# sentinel string, or None (unknown). See usage_store.UsageEntry.decision_value.
UsageValue = dict[str, Any] | str | None

# Sentinel usage values that mean "no real quota figure" — shared SSOT for the
# switcher and the list reporter so trust checks can never diverge.
_KNOWN_USAGE_SENTINELS = frozenset({
    USAGE_API_KEY,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_NO_CREDENTIALS,
    USAGE_TOKEN_EXPIRED,
})


def slot_for_identity(
    accounts: dict[str, Any],
    email: str,
    org_uuid: str,
) -> str | None:
    """Map a live ``(email, organizationUuid)`` to its managed slot number."""
    for num, account in accounts.items():
        if (
            account.get("email") == email
            and (account.get("organizationUuid", "") or "") == org_uuid
        ):
            return str(num)
    return None


def _window_to_json(entry: dict[str, Any]) -> dict[str, Any]:
    """Project a 5h/7d usage window to JSON, preserving raw ``resetsAt``."""
    out: dict[str, Any] = {"pct": entry["pct"]}
    if "resets_at" in entry:
        out["resetsAt"] = entry["resets_at"]
    if "countdown" in entry:
        out["countdown"] = entry["countdown"]
    if "clock" in entry:
        out["clock"] = entry["clock"]
    return out


def _scoped_window_to_json(entry: dict[str, Any]) -> dict[str, Any]:
    """Project a per-model scoped weekly window, carrying its model name."""
    out = _window_to_json(entry)
    out["name"] = entry["name"]
    return out


def usage_to_json(usage: dict[str, Any]) -> dict[str, Any]:
    """Convert the internal usage dict to its camelCase JSON projection.

    Sub-keys are emitted only when present in the source (the API does not always
    return every window or pay-as-you-go spend).
    """
    out: dict[str, Any] = {}
    if "five_hour" in usage:
        out["fiveHour"] = _window_to_json(usage["five_hour"])
    if "seven_day" in usage:
        out["sevenDay"] = _window_to_json(usage["seven_day"])
    if "spend" in usage:
        spend = usage["spend"]
        spend_out: dict[str, Any] = {
            "used": spend["used"],
            "limit": spend["limit"],
            "pct": spend["pct"],
            "currency": spend["currency"],
        }
        if "resets_at" in spend:
            spend_out["resetsAt"] = spend["resets_at"]
        if "countdown" in spend:
            spend_out["countdown"] = spend["countdown"]
        if "clock" in spend:
            spend_out["clock"] = spend["clock"]
        out["spend"] = spend_out
    if "scoped" in usage:
        out["scoped"] = [_scoped_window_to_json(w) for w in usage["scoped"]]
    return out


def usage_fields(entry: UsageValue) -> tuple[str, dict[str, Any] | None]:
    """Map a collected usage entry to ``(usageStatus, usage|None)``.

    A collected entry is one of: a usage dict, the ``USAGE_TOKEN_EXPIRED`` sentinel
    (active token expired while Claude Code owns it), the ``USAGE_API_KEY`` sentinel
    (managed API-key account, no subscription quota), the
    ``USAGE_KEYCHAIN_UNAVAILABLE`` sentinel (active Keychain unreadable), the
    ``USAGE_NO_CREDENTIALS`` sentinel, or ``None`` (unknown — fetch failed or
    the stored measurement is too old to act on).
    """
    if isinstance(entry, dict):
        return "ok", usage_to_json(entry)
    if entry == USAGE_TOKEN_EXPIRED:
        return "token_expired", None
    if entry == USAGE_API_KEY:
        return "api_key", None
    if entry == USAGE_KEYCHAIN_UNAVAILABLE:
        return "keychain_unavailable", None
    if entry == USAGE_NO_CREDENTIALS:
        return "no_credentials", None
    if isinstance(entry, str):
        return "no_credentials", None
    return "unavailable", None


def account_ref(number: int | None, email: str) -> dict[str, Any]:
    """A minimal account reference, used for switch ``from``/``to``."""
    return {"number": number, "email": email}


def usage_freshness_fields(
    fetched_at: float | None, age_s: float | None
) -> dict[str, Any]:
    """Additive ``usageFetchedAt``/``usageAgeSeconds`` fields describing how
    old the served ``usage`` measurement is (the store may serve last-good
    data on fetch failure). Emitted only alongside a non-null ``usage``."""
    if fetched_at is None:
        return {}
    fields: dict[str, Any] = {
        "usageFetchedAt": (
            datetime.fromtimestamp(fetched_at, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    }
    if age_s is not None:
        fields["usageAgeSeconds"] = round(age_s, 1)
    return fields


def account_row(
    number: int,
    email: str,
    org_name: str,
    org_uuid: str,
    active: bool,
    usage_entry: UsageValue,
    *,
    usage_fetched_at: float | None = None,
    usage_age_s: float | None = None,
) -> dict[str, Any]:
    """A full account row for ``--list``."""
    status, usage = usage_fields(usage_entry)
    row = {
        "number": number,
        "email": email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "active": active,
        "usageStatus": status,
        "usage": usage,
    }
    if usage is not None:
        row.update(usage_freshness_fields(usage_fetched_at, usage_age_s))
    return row


def error_envelope(exc: Exception) -> dict[str, Any]:
    """The structured error payload emitted on a handled ClaudeSwitchError."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def empty_list_payload() -> dict[str, Any]:
    """Build the ``--list --json`` payload when no accounts are managed."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "activeAccountNumber": None,
        "accounts": [],
    }


def list_payload(
    accounts_info: list[tuple[int, str, str, str, bool, str]],
    entries: dict[str, StoreUsageEntry],
) -> dict[str, Any]:
    """Build the ``--list --json`` payload from gathered account + usage data."""
    active_num: int | None = None
    accounts = []
    for num, email, org_name, org_uuid, is_active, _ in accounts_info:
        if is_active:
            active_num = num
        entry = entries[str(num)]
        # JSON carries the decision-grade value: last-good only while it is
        # recent enough to act on (≤ STALE_OK_S), else unavailable. Showing
        # older measurements is a human-display affordance only — scripts
        # keying on usageStatus == "ok" must not act on arbitrarily old data.
        accounts.append(
            account_row(
                num, email, org_name, org_uuid, is_active,
                entry.decision_value(),
                usage_fetched_at=entry.fetched_at,
                usage_age_s=entry.age_s,
            )
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "activeAccountNumber": active_num,
        "accounts": accounts,
    }


def switch_result_from_op(
    op: dict[str, Any],
    strategy: str,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a switch result from a ``_perform_switch`` return value."""
    from_ref = op["from"]
    to_ref = op["to"]
    switched = from_ref != to_ref
    if switched:
        reason = "switched"
        message = f"Switched to Account-{to_ref['number']} ({to_ref['email']})"
    else:
        reason = "already-active"
        message = f"Already on Account-{to_ref['number']} ({to_ref['email']})"
    return {
        "schemaVersion": SCHEMA_VERSION,
        "switched": switched,
        "from": from_ref,
        "to": to_ref,
        "strategy": strategy,
        "reason": reason,
        "message": message,
        "warnings": (extra_warnings or []) + op["warnings"],
    }


def switch_noop(
    *,
    strategy: str,
    reason: str,
    message: str,
    from_ref: dict[str, Any] | None = None,
    to_ref: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a no-op switch result (``switched: false``)."""
    if from_ref is None:
        from_ref = to_ref
    return {
        "schemaVersion": SCHEMA_VERSION,
        "switched": False,
        "from": from_ref,
        "to": to_ref,
        "strategy": strategy,
        "reason": reason,
        "message": message,
        "warnings": warnings or [],
    }


def status_payload(
    *,
    identity: tuple[str, str] | None,
    account_num: str | None,
    account_record: dict[str, Any] | None,
    usage_entry: StoreUsageEntry | None,
    total_managed: int | None = None,
) -> dict[str, Any]:
    """Build the ``--status --json`` payload."""
    if identity is None:
        return {"schemaVersion": SCHEMA_VERSION, "active": None}
    current_email, current_org_uuid = identity
    if account_num is None or account_record is None:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "active": {"email": current_email, "managed": False},
        }
    org_name = account_record.get("organizationName", "") or ""
    org_uuid = account_record.get("organizationUuid", "") or ""
    # Decision-grade projection, same rule as the --list payload: stale
    # beyond STALE_OK_S reports unavailable, not "ok" with old numbers.
    entry = usage_entry if usage_entry is not None else StoreUsageEntry()
    status, usage = usage_fields(entry.decision_value())
    active: dict[str, Any] = {
        "number": int(account_num),
        "email": current_email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "managed": True,
        "usageStatus": status,
        "usage": usage,
    }
    if usage is not None:
        active.update(usage_freshness_fields(entry.fetched_at, entry.age_s))
    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "active": active,
    }
    if total_managed is not None:
        payload["totalManagedAccounts"] = total_managed
    return payload
