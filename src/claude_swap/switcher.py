"""Core account switcher logic for Claude Code."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from claude_swap import macos_keychain

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    SessionError,
    SwitchError,
    ValidationError,
)
from claude_swap import oauth
from claude_swap.cache import (
    MISSING,
    read_cache,
    read_cache_data,
    read_cache_with_timestamp,
    write_cache,
)
from claude_swap.json_output import (
    SCHEMA_VERSION,
    USAGE_API_KEY,
    USAGE_NO_CREDENTIALS,
    USAGE_TOKEN_EXPIRED,
    account_ref,
    account_row,
    usage_fields,
)
from claude_swap.locking import FileLock
from claude_swap.logging_config import setup_logging
from claude_swap.models import (
    AutoSwitchDecisionContext,
    BackgroundAutoSwitchIntent,
    InteractiveAutoSwitchIntent,
    ManualSwitchIntent,
    Platform,
    SwitchIntent,
    SwitchPlanResult,
    SwitchTransaction,
    get_timestamp,
)
from claude_swap.printer import (
    abbreviate_path,
    accent,
    bold_accent,
    bolded,
    dimmed,
    entrypoint_label,
    ide_short_name,
    error,
    format_age,
    muted,
    warning,
)
from claude_swap.paths import (
    get_backup_root,
    get_global_config_path,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)
from claude_swap.process_detection import get_running_instances

# The credential storage/freshness mechanics live in credentials.py /
# credential_refresh.py (plan 020). SECURITY_SERVICE / CLAUDE_CODE_KEYCHAIN_SERVICE
# are re-exported here so callers and tests referencing them off ``switcher`` keep
# working unchanged.
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,  # noqa: F401  (re-exported for back-compat)
    SECURITY_SERVICE,
    CredentialStore,
    looks_like_api_key,
)
from claude_swap.credential_refresh import CredentialRefresher
from claude_swap.usage_cache import (
    _merge_usage_with_previous,
    _persist_usage_cache_entry,
    _usage_from_cache,
    _usage_slot_trusted,
    _usage_to_cache,  # noqa: F401  (re-exported for back-compat with tests)
)

# Service name under which the legacy ``keyring`` backend stored per-account
# backup credentials on macOS (kept for the one-time keyring → security migration
# and for the Windows Credential Manager migration).
KEYRING_SERVICE = "claude-code"

# Setup-tokens are inference-only server-side; wider scopes trigger 403s
# on profile endpoints. Matches Claude Code's CLAUDE_CODE_OAUTH_TOKEN path.
SETUP_TOKEN_SCOPES = ("user:inference",)

# Auto-switch (Beta): when the active account's 5h/7d usage reaches this
# percentage, automated paths pick the cooldown-aware best target.
DEFAULT_AUTO_SWITCH_THRESHOLD = 95
_SATURATED_SWITCH_MARGIN_S = 300  # minimum reset-time lead before switching between two saturated accounts
_USAGE_CACHE_TTL = 15  # seconds (upstream JSON/status paths)


def auto_switch_display(cfg: dict) -> tuple[bool, int, str, str]:
    """Normalized auto-switch fields for CLI/TUI status formatters."""
    enabled = bool(cfg["enabled"])
    threshold = int(cfg["threshold"])
    on_off = "ON" if enabled else "OFF"
    enabled_disabled = "enabled" if enabled else "disabled"
    return enabled, threshold, on_off, enabled_disabled


# Cooldown-aware target picker (``_pick_best_switch_target``):
# Score buckets used by ``_slot_switch_score`` — lower is better.
# 0: unsaturated (max(5h,7d) < threshold) — within bucket, prefer lowest pct
# 1: saturated (>= threshold) — within bucket, prefer soonest ``resets_at``
# 2: unknown usage — worst (cold cache, no signal)
# Named so callers don't need to reverse-engineer the magic ints.
_SLOT_SCORE_BUCKET_UNSATURATED = 0
_SLOT_SCORE_BUCKET_SATURATED = 1
_SLOT_SCORE_BUCKET_UNKNOWN = 2


def _slot_for_identity(
    accounts: dict,
    email: str,
    org_uuid: str,
) -> str | None:
    """Map live (email, organizationUuid) to a managed slot number."""
    for num, account in accounts.items():
        if (
            account.get("email") == email
            and (account.get("organizationUuid", "") or "") == org_uuid
        ):
            return str(num)
    return None


def _slot_switch_score(
    usage: object,
    threshold: int,
) -> tuple[int, float]:
    """Score a slot for cooldown-aware switch target selection.

    Lower scores are better.  Pure function (no I/O); easily tested in
    isolation.  Returns a sortable ``(bucket, primary_metric)`` tuple where
    Python's lexicographic tuple comparison gives the right total order:

    * ``(0, max_pct)``        — unsaturated; within bucket, prefer lower pct
                                 so we stay on the freshest account.
    * ``(1, soonest_ts)``     — saturated; within bucket, prefer the soonest
                                 ``resets_at`` so the user parks on the
                                 account that will free up first.
    * ``(1, math.inf)``       — saturated but no ``resets_at`` available;
                                 ranks below any known-reset saturated slot.
    * ``(2, math.inf)``       — unknown usage (cold cache, parse failure,
                                 missing keys); ranks behind every signal we
                                 actually have.

    The ``threshold`` is the auto-switch threshold; an account with
    ``max(5h, 7d) >= threshold`` is treated as "saturated" for picking
    purposes regardless of the exact percent.
    """
    if not isinstance(usage, dict):
        return (_SLOT_SCORE_BUCKET_UNKNOWN, math.inf)

    pcts: list[float] = []
    saturated_resets: list[float] = []
    for key in ("five_hour", "seven_day"):
        entry = usage.get(key)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("pct")
        if not isinstance(pct, (int, float)):
            continue
        pct_f = float(pct)
        pcts.append(pct_f)
        if pct_f >= threshold:
            resets_at = entry.get("resets_at")
            if isinstance(resets_at, str):
                try:
                    ts = datetime.fromisoformat(resets_at).timestamp()
                except ValueError:
                    continue
                saturated_resets.append(ts)

    if not pcts:
        return (_SLOT_SCORE_BUCKET_UNKNOWN, math.inf)

    max_pct = max(pcts)
    if max_pct < threshold:
        return (_SLOT_SCORE_BUCKET_UNSATURATED, max_pct)
    if not saturated_resets:
        # Saturated but we don't know when it frees — worst within saturated.
        return (_SLOT_SCORE_BUCKET_SATURATED, math.inf)
    return (_SLOT_SCORE_BUCKET_SATURATED, min(saturated_resets))


def _max_usage_pct(usage: dict | None) -> float | None:
    """Return the highest 5h/7d utilization percentage in a usage dict.

    Only the rate-limit windows (``five_hour``/``seven_day``) are considered —
    the dollar-denominated ``spend`` entry is intentionally ignored, since it is
    not the limit that blocks Claude Code sessions. Returns ``None`` when no
    usable percentage is present.
    """
    if not isinstance(usage, dict):
        return None
    pcts: list[float] = []
    for key in ("five_hour", "seven_day"):
        entry = usage.get(key)
        if isinstance(entry, dict):
            pct = entry.get("pct")
            if isinstance(pct, (int, float)):
                pcts.append(float(pct))
    return max(pcts) if pcts else None


def _format_usage_lines(usage: dict) -> list[str]:
    lines: list[str] = []
    spend = usage.get("spend")
    if spend:
        used = spend["used"]
        limit = spend["limit"]
        pct = spend["pct"]
        if "clock" in spend:
            lines.append(f"$$: {pct:>3.0f}%   resets {spend['clock']:<12}  ${used:,.2f} / ${limit:,.2f}")
        else:
            lines.append(f"$$: {pct:>3.0f}%   ${used:,.2f} / ${limit:,.2f}")
    h5 = usage.get("five_hour")
    if h5:
        if "clock" in h5:
            lines.append(f"5h: {h5['pct']:>3.0f}%   resets {h5['clock']:<12}  in {h5['countdown']}")
        else:
            lines.append(f"5h: {h5['pct']:>3.0f}%")
    d7 = usage.get("seven_day")
    if d7:
        if "clock" in d7:
            lines.append(f"7d: {d7['pct']:>3.0f}%   resets {d7['clock']:<12}  in {d7['countdown']}")
        else:
            lines.append(f"7d: {d7['pct']:>3.0f}%")
    return lines


def _sweep_legacy_keyring(usernames: list[str], removed_items: list[str]) -> None:
    """Best-effort purge of legacy ``KEYRING_SERVICE`` entries via ``keyring``.

    Used only during ``purge()`` to mop up entries a never-completed
    keyring → file/security migration left behind. Never raises: keyring being
    unavailable or an entry being absent just means nothing to clean up.
    """
    try:
        import keyring  # noqa: PLC0415 - legacy cleanup only

        for username in usernames:
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
                removed_items.append(f"Legacy keyring credential: {username}")
            except Exception:
                pass  # Doesn't exist / other error — ignore
    except Exception:
        pass  # keyring unavailable — nothing to clean up


class ClaudeAccountSwitcher:
    """Multi-account switcher for Claude Code."""

    def __init__(self, debug: bool = False):
        self.home = Path.home()
        self.platform = Platform.detect()
        self.backup_dir = get_backup_root()

        # Migrate legacy ~/.claude-swap-backup to the new XDG path on Linux/WSL
        # before any logger or directory setup writes to the new location.
        # Migration is a no-op on macOS/Windows where backup_dir already
        # equals the legacy path. MigrationError on a genuine collision
        # propagates as a ClaudeSwitchError and is caught by the CLI.
        if migrate_legacy_backup_dir(self.backup_dir):
            legacy = get_legacy_backup_root()
            print(
                f"claude-swap: migrated data from {legacy} to {self.backup_dir}",
                file=sys.stderr,
            )

        self.sequence_file = self.backup_dir / "sequence.json"
        self.configs_dir = self.backup_dir / "configs"
        self.credentials_dir = self.backup_dir / "credentials"
        self.lock_file = self.backup_dir / ".lock"
        self._logger = setup_logging(self.backup_dir, debug=debug)

        # Credential storage layer (plan 020). Reads platform / credentials_dir
        # / _logger off this switcher at call time via the _StoreHost Protocol.
        self._store = CredentialStore(self)
        # OAuth credential-freshness layer (plan 020 follow-on): verify/refresh/
        # sync of backup tokens. Collaborator holding this switcher.
        self._refresher = CredentialRefresher(self)

        # Run any pending one-time data migrations (e.g. relocating Windows
        # backup credentials out of Credential Manager into files). Imported
        # lazily to avoid a circular import, and self-contained so it never
        # aborts construction. No-op on fresh installs / once recorded.
        from claude_swap.migrations import run_migrations

        run_migrations(self)

    def _is_running_in_container(self) -> bool:
        """Check if running inside a container."""
        # Check environment variables (works on all platforms)
        if os.environ.get("CONTAINER") or os.environ.get("container"):
            return True

        # Windows doesn't have the same container indicators
        if self.platform == Platform.WINDOWS:
            return False

        # Check for Docker environment file (Linux/macOS)
        if Path("/.dockerenv").exists():
            return True

        # Check cgroup for container indicators (Linux)
        cgroup_path = Path("/proc/1/cgroup")
        if cgroup_path.exists():
            try:
                content = cgroup_path.read_text()
                if any(
                    x in content
                    for x in ["docker", "lxc", "containerd", "kubepods"]
                ):
                    return True
            except PermissionError:
                pass

        # Check mount info (Linux)
        mountinfo_path = Path("/proc/self/mountinfo")
        if mountinfo_path.exists():
            try:
                content = mountinfo_path.read_text()
                if any(x in content for x in ["docker", "overlay"]):
                    return True
            except PermissionError:
                pass

        return False

    def _get_claude_config_path(self) -> Path:
        """Get the Claude configuration file path, mirroring claude-code."""
        return get_global_config_path()

    def _validate_email(self, email: str) -> bool:
        """Validate email format."""
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email))

    def _setup_directories(self) -> None:
        """Create backup directories with proper permissions."""
        for directory in [self.backup_dir, self.configs_dir, self.credentials_dir]:
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _read_json(self, path: Path) -> dict | None:
        """Read and parse JSON file."""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._logger.warning(f"Invalid JSON in {path}")
            return None

    def _write_json(self, path: Path, data: dict) -> None:
        """Write JSON file with validation."""
        content = json.dumps(data, indent=2)

        # Write to temp file first
        temp_path = path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(content, encoding="utf-8")

        # Validate written content
        try:
            json.loads(temp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            temp_path.unlink()
            raise ConfigError("Generated invalid JSON")

        # Move to final location
        shutil.move(str(temp_path), str(path))
        if sys.platform != "win32":
            os.chmod(path, 0o600)

    def _read_credentials(self) -> str | None:
        """Read Claude Code's active credentials (delegates to CredentialStore)."""
        return self._store._read_credentials()

    def _write_credentials(self, credentials: str, *, verify: bool = False) -> None:
        """Write Claude Code's active credentials (delegates to CredentialStore)."""
        self._store._write_credentials(credentials, verify=verify)

    def _uses_file_backup_backend(self) -> bool:
        """Whether per-account backups live in files vs. Keychain (delegates)."""
        return self._store._uses_file_backup_backend()

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        """Read account credentials from backup (delegates to CredentialStore)."""
        return self._store._read_account_credentials(account_num, email)

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account backup credentials, then invalidate the session profile.

        Pure storage is delegated to ``CredentialStore``; the session-lifecycle
        side effect below is switcher-owned (the store is data-only toward its
        host) and so stays here, keeping behavior identical for every caller.
        """
        self._store._write_account_credentials(account_num, email, credentials)
        # Backup credentials changed (re-login via --add-account, --add-token,
        # import, switch backing up, or a usage-refresh rotation): a session
        # profile seeded from the old credentials may now hold a stale or
        # rotated-out token that still passes the local reuse check. Drop the
        # profile's credential material so the next `cswap run` re-bootstraps
        # from this fresh backup (history is preserved). A LIVE session keeps
        # its own copy untouched — claude manages it; pulling credentials out
        # from under a running process would be worse than the drift caveat —
        # but gets a stale marker so setup_session re-bootstraps it once it
        # is no longer live, instead of trusting the local reuse check.
        if self._live_session_pids(account_num, email):
            from claude_swap.session import mark_session_stale

            mark_session_stale(self._session_dir(account_num, email))
        else:
            self._invalidate_session_credentials(account_num, email)

    def _write_verified_live_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> str:
        """Persist + verify live credentials (delegates to CredentialRefresher)."""
        return self._refresher.write_verified_live(account_num, email, credentials)

    def _sync_live_account_credentials_to_backup(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Best-effort sync of refreshed live credentials (delegates)."""
        self._refresher.sync_live_to_backup(account_num, email, credentials)

    def _refresh_target_credentials_before_activation(
        self, account_num: str, email: str, credentials: str, *, force: bool = False
    ) -> str:
        """Refresh a target's OAuth token before activation (delegates)."""
        return self._refresher.refresh_target_before_activation(
            account_num, email, credentials, force=force
        )

    def _refresh_inactive_credentials_if_needed(
        self, account_num: str, email: str, credentials: str
    ) -> tuple[str, str | None]:
        """Refresh an inactive backup token before expiry (delegates)."""
        return self._refresher.refresh_inactive_if_needed(account_num, email, credentials)

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        """Delete account credentials from backup (delegates to CredentialStore)."""
        self._store._delete_account_credentials(account_num, email)


    def _kc_read_backup(self, account_num: str, email: str) -> str:
        return self._read_account_credentials(account_num, email)

    def _kc_write_backup(self, account_num: str, email: str, credentials: str) -> None:
        self._write_account_credentials(account_num, email, credentials)

    def _delete_backup_keychain_quiet(self, account_num: str, email: str) -> None:
        self._delete_account_credentials(account_num, email)

    @staticmethod
    def _find_account_slot(
        data: dict, email: str, organization_uuid: str
    ) -> str | None:
        return _slot_for_identity(data.get("accounts", {}), email, organization_uuid)

    def _delete_account_files(self, account_num: str, email: str) -> None:
        """Delete all backup files for an account (credentials + config).

        Single chokepoint for every path that removes or displaces a slot
        (remove_account, add_account/add_token slot overwrite & migration):
        refuses while a session-mode claude is live against the slot, and
        removes the slot's session profile alongside the backups so a stale
        profile can never outlive its account.

        Raises:
            SessionError: a live session-mode instance is using this account.
        """
        self._ensure_no_live_session(account_num, email, "the operation")
        self._delete_account_credentials(account_num, email)
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            config_file.unlink()
        self._delete_session_profile(account_num, email)

    def _read_account_config(self, account_num: str, email: str) -> str:
        """Read account config from backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            return config_file.read_text(encoding="utf-8")
        return ""

    def _account_is_switchable(self, account_num: str) -> bool:
        """Whether a slot has both stored credentials and config backups.

        Used by switch() and switch_to() to decide whether a target slot can
        be activated without re-adding the account. Tolerates stale sequence
        entries that reference a removed account record.
        """
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(str(account_num))
        if not record:
            return False
        email = record.get("email", "")
        if not self._read_account_credentials(str(account_num), email):
            return False
        if not self._read_account_config(str(account_num), email):
            return False
        return True

    def _resolve_active_slots(self) -> tuple[str | None, str | None]:
        """Return ``(live_active_slot, sequence_active_slot)``."""
        data = self._get_sequence_data() or {}
        sequence_slot = data.get("activeAccountNumber")
        sequence_slot_str = (
            str(sequence_slot) if sequence_slot is not None else None
        )

        live_slot: str | None = None
        identity = self._get_current_account()
        if identity is not None:
            current_email, current_org_uuid = identity
            live_slot = _slot_for_identity(
                data.get("accounts", {}), current_email, current_org_uuid,
            )

        return live_slot, sequence_slot_str

    def _usage_cache_fresh(
        self,
        cached: dict,
        account_keys: set[str],
        *,
        file_timestamp: float | None = None,
    ) -> bool:
        """True when every account row is within the shared per-slot TTL."""
        if not isinstance(cached, dict) or set(cached.keys()) != account_keys:
            return False
        now = time.time()
        for key in account_keys:
            entry = cached.get(key)
            usage = _usage_from_cache(entry)
            if not isinstance(usage, dict) or not _usage_slot_trusted(
                usage, now, file_timestamp,
            ):
                return False
        return True

    def _trusted_usage_snapshots(self) -> dict[str, dict]:
        """Usage entries with per-slot freshness — safe for unattended planning."""
        usage_cache_path = self.backup_dir / "cache" / "usage.json"
        cached, file_ts = read_cache_with_timestamp(usage_cache_path)
        if cached is None:
            return {}

        data = self._get_sequence_data() or {}
        switchable = [
            str(num)
            for num in data.get("sequence", [])
            if self._account_is_switchable(str(num))
        ]
        if not switchable:
            return {}

        now = time.time()
        snapshots: dict[str, dict] = {}
        for slot in switchable:
            if slot not in cached:
                return {}
            usage = _usage_from_cache(cached[slot])
            if not isinstance(usage, dict):
                return {}
            if not _usage_slot_trusted(usage, now, file_ts):
                return {}
            snapshots[slot] = {
                k: v for k, v in usage.items() if k != "_cached_at"
            }
        return snapshots

    def _refresh_switchable_usage_cache(self) -> None:
        """Fetch usage for every switchable slot before automated planning."""
        data = self._get_sequence_data_migrated() or {}
        current_identity = self._get_current_account()
        active_num: str | None = None
        if current_identity is not None:
            current_email, current_org_uuid = current_identity
            active_num = _slot_for_identity(
                data.get("accounts", {}), current_email, current_org_uuid,
            )

        accounts_info: list[tuple[int, str, bool, str]] = []
        for num in data.get("sequence", []):
            num_str = str(num)
            if not self._account_is_switchable(num_str):
                continue
            account = data.get("accounts", {}).get(num_str, {})
            email = account.get("email", "unknown")
            is_active = num_str == active_num
            creds = (
                self._read_credentials() or ""
                if is_active
                else self._read_account_credentials(num_str, email)
            )
            accounts_info.append((num, email, is_active, creds))

        def fetch(
            item: tuple[int, str, bool, str],
        ) -> tuple[str, dict | oauth.UsageFetchError | None | str]:
            num, email, is_active, creds = item
            num_str = str(num)
            if not creds or not oauth.extract_access_token(creds):
                return num_str, None

            def persist(acct_num: str, acct_email: str, new_creds: str) -> None:
                with FileLock(self.lock_file):
                    self._write_account_credentials(acct_num, acct_email, new_creds)

            has_live_session = bool(self._live_session_pids(num_str, email))
            return num_str, oauth.fetch_usage_for_account(
                num_str,
                email,
                creds,
                is_active=is_active or has_live_session,
                persist_credentials=persist,
            )

        usage_cache_path = self.backup_dir / "cache" / "usage.json"
        max_workers = min(4, max(len(accounts_info), 1))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(fetch, accounts_info))

        with FileLock(self.lock_file):
            previous_cached = read_cache_data(usage_cache_path, default={})
            existing = previous_cached if isinstance(previous_cached, dict) else {}
            for num_str, usage in results:
                _persist_usage_cache_entry(
                    existing, num_str, usage, existing.get(num_str),
                )
            write_cache(usage_cache_path, existing)

    def build_auto_switch_decision(
        self,
        threshold: int,
        active_usage_pct: float | None,
    ) -> AutoSwitchDecisionContext:
        """Build the poll-cycle decision snapshot for automated switching."""
        live_slot, sequence_slot = self._resolve_active_slots()
        if (
            live_slot
            and sequence_slot
            and live_slot != sequence_slot
        ):
            drift_key = (live_slot, sequence_slot)
            if getattr(self, "_logged_active_drift", None) != drift_key:
                self._logger.warning(
                    "auto-switch active slot drift: live=Account-%s "
                    "sequence=Account-%s — using live identity",
                    live_slot,
                    sequence_slot,
                )
                self._logged_active_drift = drift_key

        data = self._get_sequence_data() or {}
        switchable = [
            str(num)
            for num in data.get("sequence", [])
            if self._account_is_switchable(str(num))
        ]
        snapshots = self._trusted_usage_snapshots()
        if switchable and len(snapshots) < len(switchable):
            self._refresh_switchable_usage_cache()
            snapshots = self._trusted_usage_snapshots()

        return AutoSwitchDecisionContext(
            threshold=threshold,
            active_usage_pct=active_usage_pct,
            live_active_slot=live_slot,
            sequence_active_slot=sequence_slot,
            usage_by_slot=snapshots,
        )

    def _pick_best_from_snapshots(
        self,
        threshold: int,
        snapshots: dict[str, object],
        *,
        exclude: str | None = None,
    ) -> str | None:
        """Score switchable slots from trusted usage snapshots only."""
        data = self._get_sequence_data() or {}
        sequence = data.get("sequence", [])
        if not sequence:
            return None

        scored: list[tuple[tuple[int, float], str]] = []
        for num in sequence:
            num_str = str(num)
            if exclude is not None and num_str == exclude:
                continue
            if not self._account_is_switchable(num_str):
                continue
            cached_entry = snapshots.get(num_str)
            score = _slot_switch_score(cached_entry, threshold)
            scored.append((score, num_str))

        if not scored:
            return None
        if all(s[0][0] == _SLOT_SCORE_BUCKET_UNKNOWN for s in scored):
            return None

        scored.sort()
        return scored[0][1]

    def _plan_automated_switch(
        self,
        decision: AutoSwitchDecisionContext,
    ) -> SwitchPlanResult:
        """Choose an automated switch target from a trusted decision snapshot."""
        active = decision.live_active_slot or decision.sequence_active_slot
        best = self._pick_best_from_snapshots(
            decision.threshold,
            decision.usage_by_slot,
        )

        if best is None:
            return SwitchPlanResult(
                outcome="no_trusted_signal",
                reason=(
                    "no trusted usage snapshots — run `cswap --list` or wait "
                    "for the monitor to refresh cache"
                ),
            )

        if active is not None and best == active:
            return SwitchPlanResult(
                outcome="already_optimal",
                target=best,
                reason=f"already on optimal Account-{best}",
            )

        # When both the active account and the best candidate are saturated,
        # only switch if the candidate resets at least this many seconds sooner.
        # Without this guard, continued use on the active account pushes its
        # resets_at forward on every poll, making the idle account always
        # appear "better" — causing indefinite oscillation between two accounts
        # that are both rate-limited. 5 minutes is enough to outweigh API
        # timestamp jitter while still reacting to meaningful reset differences.
        if active is not None and best is not None:
            best_score = _slot_switch_score(
                decision.usage_by_slot.get(best), decision.threshold
            )
            if best_score[0] == _SLOT_SCORE_BUCKET_SATURATED:
                active_score = _slot_switch_score(
                    decision.usage_by_slot.get(active), decision.threshold
                )
                if active_score[0] == _SLOT_SCORE_BUCKET_SATURATED:
                    if best_score[1] >= active_score[1] - _SATURATED_SWITCH_MARGIN_S:
                        return SwitchPlanResult(
                            outcome="already_optimal",
                            target=active,
                            reason=(
                                f"both accounts saturated; staying on "
                                f"Account-{active} (target resets at most "
                                f"{_SATURATED_SWITCH_MARGIN_S}s sooner)"
                            ),
                        )

        return SwitchPlanResult(
            outcome="chosen",
            target=best,
            reason=f"cooldown-aware pick Account-{best}",
        )

    def _pick_best_switch_target(
        self,
        threshold: int,
        *,
        exclude: str | None = None,
    ) -> str | None:
        """Pick the switchable slot with the best cooldown-aware score.

        Test helper: reads the on-disk cache without TTL enforcement.
        Production automated paths must use ``_plan_automated_switch`` with
        ``build_auto_switch_decision`` instead.
        """
        usage_cache_path = self.backup_dir / "cache" / "usage.json"
        cached = read_cache_data(usage_cache_path, default={}) or {}
        if not isinstance(cached, dict):
            cached = {}
        snapshots = {
            str(k): _usage_from_cache(v)
            for k, v in cached.items()
        }
        return self._pick_best_from_snapshots(
            threshold, snapshots, exclude=exclude,
        )

    def _write_account_config(
        self, account_num: str, email: str, config: str
    ) -> None:
        """Write account config to backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.write_text(config, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(config_file, 0o600)

    # -- public accessors for session mode (claude_swap.session) ---------

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        """Resolve NUM|EMAIL to (account_num, email, organizationUuid).

        Unlike switch_to/remove_account, ambiguity is a hard error rather
        than an interactive prompt: session mode ends in an exec, so callers
        need a deterministic resolution.

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
            ConfigError: email matches multiple accounts.
        """
        self._get_sequence_data_migrated()
        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(account_num)
        if not record:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")
        return (
            account_num,
            record.get("email", ""),
            record.get("organizationUuid", "") or "",
        )

    def read_account_credentials(self, account_num: str, email: str) -> str:
        """Public wrapper for session bootstrap. Empty string when missing."""
        return self._read_account_credentials(account_num, email)

    def write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Public wrapper for session bootstrap.

        Takes NO lock: the caller is expected to hold ``self.lock_file``
        already. Never combine with the locking persist callback in
        list_accounts() — FileLock is not re-entrant across instances in one
        process (see the v0.7.3 deadlock history).
        """
        self._write_account_credentials(account_num, email, credentials)

    def read_account_config(self, account_num: str, email: str) -> str:
        """Public wrapper for session bootstrap. Empty string when missing."""
        return self._read_account_config(account_num, email)

    # -- session profile lifecycle ----------------------------------------

    def _session_dir(self, account_num: str, email: str) -> Path:
        from claude_swap.session import session_dir_for

        return session_dir_for(self.backup_dir, account_num, email)

    def _live_session_pids(self, account_num: str, email: str) -> list[int]:
        """PIDs of Claude instances running against an account's session profile."""
        from claude_swap.session import live_sessions_for

        return [s.pid for s in live_sessions_for(self._session_dir(account_num, email))]

    def _live_default_mode_claude_pids(self) -> list[int]:
        """PIDs of default-mode Claude Code processes that share the active credential store.

        Read from ``~/.claude/sessions/*.json`` (or ``$CLAUDE_CONFIG_DIR``) —
        the same source Claude Code itself maintains. Session-mode profiles
        (``cswap run``) live under their own config dirs and are NOT counted
        here; only default-mode sessions are affected by an active-credential
        swap.

        Used to detect the multi-session OAuth refresh race described in
        Anthropic claude-code#24317: each running Claude Code process loaded
        the refresh token into memory at startup, so when several of them
        race to refresh near-simultaneously after a swap, all but one fail
        with ``invalid_grant`` and trigger an interactive re-login prompt.
        We can't fix the race from outside the CLI; we surface a warning so
        the user (or launchd log reader) understands what they're seeing.
        """
        from claude_swap.process_detection import list_sessions

        return [s.pid for s in list_sessions()]

    def _ensure_no_live_session(self, account_num: str, email: str, action: str) -> None:
        """Refuse a destructive operation while a session-mode claude is live."""
        pids = self._live_session_pids(account_num, email)
        if pids:
            raise SessionError(
                f"Account-{account_num} ({email}) has a live session-mode Claude "
                f"instance (PID {', '.join(map(str, pids))}). "
                f"Exit it first, then retry {action}."
            )

    def _invalidate_session_credentials(self, account_num: str, email: str) -> None:
        """Drop a session profile's credential material, keeping its history.

        The next `cswap run` fails the reuse check and re-bootstraps from
        backup; the bootstrap merges .claude.json, so the profile's own
        projects/history survive. Used when backup credentials change under
        an existing profile (e.g. --import --force).
        """
        from claude_swap.session import STALE_MARKER, delete_macos_keychain_entry

        session_dir = self._session_dir(account_num, email)
        if not session_dir.exists():
            return
        delete_macos_keychain_entry(session_dir)
        (session_dir / ".credentials.json").unlink(missing_ok=True)
        (session_dir / STALE_MARKER).unlink(missing_ok=True)
        self._logger.info(
            f"Invalidated session credentials for account {account_num}"
        )

    def _delete_session_profile(self, account_num: str, email: str) -> None:
        """Remove an account's session profile dir and its keychain entry.

        Keychain first: the hashed service name is derived from the dir path
        and can't be recomputed once the dir is gone.
        """
        from claude_swap.session import delete_macos_keychain_entry

        session_dir = self._session_dir(account_num, email)
        if not session_dir.exists():
            return
        delete_macos_keychain_entry(session_dir)
        shutil.rmtree(session_dir, ignore_errors=True)
        self._logger.info(
            f"Removed session profile for account {account_num} at {session_dir}"
        )

    def _init_sequence_file(self) -> None:
        """Initialize sequence.json if it doesn't exist."""
        if not self.sequence_file.exists():
            init_data = {
                "activeAccountNumber": None,
                "lastUpdated": get_timestamp(),
                "sequence": [],
                "accounts": {},
            }
            self._write_json(self.sequence_file, init_data)

    def _get_sequence_data(self) -> dict | None:
        """Get sequence data."""
        return self._read_json(self.sequence_file)

    def _get_next_account_number(self) -> int:
        """Get next account number."""
        data = self._get_sequence_data()
        if not data or not data.get("accounts"):
            return 1

        account_nums = [int(k) for k in data["accounts"].keys()]
        return max(account_nums, default=0) + 1

    def _get_current_account(self) -> tuple[str, str] | None:
        """Get current account identity (email, organization_uuid) from .claude.json.

        Returns:
            (email, organization_uuid) tuple if found, None otherwise.
            organization_uuid is "" for personal accounts.
        """
        config_path = self._get_claude_config_path()
        if not config_path.exists():
            return None

        data = self._read_json(config_path)
        if not data:
            return None

        oauth = data.get("oauthAccount", {})
        email = oauth.get("emailAddress", "")
        if not email:
            return None

        organization_uuid = oauth.get("organizationUuid", "") or ""
        return (email, organization_uuid)

    def _account_exists(self, email: str, organization_uuid: str) -> bool:
        """Check if account exists by (email, organizationUuid) composite key."""
        data = self._get_sequence_data()
        if not data:
            return False

        for account in data.get("accounts", {}).values():
            if (account.get("email") == email and
                    account.get("organizationUuid", "") == organization_uuid):
                return True
        return False

    def _account_kind(self, account_num: str | None) -> str:
        """Stored kind for a managed slot: ``"api_key"`` or ``"oauth"`` (default).

        Slots added before this field existed have no ``kind`` and read as
        ``"oauth"`` (back-compat).
        """
        if account_num is None:
            return "oauth"
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(str(account_num), {})
        return "api_key" if record.get("kind") == "api_key" else "oauth"

    def _reject_live_api_key_capture(self, creds: str) -> None:
        """Guard for ``add_account``: never capture a live managed key as OAuth.

        ``add_account`` snapshots the *live* active credential under an
        ``oauthAccount`` identity. Now that ``_read_credentials`` can return a
        raw ``sk-ant-api…`` key, a live ``/login`` key could be backed up as a
        kindless account, corrupting the kind-keyed logic. Reject with guidance
        toward the supported path instead.
        """
        if looks_like_api_key(creds):
            raise ValidationError(
                "Active login is an API-key account. Add it with "
                "'cswap --add-token sk-ant-api...' instead of --add-account."
            )

    def _reject_cross_kind_collision(self, email: str, is_api_key: bool) -> None:
        """Reject registering a token whose (email, personal-org) already exists
        as the *other* kind.

        Identity is matched on ``(email, organizationUuid)`` only, so two slots
        sharing an email across kinds could not be told apart at switch time.
        Refuse the collision and point the user at a distinct ``--email``. The
        default ``…@token.local`` labels never collide; this only guards a
        forced ``--email``.
        """
        data = self._get_sequence_data()
        if not data:
            return
        slot = _slot_for_identity(data.get("accounts", {}), email, "")
        if slot is None:
            return
        existing_kind = self._account_kind(slot)
        new_kind = "api_key" if is_api_key else "oauth"
        if existing_kind != new_kind:
            existing_label = "API-key" if existing_kind == "api_key" else "OAuth"
            new_label = "API-key" if is_api_key else "OAuth"
            raise ValidationError(
                f"'{email}' already exists as an {existing_label} account "
                f"(slot {slot}); cannot add it as an {new_label} account. "
                f"Pass a distinct --email."
            )

    @staticmethod
    def _get_display_tag(email: str, org_name: str, org_uuid: str) -> str:
        """Return display tag for an account's org context."""
        return org_name if org_name else "personal"

    def _resolve_account_identifier(self, identifier: str) -> str | None:
        """Resolve account identifier (number or email) to account number.

        Raises:
            ConfigError: if the email matches multiple accounts (ambiguous).
        """
        if identifier.isdigit():
            return identifier

        data = self._get_sequence_data()
        if not data:
            return None

        matches = [
            num for num, account in data.get("accounts", {}).items()
            if account.get("email") == identifier
        ]

        if len(matches) == 0:
            return None
        if len(matches) == 1:
            return matches[0]

        details = ", ".join(
            f"{num} [{data['accounts'][num].get('organizationName') or 'personal'}]"
            for num in matches
        )
        raise ConfigError(
            f"Email '{identifier}' is ambiguous — matches accounts: {details}. "
            f"Use account number instead (e.g., cswap --switch-to 1)."
        )

    def _get_sequence_data_migrated(self) -> dict | None:
        """Get sequence data, ensuring org-field migration has run."""
        data = self._get_sequence_data()
        if not data:
            return data
        needs_migration = any(
            "organizationUuid" not in acc
            for acc in data.get("accounts", {}).values()
        )
        if needs_migration:
            self._migrate_org_fields()
            data = self._get_sequence_data()  # Re-read after migration
        return data

    def _migrate_org_fields(self) -> None:
        """Backfill organizationUuid/Name for accounts added before org support.

        For the currently active account, reads org info from the live config
        (which is authoritative). For inactive accounts, falls back to backup
        configs. Writes updated fields back to sequence.json.
        """
        data = self._get_sequence_data()
        if not data:
            return

        # Read live config for the currently active account
        live_email = ""
        live_org_uuid = ""
        live_org_name = ""
        config_path = self._get_claude_config_path()
        if config_path.exists():
            try:
                config_data = self._read_json(config_path)
                if config_data:
                    oauth = config_data.get("oauthAccount", {})
                    live_email = oauth.get("emailAddress", "")
                    live_org_uuid = oauth.get("organizationUuid", "") or ""
                    live_org_name = oauth.get("organizationName", "") or ""
            except Exception:
                pass

        updated = False
        for num, account in data.get("accounts", {}).items():
            if "organizationUuid" in account:
                continue  # Already migrated

            email = account.get("email", "")

            # For the active account, prefer live config (backup may lack org fields)
            if email == live_email and live_email:
                account["organizationUuid"] = live_org_uuid
                account["organizationName"] = live_org_name
                updated = True
                continue

            # For inactive accounts, fall back to backup config
            config_text = self._read_account_config(num, email)
            if config_text:
                try:
                    config_data = json.loads(config_text)
                    oauth = config_data.get("oauthAccount", {})
                    account["organizationUuid"] = oauth.get("organizationUuid", "") or ""
                    account["organizationName"] = oauth.get("organizationName", "") or ""
                except (json.JSONDecodeError, AttributeError):
                    account["organizationUuid"] = ""
                    account["organizationName"] = ""
            else:
                account["organizationUuid"] = ""
                account["organizationName"] = ""
            updated = True

        if updated:
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)

    def _resolve_target_slot(
        self, email: str, org_uuid: str, slot: int | None
    ) -> tuple[str, tuple[str, str] | None, str | None] | None:
        """Resolve where a new/moved account should land.

        Returns ``(account_num, displace_slot, migrate_from)`` or ``None`` when
        the user declines an overwrite prompt. No destructive work is done here.
        """
        displace_slot: tuple[str, str] | None = None
        migrate_from: str | None = None

        if slot is None:
            return str(self._get_next_account_number()), None, None

        if slot < 1:
            raise ConfigError("Slot number must be >= 1")
        account_num = str(slot)
        data = self._get_sequence_data()

        # Find if the same account already exists in a different slot.
        if self._account_exists(email, org_uuid):
            old_num = next(
                (num for num, acc in data.get("accounts", {}).items()
                 if acc.get("email") == email
                 and acc.get("organizationUuid", "") == org_uuid),
                None,
            )
            if old_num and old_num != account_num:
                migrate_from = old_num

        # Check if the target slot is occupied by a different account.
        if account_num in data.get("accounts", {}):
            existing = data["accounts"][account_num]
            existing_email = existing.get("email", "unknown")
            is_same = (existing_email == email
                       and existing.get("organizationUuid", "") == org_uuid)
            if not is_same:
                existing_tag = self._get_display_tag(
                    existing_email,
                    existing.get("organizationName", ""),
                    existing.get("organizationUuid", ""),
                )
                warning(f"Slot {slot} already occupied")
                print(f"{existing_email} {muted(f'[{existing_tag}]')}")
                try:
                    answer = input(f"Overwrite slot {slot}? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print(f"\n{dimmed('Cancelled')}")
                    return None
                if answer not in ("y", "yes"):
                    print(dimmed("Cancelled"))
                    return None
                displace_slot = (account_num, existing_email)

        return account_num, displace_slot, migrate_from

    def _apply_slot_displacement(
        self,
        displace_slot: tuple[str, str] | None,
        migrate_from: str | None,
        slot: int | None,
    ) -> None:
        """Delete the slot(s) freed by an overwrite/move, in sequence.json."""
        if displace_slot:
            d_num, d_email = displace_slot
            self._delete_account_files(d_num, d_email)
            data = self._get_sequence_data()
            if int(d_num) in data["sequence"]:
                data["sequence"].remove(int(d_num))
            del data["accounts"][d_num]
            self._write_json(self.sequence_file, data)

        if migrate_from:
            data = self._get_sequence_data()
            old_email = data["accounts"][migrate_from].get("email", "")
            self._delete_account_files(migrate_from, old_email)
            if int(migrate_from) in data["sequence"]:
                data["sequence"].remove(int(migrate_from))
            del data["accounts"][migrate_from]
            self._write_json(self.sequence_file, data)
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")

    def _register_account_slot(
        self, account_num: str, metadata: dict, *, set_active: bool
    ) -> None:
        """Record the new slot in sequence.json and persist it."""
        data = self._get_sequence_data()
        data["accounts"][account_num] = metadata
        if int(account_num) not in data["sequence"]:
            data["sequence"].append(int(account_num))
            data["sequence"].sort()
        if set_active:
            data["activeAccountNumber"] = int(account_num)
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)

    def add_account(self, slot: int | None = None) -> None:
        """Add current account to managed accounts.

        Args:
            slot: Specify the slot number to store the account in.
                  When None, auto-assigns the next available number.
                  When specified, prompts for confirmation if the slot
                  is already occupied by a different account.
        """
        self._setup_directories()
        self._init_sequence_file()
        self._migrate_org_fields()

        identity = self._get_current_account()
        if identity is None:
            raise ConfigError("No active Claude account found. Please log in first.")
        current_email, current_org_uuid = identity

        # When no slot specified and account already exists, refresh credentials in place
        if slot is None and self._account_exists(current_email, current_org_uuid):
            seq = self._get_sequence_data()
            account_num = next(
                (num for num, acc in seq.get("accounts", {}).items()
                 if acc.get("email") == current_email and
                 acc.get("organizationUuid", "") == current_org_uuid),
                None,
            )
            matched_org_name = seq["accounts"][account_num].get("organizationName", "") if account_num else ""

            current_creds = self._read_credentials()
            if current_creds is None:
                raise CredentialReadError("Failed to read credentials for current account")
            if not current_creds:
                raise CredentialReadError("No credentials found for current account")
            self._reject_live_api_key_capture(current_creds)

            config_path = self._get_claude_config_path()
            try:
                current_config = config_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise ConfigError("Claude config file not found")
            except PermissionError:
                raise ConfigError("Permission denied reading Claude config")

            current_creds = self._write_verified_live_account_credentials(
                account_num,
                current_email,
                current_creds,
            )
            self._write_account_config(account_num, current_email, current_config)

            seq["activeAccountNumber"] = int(account_num)
            seq["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, seq)

            tag = self._get_display_tag(current_email, matched_org_name, current_org_uuid)
            self._logger.info(f"Updated credentials for account {account_num}: {current_email}")
            print(
                f"{accent('Updated credentials')} for Account {account_num} "
                f"({current_email} {muted(f'[{tag}]')})."
            )
            return

        # Determine slot number and collect confirmation decisions
        # (no destructive operations until new account is verified readable)
        resolved = self._resolve_target_slot(current_email, current_org_uuid, slot)
        if resolved is None:
            return
        account_num, displace_slot, migrate_from = resolved

        # Read new account credentials BEFORE any destructive operations
        current_creds = self._read_credentials()
        if current_creds is None:
            raise CredentialReadError("Failed to read credentials for current account")
        if not current_creds:
            raise CredentialReadError("No credentials found for current account")
        self._reject_live_api_key_capture(current_creds)

        config_path = self._get_claude_config_path()
        try:
            current_config = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigError("Claude config file not found")
        except PermissionError:
            raise ConfigError("Permission denied reading Claude config")

        # Get account UUID and org fields
        config_data = self._read_json(config_path)
        oauth_data = config_data.get("oauthAccount", {})
        account_uuid = oauth_data.get("accountUuid", "")
        organization_uuid = oauth_data.get("organizationUuid", "") or ""
        organization_name = oauth_data.get("organizationName", "") or ""

        # Now safe to perform destructive cleanup (new account data is in memory)
        self._apply_slot_displacement(displace_slot, migrate_from, slot)

        # Store backups
        current_creds = self._write_verified_live_account_credentials(
            account_num,
            current_email,
            current_creds,
        )
        self._write_account_config(account_num, current_email, current_config)

        # Update sequence.json
        self._register_account_slot(
            account_num,
            {
                "email": current_email,
                "uuid": account_uuid,
                "organizationUuid": organization_uuid,
                "organizationName": organization_name,
                "added": get_timestamp(),
            },
            set_active=True,
        )
        tag = self._get_display_tag(current_email, organization_name, organization_uuid)
        self._logger.info(f"Added account {account_num}: {current_email} (org: {organization_uuid or 'personal'})")
        print(f"{accent('Added')} Account {account_num}: {current_email} {muted(f'[{tag}]')}")

    def add_account_from_token(
        self, token: str, email: str | None = None, slot: int | None = None
    ) -> None:
        """Register a raw OAuth setup-token or managed API key as a new account.

        Useful for headless servers or when the token is received from another
        machine, without needing a prior Claude Code login on this machine. The
        token type is auto-detected: an ``sk-ant-api…`` value is a managed API
        key (stored raw, activated on Claude Code's API-key auth axis), anything
        else is treated as an OAuth setup-token. No Anthropic API calls are made.

        Args:
            token: Raw OAuth setup-token or ``sk-ant-api…`` key, or ``"-"`` to
                   read one line from stdin, or ``""`` to prompt via getpass.
            email: Email address to associate with the account. When omitted,
                   defaults to ``setup-token-{slot}@token.local`` (or
                   ``api-key-{slot}@token.local`` for API keys) since these
                   tokens carry no real email metadata.
            slot:  Slot number to use; auto-assigned when ``None``.
        """
        import getpass

        if token == "-":
            token = sys.stdin.readline().rstrip("\n")
        elif not token:
            token = getpass.getpass("Token: ")

        token = token.strip()
        if not token:
            raise ValidationError("Token cannot be empty")

        is_api_key = looks_like_api_key(token)

        if email and not self._validate_email(email):
            raise ValidationError(f"Invalid email format: {email}")

        self._setup_directories()
        self._init_sequence_file()
        self._migrate_org_fields()

        # Synthesize a placeholder email when one isn't provided. These tokens
        # have no real email metadata, so requiring users to invent one is
        # noise; the slot number gives every default account a unique key.
        if not email:
            if slot is None:
                slot = self._get_next_account_number()
            label = "api-key" if is_api_key else "setup-token"
            email = f"{label}-{slot}@token.local"

        # Don't silently overwrite/convert an existing account of the other kind:
        # identity is matched on (email, org) only, so an api-key and an OAuth
        # account sharing an email would be indistinguishable at switch time.
        self._reject_cross_kind_collision(email, is_api_key)

        # Build the credential payload by kind: a managed key is stored raw; an
        # OAuth setup-token is wrapped in Claude Code's credential JSON. The
        # synthesized config is identical for both (no real org metadata).
        if is_api_key:
            credentials = token
        else:
            credentials = json.dumps({
                "claudeAiOauth": {
                    "accessToken": token,
                    "scopes": list(SETUP_TOKEN_SCOPES),
                }
            })
        config = json.dumps({
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })
        kind_label = "API key" if is_api_key else "token"
        source_label = "API key" if is_api_key else "token"

        # If the account already exists (same email, personal), refresh in place.
        if slot is None and self._account_exists(email, ""):
            seq = self._get_sequence_data()
            account_num = next(
                (num for num, acc in seq.get("accounts", {}).items()
                 if acc.get("email") == email
                 and acc.get("organizationUuid", "") == ""),
                None,
            )
            if account_num is None:
                raise ConfigError(
                    f"Existing account metadata for {email} is inconsistent"
                )
            self._write_account_credentials(account_num, email, credentials)
            self._write_account_config(account_num, email, config)
            seq["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, seq)
            self._logger.info(f"Updated {kind_label} for account {account_num}: {email}")
            print(
                f"{accent(f'Updated {kind_label}')} for Account {account_num} "
                f"({email} {muted('[personal]')})."
            )
            return

        resolved = self._resolve_target_slot(email, "", slot)
        if resolved is None:
            return
        account_num, displace_slot, migrate_from = resolved

        self._apply_slot_displacement(displace_slot, migrate_from, slot)

        self._write_account_credentials(account_num, email, credentials)
        self._write_account_config(account_num, email, config)

        record = {
            "email": email,
            "uuid": "",
            "organizationUuid": "",
            "organizationName": "",
            "added": get_timestamp(),
        }
        if is_api_key:
            record["kind"] = "api_key"
        self._register_account_slot(account_num, record, set_active=False)
        self._logger.info(f"Added account {account_num} from {source_label}: {email}")
        print(
            f"{accent('Added')} Account {account_num}: {email} "
            f"{muted('[personal]')} {muted(f'(from {source_label})')}"
        )

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Remove account from managed accounts."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

            # For email identifiers, handle ambiguous matches interactively
            data = self._get_sequence_data()
            matches = [
                num for num, acc in (data or {}).get("accounts", {}).items()
                if acc.get("email") == identifier
            ]
            if len(matches) > 1:
                print(f"Multiple accounts found for '{identifier}':")
                for num in matches:
                    acc = data["accounts"][num]
                    tag = self._get_display_tag(
                        acc.get("email", ""),
                        acc.get("organizationName", ""),
                        acc.get("organizationUuid", ""),
                    )
                    print(f"  {num}: {identifier} {muted(f'[{tag}]')}")
                choice = input("Enter account number to remove: ").strip()
                if not choice.isdigit() or choice not in matches:
                    print(dimmed("Cancelled"))
                    return
                identifier = choice

        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        account_info = data.get("accounts", {}).get(account_num)

        if not account_info:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        email = account_info.get("email")
        active_account = data.get("activeAccountNumber")

        # Check before the confirmation prompt (better UX); the chokepoint in
        # _delete_account_files re-checks as a safety net for all paths.
        self._ensure_no_live_session(account_num, email, "--remove-account")

        if str(active_account) == account_num:
            warning(f"Warning: Account-{account_num} ({email}) is currently active")

        if not assume_yes:
            confirm = input(
                f"Are you sure you want to permanently remove "
                f"Account-{account_num} ({email})? [y/N] "
            )
            if confirm.lower() != "y":
                print(dimmed("Cancelled"))
                return

        # Remove backup files
        self._delete_account_files(account_num, email)

        # Update sequence.json
        del data["accounts"][account_num]
        data["sequence"] = [n for n in data["sequence"] if n != int(account_num)]
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        self._logger.info(f"Removed account {account_num}: {email}")
        print(f"{accent('Removed')} Account-{account_num} ({email})")

    def _collect_accounts_info(
        self, data: dict, active_num: str | None
    ) -> tuple[list[tuple[int, str, str, str, bool, str]], dict[str, list[str]]]:
        """Build per-account rows, syncing the live account and refreshing
        inactive backups. Returns (accounts_info, health_notes)."""
        accounts_info = []
        health_notes: dict[str, list[str]] = {}
        for num in data.get("sequence", []):
            account = data.get("accounts", {}).get(str(num), {})
            email = account.get("email", "unknown")
            org_name = account.get("organizationName", "") or ""
            org_uuid = account.get("organizationUuid", "") or ""
            is_active = str(num) == active_num

            if is_active:
                creds = self._read_credentials() or ""
                if creds:
                    self._sync_live_account_credentials_to_backup(
                        str(num),
                        email,
                        creds,
                    )
            else:
                creds = self._read_account_credentials(str(num), email)
                if creds:
                    creds, refresh_note = self._refresh_inactive_credentials_if_needed(
                        str(num),
                        email,
                        creds,
                    )
                    if refresh_note:
                        health_notes.setdefault(str(num), []).append(refresh_note)

            accounts_info.append((num, email, org_name, org_uuid, is_active, creds))
        return accounts_info, health_notes

    def _fetch_account_usage(
        self, account_info: tuple[int, str, str, str, bool, str]
    ):
        """Fetch live usage for one account row (used by the thread pool)."""
        num, email, _, _, is_active, creds = account_info
        if looks_like_api_key(creds):
            # Managed API-key account: no subscription quota to fetch.
            return "API key (no quota)"
        if not creds or not oauth.extract_access_token(creds):
            return "no credentials"

        def persist(acct_num: str, acct_email: str, new_creds: str) -> None:
            with FileLock(self.lock_file):
                self._write_account_credentials(acct_num, acct_email, new_creds)

        # An account running in session mode is "inactive" here but live
        # in its own config dir, where claude manages the token. Treat it
        # like the active account (no proactive refresh / 401-retry):
        # refreshing the backup copy could rotate the refresh token out
        # from under the live session. Worst case its usage shows as
        # unavailable until the session exits.
        has_live_session = bool(self._live_session_pids(str(num), email))

        result = oauth.fetch_usage_for_account(
            str(num), email, creds,
            is_active=is_active or has_live_session,
            persist_credentials=persist,
        )
        if isinstance(result, oauth.UsageFetchError):
            self._logger.info(
                "Usage fetch unavailable: account=%s email=%s active=%s reason=%s status=%s",
                num,
                email,
                is_active,
                result.reason,
                result.status_code,
            )
        return result

    def _resolve_usages(
        self, accounts_info: list[tuple[int, str, str, str, bool, str]]
    ) -> tuple[list, list]:
        """Return (usages, usage_notes): the fresh cache when trusted, else a
        live fetch that is merged back into the cache."""
        usage_cache_path = self.backup_dir / "cache" / "usage.json"
        cached_data, file_ts = read_cache_with_timestamp(usage_cache_path)
        previous_cached = cached_data if cached_data is not None else {}
        account_keys = {str(info[0]) for info in accounts_info}
        if self._usage_cache_fresh(
            previous_cached, account_keys, file_timestamp=file_ts,
        ):
            cached_data = previous_cached
            usages = [
                _usage_from_cache(cached_data.get(str(info[0])))
                for info in accounts_info
            ]
            usage_notes = [None for _ in accounts_info]
        else:
            with ThreadPoolExecutor() as executor:
                usages = list(executor.map(self._fetch_account_usage, accounts_info))
            merged_usages = {}
            usage_notes = []
            for info, usage in zip(accounts_info, usages):
                key = str(info[0])
                previous = previous_cached.get(key)
                _, note = _merge_usage_with_previous(usage, previous)
                _persist_usage_cache_entry(merged_usages, key, usage, previous)
                usage_notes.append(note)
            usages = [_usage_from_cache(merged_usages[str(info[0])]) for info in accounts_info]
            write_cache(usage_cache_path, merged_usages)
        return usages, usage_notes

    def _print_account_rows(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str]],
        usages: list,
        usage_notes: list,
        health_notes: dict[str, list[str]],
        *,
        show_health: bool,
        show_token_status: bool,
    ) -> None:
        """Render the per-account usage/health/token block."""
        print(bolded("Accounts:"))
        for i, ((num, email, org_name, org_uuid, is_active, _), usage) in enumerate(zip(accounts_info, usages)):
            tag = self._get_display_tag(email, org_name, org_uuid)
            if is_active:
                marker = f" {bold_accent('(active)')}"
                print(f"  {num}: {email} {muted(f'[{tag}]')}{marker}")
            else:
                print(f"  {num}: {email} {muted(f'[{tag}]')}")
            if isinstance(usage, str):
                print(f"     {dimmed(usage)}")
                health_notes.setdefault(str(num), []).append(usage)
            elif isinstance(usage, oauth.UsageFetchError):
                print(f"     {dimmed(oauth.describe_usage_error(usage))}")
                health_notes.setdefault(str(num), []).append(usage.reason)
            elif usage is None:
                print(f"     {dimmed('usage unavailable')}")
                health_notes.setdefault(str(num), []).append("usage unavailable")
            else:
                lines = _format_usage_lines(usage)
                for j, line in enumerate(lines):
                    connector = "└" if j == len(lines) - 1 else "├"
                    print(f"     {dimmed(connector)} {muted(line)}")
            if show_health:
                notes = health_notes.get(str(num), [])
                health = "ok" if not notes else ", ".join(notes)
                print(f"     {dimmed('•')} {muted(f'health: {health}')}")
            note = usage_notes[i]
            if isinstance(note, oauth.UsageFetchError):
                print(f"     {dimmed('•')} {muted(f'cached; live fetch {oauth.describe_usage_error(note)}')}")

            if show_token_status:
                token_status = oauth.build_token_status(accounts_info[i][5])
                if token_status:
                    print(f"     {dimmed('•')} {muted(token_status)}")
            if i < len(accounts_info) - 1:
                print()

    def _print_running_instances(self) -> None:
        """Render the grouped 'Running instances' block (best-effort)."""
        try:
            sessions, ide_instances = get_running_instances()

            if sessions or ide_instances:
                # Group by (label, folder) to avoid repetitive lines
                groups: dict[tuple[str, str], dict[str, int]] = {}
                for session in sessions:
                    label = entrypoint_label(session.entrypoint)
                    cwd = abbreviate_path(session.cwd)
                    key = (label, cwd)
                    counts = groups.setdefault(key, {"sessions": 0, "ide": 0})
                    counts["sessions"] += 1
                for ide in ide_instances:
                    name = ide_short_name(ide.ide_name)
                    for folder in ide.workspace_folders:
                        key = (name, abbreviate_path(folder))
                        counts = groups.setdefault(key, {"sessions": 0, "ide": 0})
                        counts["ide"] += 1

                print()
                print(bolded("Running instances:"))
                for (label, cwd), counts in groups.items():
                    parts = []
                    s = counts["sessions"]
                    if s:
                        parts.append(f"{s} session{'s' if s > 1 else ''}")
                    if counts["ide"]:
                        parts.append("IDE")
                    print(f"  {dimmed('●')} {muted(label)}   {muted(cwd)}  {dimmed(f'({", ".join(parts)})')}")
        except Exception:
            self._logger.debug("Failed to detect running instances", exc_info=True)

    def _build_accounts_info(self) -> list[tuple[int, str, str, str, bool, str]]:
            """Build per-account (num, email, org_name, org_uuid, is_active, creds).

            Shared by list_accounts and the usage-aware switch helpers so the active
            slot is detected and credentials are read in exactly one place. The
            active account's credentials come from Claude Code's live store; every
            other slot reads its backup copy.
            """
            data = self._get_sequence_data_migrated() or {}
            current_identity = self._get_current_account()

            # Find active account number by (email, organizationUuid) composite key
            active_num = None
            if current_identity is not None:
                current_email, current_org_uuid = current_identity
                active_num = self._find_account_slot(data, current_email, current_org_uuid)

            accounts_info: list[tuple[int, str, str, str, bool, str]] = []
            for num in data.get("sequence", []):
                account = data.get("accounts", {}).get(str(num), {})
                email = account.get("email", "unknown")
                org_name = account.get("organizationName", "") or ""
                org_uuid = account.get("organizationUuid", "") or ""
                is_active = str(num) == active_num

                if is_active:
                    creds = self._read_credentials() or ""
                else:
                    creds = self._read_account_credentials(str(num), email)

                accounts_info.append((num, email, org_name, org_uuid, is_active, creds))
            return accounts_info

    def _active_cc_running(self) -> bool:
        """Whether any default-profile Claude Code instance is running.

        Fails closed: if instance detection raises, assume an owner may exist so we
        never refresh the live credential out from under a running Claude Code.
        """
        try:
            sessions, ides = get_running_instances()
            return bool(sessions or ides)
        except Exception:
            self._logger.debug("Failed to detect running Claude instances", exc_info=True)
            return True

    def _fetch_active_usage(
        self, account_num: str, email: str, creds: str
    ) -> dict | str | None:
            """Usage for the active/default account, refreshing its token only when safe.

            The active credential is the one Claude Code concurrently owns, so cswap
            normally leaves it alone (issue #62). But when no *owner* is detected —
            neither a default-profile Claude Code (``_active_cc_running``) nor a live
            ``cswap run`` session for this same account (``_live_session_pids``) — there
            is no concurrent refresher, so an expired token can be refreshed and written
            back to the **active** store (Claude Code reads the rotated credential on its
            next start).

            When an owner *is* present and the token is expired, returns the
            ``USAGE_TOKEN_EXPIRED`` sentinel so the UI shows an intentional line rather
            than a bare "usage unavailable".
            """
            oauth_data = oauth.extract_oauth_data(creds)
            if not oauth_data or not oauth_data.get("accessToken"):
                return USAGE_NO_CREDENTIALS

            owned = self._active_cc_running() or bool(
                self._live_session_pids(account_num, email)
            )
            if owned:
                usage = oauth.fetch_usage_for_account(
                    account_num, email, creds, is_active=True,
                )
                if usage is None and oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
                    return USAGE_TOKEN_EXPIRED
                return usage

            # No owner detected → safe to refresh the active token. Reuse the inactive
            # refresh machinery (proactive refresh + 401 retry), persisting the rotated
            # credential to BOTH the active store and the backup. Do NOT hold the lock
            # across the network refresh: FileLock is non-reentrant and persist_active
            # re-acquires it (regressing commit a07c767 would deadlock and silently drop
            # the refreshed token).
            original_refresh = oauth_data.get("refreshToken")
            persist_skipped = False

            def persist_active(num: str, acct_email: str, new_creds: str) -> None:
                nonlocal persist_skipped
                with FileLock(self.lock_file):
                    live = self._read_credentials() or ""
                    live_oauth = oauth.extract_oauth_data(live) if live else None
                    live_refresh = live_oauth.get("refreshToken") if live_oauth else None
                    # Re-check owners + refresh-token lineage under the lock. If a Claude
                    # Code / session appeared, or an external write (e.g. a user /login)
                    # replaced the credential since we read it, skip rather than clobber a
                    # live process's newer credential. Best effort, not perfectly atomic.
                    if (
                        self._active_cc_running()
                        or self._live_session_pids(num, acct_email)
                        or live_refresh != original_refresh
                    ):
                        persist_skipped = True
                        self._logger.warning(
                            "Active-account refresh for %s (%s): owner appeared or refresh "
                            "token changed mid-refresh; discarding rotated credential.",
                            num, acct_email,
                        )
                        return
                    # A write failure leaves the live store holding the now-consumed
                    # original refresh token, so mark the persist as skipped (never show
                    # usage for it) and re-raise — oauth._persist swallows the exception
                    # but logs its "failed to persist" warning first.
                    try:
                        self._write_credentials(new_creds)  # active store — Claude Code reads this
                        self._write_account_credentials(num, acct_email, new_creds)  # backup in sync
                    except Exception:
                        persist_skipped = True
                        raise

            usage = oauth.fetch_usage_for_account(
                account_num, email, creds,
                is_active=False, persist_credentials=persist_active,
            )
            # If we refreshed but discarded the rotated credential, never show usage for
            # a credential we didn't keep — surface the expired state and let Claude Code
            # settle it.
            if persist_skipped:
                return USAGE_TOKEN_EXPIRED
            if usage is None and oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
                return USAGE_TOKEN_EXPIRED
            return usage

    def _collect_usage(
            self, accounts_info: list[tuple[int, str, str, str, bool, str]]
        ) -> list[dict | str | None]:
            """Fetch usage for each account (cache-first), returning one entry per row.

            Each entry is a usage dict, the string ``"no credentials"``, or ``None``
            when the API call failed. Results are cached under
            ``<backup_dir>/cache/usage.json`` for ``_USAGE_CACHE_TTL`` seconds and
            reused only when the cache covers exactly the same set of accounts.
            """
            def fetch(
                account_info: tuple[int, str, str, str, bool, str]
            ) -> dict | str | None:
                num, email, _, _, is_active, creds = account_info
                if looks_like_api_key(creds):
                    # Managed API-key account: no subscription quota to fetch.
                    return USAGE_API_KEY
                if not creds or not oauth.extract_access_token(creds):
                    return USAGE_NO_CREDENTIALS

                # The active/default account owns the live credential — route it through
                # the owner-aware path that refreshes only when no Claude Code/session is
                # running and writes the rotated credential back to the active store.
                if is_active:
                    return self._fetch_active_usage(str(num), email, creds)

                def persist(acct_num: str, acct_email: str, new_creds: str) -> None:
                    with FileLock(self.lock_file):
                        self._write_account_credentials(acct_num, acct_email, new_creds)

                # An account running in session mode is "inactive" here but live
                # in its own config dir, where claude manages the token. Treat it
                # like the active account (no proactive refresh / 401-retry):
                # refreshing the backup copy could rotate the refresh token out
                # from under the live session. Worst case its usage shows as
                # unavailable until the session exits.
                has_live_session = bool(self._live_session_pids(str(num), email))

                return oauth.fetch_usage_for_account(
                    str(num), email, creds,
                    is_active=has_live_session,
                    persist_credentials=persist,
                )

            usage_cache_path = self.backup_dir / "cache" / "usage.json"
            cached = read_cache(usage_cache_path, _USAGE_CACHE_TTL)
            account_keys = {str(info[0]) for info in accounts_info}
            if cached is not MISSING and isinstance(cached, dict) and cached.keys() == account_keys:
                return [cached.get(str(info[0])) for info in accounts_info]

            with ThreadPoolExecutor() as executor:
                usages = list(executor.map(fetch, accounts_info))
            write_cache(usage_cache_path, {
                str(info[0]): usage
                for info, usage in zip(accounts_info, usages)
            })
            return usages

    def _usage_by_account(self) -> dict[str, dict | str | None]:
            """Map account number → usage entry (cache-first) for managed accounts."""
            accounts_info = self._build_accounts_info()
            usages = self._collect_usage(accounts_info)
            return {
                str(info[0]): usage for info, usage in zip(accounts_info, usages)
            }

    def _select_best_switchable(
            self, current_num: str | None
        ) -> tuple[str | None, str]:
            """Decide the ``best`` strategy target relative to the current account.

            Compares the rate-limit headroom of every *other* switchable account
            against the current one and only recommends a switch it can *prove*
            lands on strictly more headroom — never onto an account worse than (or
            merely unverifiable against) where the user already is. When a switch
            can't be proven beneficial, it stays put; bare ``cswap --switch``
            remains the way to force a plain rotation. Returns ``(target, note)``:

            - ``(num, "")`` — switch to ``num`` (strictly more headroom than current)
            - ``(None, "current-unavailable")`` — current account's usage is unknown,
              so no comparison is possible → stay
            - ``(None, "no-comparison")`` — no other account has known usage → stay
            - ``(None, "incomplete-comparison")`` — current is best among the
              accounts we can measure, but some candidate's usage is unknown, so we
              can't claim it's the best or that everything is exhausted → stay
            - ``(None, "stay")`` — current account provably has the most headroom
            - ``(None, "exhausted")`` — current is the best and every account is at
              its limit (switching would not help) → stay
            - ``(None, "none")`` — no other switchable account exists

            Ties (including current-vs-other) resolve in favour of staying put.
            Never raises on network failure.
            """
            data = self._get_sequence_data() or {}
            others = [
                str(n) for n in data.get("sequence", [])
                if str(n) != str(current_num) and self._account_is_switchable(str(n))
            ]
            if not others:
                return None, "none"

            usage = self._usage_by_account()
            current_headroom = oauth.account_headroom(usage.get(str(current_num)))
            if current_headroom is None:
                # Can't measure where the user is → can't prove any target is
                # better. Stay rather than risk moving onto a worse account.
                return None, "current-unavailable"

            scored = [(oauth.account_headroom(usage.get(num)), num) for num in others]
            known = [(h, num) for h, num in scored if h is not None]
            if not known:
                return None, "no-comparison"

            # max() keeps the first maximal element; `known` preserves rotation
            # order, so ties resolve to the earliest slot.
            best_headroom, best_num = max(known, key=lambda t: t[0])
            if best_headroom > current_headroom:
                return best_num, ""

            # Current is at least as good as every account we can measure. Stay —
            # but only claim "all exhausted" when every candidate's usage is known.
            if any(h is None for h, _ in scored):
                return None, "incomplete-comparison"
            if current_headroom <= 0:
                return None, "exhausted"
            return None, "stay"

    def _build_list_payload(
            self,
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

    def _active_account_usage(
            self, account_num: str, current_email: str
        ) -> dict | str | None:
            """Usage for the active account as a ``_collect_usage``-style entry.

            Returns ``USAGE_NO_CREDENTIALS`` when the live store has no usable token, a
            usage dict on success, ``USAGE_TOKEN_EXPIRED`` when the token is expired and
            Claude Code owns it, or ``None`` when the fetch fails. Reuses the same
            ``cache/usage.json`` list_accounts writes — cache-first — and delegates the
            owner-aware refresh decision to ``_fetch_active_usage``.
            """
            creds = self._read_credentials() or ""
            if looks_like_api_key(creds):
                # Managed API-key account: no subscription quota to fetch.
                return USAGE_API_KEY
            if not creds or not oauth.extract_access_token(creds):
                return USAGE_NO_CREDENTIALS
            usage_cache_path = self.backup_dir / "cache" / "usage.json"
            cached = read_cache(usage_cache_path, _USAGE_CACHE_TTL)
            if (cached is not MISSING and isinstance(cached, dict)
                    and account_num in cached):
                return cached[account_num]
            usage = self._fetch_active_usage(account_num, current_email, creds)
            existing = cached if (cached is not MISSING and isinstance(cached, dict)) else {}
            existing[account_num] = usage
            write_cache(usage_cache_path, existing)
            return usage

    def _build_status_payload(self) -> dict:
            """Build the ``--status --json`` payload (no active / unmanaged / managed)."""
            identity = self._get_current_account()
            if identity is None:
                return {"schemaVersion": SCHEMA_VERSION, "active": None}
            current_email, current_org_uuid = identity

            data = self._get_sequence_data_migrated()
            if not data:
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "active": {"email": current_email, "managed": False},
                }

            account_num = self._find_account_slot(data, current_email, current_org_uuid)
            if not account_num:
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "active": {"email": current_email, "managed": False},
                }

            acct = data["accounts"][account_num]
            org_name = acct.get("organizationName", "") or ""
            org_uuid = acct.get("organizationUuid", "") or ""
            status, usage = usage_fields(
                self._active_account_usage(account_num, current_email)
            )
            return {
                "schemaVersion": SCHEMA_VERSION,
                "active": {
                    "number": int(account_num),
                    "email": current_email,
                    "organizationName": org_name,
                    "organizationUuid": org_uuid,
                    "isOrganization": bool(org_uuid),
                    "managed": True,
                    "usageStatus": status,
                    "usage": usage,
                },
                "totalManagedAccounts": len(data.get("accounts", {})),
            }

    def _switch_result_from_op(
            self, op: dict, strategy: str, extra_warnings: list[str] | None = None
        ) -> dict:
            """Build a switch result from a ``_perform_switch`` return value.

            ``switched`` is derived from whether the live identity actually changed
            (``from != to``) — covering recorded/live drift in plain rotation, not just
            ``switch_to`` onto the already-active account.
            """
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

    def _switch_noop(
            self,
            *,
            strategy: str,
            reason: str,
            message: str,
            from_ref: dict | None = None,
            to_ref: dict | None = None,
            warnings: list[str] | None = None,
        ) -> dict:
            """Build a no-op switch result (``switched: false``).

            For a no-op the user neither left nor arrived anywhere — ``from`` and
            ``to`` are both the current account. Callers pass ``to_ref`` (where they
            stayed); ``from_ref`` defaults to it so every ``switched: false`` payload
            reports ``from == to``.
            """
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

    def _print_switch_followup(self) -> None:
            """Print the note after a successful switch, keyed to where the active
            credential write actually landed.

            A restart is never required: Claude Code clears its cached OAuth token when
            ``.credentials.json`` changes (file storage — effective on the next message)
            or when the macOS Keychain cache TTL (~30s) expires. Both lines are dim
            hints, not warnings; the Keychain line adds that a restart skips the wait.
            The file line also covers macOS when the Keychain was unavailable and the
            switch fell back to the file.
            """
            backend = self._last_active_credentials_backend
            if backend is None:
                # No write happened this run; fall back to the routing hint.
                backend = "keychain" if self._use_keychain() else "file"
            if backend == "keychain":
                print(dimmed(
                    "Restart Claude Code to apply immediately — otherwise the "
                    "session can take up to ~30 seconds to pick up the new account."
                ))
            else:
                print(dimmed("New account is active on your next message — no restart needed."))

    def _switch_cli(
            self, strategy: str | None = None, json_output: bool = False
        ) -> dict | None:
            """Switch to next account in sequence.

            Args:
                strategy: Usage-aware target selection. ``"best"`` jumps to the
                      switchable account with the most remaining 5h/7d quota instead
                      of advancing the rotation; ``"next-available"`` rotates to the
                      next account, skipping any currently at its 5h/7d limit. ``None``
                      (the default) performs a plain rotation.

            ``"best"`` only switches when it can prove another account has more
            remaining quota; if usage can't be fetched or no candidate is provably
            better, it stays put (run a plain ``cswap --switch`` to rotate anyway).
            ``"next-available"`` rotates and skips accounts at their limit, falling
            back to plain rotation when usage is unavailable. Both apply only to the
            normal path (a live Claude login present); the fresh-machine path (no
            live login, e.g. right after --import) ignores them.
            """
            strategy_label = strategy if strategy in ("best", "next-available") else "rotation"
            warnings: list[str] = []

            if not self.sequence_file.exists():
                raise ConfigError("No accounts are managed yet")

            identity = self._get_current_account()

            # Ensure org fields are migrated before checking composite key
            self._get_sequence_data_migrated()

            # Fresh-machine path: no live Claude session, but we have managed accounts
            # (e.g. right after cswap --import). Activate the recorded
            # activeAccountNumber, or fall back to the first slot in sequence.
            # With no live state to capture, the target must have valid backups —
            # walk the sequence if the preferred target is broken.
            if identity is None:
                data = self._get_sequence_data() or {}
                sequence = data.get("sequence", [])
                preferred = data.get("activeAccountNumber")
                if not preferred and sequence:
                    preferred = sequence[0]
                if not preferred:
                    raise ConfigError("No accounts are managed yet")

                target = str(preferred)
                if not self._account_is_switchable(target):
                    if json_output:
                        warnings.append(
                            f"Skipped Account-{target} (no stored credentials/config)"
                        )
                    else:
                        print(
                            f"{accent('Skipping')} Account-{target} "
                            f"(no stored credentials/config, re-add with "
                            f"cswap --add-account --slot {target})"
                        )
                    fallback = next(
                        (str(num) for num in sequence
                         if str(num) != target and self._account_is_switchable(str(num))),
                        None,
                    )
                    if not fallback:
                        raise ConfigError(
                            "No managed accounts have valid stored credentials/config. "
                            "Re-add a slot with: cswap --add-account --slot <number>"
                        )
                    target = fallback
                op = self._perform_switch(target, emit_output=not json_output)
                return (
                    self._switch_result_from_op(op, strategy_label, warnings)
                    if json_output else None
                )

            current_email, current_org_uuid = identity

            # Check if current account is managed
            if not self._account_exists(current_email, current_org_uuid):
                # In JSON mode, don't silently auto-add (a surprising side effect in
                # automation) — report it as a structured no-op instead.
                if json_output:
                    ref = account_ref(None, current_email)
                    return self._switch_noop(
                        strategy=strategy_label,
                        reason="unmanaged-account",
                        from_ref=ref,
                        to_ref=ref,
                        message="Active account is not managed; run cswap --add-account",
                    )
                print(f"{accent('Notice:')} Active account '{current_email}' was not managed.")
                self.add_account()
                data = self._get_sequence_data()
                account_num = data.get("activeAccountNumber")
                print(f"It has been automatically added as Account-{account_num}.")
                print(dimmed("Please run the switch command again to switch to the next account."))
                return None

            data = self._get_sequence_data()
            sequence = data.get("sequence", [])

            if len(sequence) < 2:
                if json_output:
                    num = self._find_account_slot(data, current_email, current_org_uuid)
                    return self._switch_noop(
                        strategy=strategy_label,
                        reason="only-one-account",
                        to_ref=account_ref(int(num), current_email) if num else None,
                        message="Only one account is managed. Add more accounts to switch between.",
                    )
                print(dimmed("Only one account is managed. Add more accounts to switch between."))
                return None

            active_account = data.get("activeAccountNumber")
            # Where the user actually is right now (live identity), falling back to
            # the recorded active slot. Used so usage-aware switching never moves
            # them onto an account worse than their current one.
            current_num = self._find_account_slot(data, current_email, current_org_uuid)
            if current_num is None:
                current_num = str(active_account) if active_account is not None else None

            current_ref = (
                account_ref(int(current_num), current_email) if current_num else None
            )

            # Usage-aware "jump to most headroom". Only switches when another
            # account is provably better; otherwise stays put (never moves onto a
            # worse or unverifiable account). Bare `cswap --switch` rotates anyway.
            if strategy == "best":
                target, note = self._select_best_switchable(current_num)
                if target is not None:
                    op = self._perform_switch(target, emit_output=not json_output)
                    return (
                        self._switch_result_from_op(op, strategy_label, warnings)
                        if json_output else None
                    )
                if note == "current-unavailable":
                    if json_output:
                        return self._switch_noop(
                            strategy=strategy_label, reason="usage-unavailable",
                            to_ref=current_ref,
                            message=(
                                f"Current account usage is unavailable — staying on "
                                f"Account-{current_num}."
                            ),
                        )
                    print(dimmed(
                        f"Current account usage is unavailable — staying on "
                        f"Account-{current_num}. Run cswap --switch to rotate."
                    ))
                    return None
                if note == "no-comparison":
                    if json_output:
                        return self._switch_noop(
                            strategy=strategy_label, reason="usage-unavailable",
                            to_ref=current_ref,
                            message=(
                                f"No other account has usage data to compare — staying "
                                f"on Account-{current_num}."
                            ),
                        )
                    print(dimmed(
                        f"No other account has usage data to compare — staying on "
                        f"Account-{current_num}. Run cswap --switch to rotate."
                    ))
                    return None
                if note == "incomplete-comparison":
                    if json_output:
                        return self._switch_noop(
                            strategy=strategy_label, reason="usage-unavailable",
                            to_ref=current_ref,
                            message=(
                                f"No account with known usage has more remaining quota; "
                                f"some usage is unavailable — staying on Account-{current_num}."
                            ),
                        )
                    print(dimmed(
                        f"No account with known usage has more remaining quota; some "
                        f"usage is unavailable — staying on Account-{current_num}."
                    ))
                    return None
                if note == "stay":
                    if json_output:
                        return self._switch_noop(
                            strategy=strategy_label, reason="already-best",
                            to_ref=current_ref,
                            message=(
                                f"Already on the account with the most remaining quota "
                                f"(Account-{current_num})."
                            ),
                        )
                    print(
                        f"{accent('Already on the account with the most remaining quota')} "
                        f"(Account-{current_num})."
                    )
                    return None
                if note == "exhausted":
                    if json_output:
                        return self._switch_noop(
                            strategy=strategy_label, reason="candidates-exhausted",
                            to_ref=current_ref,
                            message=(
                                f"All accounts are at their 5h/7d limit — staying on "
                                f"Account-{current_num}."
                            ),
                        )
                    warning(
                        f"All accounts are at their 5h/7d limit — staying on "
                        f"Account-{current_num}."
                    )
                    return None
                # note == "none": fall through; rotation reports the lack of targets.

            # Find current index and get next, skipping broken candidates.
            # The active slot is never checked here — _perform_switch captures
            # live state into a fresh backup before swapping, so the active
            # slot's stored backup may be stale or absent without blocking us.
            #
            # Usage-aware rotation anchors on the live account (current_num) so it
            # never lands a no-op on the slot you're already on when the live login
            # has drifted from the recorded activeAccountNumber. Plain rotation keeps
            # anchoring on active_account for byte-for-byte unchanged behavior.
            anchor = current_num if strategy == "next-available" else active_account
            try:
                current_index = sequence.index(int(anchor))
            except (TypeError, ValueError):
                try:
                    current_index = sequence.index(active_account)
                except (TypeError, ValueError):
                    current_index = 0

            # Only fetch usage when needed; an empty map means the headroom check
            # below is always None (skipped), preserving the non-usage-aware path.
            usage = self._usage_by_account() if strategy == "next-available" else {}

            next_account: str | None = None
            skipped_exhausted: list[str] = []
            for offset in range(1, len(sequence)):
                candidate = str(sequence[(current_index + offset) % len(sequence)])
                if not self._account_is_switchable(candidate):
                    if json_output:
                        warnings.append(
                            f"Skipped Account-{candidate} (no stored credentials/config)"
                        )
                    else:
                        print(
                            f"{accent('Skipping')} Account-{candidate} "
                            f"(no stored credentials/config, re-add with "
                            f"cswap --add-account --slot {candidate})"
                        )
                    continue
                if strategy == "next-available":
                    headroom = oauth.account_headroom(usage.get(candidate))
                    if headroom is not None and headroom <= 0:
                        skipped_exhausted.append(candidate)
                        if json_output:
                            warnings.append(
                                f"Skipped Account-{candidate} (at 5h/7d limit)"
                            )
                        else:
                            print(f"{accent('Skipping')} Account-{candidate} (at 5h/7d limit)")
                        continue
                next_account = candidate
                break

            # Every rotation target is at its limit. Switching onto an exhausted
            # account would not help, so stay on the current one instead.
            if next_account is None and skipped_exhausted:
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="candidates-exhausted",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"All other accounts are at their 5h/7d limit — staying on "
                            f"Account-{current_num}."
                        ),
                    )
                warning(
                    f"All other accounts are at their 5h/7d limit — staying on "
                    f"Account-{current_num}."
                )
                return None

            if next_account is None:
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="no-valid-target",
                        to_ref=current_ref, warnings=warnings,
                        message="No other accounts have valid stored credentials/config.",
                    )
                print(dimmed(
                    "No other accounts have valid stored credentials/config.\n"
                    "Re-add a skipped slot with: cswap --add-account --slot <number>"
                ))
                return None

            op = self._perform_switch(next_account, emit_output=not json_output)
            return (
                self._switch_result_from_op(op, strategy_label, warnings)
                if json_output else None
            )

    def switch_to(
            self, identifier: str, json_output: bool = False
        ) -> dict | None:
            """Switch to specific account."""
            if not self.sequence_file.exists():
                raise ConfigError("No accounts are managed yet")

            # Ensure org fields are migrated before resolving accounts
            self._get_sequence_data_migrated()

            # Resolve identifier
            if not identifier.isdigit():
                if not self._validate_email(identifier):
                    raise ValidationError(f"Invalid email format: {identifier}")

                # For email identifiers, handle ambiguous matches interactively —
                # except in JSON mode, where we never prompt. There we fall through
                # to _resolve_account_identifier, which raises a ConfigError listing
                # the matching slots (+ org labels) → structured error envelope.
                if not json_output:
                    data = self._get_sequence_data()
                    matches = [
                        num for num, acc in (data or {}).get("accounts", {}).items()
                        if acc.get("email") == identifier
                    ]
                    if len(matches) > 1:
                        print(f"Multiple accounts found for '{identifier}':")
                        for num in matches:
                            acc = data["accounts"][num]
                            tag = self._get_display_tag(
                                acc.get("email", ""),
                                acc.get("organizationName", ""),
                                acc.get("organizationUuid", ""),
                            )
                            print(f"  {num}: {identifier} {muted(f'[{tag}]')}")
                        choice = input("Enter account number to switch to: ").strip()
                        if not choice.isdigit() or choice not in matches:
                            print(dimmed("Cancelled"))
                            return None
                        identifier = choice

            target_account = self._resolve_account_identifier(identifier)
            if not target_account:
                raise AccountNotFoundError(
                    f"No account found with identifier: {identifier}"
                )

            data = self._get_sequence_data()
            if target_account not in data.get("accounts", {}):
                raise AccountNotFoundError(f"Account-{target_account} does not exist")

            # JSON mode: short-circuit a no-op before mutating. Re-activating the
            # account you're already on would otherwise re-write credentials, take
            # the lock, and (on macOS) touch the Keychain — wasteful for a scripted
            # idempotent "ensure active = N". Human mode keeps its existing behavior.
            if json_output and data:
                identity = self._get_current_account()
                if identity is not None:
                    cur_slot = self._find_account_slot(data, identity[0], identity[1])
                    if cur_slot == target_account:
                        email = (
                            data.get("accounts", {}).get(target_account, {}).get("email", "")
                        )
                        ref = account_ref(int(target_account), email)
                        return self._switch_noop(
                            strategy="direct",
                            reason="already-active",
                            from_ref=ref,
                            to_ref=ref,
                            message=f"Already on Account-{target_account} ({email})",
                        )

            op = self._perform_switch(target_account, emit_output=not json_output)
            return self._switch_result_from_op(op, "direct") if json_output else None

    def list_accounts(
        self,
        show_token_status: bool = False,
        show_health: bool = False,
        json_output: bool = False,
    ) -> dict | None:
        """List all managed accounts."""
        if json_output:
            if not self.sequence_file.exists():
                return {"schemaVersion": SCHEMA_VERSION, "activeAccountNumber": None, "accounts": []}
            data = self._get_sequence_data_migrated()
            current_identity = self._get_current_account()
            active_num = None
            if current_identity is not None:
                ce, ou = current_identity
                active_num = _slot_for_identity(data.get("accounts", {}), ce, ou)
            accounts_info, _ = self._collect_accounts_info(data, active_num)
            usages, _ = self._resolve_usages(accounts_info)
            mapped = [(num, email, org_name, org_uuid, is_active, creds) for (num, email, org_name, org_uuid, is_active, creds, *_) in [(*t[:6],) for t in [ai + (None,) for ai in accounts_info]]]
            # accounts_info tuples: (num, email, org_name, org_uuid, is_active, creds)
            mapped = [(t[0], t[1], t[2], t[3], t[4], t[5]) for t in accounts_info]
            return self._build_list_payload(mapped, usages)
        if not self.sequence_file.exists():
            print(dimmed("No accounts are managed yet."))
            self._first_run_setup()
            return

        data = self._get_sequence_data_migrated()
        current_identity = self._get_current_account()
        active_num = None
        if current_identity is not None:
            current_email, current_org_uuid = current_identity
            active_num = _slot_for_identity(
                data.get("accounts", {}), current_email, current_org_uuid,
            )

        accounts_info, health_notes = self._collect_accounts_info(data, active_num)
        usages, usage_notes = self._resolve_usages(accounts_info)
        self._print_account_rows(
            accounts_info,
            usages,
            usage_notes,
            health_notes,
            show_health=show_health,
            show_token_status=show_token_status,
        )
        self._print_running_instances()

    def status(self, json_output: bool = False) -> dict | None:
        """Display current account status."""
        if json_output:
            return self._build_status_payload()
        identity = self._get_current_account()
        if identity is None:
            print(f"{bolded('Status:')} {dimmed('No active Claude account')}")
            return
        current_email, current_org_uuid = identity

        data = self._get_sequence_data_migrated()
        if not data:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
            return

        account_num = None
        org_name = ""
        for num, info in data.get("accounts", {}).items():
            if (info.get("email") == current_email and
                    info.get("organizationUuid", "") == current_org_uuid):
                account_num = num
                org_name = info.get("organizationName", "") or ""
                break

        if account_num:
            tag = self._get_display_tag(current_email, org_name, current_org_uuid)
            total = len(data.get("accounts", {}))
            print(
                f"{bolded('Status:')} {accent(f'Account-{account_num}')} "
                f"({current_email} {muted(f'[{tag}]')})"
            )
            print(f"  {dimmed(f'Total managed accounts: {total}')}")
            creds = self._read_credentials() or ""
            if creds:
                self._sync_live_account_credentials_to_backup(
                    account_num,
                    current_email,
                    creds,
                )
            if creds and oauth.extract_access_token(creds):
                # Reuse the cache list_accounts writes (cache-first). On miss,
                # fetch with is_active=True (Claude Code owns active credentials,
                # cswap must not refresh them) and merge into existing entries so
                # other accounts' rows survive.
                usage_cache_path = self.backup_dir / "cache" / "usage.json"
                cached, file_ts = read_cache_with_timestamp(usage_cache_path)
                previous_cached = cached if cached is not None else {}
                cached_usage = (
                    _usage_from_cache(cached[account_num])
                    if cached is not None and account_num in cached
                    else None
                )
                if (
                    isinstance(cached_usage, dict)
                    and _usage_slot_trusted(cached_usage, time.time(), file_ts)
                ):
                    usage = cached_usage
                    usage_note = None
                else:
                    fetched_usage = oauth.fetch_usage_for_account(
                        account_num, current_email, creds,
                        is_active=True,
                    )
                    if isinstance(fetched_usage, oauth.UsageFetchError):
                        self._logger.info(
                            "Usage fetch unavailable: account=%s email=%s active=True reason=%s status=%s",
                            account_num,
                            current_email,
                            fetched_usage.reason,
                            fetched_usage.status_code,
                        )
                    existing = (
                        previous_cached
                        if isinstance(previous_cached, dict)
                        else {}
                    )
                    previous_usage = existing.get(account_num)
                    usage, usage_note = _merge_usage_with_previous(
                        fetched_usage,
                        previous_usage,
                    )
                    _persist_usage_cache_entry(
                        existing, account_num, fetched_usage, previous_usage,
                    )
                    write_cache(usage_cache_path, existing)
                if isinstance(usage, dict):
                    lines = _format_usage_lines(usage)
                    for j, line in enumerate(lines):
                        connector = "└" if j == len(lines) - 1 else "├"
                        print(f"  {dimmed(connector)} {muted(line)}")
                elif isinstance(usage, oauth.UsageFetchError):
                    print(f"  {dimmed(oauth.describe_usage_error(usage))}")
                if isinstance(usage_note, oauth.UsageFetchError):
                    print(f"  {dimmed('•')} {muted(f'cached; live fetch {oauth.describe_usage_error(usage_note)}')}")
        else:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")

    # ------------------------------------------------------------------ #
    # Auto-switch (Beta)
    # ------------------------------------------------------------------ #

    def get_auto_switch_config(self) -> dict:
        """Return the persisted auto-switch (Beta) settings.

        Auto-switch is opt-in and stored in ``sequence.json`` under the
        ``autoSwitch`` key. Defaults to disabled at
        ``DEFAULT_AUTO_SWITCH_THRESHOLD``%.
        """
        data = self._get_sequence_data() or {}
        cfg = data.get("autoSwitch") or {}
        try:
            threshold = int(cfg.get("threshold", DEFAULT_AUTO_SWITCH_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DEFAULT_AUTO_SWITCH_THRESHOLD
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "threshold": threshold,
        }

    def set_auto_switch_config(
        self, *, enabled: bool | None = None, threshold: int | None = None
    ) -> dict:
        """Persist auto-switch (Beta) settings, returning the merged config.

        Only the provided fields are updated; the rest keep their stored (or
        default) values.

        Raises:
            ValidationError: if ``threshold`` is outside the 1-100 range.
        """
        self._setup_directories()
        self._init_sequence_file()
        data = self._get_sequence_data() or {}
        cfg = dict(data.get("autoSwitch") or {})
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        if threshold is not None:
            t = int(threshold)
            if not 1 <= t <= 100:
                raise ValidationError("Threshold must be between 1 and 100")
            cfg["threshold"] = t
        cfg.setdefault("enabled", False)
        cfg.setdefault("threshold", DEFAULT_AUTO_SWITCH_THRESHOLD)
        data["autoSwitch"] = cfg
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)
        return {"enabled": cfg["enabled"], "threshold": cfg["threshold"]}

    def ensure_auto_switch_enabled(self) -> dict:
        """Return config, persisting ``enabled=True`` if currently disabled.

        Foreground monitors (CLI ``--monitor``, TUI "Start monitor now")
        treat starting as opt-in.
        """
        cfg = self.get_auto_switch_config()
        if not cfg["enabled"]:
            cfg = self.set_auto_switch_config(enabled=True)
        return cfg

    def _resolve_active_usage(self) -> dict | None:
        """Return the active account's usage dict (cached or freshly fetched).

        Returns ``None`` when there is no active login, no usable credentials,
        or the usage API is unreachable / returned an error. Shared single
        source for ``get_active_usage_pct`` and ``get_active_usage_breakdown``
        so a monitor poll performs exactly one fetch.

        Reuses the same short-lived usage cache as ``list_accounts()`` /
        ``status()`` so repeated polling stays cheap and consistent. The active
        account is never refreshed (``is_active=True``) — Claude Code owns those
        credentials.
        """
        identity = self._get_current_account()
        if identity is None:
            return None
        current_email, current_org_uuid = identity

        creds = self._read_credentials() or ""
        if not creds or not oauth.extract_access_token(creds):
            return None

        data = self._get_sequence_data() or {}
        account_num = _slot_for_identity(
            data.get("accounts", {}), current_email, current_org_uuid,
        )

        usage_cache_path = self.backup_dir / "cache" / "usage.json"
        usage = None
        if account_num is not None:
            cached, file_ts = read_cache_with_timestamp(usage_cache_path)
            if cached is not None and account_num in cached:
                cached_usage = _usage_from_cache(cached[account_num])
                if (
                    isinstance(cached_usage, dict)
                    and _usage_slot_trusted(cached_usage, time.time(), file_ts)
                ):
                    usage = cached_usage

        if usage is None:
            fetched_usage = oauth.fetch_usage_for_account(
                account_num or "active", current_email, creds, is_active=True,
            )
            usage = fetched_usage
            if account_num is not None:
                previous_cached = read_cache_data(usage_cache_path, default={})
                previous_usage = (
                    previous_cached.get(account_num)
                    if isinstance(previous_cached, dict)
                    else None
                )
                usage, _ = _merge_usage_with_previous(fetched_usage, previous_usage)
            if account_num is not None and not isinstance(usage, oauth.UsageFetchError):
                existing = read_cache_data(usage_cache_path, default={})
                existing = (
                    existing
                    if isinstance(existing, dict)
                    else {}
                )
                _persist_usage_cache_entry(
                    existing,
                    account_num,
                    fetched_usage,
                    previous_usage if account_num is not None else None,
                )
                write_cache(usage_cache_path, existing)

        # Schema-drift detection: a non-error dict that lacks BOTH expected
        # rate-limit window keys looks identical to a network failure
        # downstream (``_max_usage_pct`` returns None for either).  Surface
        # it as a distinct WARNING at the boundary so the auto-switch
        # monitor's exponential backoff doesn't mask the schema break as a
        # transient outage.  We rate-limit the warning via the structured
        # logger to avoid spamming on every poll.
        if (
            isinstance(usage, dict)
            and "five_hour" not in usage
            and "seven_day" not in usage
        ):
            keys_seen = sorted(usage.keys())
            self._logger.warning(
                "usage API returned no recognized rate-limit windows "
                "(keys: %s) — possible schema change",
                keys_seen,
            )

        return usage if isinstance(usage, dict) else None

    def get_active_usage_pct(self) -> float | None:
        """Return the highest 5h/7d utilization pct for the active account.

        Used by the auto-switch monitor and CLI/TUI. Returns ``None`` when no
        usage is available. Thin ``max`` over ``get_active_usage_breakdown``'s
        source so all callers share one fetch path.
        """
        return _max_usage_pct(self._resolve_active_usage())

    def get_active_usage_breakdown(self) -> dict[str, float] | None:
        """Return per-window utilization for the active account.

        ``{"five_hour": 72.0, "seven_day": 87.0}`` — windows without a usable
        pct are omitted; returns ``None`` when no usage is available. Lets the
        monitor track each window's velocity independently so a fast 5h climb
        is not masked by a flat, higher 7d value (plan 019).
        """
        usage = self._resolve_active_usage()
        if not isinstance(usage, dict):
            return None
        out: dict[str, float] = {}
        for key in ("five_hour", "seven_day"):
            entry = usage.get(key)
            if isinstance(entry, dict):
                pct = entry.get("pct")
                if isinstance(pct, (int, float)):
                    out[key] = float(pct)
        return out or None

    def active_account_is_api_key(self) -> bool:
        """Whether the live active credential is a managed API key (no quota).

        Lets the auto-switch monitor distinguish "API-key account, nothing to
        monitor" (idle) from "usage fetch failed" (backoff) when usage is None.
        A local credential read — does not touch the network usage API.
        """
        return looks_like_api_key(self._read_credentials() or "")

    def _first_run_setup(self) -> None:
        """First-run setup workflow."""
        identity = self._get_current_account()

        if identity is None:
            print(dimmed("No active Claude account found. Please log in first."))
            return
        current_email, _ = identity

        response = input(
            f"No managed accounts found. Add current account "
            f"({current_email}) to managed list? [Y/n] "
        )
        if response.lower() == "n":
            print(dimmed("Setup cancelled. You can run 'cswap --add-account' later."))
            return

        self.add_account()

    def _activation_followup_text(self) -> str:
        """Platform-aware reassurance about when the activated account takes effect.

        Replaces the historical "Please restart Claude Code" warning which
        contradicted the README's accurate description of Claude Code's
        credential-reading behavior (re-reads ``.credentials.json`` per message
        on Linux/Windows; Keychain cache TTL ~30s on macOS). The new message
        matches reality so users (and tooling) trust the output.
        """
        if self.platform == Platform.MACOS:
            return "New account active within ~30s (Claude Code's Keychain cache TTL)."
        return "New account active on the next message."

    def switch(
        self,
        intent: SwitchIntent | None = None,
        *,
        strategy: str | None = None,
        json_output: bool = False,
    ) -> dict | bool | None:
        """Switch to another managed account.

        Returns ``True`` when credentials were activated on a different slot,
        ``False`` when no switch was needed.

        Pass an explicit intent:
          * ``ManualSwitchIntent()`` — interactive round-robin (default)
          * ``InteractiveAutoSwitchIntent(decision=...)`` — TUI monitor
          * ``BackgroundAutoSwitchIntent(decision=...)`` — CLI / launchd
        """
        if isinstance(intent, (InteractiveAutoSwitchIntent, BackgroundAutoSwitchIntent)):
            pass
        elif strategy is not None or json_output:
            return self._switch_cli(strategy=strategy, json_output=json_output)
        else:
            if intent is None:
                intent = ManualSwitchIntent()

        if isinstance(intent, (InteractiveAutoSwitchIntent, BackgroundAutoSwitchIntent)):
            quiet = intent.quiet
            decision = intent.decision
            automated = True
        else:
            quiet = intent.quiet
            decision = None
            automated = False

        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        identity = self._get_current_account()

        # Ensure org fields are migrated before checking composite key
        self._get_sequence_data_migrated()

        # Fresh-machine path: no live Claude session, but we have managed accounts
        # (e.g. right after cswap --import). Activate the recorded
        # activeAccountNumber, or fall back to the first slot in sequence.
        # With no live state to capture, the target must have valid backups —
        # walk the sequence if the preferred target is broken.
        if identity is None:
            data = self._get_sequence_data() or {}
            sequence = data.get("sequence", [])
            preferred = data.get("activeAccountNumber")
            if not preferred and sequence:
                preferred = sequence[0]
            if not preferred:
                raise ConfigError("No accounts are managed yet")

            target = str(preferred)
            if not self._account_is_switchable(target):
                skip_msg = (
                    f"Skipping Account-{target} (no stored credentials/config, "
                    f"re-add with cswap --add-account --slot {target})"
                )
                if quiet:
                    # Background service must not leak per-candidate prints to
                    # launchd's stdout (where there is no human reader); route
                    # through the structured logger instead.
                    self._logger.info(skip_msg)
                else:
                    print(f"{accent('Skipping')} Account-{target} "
                          f"(no stored credentials/config, re-add with "
                          f"cswap --add-account --slot {target})")
                fallback = next(
                    (str(num) for num in sequence
                     if str(num) != target and self._account_is_switchable(str(num))),
                    None,
                )
                if not fallback:
                    raise ConfigError(
                        "No managed accounts have valid stored credentials/config. "
                        "Re-add a slot with: cswap --add-account --slot <number>"
                    )
                target = fallback
            self._perform_switch(target, intent=intent)
            return True

        current_email, current_org_uuid = identity

        # Check if current account is managed
        if not self._account_exists(current_email, current_org_uuid):
            print(f"{accent('Notice:')} Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print(dimmed("Please run the switch command again to switch to the next account."))
            return False

        data = self._get_sequence_data()
        sequence = data.get("sequence", [])

        if len(sequence) < 2:
            msg = "Only one account is managed. Add more accounts to switch between."
            if quiet:
                # Automated callers (auto-switch monitor) must NOT silently
                # treat "nothing to switch to" as a successful rotation —
                # that would cause the monitor to log a false "switched
                # account" on every threshold crossing.  Raise so the caller
                # can dedup the actionable error.
                raise SwitchError(msg)
            print(dimmed(msg))
            return False

        active_account = (
            decision.live_active_slot
            if automated and decision is not None and decision.live_active_slot
            else data.get("activeAccountNumber")
        )

        next_account: str | None = None

        if automated and decision is not None:
            plan = self._plan_automated_switch(decision)
            if plan.outcome == "chosen":
                self._logger.info(
                    "switch: %s (active=%s threshold=%s)",
                    plan.reason,
                    active_account,
                    decision.threshold,
                )
                next_account = plan.target
            elif plan.outcome == "already_optimal":
                msg = f"{plan.reason}; waiting for cooldown."
                if quiet:
                    self._logger.info("switch: %s", msg)
                else:
                    print(dimmed(msg))
                return False
            else:
                msg = (
                    "Cannot choose auto-switch target safely: "
                    f"{plan.reason}"
                )
                if not quiet:
                    print(dimmed(msg))
                raise SwitchError(msg)

        if next_account is None and not automated:
            # Find current index and get next, skipping broken candidates.
            # The active slot is never checked here — _perform_switch captures
            # live state into a fresh backup before swapping, so the active
            # slot's stored backup may be stale or absent without blocking us.
            try:
                current_index = sequence.index(active_account)
            except ValueError:
                current_index = 0

            for offset in range(1, len(sequence)):
                candidate = str(sequence[(current_index + offset) % len(sequence)])
                if self._account_is_switchable(candidate):
                    next_account = candidate
                    break
                if quiet:
                    # Background service must not leak per-candidate prints; the
                    # structured logger is the right destination for these.
                    self._logger.info(
                        "Skipping Account-%s (no stored credentials/config, "
                        "re-add with cswap --add-account --slot %s)",
                        candidate, candidate,
                    )
                else:
                    print(
                        f"{accent('Skipping')} Account-{candidate} "
                        f"(no stored credentials/config, re-add with "
                        f"cswap --add-account --slot {candidate})"
                    )

        if next_account is None:
            msg = (
                "No other accounts have valid stored credentials/config. "
                "Re-add a skipped slot with: cswap --add-account --slot <number>"
            )
            if quiet:
                # See the analogous guard above: the auto-switch monitor must
                # see this as a failure so it can dedup-log and not falsely
                # report a successful rotation every poll.
                raise SwitchError(msg)
            print(dimmed(msg))
            return False

        if next_account == str(active_account):
            if quiet:
                raise SwitchError(
                    f"Cooldown picker selected the active account "
                    f"(Account-{active_account}) — nothing to switch to."
                )
            print(dimmed(
                f"Already on Account-{active_account}; no switch needed."
            ))
            return False

        self._perform_switch(next_account, intent=intent)
        return True


    def _warn_switch_session_hazards(self, target_account: str, *, quiet: bool) -> None:
        """Emit pre-switch warnings for live-session and multi-session races.

        Warn-only (never blocks): both hazards are external to the CLI.
        """
        # Session-mode drift warning (warn, never block): switching the
        # default login to an account that also has a live session profile
        # puts the same refresh token in two config dirs — if the server
        # rotates it, one copy goes stale.
        pre_data = self._get_sequence_data() or {}
        pre_email = (
            pre_data.get("accounts", {}).get(target_account, {}).get("email", "")
        )
        if pre_email:
            pids = self._live_session_pids(target_account, pre_email)
            if pids:
                warning(
                    f"Account-{target_account} ({pre_email}) has a live session-mode "
                    f"Claude instance (PID {', '.join(map(str, pids))}). Running the "
                    "same account as both the default login and a session can make "
                    "one copy's token go stale if the server rotates it. If the "
                    "session later fails to authenticate, exit it and re-run "
                    f"'cswap run {target_account}'."
                )

        # Multi-session race awareness (claude-code#24317): when more than one
        # default-mode Claude Code process is running, each holds its own
        # in-memory copy of the old refresh token. After we swap credentials,
        # all of them try to refresh near-simultaneously and Anthropic's
        # single-use refresh token allows only one to succeed — the rest
        # surface an interactive re-login prompt. We can't prevent this from
        # outside the CLI; we log a structured warning so launchd readers
        # (monitor.err) and interactive users understand what they're seeing.
        live_default_pids = self._live_default_mode_claude_pids()
        if len(live_default_pids) > 1:
            pid_list = ", ".join(map(str, sorted(live_default_pids)))
            self._logger.warning(
                "multi-session race possible: %d live Claude Code processes "
                "(PIDs %s); claude-code#24317 may force re-login on one or more "
                "after switch",
                len(live_default_pids),
                pid_list,
            )
            if not quiet:
                warning(
                    f"{len(live_default_pids)} Claude Code sessions running "
                    f"(PIDs {pid_list}). After the swap, one or more may need "
                    "re-login due to a single-use refresh-token race "
                    "(claude-code#24317). Close extra sessions first to avoid this."
                )

    def _activate_target_directly(
        self,
        data: dict,
        target_account: str,
        target_email: str,
        config_path: Path,
        current_identity: tuple[str, str] | None,
        *,
        quiet: bool,
        force_refresh: bool,
    ) -> None:
        """Activate the target without backing up a prior account.

        Used when there is no live Claude session yet (e.g. right after import)
        or claude-swap has no tracked active account (e.g. purge -> add-token ->
        switch-to while a live credential still exists). Skips the back-up step
        so we never write account-None-* backups.
        """
        target_creds = self._read_account_credentials(
            target_account, target_email
        )
        target_config = self._read_account_config(target_account, target_email)
        if not target_creds:
            raise SwitchError(
                f"Account-{target_account} has no stored credentials. "
                f"Re-add with: cswap --add-account --slot {target_account}"
            )
        if not target_config:
            raise SwitchError(
                f"Account-{target_account} has no stored config backup. "
                f"Re-add with: cswap --add-account --slot {target_account}"
            )
        target_creds = self._refresh_target_credentials_before_activation(
            target_account,
            target_email,
            target_creds,
            force=force_refresh,
        )
        try:
            target_config_data = json.loads(target_config)
        except json.JSONDecodeError as exc:
            raise SwitchError(f"Invalid backup config: {exc}")
        target_oauth = target_config_data.get("oauthAccount")
        if not target_oauth:
            raise SwitchError("Invalid oauthAccount in backup")

        # Snapshot live state so a mid-operation failure can be undone.
        # When a live session exists, fail fast if the snapshot is
        # unreadable rather than proceeding to overwrite without a
        # safety net. The fresh-machine case has nothing to restore.
        rollback_creds: str | None = None
        rollback_config_text: str | None = None
        if current_identity is not None:
            rollback_creds = self._read_credentials()
            if rollback_creds is None:
                raise CredentialReadError(
                    "Cannot snapshot live credentials before activation"
                )
            if config_path.exists():
                try:
                    rollback_config_text = config_path.read_text(
                        encoding="utf-8"
                    )
                except OSError as e:
                    raise ConfigError(
                        f"Cannot snapshot live config before activation: {e}"
                    )

        creds_written = False
        config_written = False
        try:
            self._write_credentials(target_creds, verify=True)
            creds_written = True

            # Mirror the normal switch path: preserve existing local
            # settings/projects when ~/.claude.json already exists, only
            # swapping in oauthAccount. Fall back to the full imported
            # config when no usable local config exists.
            existing_config = (
                self._read_json(config_path) if config_path.exists() else None
            )
            if existing_config:
                existing_config["oauthAccount"] = target_oauth
                self._write_json(config_path, existing_config)
            else:
                self._write_json(config_path, target_config_data)
            config_written = True

            data["activeAccountNumber"] = int(target_account)
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
        except Exception:
            if config_written and rollback_config_text is not None:
                try:
                    config_path.write_text(
                        rollback_config_text, encoding="utf-8"
                    )
                    if sys.platform != "win32":
                        os.chmod(config_path, 0o600)
                except Exception as e:
                    self._logger.error(
                        f"Failed to rollback config: {e}"
                    )
            if creds_written and rollback_creds is not None:
                try:
                    self._write_credentials(rollback_creds)
                except Exception as e:
                    self._logger.error(
                        f"Failed to rollback credentials: {e}"
                    )
            raise

        self._logger.info(
            f"Activated account {target_account} (no prior live account)"
        )
        if not quiet:
            print(
                f"{accent('Activated')} Account-{target_account} ({target_email})"
            )
            print()
            print(dimmed(self._activation_followup_text()))
            print()

    def _swap_target_transactional(
        self,
        data: dict,
        target_account: str,
        target_email: str,
        current_account: str,
        current_email: str,
        config_path: Path,
        *,
        force_refresh: bool,
    ) -> None:
        """Back up the current account and activate the target with rollback."""
        # Create transaction for rollback capability
        try:
            original_creds = self._read_credentials()
            if original_creds is None:
                raise CredentialReadError("Failed to read current credentials")
            original_config = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigError("Claude config file not found")
        except PermissionError:
            raise ConfigError("Permission denied reading Claude config")

        transaction = SwitchTransaction(
            original_credentials=original_creds,
            original_config=original_config,
            original_account_num=current_account,
            original_email=current_email,
            config_path=config_path,
        )

        try:
            # Step 1: Backup current account
            original_creds = self._write_verified_live_account_credentials(
                current_account, current_email, original_creds
            )
            self._write_account_config(
                current_account, current_email, original_config
            )
            self._logger.info(f"Backed up account {current_account}")

            # Step 2: Retrieve target account
            target_creds = self._read_account_credentials(
                target_account, target_email
            )
            target_config = self._read_account_config(target_account, target_email)

            if not target_creds:
                raise SwitchError(
                    f"Account-{target_account} has no stored credentials. "
                    f"Re-add with: cswap --add-account --slot {target_account}"
                )
            if not target_config:
                raise SwitchError(
                    f"Account-{target_account} has no stored config backup. "
                    f"Re-add with: cswap --add-account --slot {target_account}"
                )
            target_creds = self._refresh_target_credentials_before_activation(
                target_account,
                target_email,
                target_creds,
                force=force_refresh,
            )

            # Step 3: Activate target account - credentials
            self._write_credentials(target_creds, verify=True)
            transaction.record_step("credentials_written")
            self._logger.info("Wrote target credentials")

            # Step 4: Update config with target oauthAccount
            target_config_data = json.loads(target_config)
            oauth_section = target_config_data.get("oauthAccount")

            if not oauth_section:
                raise SwitchError("Invalid oauthAccount in backup")

            current_config_data = self._read_json(config_path)
            current_config_data["oauthAccount"] = oauth_section

            self._write_json(config_path, current_config_data)
            transaction.record_step("config_written")
            self._logger.info("Updated config file")

            # Step 5: Update sequence state
            data["activeAccountNumber"] = int(target_account)
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
            transaction.record_step("sequence_updated")

            self._logger.info(
                f"Switched from account {current_account} to {target_account}"
            )

        except Exception as e:
            self._logger.error(f"Switch failed: {e}, attempting rollback")
            if transaction.completed_steps:
                success = transaction.rollback(self)
                if success:
                    self._logger.info("Rollback successful")
                    raise SwitchError(
                        f"Switch failed and was rolled back: {e}"
                    )
                else:
                    self._logger.error("Rollback failed!")
                    raise SwitchError(
                        f"Switch failed and rollback also failed: {e}. "
                        f"Manual recovery may be needed."
                    )
            raise

    def _print_switch_result(self, target_account: str, target_email: str) -> None:
        """Render the post-switch summary.

        Runs after the lock releases so persist callbacks inside
        list_accounts() can re-acquire it.
        """
        print(f"{accent('Switched to')} Account-{target_account} ({target_email})")
        try:
            self.list_accounts()
        except Exception as e:
            self._logger.warning(f"Post-switch usage display failed: {e!r}")
            print(dimmed("  (usage display unavailable — run `cswap --list` to retry)"))
        print()
        print(dimmed(self._activation_followup_text()))
        print()

    def _perform_switch(
        self,
        target_account: str,
        *,
        intent: SwitchIntent | None = None,
        emit_output: bool = True,
    ) -> dict | None:
        """Perform the actual account switch with transaction support.

        When ``emit_output`` is False (JSON mode) human output is suppressed and
        a result dict is returned for ``_switch_result_from_op``. The post-switch
        display runs after the lock releases so that persist callbacks inside
        list_accounts() can re-acquire it.
        """
        if intent is None:
            intent = ManualSwitchIntent()
        quiet = intent.quiet or not emit_output
        force_refresh = intent.force_refresh

        self._warn_switch_session_hazards(target_account, quiet=quiet)

        with FileLock(self.lock_file):
            data = self._get_sequence_data()
            active_account = data.get("activeAccountNumber")
            current_account = str(active_account) if active_account is not None else None
            target_email = data["accounts"][target_account]["email"]
            current_identity = self._get_current_account()
            if current_identity is not None:
                current_email, current_org_uuid = current_identity
                current_account = next(
                    (
                        num for num, account in data.get("accounts", {}).items()
                        if account.get("email") == current_email
                        and account.get("organizationUuid", "") == current_org_uuid
                    ),
                    None,
                )

            config_path = self._get_claude_config_path()

            # Direct activation path: either there is no live Claude session
            # yet (e.g. right after import), or claude-swap has no tracked
            # active account yet (e.g. purge -> add-token -> switch-to while a
            # live Claude credential still exists). In both cases, skip the
            # back-up-current step so we never write account-None-* backups.
            if current_identity is None or current_account is None:
                self._activate_target_directly(
                    data,
                    target_account,
                    target_email,
                    config_path,
                    current_identity,
                    quiet=quiet,
                    force_refresh=force_refresh,
                )
                if not emit_output:
                    from_ref = (
                        None if current_identity is None
                        else account_ref(None, current_identity[0])
                    )
                    return {
                        "from": from_ref,
                        "to": account_ref(int(target_account), target_email),
                        "warnings": [],
                    }
                if not quiet:
                    self._print_switch_result(target_account, target_email)
                return None

            current_email, _ = current_identity
            from_num = current_account
            self._swap_target_transactional(
                data,
                target_account,
                target_email,
                current_account,
                current_email,
                config_path,
                force_refresh=force_refresh,
            )

        # Lock released. Safe to do network I/O and let persist callbacks
        # re-acquire the lock from inside list_accounts().
        if not emit_output:
            data = self._get_sequence_data() or {}
            to_email = data.get("accounts", {}).get(target_account, {}).get("email", target_email)
            return {
                "from": account_ref(int(from_num), current_email) if from_num else None,
                "to": account_ref(int(target_account), to_email),
                "warnings": [],
            }
        if quiet:
            return None
        self._print_switch_result(target_account, target_email)
        return None

    def purge(self) -> None:
        """Remove all traces of claude-swap from the system.

        This removes:
        - All stored account credentials (files on Linux/WSL/Windows, macOS
          Keychain items via ``security`` on macOS), plus a best-effort sweep of
          any pre-migration keyring / Windows Credential Manager entries left behind
        - The active backup directory (XDG path on Linux/WSL, ~/.claude-swap-backup elsewhere)
        - Any stale legacy ~/.claude-swap-backup directory left around from
          before the XDG migration
        """
        legacy = get_legacy_backup_root()
        legacy_distinct = legacy != self.backup_dir

        # Refuse while any session-mode claude is running: purging would pull
        # its profile (and keychain entry) out from under a live process.
        sessions_root = self.backup_dir / "sessions"
        session_dirs = (
            [d for d in sessions_root.iterdir() if d.is_dir()]
            if sessions_root.is_dir()
            else []
        )
        from claude_swap.session import live_sessions_for

        live = {}
        for d in session_dirs:
            pids = [s.pid for s in live_sessions_for(d)]
            if pids:
                live[d.name] = pids
        if live:
            details = "; ".join(
                f"{name} (PID {', '.join(map(str, pids))})"
                for name, pids in live.items()
            )
            raise SessionError(
                f"Live session-mode Claude instance(s) found: {details}. "
                "Exit them first, then retry --purge."
            )

        warning("This will remove ALL claude-swap data from your system:")
        print(f"  - Backup directory: {self.backup_dir}")
        if legacy_distinct and legacy.exists():
            print(f"  - Legacy backup directory: {legacy}")
        if self._uses_file_backup_backend():
            print("  - All stored account credential files")
        else:
            print("  - All stored account credentials from the macOS Keychain")
        if session_dirs:
            print("  - All session profiles and their Keychain entries")
        print()
        print(dimmed("Note: This does NOT affect your current Claude Code login."))
        print()

        confirm = input("Are you sure you want to purge all data? [y/N] ")
        if confirm.lower() != "y":
            print(dimmed("Cancelled"))
            return

        removed_items = []

        # Remove credentials
        data = self._get_sequence_data()
        if data:
            for account_num, account_info in data.get("accounts", {}).items():
                email = account_info.get("email", "")
                if self._uses_file_backup_backend():
                    # Remove credential files (Linux/WSL/Windows)
                    cred_files = [
                        self.credentials_dir / f".creds-{account_num}-{email}.enc"
                    ]
                    if str(account_num) != "None":
                        cred_files.append(
                            self.credentials_dir / f".creds-None-{email}.enc"
                        )
                    for cred_file in cred_files:
                        try:
                            if cred_file.exists():
                                cred_file.unlink()
                                removed_items.append(f"Credential file: {cred_file.name}")
                        except Exception:
                            pass  # Ignore errors during purge
                    if self.platform == Platform.WINDOWS:
                        # Best-effort cleanup of any pre-migration Credential
                        # Manager entries left behind if the keyring → file
                        # migration never completed. Files are authoritative
                        # now; these are just stale cruft.
                        usernames = [f"account-{account_num}-{email}"]
                        if str(account_num) != "None":
                            usernames.append(f"account-None-{email}")
                        _sweep_legacy_keyring(usernames, removed_items)
                else:
                    # macOS: remove the Keychain items via `security`.
                    usernames = [f"account-{account_num}-{email}"]
                    if str(account_num) != "None":
                        usernames.append(f"account-None-{email}")
                    for username in usernames:
                        try:
                            macos_keychain.delete_password(SECURITY_SERVICE, username)
                            removed_items.append(f"Credential: {username}")
                        except Exception:
                            pass  # Ignore errors during purge
                    # Best-effort cleanup of any pre-migration keyring entries left
                    # behind if the keyring → security migration never completed.
                    _sweep_legacy_keyring(usernames, removed_items)

        # Session-profile keychain entries must go BEFORE the backup dir:
        # the hashed service names are derived from the dir paths and can't
        # be recomputed once the directories are deleted.
        if session_dirs:
            from claude_swap.session import delete_macos_keychain_entry

            for d in session_dirs:
                delete_macos_keychain_entry(d)
            removed_items.append(
                f"Session profiles: {', '.join(d.name for d in session_dirs)}"
            )

        # Remove backup directory
        if self.backup_dir.exists():
            # Close log handlers before deleting (required on Windows)
            for handler in self._logger.handlers[:]:
                handler.close()
                self._logger.removeHandler(handler)

            shutil.rmtree(self.backup_dir)
            removed_items.append(f"Directory: {self.backup_dir}")

        # Also clean a stale legacy directory if it somehow still exists
        # (e.g. a partial pre-migration state, or files re-created after init).
        if legacy_distinct and legacy.exists():
            try:
                shutil.rmtree(legacy)
                removed_items.append(f"Legacy directory: {legacy}")
            except OSError:
                pass

        if removed_items:
            print(f"\n{accent('Removed:')}")
            for item in removed_items:
                print(f"  {dimmed('-')} {item}")
        else:
            print(f"\n{dimmed('No claude-swap data found to remove.')}")

        print(f"\n{accent('Purge complete.')}")
