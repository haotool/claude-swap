"""Export and import account data for claude-swap.

Moves the OAuth credentials and config across machines via a portable
JSON envelope. No encryption is built in — users compose their own
(e.g. `cswap --export - | gpg -c > out.gpg`).
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_swap import __version__
from claude_swap.credentials import looks_like_api_key
from claude_swap.locking import FileLock
from claude_swap.exceptions import (
    ConfigError,
    CredentialReadError,
    TransferError,
)
from claude_swap.models import Platform, get_timestamp

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


FORMAT_VERSION = 1

# Cap import payloads so a huge/malicious file can't exhaust memory.
_MAX_IMPORT_BYTES = 16 * 1024 * 1024  # 16 MiB

_PLATFORM_TAG = {
    Platform.MACOS: "macos",
    Platform.LINUX: "linux",
    Platform.WSL: "wsl",
    Platform.WINDOWS: "windows",
    Platform.UNKNOWN: "unknown",
}


def _eprint(msg: str) -> None:
    """Print to stderr so stdout stays pure JSON in pipe mode."""
    print(msg, file=sys.stderr)


def _parse_payload(text: str, label: str) -> dict[str, Any]:
    """Parse a JSON string that should decode to an object."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransferError(f"{label} is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise TransferError(f"{label} must be a JSON object")
    return parsed


def _validate_imported_account(
    switcher: ClaudeAccountSwitcher, account: dict[str, Any]
) -> tuple[str, str]:
    """Validate per-account fields BEFORE any filename construction.

    Defends against path traversal: email + slot number flow into f-string
    filenames in switcher._read_account_credentials etc., so they must be
    constrained before use.
    """
    if not isinstance(account, dict):
        raise TransferError("account entry must be a JSON object")

    email = account.get("email")
    if not isinstance(email, str) or not switcher._validate_email(email):
        raise TransferError(f"invalid or missing email in imported account: {email!r}")

    raw_number = account.get("number")
    if isinstance(raw_number, bool) or not isinstance(raw_number, int) or raw_number < 1:
        raise TransferError(
            f"invalid slot number in imported account ({email}): {raw_number!r}"
        )

    # Org/uuid/added must be strings (or absent). A list/dict here would
    # otherwise blow up downstream (unhashable in seen_keys, broken composite
    # key matching, garbage in sequence.json).
    for field in ("organizationUuid", "organizationName", "uuid", "added"):
        if field in account and account[field] is not None:
            if not isinstance(account[field], str):
                raise TransferError(
                    f"{field} for {email} must be a string, got {type(account[field]).__name__}"
                )

    return email, str(raw_number)


def _snapshot_overwrite_slot(
    switcher: ClaudeAccountSwitcher,
    data: dict[str, Any],
    target_num: str,
    email: str,
) -> dict[str, Any]:
    return {
        "prev_creds": switcher._read_account_credentials(target_num, email) or None,
        "prev_config": switcher._read_account_config(target_num, email) or None,
        "prev_record": copy.deepcopy(data.get("accounts", {}).get(target_num)),
        "prev_in_sequence": int(target_num) in data.get("sequence", []),
    }


def _rollback_imported_slot(
    switcher: ClaudeAccountSwitcher, target_num: str, email: str
) -> None:
    switcher._delete_account_files(target_num, email)
    data = switcher._get_sequence_data_migrated() or {
        "activeAccountNumber": None,
        "lastUpdated": get_timestamp(),
        "sequence": [],
        "accounts": {},
    }
    accounts = data.setdefault("accounts", {})
    if target_num in accounts:
        del accounts[target_num]
    seq = data.setdefault("sequence", [])
    num_int = int(target_num)
    if num_int in seq:
        seq.remove(num_int)
    data["lastUpdated"] = get_timestamp()
    switcher._write_json(switcher.sequence_file, data)


def _rollback_overwritten_slot(
    switcher: ClaudeAccountSwitcher,
    target_num: str,
    email: str,
    snap: dict[str, Any],
) -> None:
    prev_creds = snap.get("prev_creds")
    if prev_creds:
        switcher._write_account_credentials(target_num, email, prev_creds)
    else:
        switcher._delete_account_credentials(target_num, email)
    prev_config = snap.get("prev_config")
    if prev_config:
        switcher._write_account_config(target_num, email, prev_config)
    else:
        config_file = (
            switcher.configs_dir / f".claude-config-{target_num}-{email}.json"
        )
        if config_file.exists():
            config_file.unlink()
    data = switcher._get_sequence_data_migrated() or {
        "activeAccountNumber": None,
        "lastUpdated": get_timestamp(),
        "sequence": [],
        "accounts": {},
    }
    prev_record = snap.get("prev_record")
    if prev_record is not None:
        data.setdefault("accounts", {})[target_num] = prev_record
    elif target_num in data.get("accounts", {}):
        del data["accounts"][target_num]
    seq = data.setdefault("sequence", [])
    num_int = int(target_num)
    if snap.get("prev_in_sequence"):
        if num_int not in seq:
            seq.append(num_int)
            seq.sort()
    elif num_int in seq:
        seq.remove(num_int)
    data["lastUpdated"] = get_timestamp()
    switcher._write_json(switcher.sequence_file, data)


def _undo_incomplete_import_write(
    switcher: ClaudeAccountSwitcher,
    target_num: str,
    email: str,
    *,
    creds_done: bool,
    config_done: bool,
) -> None:
    if not creds_done and not config_done:
        return
    switcher._delete_account_files(target_num, email)


def _undo_incomplete_overwrite(
    switcher: ClaudeAccountSwitcher,
    target_num: str,
    email: str,
    snap: dict[str, Any],
    *,
    creds_done: bool,
    config_done: bool,
) -> None:
    if not creds_done and not config_done:
        return
    if creds_done:
        prev_creds = snap.get("prev_creds")
        if prev_creds:
            switcher._write_account_credentials(target_num, email, prev_creds)
        else:
            switcher._delete_account_credentials(target_num, email)
    if config_done:
        prev_config = snap.get("prev_config")
        if prev_config:
            switcher._write_account_config(target_num, email, prev_config)
        else:
            config_file = (
                switcher.configs_dir / f".claude-config-{target_num}-{email}.json"
            )
            if config_file.exists():
                config_file.unlink()


def _rollback_import_writes(
    switcher: ClaudeAccountSwitcher, writes: list[dict[str, Any]]
) -> list[str]:
    """Undo completed writes from this import, newest first. Returns rollback failures."""
    failures: list[str] = []
    for write in reversed(writes):
        try:
            if write["outcome"] == "imported":
                _rollback_imported_slot(
                    switcher, write["target_num"], write["email"]
                )
            else:
                _rollback_overwritten_slot(
                    switcher,
                    write["target_num"],
                    write["email"],
                    write["snap"],
                )
        except Exception:
            failures.append(write["email"])
    return failures


def _atomic_write_file(path: Path, content: str) -> None:
    """Write text atomically with 0600 perms.

    Uses ``mkstemp`` so the temp file is owner-only from creation — a full
    export contains credentials, so there must be no world-readable window
    between write and chmod.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        if sys.platform != "win32":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _slim_config(config_obj: dict[str, Any], label: str) -> dict[str, Any]:
    """Reduce a parsed ~/.claude.json to just the keys a switch will consume.

    Today, only `oauthAccount` is read back during a switch. Stripping the
    rest at export time keeps cross-machine transfers small and avoids
    leaking source-machine identity (userID, anonymousId, absolute paths,
    cached feature flags) into the destination.
    """
    oauth = config_obj.get("oauthAccount")
    if not isinstance(oauth, dict):
        raise TransferError(
            f"{label} is missing oauthAccount — cannot export"
        )
    return {"oauthAccount": oauth}


def export_accounts(
    switcher: ClaudeAccountSwitcher,
    destination: str,
    account: str | None = None,
    full: bool = False,
) -> None:
    """Export accounts to a JSON file or stdout.

    Args:
        switcher: Initialized ClaudeAccountSwitcher.
        destination: File path, or "-" for stdout.
        account: Optional NUM|EMAIL to limit export to a single account.
        full: When True, include the entire ~/.claude.json snapshot per
            account (same-PC backup). Default False writes only oauthAccount.

    Raises:
        TransferError: malformed/missing data, unknown account.
        CredentialReadError: failed to read credentials.
    """
    sequence_data = switcher._get_sequence_data_migrated()
    if not sequence_data or not sequence_data.get("accounts"):
        raise TransferError("no accounts to export — run cswap --add-account first")

    accounts_map = sequence_data["accounts"]

    # Resolve which account numbers to export. When the user named a specific
    # account, missing backup data is a hard failure (they asked for that one);
    # in the all-accounts case we skip broken slots with a warning so one
    # damaged slot doesn't poison the whole backup.
    explicit_account = account is not None
    if account is not None:
        resolved = switcher._resolve_account_identifier(account)
        if resolved is None or resolved not in accounts_map:
            raise TransferError(f"account not found: {account}")
        target_nums = [resolved]
    else:
        target_nums = sorted(accounts_map.keys(), key=int)

    # Identify the live active account (live vault has fresher tokens than backup)
    current_identity = switcher._get_current_account()

    accounts_payload: list[dict[str, Any]] = []
    for num in target_nums:
        record = accounts_map[num]
        email = record.get("email", "")
        org_uuid = record.get("organizationUuid", "") or ""

        is_active = (
            current_identity is not None
            and current_identity[0] == email
            and current_identity[1] == org_uuid
        )

        if is_active:
            creds_text = switcher._read_credentials()
            if not creds_text:
                raise CredentialReadError(
                    f"failed to read live credentials for active account {email}"
                )
            config_path = switcher._get_claude_config_path()
            if not config_path.exists():
                raise ConfigError("Claude config file not found")
            config_text = config_path.read_text(encoding="utf-8")
        else:
            creds_text = switcher._read_account_credentials(num, email)
            config_text = switcher._read_account_config(num, email)
            if not creds_text or not config_text:
                if explicit_account:
                    if not creds_text:
                        raise CredentialReadError(
                            f"no backup credentials found for account {num} ({email})"
                        )
                    raise ConfigError(
                        f"no backup config found for account {num} ({email})"
                    )
                _eprint(
                    f"Skipping Account-{num} ({email}): no stored "
                    f"credentials/config — re-add with: "
                    f"cswap --add-account --slot {num}"
                )
                continue

        config_obj = _parse_payload(config_text, f"config for {email}")
        if not full:
            config_obj = _slim_config(config_obj, f"config for {email}")

        # API-key accounts store the credential as a raw ``sk-ant-api…`` string,
        # not OAuth JSON — carry it verbatim (and tag the kind) so the JSON parse
        # below doesn't choke and import can restore it as-is.
        is_api_key = looks_like_api_key(creds_text)
        entry: dict[str, Any] = {
            "number": int(num),
            "email": email,
            "uuid": record.get("uuid", ""),
            "organizationUuid": org_uuid,
            "organizationName": record.get("organizationName", "") or "",
            "added": record.get("added", ""),
            "credentials": (
                creds_text.strip()
                if is_api_key
                else _parse_payload(creds_text, f"credentials for {email}")
            ),
            "config": config_obj,
        }
        if is_api_key:
            entry["kind"] = "api_key"
        accounts_payload.append(entry)

    if not accounts_payload:
        raise TransferError(
            "no exportable accounts — all managed slots are missing stored "
            "credentials/config. Re-add with: cswap --add-account --slot <number>"
        )

    # Only carry activeAccountNumber if that slot is actually present in the
    # payload — otherwise import would reference an account that isn't there
    # (e.g., the recorded active slot was skipped due to missing backup).
    recorded_active = sequence_data.get("activeAccountNumber")
    exported_nums = {a["number"] for a in accounts_payload}
    active_in_payload = (
        recorded_active if recorded_active in exported_nums else None
    )

    envelope = {
        "version": FORMAT_VERSION,
        "exportedAt": get_timestamp(),
        "exportedFrom": _PLATFORM_TAG.get(switcher.platform, "unknown"),
        "swapVersion": __version__,
        "encrypted": False,
        "activeAccountNumber": active_in_payload,
        "accounts": accounts_payload,
    }

    serialized = json.dumps(envelope, indent=2)

    if destination == "-":
        sys.stdout.write(serialized)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return

    out_path = Path(destination).expanduser()
    _atomic_write_file(out_path, serialized + "\n")
    _eprint(f"Exported {len(accounts_payload)} account(s) to {out_path}")


def _read_import_envelope(source: str) -> dict[str, Any]:
    if source == "-":
        # Bound memory: read one byte past the cap to detect oversize input.
        text = sys.stdin.read(_MAX_IMPORT_BYTES + 1)
        if len(text) > _MAX_IMPORT_BYTES:
            raise TransferError(
                f"import payload exceeds the {_MAX_IMPORT_BYTES // (1024 * 1024)} MiB limit"
            )
    else:
        in_path = Path(source).expanduser()
        if not in_path.exists():
            raise TransferError(f"import file not found: {in_path}")
        if in_path.stat().st_size > _MAX_IMPORT_BYTES:
            raise TransferError(
                f"import file exceeds the {_MAX_IMPORT_BYTES // (1024 * 1024)} MiB limit"
            )
        text = in_path.read_text(encoding="utf-8")

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransferError(f"export file is not valid JSON: {exc}")

    if not isinstance(envelope, dict):
        raise TransferError("export file must be a JSON object")

    version = envelope.get("version")
    if version != FORMAT_VERSION:
        raise TransferError(
            f"unsupported export version: {version!r} (expected {FORMAT_VERSION})"
        )

    if envelope.get("encrypted") is True:
        raise TransferError(
            "encrypted exports are not supported in this version — "
            "decrypt before piping (e.g. gpg -d backup.gpg | cswap --import -)"
        )

    accounts = envelope.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise TransferError("export file has no accounts to import")

    return envelope


def _normalize_import_accounts(
    switcher: ClaudeAccountSwitcher, accounts: list[Any]
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for raw in accounts:
        email, exported_num = _validate_imported_account(switcher, raw)
        org_uuid = raw.get("organizationUuid", "") or ""
        creds_obj = raw.get("credentials")
        config_obj = raw.get("config")
        if not isinstance(config_obj, dict):
            raise TransferError(f"config for {email} must be a JSON object")
        is_api_key = raw.get("kind") == "api_key" or (
            isinstance(creds_obj, str) and looks_like_api_key(creds_obj)
        )
        if is_api_key:
            if not (isinstance(creds_obj, str) and looks_like_api_key(creds_obj)):
                raise TransferError(
                    f"API-key credentials for {email} must be a raw sk-ant-api… string"
                )
            creds_text = creds_obj.strip()
        else:
            if not isinstance(creds_obj, dict):
                raise TransferError(
                    f"credentials for {email} must be a JSON object"
                )
            creds_text = json.dumps(creds_obj)
        key = (email, org_uuid)
        if key in seen_keys:
            raise TransferError(
                f"duplicate account in export: {email} (org={org_uuid or 'personal'})"
            )
        seen_keys.add(key)
        normalized.append(
            {
                "email": email,
                "exported_num": exported_num,
                "org_uuid": org_uuid,
                "org_name": raw.get("organizationName", "") or "",
                "uuid": raw.get("uuid", "") or "",
                "added": raw.get("added") or get_timestamp(),
                "kind": "api_key" if is_api_key else "oauth",
                "creds_text": creds_text,
                "config_text": json.dumps(config_obj, indent=2),
            }
        )
    return normalized


def _empty_sequence_data() -> dict[str, Any]:
    return {
        "activeAccountNumber": None,
        "lastUpdated": get_timestamp(),
        "sequence": [],
        "accounts": {},
    }


def _resolve_import_target(
    switcher: ClaudeAccountSwitcher,
    entry: dict[str, Any],
    data: dict[str, Any],
    *,
    force: bool,
) -> tuple[str, str, dict[str, Any] | None] | None:
    existing_slot = switcher._find_account_slot(
        data, entry["email"], entry["org_uuid"]
    )
    if existing_slot is not None:
        if not force:
            _eprint(
                f"Skipped {entry['email']} (already exists, use --force)"
            )
            return None
        target_num = existing_slot
        outcome = "overwrote"
        live_pids = switcher._live_session_pids(target_num, entry["email"])
        if live_pids:
            _eprint(
                f"Warning: {entry['email']} (slot {target_num}) has a live "
                f"session-mode instance (PID {', '.join(map(str, live_pids))}); "
                "its session profile keeps the pre-import credentials until "
                "it is restarted via 'cswap run'."
            )
    else:
        if entry["exported_num"] not in data.get("accounts", {}):
            target_num = entry["exported_num"]
        else:
            target_num = str(switcher._get_next_account_number())
        outcome = "imported"

    overwrite_snap: dict[str, Any] | None = None
    if outcome == "overwrote":
        overwrite_snap = _snapshot_overwrite_slot(
            switcher, data, target_num, entry["email"]
        )
    return target_num, outcome, overwrite_snap


def _raise_import_write_failure(
    switcher: ClaudeAccountSwitcher,
    entry: dict[str, Any],
    completed_writes: list[dict[str, Any]],
    exc: Exception,
) -> None:
    succeeded = [w["email"] for w in completed_writes]
    rollback_failures = _rollback_import_writes(switcher, completed_writes)
    if rollback_failures:
        kept = [
            email
            for email in succeeded
            if email not in rollback_failures
        ]
        raise TransferError(
            "import failed on "
            f"{entry['email']}: {exc}; "
            f"partial import kept "
            f"{', '.join(kept) if kept else 'none'}; "
            f"could not roll back "
            f"{', '.join(rollback_failures)}"
        ) from exc
    if succeeded:
        raise TransferError(
            "import failed on "
            f"{entry['email']}: {exc}; "
            f"rolled back {len(succeeded)} account(s) from this import"
        ) from exc
    raise TransferError(
        f"import failed on {entry['email']}: {exc}"
    ) from exc


def _import_one_entry(
    switcher: ClaudeAccountSwitcher,
    entry: dict[str, Any],
    *,
    force: bool,
    is_envelope_active: bool,
    completed_writes: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    data = switcher._get_sequence_data_migrated() or _empty_sequence_data()
    resolved = _resolve_import_target(switcher, entry, data, force=force)
    if resolved is None:
        if is_envelope_active:
            existing_slot = switcher._find_account_slot(
                data, entry["email"], entry["org_uuid"]
            )
            return None, existing_slot
        return None, None

    target_num, outcome, overwrite_snap = resolved
    creds_done = False
    config_done = False
    try:
        switcher._write_account_credentials(
            target_num, entry["email"], entry["creds_text"]
        )
        creds_done = True
        switcher._write_account_config(
            target_num, entry["email"], entry["config_text"]
        )
        config_done = True

        data.setdefault("accounts", {})
        data.setdefault("sequence", [])
        new_record = {
            "email": entry["email"],
            "uuid": entry["uuid"],
            "organizationUuid": entry["org_uuid"],
            "organizationName": entry["org_name"],
            "added": entry["added"],
        }
        if entry["kind"] == "api_key":
            new_record["kind"] = "api_key"
        data["accounts"][target_num] = new_record
        if int(target_num) not in data["sequence"]:
            data["sequence"].append(int(target_num))
            data["sequence"].sort()
        data["lastUpdated"] = get_timestamp()
        switcher._write_json(switcher.sequence_file, data)
    except Exception as exc:
        if outcome == "imported":
            try:
                _undo_incomplete_import_write(
                    switcher,
                    target_num,
                    entry["email"],
                    creds_done=creds_done,
                    config_done=config_done,
                )
            except Exception:
                pass
        elif overwrite_snap is not None:
            try:
                _undo_incomplete_overwrite(
                    switcher,
                    target_num,
                    entry["email"],
                    overwrite_snap,
                    creds_done=creds_done,
                    config_done=config_done,
                )
            except Exception:
                pass
        _raise_import_write_failure(
            switcher, entry, completed_writes, exc
        )

    write = {
        "outcome": outcome,
        "target_num": target_num,
        "email": entry["email"],
        "snap": overwrite_snap,
    }
    if outcome == "overwrote":
        _eprint(f"Overwrote {entry['email']} (slot {target_num})")
    else:
        _eprint(f"Imported {entry['email']} → slot {target_num}")

    resolved_active = target_num if is_envelope_active else None
    return write, resolved_active


def _finalize_import_active_slot(
    switcher: ClaudeAccountSwitcher, resolved_active_slot: str | None
) -> None:
    final = switcher._get_sequence_data()
    if (
        final is not None
        and final.get("activeAccountNumber") in (None, 0)
        and resolved_active_slot is not None
    ):
        final["activeAccountNumber"] = int(resolved_active_slot)
        final["lastUpdated"] = get_timestamp()
        switcher._write_json(switcher.sequence_file, final)


def import_accounts(
    switcher: ClaudeAccountSwitcher,
    source: str,
    force: bool = False,
) -> None:
    """Import accounts from a JSON file or stdin.

    Args:
        switcher: Initialized ClaudeAccountSwitcher.
        source: File path, or "-" for stdin.
        force: When True, overwrites the existing matching slot in place.

    Raises:
        TransferError: malformed file, version mismatch, encrypted payload,
            or an environmental failure during import (writes from this run
            are rolled back when possible).
    """
    envelope = _read_import_envelope(source)
    normalized = _normalize_import_accounts(switcher, envelope["accounts"])

    switcher._setup_directories()
    switcher._init_sequence_file()

    imported = 0
    skipped = 0
    overwritten = 0

    envelope_active = envelope.get("activeAccountNumber")
    envelope_active_str = (
        str(envelope_active) if isinstance(envelope_active, int) else None
    )
    resolved_active_slot: str | None = None
    completed_writes: list[dict[str, Any]] = []

    # Hold the cross-process lock for the whole import so its many sequence.json
    # read-modify-write cycles stay atomic against a concurrent switch/add.
    with FileLock(switcher.lock_file):
        for entry in normalized:
            is_envelope_active = (
                envelope_active_str is not None
                and entry["exported_num"] == envelope_active_str
            )
            write, active_slot = _import_one_entry(
                switcher,
                entry,
                force=force,
                is_envelope_active=is_envelope_active,
                completed_writes=completed_writes,
            )
            if write is None:
                skipped += 1
                if active_slot is not None:
                    resolved_active_slot = active_slot
                continue

            completed_writes.append(write)
            if active_slot is not None:
                resolved_active_slot = active_slot
            if write["outcome"] == "overwrote":
                overwritten += 1
            else:
                imported += 1

        _finalize_import_active_slot(switcher, resolved_active_slot)

    _eprint(
        f"Done: {imported} imported, {overwritten} overwritten, {skipped} skipped"
    )
