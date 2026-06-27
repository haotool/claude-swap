"""Serialization helpers for ``--json`` structured output.

Centralizes the schema-v1 shapes so ``--list``/``--status``/``--switch`` agree on
field names (camelCase, matching the export envelope in transfer.py) and on how the
internal usage dict is projected to JSON. Callers build payloads here; the CLI does
the single ``json.dumps`` (see cli.py).
"""

from __future__ import annotations

# Bump only on a breaking change to any payload shape. Scripts key off this.
SCHEMA_VERSION = 1

# Sentinel entries that ``_collect_usage`` / ``_fetch_active_usage`` yield in place
# of a usage dict. Kept here (the serialization hub) so the human renderer and the
# JSON projection agree instead of scattering raw strings.
USAGE_NO_CREDENTIALS = "no credentials"
USAGE_TOKEN_EXPIRED = "token expired"
# API-key (``/login`` managed key) accounts have no subscription quota; usage is
# reported as this sentinel instead of being fetched from the OAuth usage API.
USAGE_API_KEY = "api key"


def _window_to_json(entry: dict) -> dict:
    """Project a 5h/7d usage window to JSON, preserving raw ``resetsAt``."""
    out: dict = {"pct": entry["pct"]}
    if "resets_at" in entry:
        out["resetsAt"] = entry["resets_at"]
    if "countdown" in entry:
        out["countdown"] = entry["countdown"]
    if "clock" in entry:
        out["clock"] = entry["clock"]
    return out


def usage_to_json(usage: dict) -> dict:
    """Convert the internal usage dict to its camelCase JSON projection.

    Sub-keys are emitted only when present in the source (the API does not always
    return every window or pay-as-you-go spend).
    """
    out: dict = {}
    if "five_hour" in usage:
        out["fiveHour"] = _window_to_json(usage["five_hour"])
    if "seven_day" in usage:
        out["sevenDay"] = _window_to_json(usage["seven_day"])
    if "spend" in usage:
        spend = usage["spend"]
        spend_out: dict = {
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
    return out


def usage_fields(entry: dict | str | None) -> tuple[str, dict | None]:
    """Map a collected usage entry to ``(usageStatus, usage|None)``.

    A collected entry is one of: a usage dict, the ``USAGE_TOKEN_EXPIRED`` sentinel
    (active token expired while Claude Code owns it), the ``USAGE_API_KEY`` sentinel
    (managed API-key account, no subscription quota), the ``USAGE_NO_CREDENTIALS``
    sentinel, or ``None`` (fetch failed).
    """
    if isinstance(entry, dict):
        return "ok", usage_to_json(entry)
    if entry == USAGE_TOKEN_EXPIRED:
        return "token_expired", None
    if entry == USAGE_API_KEY:
        return "api_key", None
    if isinstance(entry, str):
        return "no_credentials", None
    return "unavailable", None


def account_ref(number: int | None, email: str) -> dict:
    """A minimal account reference, used for switch ``from``/``to``."""
    return {"number": number, "email": email}


def account_row(
    number: int,
    email: str,
    org_name: str,
    org_uuid: str,
    active: bool,
    usage_entry: dict | str | None,
) -> dict:
    """A full account row for ``--list``."""
    status, usage = usage_fields(usage_entry)
    return {
        "number": number,
        "email": email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "active": active,
        "usageStatus": status,
        "usage": usage,
    }


def error_envelope(exc: Exception) -> dict:
    """The structured error payload emitted on a handled ClaudeSwitchError."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def list_payload(
    accounts_info: list[tuple[int, str, str, str, bool, str]],
    usages: list[dict | str | None],
) -> dict:
    """Build the ``--list --json`` payload from gathered account + usage data."""
    active_num: int | None = None
    accounts = []
    for (num, email, org_name, org_uuid, is_active, _), usage in zip(
        accounts_info, usages
    ):
        if is_active:
            active_num = num
        accounts.append(
            account_row(num, email, org_name, org_uuid, is_active, usage)
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "activeAccountNumber": active_num,
        "accounts": accounts,
    }


def switch_result_from_op(
    op: dict,
    strategy: str,
    extra_warnings: list[str] | None = None,
) -> dict:
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
    from_ref: dict | None = None,
    to_ref: dict | None = None,
    warnings: list[str] | None = None,
) -> dict:
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
