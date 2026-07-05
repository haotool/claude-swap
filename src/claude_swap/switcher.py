"""Core account switcher logic for Claude Code."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
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
from claude_swap.claude_locks import claude_config_lock, claude_credentials_lock
from claude_swap.json_output import (
    account_ref,
    slot_for_identity,
    switch_noop,
    switch_result_from_op,
)
from claude_swap.credentials import (
    SECURITY_SERVICE,
    ActiveCredentials,
    CredentialStore,
    looks_like_api_key,
)
from claude_swap.locking import FileLock
from claude_swap.logging_config import setup_logging
from claude_swap.models import (
    _ONLY_ONE_ACCOUNT_MSG,
    ManualSwitchIntent,
    Platform,
    SwitchIntent,
    SwitchPreconditionKind,
    SwitchPreconditions,
    SwitchTransaction,
    get_timestamp,
)
from claude_swap.switch_cli import run_switch_cli
from claude_swap.printer import (
    accent,
    dimmed,
    muted,
    warning,
)
from claude_swap.paths import (
    get_backup_root,
    get_global_config_path,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)
from claude_swap.credential_refresh import CredentialRefresher
from claude_swap.list_reporter import ListReporter, run_list, run_status
from claude_swap.sequence_store import (
    AccountRecord,
    SequenceData,
    SequenceStore,
)
from claude_swap.usage_policy import headroom, rank_headroom_best
from claude_swap.usage_store import UsageEntry, UsageStore
from typing import Any, cast

# Service name under which the legacy ``keyring`` backend stored per-account
# backup credentials on macOS (kept for the one-time keyring → security migration
# and for the Windows Credential Manager migration).
KEYRING_SERVICE = "claude-code"

# Setup-tokens are inference-only server-side; wider scopes trigger 403s
# on profile endpoints. Matches Claude Code's CLAUDE_CODE_OAUTH_TOKEN path.
SETUP_TOKEN_SCOPES = ("user:inference",)

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
        self._usage_store = UsageStore(self.backup_dir / "cache")

        # The credential storage layer (active + per-account backup stores, macOS
        # Keychain-vs-file routing, the per-process capability cache). Reads its
        # live config (platform, _logger, credentials_dir) back off this switcher.
        # Constructed BEFORE run_migrations(), which performs storage ops on macOS.
        # One store per switcher: the capability cache is per-process.
        self._store = CredentialStore(self)
        # List/status renderer, created lazily by _list_reporter().
        self._reporter: ListReporter | None = None
        # OAuth credential-freshness: verify/refresh/sync of backup tokens.
        self._refresher = CredentialRefresher(self)
        # Typed owner of sequence.json. Lock-agnostic: this switcher wraps
        # read-modify-write transactions in FileLock; the store never locks.
        # Late-bind the JSON helpers so tests that monkeypatch
        # ``switcher._read_json`` / ``_write_json`` after construction still
        # reach the store (a captured bound method would ignore the patch).
        self._sequence_store = SequenceStore(
            self.sequence_file,
            read_json=lambda p: self._read_json(p),
            write_json=lambda p, d: self._write_json(p, d),
        )
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

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        """Read and parse JSON file."""
        if not path.exists():
            return None
        try:
            return cast("dict[str, Any] | None", json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._logger.warning(f"Invalid JSON in {path}")
            return None

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        """Write JSON file with validation."""
        content = json.dumps(data, indent=2)

        # mkstemp keeps the temp file owner-only from creation — sequence/config
        # JSON must never be briefly world-readable in its parent dir.
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        )
        temp_path = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            # Validate written content
            try:
                json.loads(temp_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raise ConfigError("Generated invalid JSON")
            os.replace(temp_path, path)
            if sys.platform != "win32":
                os.chmod(path, 0o600)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _read_credentials(self) -> str | None:
        return self._store._read_credentials()

    def _read_active_credentials(self) -> ActiveCredentials:
        return self._store._read_active_credentials()

    def _write_credentials(self, credentials: str, *, verify: bool = False) -> None:
        self._store._write_credentials(credentials, verify=verify)

    def _uses_file_backup_backend(self) -> bool:
        return self._store._uses_file_backup_backend()

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        return self._store._read_account_credentials(account_num, email)

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup, then invalidate the slot's session.

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
        self, account_num: str, email: str, credentials: str,
        *, assume_locked: bool = False,
    ) -> str:
        """Persist + verify live credentials (delegates to CredentialRefresher).

        Callers already holding ``self.lock_file`` (e.g. ``_perform_switch``)
        must pass ``assume_locked=True``; ``FileLock`` is not re-entrant.
        """
        return self._refresher.write_verified_live(
            account_num, email, credentials, assume_locked=assume_locked,
        )

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
        self._store._delete_account_credentials(account_num, email)


    def _kc_read_backup(self, account_num: str, email: str) -> str:
        return self._store._kc_read_backup(account_num, email)

    def _kc_write_backup(self, account_num: str, email: str, credentials: str) -> None:
        self._store._kc_write_backup(account_num, email, credentials)

    def _delete_backup_keychain_quiet(self, account_num: str, email: str) -> None:
        self._store._delete_backup_keychain_quiet(account_num, email)

    @staticmethod
    def _find_account_slot(
        data: dict[str, Any], email: str, organization_uuid: str
    ) -> str | None:
        return slot_for_identity(data.get("accounts", {}), email, organization_uuid)

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

    # -- public accessors for the auto-switch engine -----------------------

    def usage_by_account(
        self,
    ) -> dict[str, dict[str, Any] | str | None]:
        """Public wrapper: account number → decision-grade usage value.

        Each value is a usage dict (last-good, trusted while ≤
        ``usage_store.STALE_OK_S`` old), a sentinel string, or ``None``
        (unknown).
        """
        return self._usage_by_account()

    def usage_entries_by_account(
        self, fetch: set[str] | None = None
    ) -> dict[str, UsageEntry]:
        """Store-backed usage entries (ages, errors, poll state) per account.

        ``fetch`` restricts which accounts *may* be fetched this pass (the
        auto engine's scheduler); ``None`` means every stale account is
        eligible (on-demand callers).
        """
        reporter = self._list_reporter()
        accounts_info = reporter.build_accounts_info()
        return reporter.collect_usage_entries(accounts_info, fetch=fetch)

    def usage_fetch_stamps(self) -> dict[str, float | None]:
        """Per-slot ``fetchedAt`` snapshot from the usage store — a pure file
        read (no fetching, no credential access). The TUI watch view diffs
        consecutive snapshots to flash rows whose usage just refreshed.
        """
        data = self._get_sequence_data() or {}
        identities = {
            num: (info.get("email", ""), info.get("organizationUuid", "") or "")
            for num, info in data.get("accounts", {}).items()
        }
        return {
            num: entry.fetched_at
            for num, entry in self._usage_store.entries(identities).items()
        }

    def set_usage_poll_plan(
        self, plans: dict[str, tuple[float | None, float | None]]
    ) -> None:
        """Persist the auto engine's per-slot ``(nextPollAt, pollIntervalS)``."""
        data = self._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        identities = {
            num: (
                accounts.get(num, {}).get("email", ""),
                accounts.get(num, {}).get("organizationUuid", "") or "",
            )
            for num in plans
        }
        self._usage_store.set_poll_plan(plans, identities)

    def switchable_account_numbers(self) -> list[str]:
        """Account numbers in rotation order that have usable stored backups."""
        data = self._get_sequence_data() or {}
        return [
            str(num)
            for num in data.get("sequence", [])
            if self._account_is_switchable(str(num))
        ]

    def account_kind_for(self, account_num: str) -> str:
        """Public wrapper: ``"api_key"`` or ``"oauth"`` (setup-tokens read as oauth)."""
        return self._account_kind(account_num)

    def account_email(self, account_num: str) -> str:
        """Stored email for a slot; empty string when unknown."""
        data = self._get_sequence_data() or {}
        email = data.get("accounts", {}).get(str(account_num), {}).get("email", "")
        return email if isinstance(email, str) else ""

    def current_account_number(self) -> str | None:
        """Slot of the live login; ``None`` when there is none or it's unmanaged.

        Deliberately no fallback to the recorded ``activeAccountNumber``: an
        unmanaged live login must return ``None`` — never a guessed slot — so
        the auto-switch engine can't evaluate the wrong account's usage and
        overwrite a login cswap doesn't own (``_perform_switch`` would take
        the no-backup direct-activation path). Use :meth:`has_live_login` to
        tell the two ``None`` cases apart.
        """
        identity = self._get_current_account()
        if identity is None:
            return None
        data = self._get_sequence_data() or {}
        email, org_uuid = identity
        return self._find_account_slot(data, email, org_uuid)

    def has_live_login(self) -> bool:
        """Whether ``~/.claude.json`` carries any live account identity."""
        return self._get_current_account() is not None

    def live_session_pids_for(self, account_num: str, email: str) -> list[int]:
        """Public wrapper: PIDs of live ``cswap run`` sessions for a slot."""
        return self._live_session_pids(account_num, email)

    def persist_backup_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Persist rotated credentials to a slot's backup store, under the lock.

        For inactive accounts only — never routes to the active store. Mirrors
        the persist callback ``_collect_usage`` uses. The caller must NOT hold
        ``self.lock_file`` (FileLock is non-reentrant).
        """
        with FileLock(self.lock_file):
            self._write_account_credentials(account_num, email, credentials)

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
        self._sequence_store.init_if_missing()

    def _get_sequence_data(self) -> dict[str, Any] | None:
        """Raw sequence.json dict shim over the typed store.

        Retained for external consumers (list_reporter/migrations via
        protocols) that still read the raw dict; switcher's own logic uses the
        typed ``self._sequence_store`` model.
        """
        return self._sequence_store.load_raw()

    def _get_sequence_view(self) -> SequenceData | None:
        """Typed ``sequence.json`` view for protocol consumers (engine track)."""
        return self._sequence_store.load()

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
        ``oauthAccount`` identity. Now that ``_read_credentials`` can return a raw
        ``sk-ant-api…`` key, a live ``/login`` key could be backed up as a kindless
        account, corrupting the session-guard / export / collision logic that keys
        off ``kind``. Reject with guidance toward the supported path instead.
        """
        if looks_like_api_key(creds):
            raise ValidationError(
                "Active login is an API-key account. Add it with "
                "'cswap --add-token sk-ant-api...' instead of --add-account."
            )

    def _reject_cross_kind_collision(self, email: str, is_api_key: bool) -> None:
        """Reject registering a token whose (email, personal-org) already exists as
        the *other* kind.

        Identity is matched on ``(email, organizationUuid)`` only, so two slots
        sharing an email across kinds (one OAuth, one API key) could not be told
        apart at switch time. Rather than thread ``kind`` through the whole identity
        system, refuse the collision and point the user at a distinct ``--email``.
        The default ``…@token.local`` labels never collide; this only guards a forced
        ``--email``.
        """
        data = self._get_sequence_data()
        if not data:
            return
        slot = slot_for_identity(data.get("accounts", {}), email, "")
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
            return str(matches[0])

        details = ", ".join(
            f"{num} [{data['accounts'][num].get('organizationName') or 'personal'}]"
            for num in matches
        )
        raise ConfigError(
            f"Email '{identifier}' is ambiguous — matches accounts: {details}. "
            f"Use account number instead (e.g., cswap --switch-to 1)."
        )

    def _get_sequence_data_migrated(self) -> dict[str, Any] | None:
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
            # Deliberately bypasses SequenceStore.save(): this migration keys off
            # the *presence* of the "organizationUuid" dict key (see the
            # needs_migration check in _get_sequence_data_migrated), which the
            # typed model normalizes away. Persist the raw dict directly so the
            # presence semantics are preserved. Any future change to save()'s
            # stamping/validation must be mirrored here.
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
        data = self._get_sequence_data() or {}

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
            self._sequence_store.save(
                self._sequence_store.load_or_empty().remove_slot(d_num)
            )

        if migrate_from:
            data = self._sequence_store.load_or_empty()
            existing = data.get(migrate_from)
            old_email = existing.email if existing else ""
            self._delete_account_files(migrate_from, old_email)
            self._sequence_store.save(data.remove_slot(migrate_from))
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")

    def _register_account_slot(
        self, account_num: str, record: AccountRecord, *, set_active: bool
    ) -> None:
        """Record the new slot in sequence.json and persist it."""
        data = self._sequence_store.load_or_empty().register_slot(
            account_num, record, set_active=set_active
        )
        self._sequence_store.save(data)

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

            # Hold the cross-process lock across slot resolution + the credential
            # and sequence writes so a concurrent switch/remove can't leave us
            # writing a stale slot id — re-resolve the slot fresh inside the lock.
            with FileLock(self.lock_file):
                data = self._sequence_store.load_or_empty()
                account_num = next(
                    (num for num, rec in data.accounts.items()
                     if rec.email == current_email
                     and rec.organization_uuid == current_org_uuid),
                    None,
                )
                if account_num is None:
                    raise ConfigError(
                        f"Active account {current_email} is no longer managed"
                    )
                matched_org_name = data.accounts[account_num].organization_name
                current_creds = self._write_verified_live_account_credentials(
                    account_num,
                    current_email,
                    current_creds,
                    assume_locked=True,
                )
                self._write_account_config(account_num, current_email, current_config)
                self._sequence_store.save(data.set_active(int(account_num)))

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
        config_data = self._read_json(config_path) or {}
        oauth_data = config_data.get("oauthAccount", {})
        account_uuid = oauth_data.get("accountUuid", "")
        organization_uuid = oauth_data.get("organizationUuid", "") or ""
        organization_name = oauth_data.get("organizationName", "") or ""

        # Now safe to perform destructive cleanup (new account data is in memory).
        # The whole transaction runs under the cross-process lock so the several
        # sequence.json read-modify-write cycles below stay atomic against a
        # concurrent switch/add. Interactive prompts already happened above in
        # ``_resolve_target_slot`` — none run while the lock is held.
        with FileLock(self.lock_file):
            self._apply_slot_displacement(displace_slot, migrate_from, slot)

            # Store backups
            current_creds = self._write_verified_live_account_credentials(
                account_num,
                current_email,
                current_creds,
                assume_locked=True,
            )
            self._write_account_config(account_num, current_email, current_config)

            # Update sequence.json
            self._register_account_slot(
                account_num,
                AccountRecord.create(
                    email=current_email,
                    uuid=account_uuid,
                    organization_uuid=organization_uuid,
                    organization_name=organization_name,
                ),
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
        token type is auto-detected: an ``sk-ant-api…`` value is a managed API key
        (stored raw, activated on Claude Code's API-key auth axis), anything else is
        treated as an OAuth setup-token. No Anthropic API calls are made.

        Args:
            token: Raw OAuth setup-token or ``sk-ant-api…`` key, or ``"-"`` to read
                   one line from stdin, or ``""`` to prompt securely via getpass.
            email: Email address to associate with the account. When omitted,
                   defaults to ``setup-token-{slot}@token.local`` (or
                   ``api-key-{slot}@token.local`` for API keys) since these tokens
                   carry no real email metadata.
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
            # Hold the lock across the read-modify-write so a concurrent
            # switch/add can't clobber sequence.json (parity with add_account's
            # refresh path); re-read fresh inside the lock.
            with FileLock(self.lock_file):
                data = self._sequence_store.load_or_empty()
                account_num = next(
                    (num for num, rec in data.accounts.items()
                     if rec.email == email and rec.organization_uuid == ""),
                    None,
                )
                if account_num is None:
                    raise ConfigError(
                        f"Existing account metadata for {email} is inconsistent"
                    )
                self._write_account_credentials(account_num, email, credentials)
                self._write_account_config(account_num, email, config)
                self._sequence_store.save(data)
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

        record = AccountRecord.create(email=email, is_api_key=is_api_key)

        # Hold the cross-process lock across displacement + credential/config
        # writes + the sequence.json registration so a concurrent switch/add
        # can't interleave these read-modify-write cycles (parity with
        # add_account). Interactive prompts already happened above in
        # _resolve_target_slot — none run while the lock is held.
        with FileLock(self.lock_file):
            self._apply_slot_displacement(displace_slot, migrate_from, slot)
            self._write_account_credentials(account_num, email, credentials)
            self._write_account_config(account_num, email, config)
            self._register_account_slot(account_num, record, set_active=False)
        self._logger.info(f"Added account {account_num} from {source_label}: {email}")
        print(
            f"{accent('Added')} Account {account_num}: {email} "
            f"{muted('[personal]')} {muted(f'(from {source_label})')}"
        )

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Remove account from managed accounts.

        When ``assume_yes`` is True the confirmation prompt is skipped (used by
        the TUI, which collects confirmation before calling).
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

            # For email identifiers, handle ambiguous matches interactively
            data = self._get_sequence_data() or {}
            matches = [
                num for num, acc in data.get("accounts", {}).items()
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

        data = self._get_sequence_data() or {}
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

        # Delete files + rewrite sequence.json under the cross-process lock,
        # re-reading fresh inside so a concurrent switch/add can't be clobbered.
        with FileLock(self.lock_file):
            seq = self._sequence_store.load_or_empty()
            if seq.get(account_num) is None:
                raise AccountNotFoundError(f"Account-{account_num} does not exist")

            # Remove backup files
            self._delete_account_files(account_num, email)

            # Update sequence.json
            self._sequence_store.save(seq.remove_slot(account_num))

        self._logger.info(f"Removed account {account_num}: {email}")
        print(f"{accent('Removed')} Account-{account_num} ({email})")

    def _list_reporter(self) -> ListReporter:
        """Lazy singleton so per-run reporter state survives across calls.

        ``_usage_by_account`` builds accounts info through one reporter call
        and resolves usages through another; the reporter's
        ``_active_keychain_unavailable`` flag set by the first must still be
        visible to the second, or a locked Keychain shows the active account
        as "no credentials" (the misread upstream PR#77 fixed). Reuse is
        thread-safe because the flag is only written by the main thread in
        ``build_accounts_info``, before ``resolve_usages`` starts its thread
        pool — keep that ordering invariant.
        """
        if self._reporter is None:
            self._reporter = ListReporter(self)
        return self._reporter

    def _usage_by_account(self) -> dict[str, dict[str, Any] | str | None]:
        """Map account number → decision-grade usage value for managed accounts."""
        reporter = self._list_reporter()
        accounts_info = reporter.build_accounts_info()
        entries = reporter.collect_usage_entries(accounts_info)
        return {num: entry.decision_value() for num, entry in entries.items()}

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

        Ties (including current-vs-other) resolve in favor of staying put.
        Never raises on network failure.
        """
        data = self._get_sequence_data() or {}
        others = [
            str(n) for n in data.get("sequence", [])
            if str(n) != str(current_num) and self._account_is_switchable(str(n))
        ]
        usage = self._usage_by_account()
        result = rank_headroom_best(
            cast("dict[str, object]", usage),
            current=str(current_num) if current_num is not None else None,
            others=others,
        )
        return result.target, result.note or ""

    def _switch_result_from_op(
        self, op: dict[str, Any], strategy: str, extra_warnings: list[str] | None = None
    ) -> dict[str, Any]:
        return switch_result_from_op(op, strategy, extra_warnings)

    def _switch_noop(
        self,
        *,
        strategy: str,
        reason: str,
        message: str,
        from_ref: dict[str, Any] | None = None,
        to_ref: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return switch_noop(
            strategy=strategy,
            reason=reason,
            message=message,
            from_ref=from_ref,
            to_ref=to_ref,
            warnings=warnings,
        )

    def _resolve_fresh_machine_target(
        self,
        *,
        quiet: bool = False,
        warnings: list[str] | None = None,
    ) -> str:
        """Pick a switchable slot when no live Claude login is present."""
        data = self._get_sequence_data() or {}
        sequence = data.get("sequence", [])
        preferred = data.get("activeAccountNumber")
        if not preferred and sequence:
            preferred = sequence[0]
        if not preferred:
            raise ConfigError("No accounts are managed yet")

        target = str(preferred)
        if not self._account_is_switchable(target):
            if warnings is not None:
                warnings.append(
                    f"Skipped Account-{target} (no stored credentials/config)"
                )
            else:
                skip_msg = (
                    f"Skipping Account-{target} (no stored credentials/config, "
                    f"re-add with cswap --add-account --slot {target})"
                )
                if quiet:
                    self._logger.info(skip_msg)
                else:
                    print(
                        f"{accent('Skipping')} Account-{target} "
                        f"(no stored credentials/config, re-add with "
                        f"cswap --add-account --slot {target})"
                    )
            fallback = next(
                (
                    str(num)
                    for num in sequence
                    if str(num) != target and self._account_is_switchable(str(num))
                ),
                None,
            )
            if not fallback:
                raise ConfigError(
                    "No managed accounts have valid stored credentials/config. "
                    "Re-add a slot with: cswap --add-account --slot <number>"
                )
            target = fallback
        return target

    def _switch_bootstrap_identity(self) -> tuple[str, str] | None:
        """Shared switch preamble: managed accounts exist, org fields migrated."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")
        identity = self._get_current_account()
        self._get_sequence_data_migrated()
        return identity

    def _classify_switch_preconditions(self) -> SwitchPreconditions:
        """Classify shared switch preamble: identity, managed state, sequence size."""
        identity = self._switch_bootstrap_identity()

        if identity is None:
            return SwitchPreconditions(kind=SwitchPreconditionKind.FRESH_MACHINE)

        current_email, current_org_uuid = identity

        if not self._account_exists(current_email, current_org_uuid):
            return SwitchPreconditions(
                kind=SwitchPreconditionKind.UNMANAGED,
                identity=identity,
            )

        data = self._get_sequence_data() or {}
        sequence = data.get("sequence", [])

        if len(sequence) < 2:
            return SwitchPreconditions(
                kind=SwitchPreconditionKind.SINGLE_ACCOUNT,
                identity=identity,
                data=data,
                sequence=sequence,
                current_slot=self._find_account_slot(
                    data, current_email, current_org_uuid,
                ),
            )

        return SwitchPreconditions(
            kind=SwitchPreconditionKind.READY,
            identity=identity,
            data=data,
            sequence=sequence,
        )

    def _switch_unmanaged_notice(self, current_email: str) -> None:
        """Adopt the unmanaged active login and ask the user to re-run the switch."""
        print(f"{accent('Notice:')} Active account '{current_email}' was not managed.")
        self.add_account()
        data = self._get_sequence_data() or {}
        account_num = data.get("activeAccountNumber")
        print(f"It has been automatically added as Account-{account_num}.")
        print(dimmed("Please run the switch command again to switch to the next account."))

    def _switch_manual_rotation_target(
        self,
        sequence: list[Any],
        anchor: str | int | None,
        *,
        quiet: bool,
        skip_exhausted: bool = False,
        warnings: list[str] | None = None,
    ) -> tuple[str | None, bool]:
        """Return the next switchable slot in rotation order.

        The one sequence walk both switch entry points share: start after
        *anchor*, skip slots without stored credentials/config, and — with
        *skip_exhausted* — also skip slots at their 5h/7d limit. Skip notices
        go to *warnings* when given (the JSON path), else the logger when
        *quiet*, else stdout. Returns ``(target, hit_limit)`` where
        *hit_limit* records whether any candidate was passed over at its
        limit, so callers can tell "everything is rate-limited" apart from
        "nothing is switchable".
        """
        current_index = 0
        if anchor is not None:
            try:
                current_index = sequence.index(int(anchor))
            except (ValueError, TypeError):
                current_index = 0

        usage = self._usage_by_account() if skip_exhausted else {}

        def skip_notice(candidate: str, short: str, long: str) -> None:
            if warnings is not None:
                warnings.append(f"Skipped Account-{candidate} ({short})")
            elif quiet:
                self._logger.info("Skipping Account-%s (%s)", candidate, long)
            else:
                print(f"{accent('Skipping')} Account-{candidate} ({long})")

        hit_limit = False
        for offset in range(1, len(sequence)):
            candidate = str(sequence[(current_index + offset) % len(sequence)])
            if not self._account_is_switchable(candidate):
                skip_notice(
                    candidate,
                    "no stored credentials/config",
                    f"no stored credentials/config, re-add with "
                    f"cswap --add-account --slot {candidate}",
                )
                continue
            if skip_exhausted:
                room = headroom(usage.get(candidate))
                if room is not None and room <= 0:
                    hit_limit = True
                    skip_notice(candidate, "at 5h/7d limit", "at 5h/7d limit")
                    continue
            return candidate, hit_limit
        return None, hit_limit

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict[str, Any] | None:
        """Switch to specific account.

        ``force`` activates the target's stored credentials directly, skipping
        both the already-active no-op guard and the backup-current step —
        the recovery path for a live login gone stale (e.g. after --import).
        """
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
                data = self._get_sequence_data() or {}
                matches = [
                    num for num, acc in data.get("accounts", {}).items()
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

        data = self._get_sequence_data() or {}
        if target_account not in data.get("accounts", {}):
            raise AccountNotFoundError(f"Account-{target_account} does not exist")

        # Short-circuit a no-op before mutating (issue #79). A self-switch
        # would first back up the live credentials into the target slot —
        # destroying a freshly imported backup with a possibly stale login —
        # then read them straight back. It also re-writes credentials, takes
        # the lock, and (on macOS) touches the Keychain for nothing. --force
        # skips this guard on purpose: its job is to rewrite the live login
        # from the stored backup.
        if not force and data:
            identity = self._get_current_account()
            if identity is not None:
                cur_slot = self._find_account_slot(data, identity[0], identity[1])
                if cur_slot == target_account:
                    email = (
                        data.get("accounts", {}).get(target_account, {}).get("email", "")
                    )
                    if not json_output:
                        print(
                            f"{accent('Already on')} Account-{target_account} ({email})"
                        )
                        print(dimmed(
                            "To rewrite the live login from the stored backup "
                            "(e.g. after --import), run: "
                            f"cswap --switch-to {target_account} --force"
                        ))
                        return None
                    ref = account_ref(int(target_account), email)
                    return self._switch_noop(
                        strategy="direct",
                        reason="already-active",
                        from_ref=ref,
                        to_ref=ref,
                        message=f"Already on Account-{target_account} ({email})",
                    )

        op = self._perform_switch(
            target_account, emit_output=not json_output, force_activate=force
        )
        result = (
            self._switch_result_from_op(op or {}, "direct") if json_output else None
        )
        # A forced self-activation really rewrote the live credentials from the
        # stored backup — "already-active" would misdescribe that mutation.
        # A cross-slot force stays "switched": reason reports the outcome, not
        # the skipped-backup mechanism.
        if result is not None and force and not result["switched"]:
            to = result["to"]
            result["reason"] = "activated"
            result["message"] = (
                f"Activated Account-{to['number']} ({to['email']}) from stored backup"
            )
        return result

    def list_accounts(
        self,
        show_token_status: bool = False,
        show_health: bool = False,
        json_output: bool = False,
        fetch: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """List all managed accounts.

        ``fetch`` restricts which accounts *may* be fetched this pass (the TUI
        watch view's adaptive set); ``None`` — the CLI default — leaves every
        stale account eligible.
        """
        return run_list(
            self,
            show_token_status=show_token_status,
            show_health=show_health,
            json_output=json_output,
            fetch=fetch,
        )

    def status(self, json_output: bool = False) -> dict[str, Any] | None:
        """Display current account status."""
        return run_status(self, json_output=json_output)

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
    ) -> dict[str, Any] | bool | None:
        """Switch to another managed account.

        Returns ``True`` when credentials were activated on a different slot,
        ``False`` when no switch was needed.

        Pass an explicit intent:
          * ``ManualSwitchIntent()`` — interactive round-robin (default)
          * ``CliSwitchIntent(...)`` — CLI ``--switch`` (strategy / JSON)
        """
        if strategy is not None or json_output:
            return run_switch_cli(
                self, strategy=strategy, json_output=json_output,
            )
        else:
            if intent is None:
                intent = ManualSwitchIntent()

        quiet = intent.quiet

        preconditions = self._classify_switch_preconditions()

        # Fresh-machine path: no live Claude session, but we have managed accounts
        # (e.g. right after cswap --import). Activate the recorded
        # activeAccountNumber, or fall back to the first slot in sequence.
        # With no live state to capture, the target must have valid backups —
        # walk the sequence if the preferred target is broken.
        if preconditions.kind == SwitchPreconditionKind.FRESH_MACHINE:
            target = self._resolve_fresh_machine_target(quiet=quiet)
            self._perform_switch(target, intent=intent)
            return True

        assert preconditions.identity is not None
        current_email, current_org_uuid = preconditions.identity

        # Check if current account is managed
        if preconditions.kind == SwitchPreconditionKind.UNMANAGED:
            self._switch_unmanaged_notice(current_email)
            return False

        data = preconditions.data or {}
        sequence = preconditions.sequence or []

        if preconditions.kind == SwitchPreconditionKind.SINGLE_ACCOUNT:
            if quiet:
                raise SwitchError(_ONLY_ONE_ACCOUNT_MSG)
            print(dimmed(_ONLY_ONE_ACCOUNT_MSG))
            return False

        active_account = data.get("activeAccountNumber")

        next_account, _ = self._switch_manual_rotation_target(
            sequence, active_account, quiet=quiet,
        )

        if next_account is None:
            msg = (
                "No other accounts have valid stored credentials/config. "
                "Re-add a skipped slot with: cswap --add-account --slot <number>"
            )
            if quiet:
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
        data: dict[str, Any],
        target_account: str,
        target_email: str,
        config_path: Path,
        current_identity: tuple[str, str] | None,
        *,
        quiet: bool,
        force_refresh: bool,
        force_activate: bool = False,
    ) -> None:
        """Activate the target without backing up a prior account.

        Used when there is no live Claude session yet (e.g. right after import),
        claude-swap has no tracked active account (e.g. purge -> add-token ->
        switch-to while a live credential still exists), or ``force_activate``
        asked to rewrite the live login from the stored backup. Skips the
        back-up step so we never write account-None-* backups (nor, with force,
        poison the stored backup with stale live creds).
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
        # When a live session exists, fail fast if the creds snapshot is
        # unreadable rather than overwriting without a safety net. On the
        # fresh-machine path there is no prior login to restore, but we still
        # record whether the config pre-existed so a failure removes a config
        # we created rather than leaving it half-written.
        rollback_creds: str | None = None
        rollback_config_text: str | None = None
        config_preexisted = config_path.exists()
        if current_identity is not None:
            rollback_creds = self._read_credentials()
            if rollback_creds is None:
                raise CredentialReadError(
                    "Cannot snapshot live credentials before activation"
                )
        if config_preexisted:
            try:
                rollback_config_text = config_path.read_text(encoding="utf-8")
            except OSError as e:
                if current_identity is not None:
                    raise ConfigError(
                        f"Cannot snapshot live config before activation: {e}"
                    )
                # Fresh path: best-effort snapshot only.
                rollback_config_text = None

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

            self._sequence_store.save(
                SequenceData(data).set_active(int(target_account))
            )
        except Exception:
            if config_written:
                try:
                    if rollback_config_text is not None:
                        config_path.write_text(
                            rollback_config_text, encoding="utf-8"
                        )
                        if sys.platform != "win32":
                            os.chmod(config_path, 0o600)
                    elif not config_preexisted:
                        # We created this config on a fresh machine — remove it.
                        config_path.unlink(missing_ok=True)
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
            elif creds_written and current_identity is None:
                # Fresh machine: no prior login to restore and no safe
                # cross-platform delete of the just-written live creds. They
                # are the target's valid credentials; only the activation
                # record is incomplete — surface it so the user can verify.
                self._logger.warning(
                    "Fresh-machine activation of Account-%s failed after "
                    "writing live credentials; the live login now points at "
                    "the target. Verify with `cswap --status` or re-run the "
                    "switch to complete the activation record.",
                    target_account,
                )
            raise

        if force_activate and current_identity is not None:
            self._logger.info(
                f"Activated account {target_account} "
                "(forced, backup of current login skipped)"
            )
        else:
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
        data: dict[str, Any],
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
            # Step 1: Backup current account (under the switch lock already)
            original_creds = self._write_verified_live_account_credentials(
                current_account, current_email, original_creds,
                assume_locked=True,
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

            current_config_data = self._read_json(config_path) or {}
            current_config_data["oauthAccount"] = oauth_section

            self._write_json(config_path, current_config_data)
            transaction.record_step("config_written")
            self._logger.info("Updated config file")

            # Step 5: Update sequence state
            self._sequence_store.save(
                SequenceData(data).set_active(int(target_account))
            )
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
        force_activate: bool = False,
    ) -> dict[str, Any] | None:
        """Perform the actual account switch with transaction support.

        When ``emit_output`` is False (JSON mode) human output is suppressed and
        a result dict is returned for ``_switch_result_from_op``. The post-switch
        display runs after the lock releases so that persist callbacks inside
        list_accounts() can re-acquire it.

        ``force_activate`` routes through the direct activation path even when a
        managed live login exists: the stored backup is written over the live
        credentials without backing the live ones up first (post-import recovery
        when the live login is stale).
        """
        if intent is None:
            intent = ManualSwitchIntent()
        quiet = intent.quiet or not emit_output
        force_refresh = intent.force_refresh

        self._warn_switch_session_hazards(target_account, quiet=quiet)

        # Beyond cswap's own lock, hold Claude Code's advisory locks for the
        # whole mutation (including rollback paths): its token refresh runs
        # under ~/.claude.lock and re-reads credentials there — holding it
        # means a mid-refresh Claude Code either finishes before our swap
        # (backup captures the rotated token) or re-checks after it and aborts.
        # ~/.claude.json.lock likewise keeps the oauthAccount splice from
        # interleaving with Claude Code's own config writes. Everything under
        # here is local I/O — no network while locks are held.
        with FileLock(self.lock_file), claude_credentials_lock(), claude_config_lock():
            data = self._get_sequence_data()
            if not data or target_account not in data.get("accounts", {}):
                raise ConfigError(
                    f"Account-{target_account} not found in managed accounts "
                    "(sequence file missing, corrupt, or out of sync)"
                )
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

            # Direct activation path: there is no live Claude session yet
            # (e.g. right after import), claude-swap has no tracked active
            # account yet (e.g. purge -> add-token -> switch-to while a live
            # Claude credential still exists), or --force asked to rewrite the
            # live login from the stored backup. In all cases, skip the
            # back-up-current step: it would either write account-None-*
            # backups or (force) poison the stored backup with stale creds.
            if force_activate or current_identity is None or current_account is None:
                self._activate_target_directly(
                    data,
                    target_account,
                    target_email,
                    config_path,
                    current_identity,
                    quiet=quiet,
                    force_refresh=force_refresh,
                    force_activate=force_activate,
                )
                if not emit_output:
                    # Account left: None on a fresh machine (no live account at
                    # all); an unnumbered ref for an unmanaged live account (slot
                    # unknown to cswap); a numbered ref when --force ran with a
                    # managed live login.
                    if current_identity is None:
                        from_ref = None
                    elif current_account is None:
                        from_ref = account_ref(None, current_identity[0])
                    else:
                        from_ref = account_ref(int(current_account), current_identity[0])
                    return {
                        "from": from_ref,
                        "to": account_ref(int(target_account), target_email),
                        "warnings": [],
                    }
                # Defer the human result to AFTER the lock: _print_switch_result
                # runs list_accounts(), whose persist callbacks re-acquire this
                # (non-reentrant) lock.
                direct_activation = True
            else:
                direct_activation = False
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
        if direct_activation:
            if not quiet:
                self._print_switch_result(target_account, target_email)
            return None
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

    def _purge_guard_live_sessions(self) -> list[Path]:
        """Refuse purge while session-mode Claude is running; else return session dirs."""
        sessions_root = self.backup_dir / "sessions"
        session_dirs = (
            [d for d in sessions_root.iterdir() if d.is_dir()]
            if sessions_root.is_dir()
            else []
        )
        from claude_swap.session import live_sessions_for

        live: dict[str, list[int]] = {}
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
        return session_dirs

    def _purge_remove_account_credentials(
        self, data: dict[str, Any] | None, removed_items: list[str]
    ) -> None:
        # On macOS backups may be in the Keychain and/or .enc files
        # (auto-fallback), and the per-process capability cache says nothing
        # about which backend *past* runs wrote — clean both unconditionally
        # rather than route through it. Linux/WSL/Windows are file-only.
        if not data:
            return
        for account_num, account_info in data.get("accounts", {}).items():
            email = account_info.get("email", "")
            nums = [account_num]
            if str(account_num) != "None":
                nums.append("None")
            usernames = [f"account-{num}-{email}" for num in nums]

            # .enc files (Linux/WSL/Windows always; macOS fallback copies).
            for num in nums:
                cred_file = self.credentials_dir / f".creds-{num}-{email}.enc"
                try:
                    if cred_file.exists():
                        cred_file.unlink()
                        removed_items.append(f"Credential file: {cred_file.name}")
                except Exception:
                    pass

            # macOS Keychain items via `security` (current macOS backend).
            if self.platform == Platform.MACOS:
                for username in usernames:
                    try:
                        macos_keychain.delete_password(SECURITY_SERVICE, username)
                        removed_items.append(f"Credential: {username}")
                    except Exception:
                        pass

            # Best-effort sweep of any pre-migration keyring / Credential
            # Manager entries left behind by an incomplete migration.
            # Linux/WSL never used a keyring backend.
            if self.platform in (Platform.MACOS, Platform.WINDOWS):
                _sweep_legacy_keyring(usernames, removed_items)

    def purge(self) -> None:
        """Remove all traces of claude-swap from the system.

        This removes:
        - All stored account credentials (``.enc`` files on Linux/WSL/Windows; on
          macOS both the Keychain items via ``security`` and any fallback ``.enc``
          files), plus a best-effort sweep of any pre-migration keyring / Windows
          Credential Manager entries left behind
        - The active backup directory (XDG path on Linux/WSL, ~/.claude-swap-backup elsewhere)
        - Any stale legacy ~/.claude-swap-backup directory left around from
          before the XDG migration
        """
        legacy = get_legacy_backup_root()
        legacy_distinct = legacy != self.backup_dir
        session_dirs = self._purge_guard_live_sessions()

        warning("This will remove ALL claude-swap data from your system:")
        print(f"  - Backup directory: {self.backup_dir}")
        if legacy_distinct and legacy.exists():
            print(f"  - Legacy backup directory: {legacy}")
        if self.platform == Platform.MACOS:
            print("  - All stored account credentials (macOS Keychain and/or files)")
        else:
            print("  - All stored account credential files")
        if session_dirs:
            print("  - All session profiles and their Keychain entries")
        print()
        print(dimmed("Note: This does NOT affect your current Claude Code login."))
        print()

        confirm = input("Are you sure you want to purge all data? [y/N] ")
        if confirm.lower() != "y":
            print(dimmed("Cancelled"))
            return

        removed_items: list[str] = []
        self._purge_remove_account_credentials(self._get_sequence_data(), removed_items)
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
