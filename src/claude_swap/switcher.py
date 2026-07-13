"""Core account switcher logic for Claude Code."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import macos_keychain, oauth

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
    ONLY_ONE_ACCOUNT_MSG,
    AccountSnapshot,
    AccountsSnapshot,
    ManualSwitchIntent,
    Platform,
    SwitchIntent,
    SwitchPreconditionKind,
    SwitchPreconditions,
    SwitchTransaction,
    get_timestamp,
    normalize_alias,
)
from claude_swap.printer import (
    accent,
    bolded,
    dimmed,
    muted,
    warning,
)
from claude_swap.paths import (
    get_backup_root,
    get_credentials_path,
    get_global_config_path,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)
from claude_swap import poll_policy
from claude_swap.credential_refresh import CredentialRefresher
# SENTINEL_NOTES / last_seen_note are re-exported for the TUI, which reads
# them from here (the upstream location) so both surfaces share one wording.
from claude_swap.list_reporter import SENTINEL_NOTES as SENTINEL_NOTES
from claude_swap.list_reporter import ListReporter, run_list, run_status
from claude_swap.list_reporter import last_seen_note as last_seen_note
from claude_swap.settings import load_settings, parse_model_names, settings_path
from claude_swap.sequence_store import (
    AccountRecord,
    SequenceData,
    SequenceStore,
)
from claude_swap.switch_cli import run_switch_cli
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore
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
        # (settings mtime, (threshold, models)) — see _poll_policy_inputs.
        self._poll_inputs_cache: tuple[float | None, tuple[float, tuple[str, ...]]] | None = None
        self._poll_inputs_override: tuple[float, tuple[str, ...]] | None = None

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

    def _prune_mappings(self, email: str, org_uuid: str) -> None:
        """Drop directory mappings for an identity that no longer has a slot.

        Called wherever an identity leaves the account table for good
        (remove_account, add_account/add_token slot overwrite). Slot
        *migration* and --import --force keep the (email, org) identity that
        mappings are keyed by, so they need no pruning.
        """
        from claude_swap.mappings import MappingStore

        pruned = MappingStore(self.backup_dir).prune_account(email, org_uuid or "")
        if pruned:
            print(dimmed(f"Removed {pruned} directory mapping(s) for this account"))

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

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        """Set (or rename) the alias for the account matching identifier.

        ``identifier`` is a slot number, email, or existing alias (so a
        typo'd alias can be corrected with ``cswap alias <old> <new>`` as
        well as by number/email). Returns ``(account_num, normalized_alias)``.

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
            ValidationError: alias format is invalid.
            ConfigError: the normalized alias is already used by another account.
        """
        try:
            normalized = normalize_alias(alias)
        except ValueError as e:
            raise ValidationError(str(e)) from e

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

        conflict = self._alias_in_use(normalized, exclude_num=account_num)
        if conflict is not None:
            raise ConfigError(f"Alias '{normalized}' is already used by account {conflict}")

        record["alias"] = normalized
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)
        return account_num, normalized

    def unset_alias(self, identifier: str) -> str:
        """Clear the alias for the account matching identifier.

        Returns the account number. Idempotent: clearing an already-unset
        alias succeeds silently (no error), matching ``cswap config unset``'s
        posture of "the end state is what you asked for".

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
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

        if "alias" in record:
            del record["alias"]
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
        return account_num

    def list_aliases(self) -> list[tuple[str, str, str]]:
        """Every set alias as ``(account_num, alias, email)``, slot-number order."""
        data = self._get_sequence_data_migrated()
        accounts = (data or {}).get("accounts", {})
        rows = [
            (num, acc.get("alias"), acc.get("email", ""))
            for num, acc in accounts.items()
            if acc.get("alias")
        ]
        return sorted(rows, key=lambda r: int(r[0]))

    def slot_for_directory(self, directory: str | Path) -> tuple[str | None, str | None]:
        """Resolve a directory to its mapped account slot, for `cswap run`.

        Returns (slot, email): (None, None) when no mapping covers the
        directory, (None, email) when a mapping exists but its account was
        removed, and (slot, email) when the mapping resolves.
        """
        from claude_swap.mappings import MappingStore

        match = MappingStore(self.backup_dir).resolve(directory)
        if match is None:
            return None, None
        _, entry = match
        email = entry.get("email", "")
        seq = self._get_sequence_data_migrated() or {}
        slot = self._find_account_slot(
            seq, email, entry.get("organizationUuid", "") or ""
        )
        return slot, email

    def list_mappings(self) -> None:
        """Print all directory → account mappings (for `cswap map`)."""
        from claude_swap.mappings import MappingStore

        mappings = MappingStore(self.backup_dir).all()
        if not mappings:
            print(dimmed("No directory mappings yet."))
            print(muted("Map one with: cswap map <NUM|EMAIL> [PATH]"))
            return
        seq = self._get_sequence_data_migrated() or {}
        print(bolded("Directory mappings:"))
        for path in sorted(mappings):
            entry = mappings[path]
            email = entry.get("email", "")
            org_uuid = entry.get("organizationUuid", "") or ""
            slot = self._find_account_slot(seq, email, org_uuid)
            if slot:
                account = seq.get("accounts", {}).get(slot, {})
                tag = self._get_display_tag(
                    email, account.get("organizationName", ""), org_uuid
                )
                print(f"  {path} {dimmed('→')} {slot}: {email} {muted(f'[{tag}]')}")
            else:
                print(f"  {path} {dimmed('→')} {email} {muted('(account removed)')}")

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

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        """One-pass structured snapshot of every managed account, for the TUI.

        Metadata, active-slot detection, and usage entries all come from a
        single ``_build_accounts_info`` + ``_collect_usage_entries`` pass, so
        the view is coherent — two separate calls could interleave with other
        collectors and disagree about the active slot or freshness. ``fetch``
        has ``_collect_usage_entries`` semantics: ``None`` makes every stale
        account eligible; a set restricts which accounts *may* be fetched
        this pass.
        """
        reporter = self._list_reporter()
        accounts_info = reporter.build_accounts_info()
        entries = reporter.collect_usage_entries(accounts_info, fetch=fetch)
        active_number: str | None = None
        accounts: list[AccountSnapshot] = []
        for num, email, org_name, org_uuid, is_active, _creds, alias in accounts_info:
            n = str(num)
            if is_active:
                active_number = n
            accounts.append(
                AccountSnapshot(
                    number=n,
                    email=email,
                    org_name=org_name,
                    org_uuid=org_uuid,
                    is_active=is_active,
                    kind=self._account_kind(n),
                    switchable=self._account_is_switchable(n),
                    usage=entries[n],
                    alias=alias,
                )
            )
        return AccountsSnapshot(
            active_number=active_number,
            accounts=tuple(accounts),
            taken_at=self._usage_store.clock(),
        )

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

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        """Pin the threshold/models poll planning keys on (set by a hosted
        auto engine so cadence follows its effective, CLI-merged settings
        instead of the settings file)."""
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        """Drop the hosted engine's pin so poll planning falls back to the
        settings file — called when the engine's screen closes, or a TUI
        session threshold override would keep steering cadence after the
        engine it belonged to is gone."""
        self._poll_inputs_override = None

    def _poll_policy_inputs(self) -> tuple[float, tuple[str, ...]]:
        """Threshold + configured model names for poll planning: the hosting
        engine's pinned values when present, else the settings file (reloaded
        only when it changes — one stat per pass)."""
        if self._poll_inputs_override is not None:
            return self._poll_inputs_override
        path = settings_path(self.backup_dir)
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        if self._poll_inputs_cache is not None and self._poll_inputs_cache[0] == mtime:
            return self._poll_inputs_cache[1]
        loaded = load_settings(self.backup_dir)
        inputs = (loaded.threshold, parse_model_names(loaded.model))
        self._poll_inputs_cache = (mtime, inputs)
        return inputs

    def switchable_account_numbers(self) -> list[str]:
        """Account numbers in rotation order eligible for automatic selection.

        Excludes slots without usable stored backups and slots the user has
        disabled (``cswap disable``). Disabled slots stay managed and remain
        valid explicit ``cswap switch <num|email>`` targets — they are only
        held out of automatic rotation and the usage-aware strategies.
        """
        data = self._get_sequence_data() or {}
        return [
            str(num)
            for num in data.get("sequence", [])
            if self._account_is_switchable(str(num))
            and not self._disabled_from_data(data, str(num))
        ]

    @staticmethod
    def _disabled_from_data(data: dict[str, Any], account_num: str) -> bool:
        """Whether a slot is flagged out of rotation in already-loaded data."""
        record = data.get("accounts", {}).get(str(account_num))
        return bool(record and record.get("disabled"))

    def is_account_disabled(self, account_num: str) -> bool:
        """Whether a slot is currently held out of rotation."""
        data = self._get_sequence_data() or {}
        return self._disabled_from_data(data, str(account_num))

    def disabled_account_numbers(self) -> list[str]:
        """Managed slots the user has disabled, in sequence order."""
        data = self._get_sequence_data() or {}
        return [
            str(num)
            for num in data.get("sequence", [])
            if self._disabled_from_data(data, str(num))
        ]

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        """Hold an account out of rotation (``disabled=True``) or return it.

        Disabling only affects automatic selection — the auto-switch engine,
        bare ``cswap switch`` rotation, and the ``best`` / ``next-available``
        strategies all skip disabled slots. The account stays managed and is
        still a valid explicit ``cswap switch <num|email>`` target, so you can
        park an account without losing its stored login. Re-enabling restores
        it to rotation in its original sequence position.

        Raises:
            ConfigError: no accounts are managed yet, or the email is ambiguous.
            AccountNotFoundError: identifier doesn't match any account.
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # resolve_account migrates org fields and hard-errors on ambiguity.
        account_num, email, _ = self.resolve_account(identifier)

        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(account_num)
        if not record:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        verb = "disabled" if disabled else "enabled"
        if bool(record.get("disabled")) == disabled:
            print(dimmed(f"Account-{account_num} ({email}) is already {verb}."))
            return

        if disabled:
            record["disabled"] = True
        else:
            record.pop("disabled", None)
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)
        self._logger.info(f"{verb.capitalize()} account {account_num}: {email}")

        print(f"{accent(verb.capitalize())} Account-{account_num} ({email}).")

        if disabled:
            active = data.get("activeAccountNumber")
            if str(active) == account_num:
                print(dimmed(
                    "  It is the active account — it stays live until you switch "
                    "away; it just won't be an automatic switch target."
                ))
            if not self.switchable_account_numbers():
                warning(
                    "  No accounts remain in rotation — auto-switch and bare "
                    "switch have nothing to pick. Re-enable one with "
                    "cswap enable <num|email>."
                )
        else:
            print(dimmed("  It is back in the rotation."))

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

    def account_identity(self, account_num: str) -> dict[str, str]:
        """Stored identity for a slot: ``{"email", "organizationUuid", "uuid"}``."""
        data = self._get_sequence_data() or {}
        acct = data.get("accounts", {}).get(str(account_num), {})
        return {
            "email": acct.get("email", ""),
            "organizationUuid": acct.get("organizationUuid", "") or "",
            "uuid": (acct.get("uuid") or "").strip(),
        }

    def backfill_account_uuid(self, account_num: str, uuid: str) -> None:
        """Record a resolved account uuid on a slot that lacks one.

        Only ever fills an empty uuid (add-token placeholders) — an existing
        uuid is identity and is never rewritten here. Caller must NOT hold
        ``self.lock_file``.
        """
        if not uuid:
            return
        with FileLock(self.lock_file):
            data = self._get_sequence_data() or {}
            acct = data.get("accounts", {}).get(str(account_num))
            if acct is not None and not (acct.get("uuid") or "").strip():
                acct["uuid"] = uuid
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)

    def list_unclaimed_credentials(self) -> dict[str, dict[str, Any]]:
        """Internal safety copies preserved at switch time (diagnostics only).

        Write-only storage: entries are created when a switch displaces live
        credential bytes it could not attribute to the outgoing slot, and are
        never consumed automatically — recovery from any such state is the
        documented ``/login`` + ``cswap add [--slot N]``.
        """
        return self._store._list_unclaimed_credentials()

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

    def _find_account_by_alias(self, alias: str) -> str | None:
        """Return the account number whose alias matches (case-insensitive), if any.

        An empty ``alias`` never matches: accounts without one store no
        ``alias`` key, and comparing against an empty string would otherwise
        match the first aliasless account.
        """
        if not alias:
            return None
        data = self._get_sequence_data()
        if not data:
            return None
        alias_key = alias.lower()
        for num, account in data.get("accounts", {}).items():
            if (account.get("alias") or "").lower() == alias_key:
                return str(num)
        return None

    def _alias_in_use(self, alias: str, *, exclude_num: str | None = None) -> str | None:
        """Return the account number already using ``alias`` (other than ``exclude_num``), if any."""
        num = self._find_account_by_alias(alias)
        if num is not None and num == exclude_num:
            return None
        return num

    def _resolve_account_identifier(self, identifier: str) -> str | None:
        """Resolve account identifier (number, alias, or email) to account number.

        Resolution precedence: number -> alias -> email.

        Raises:
            ConfigError: if the email matches multiple accounts (ambiguous).
        """
        if identifier.isdigit():
            return identifier

        data = self._get_sequence_data()
        if not data:
            return None

        alias_match = self._find_account_by_alias(identifier)
        if alias_match is not None:
            return alias_match

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
        self,
        email: str,
        org_uuid: str,
        slot: int | None,
        assume_yes: bool = False,
    ) -> tuple[str, tuple[str, str, str] | None, str | None] | None:
        """Resolve where a new/moved account should land.

        Returns ``(account_num, displace_slot, migrate_from)`` or ``None`` when
        the user declines an overwrite prompt. ``displace_slot`` carries
        ``(num, email, org_uuid)`` so cleanup can prune directory mappings.
        No destructive work is done here. ``assume_yes`` skips the
        occupied-slot overwrite prompt (callers with their own confirmation
        UI, e.g. the TUI, confirm before calling).
        """
        displace_slot: tuple[str, str, str] | None = None
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
                if not assume_yes:
                    warning(f"Slot {slot} already occupied")
                    print(f"{existing_email} {muted(f'[{existing_tag}]')}")
                    try:
                        answer = input(
                            f"Overwrite slot {slot}? [y/N] "
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print(f"\n{dimmed('Cancelled')}")
                        return None
                    if answer not in ("y", "yes"):
                        print(dimmed("Cancelled"))
                        return None
                displace_slot = (
                    account_num,
                    existing_email,
                    existing.get("organizationUuid", "") or "",
                )

        return account_num, displace_slot, migrate_from

    def _apply_slot_displacement(
        self,
        displace_slot: tuple[str, str, str] | None,
        migrate_from: str | None,
        slot: int | None,
    ) -> None:
        """Delete the slot(s) freed by an overwrite/move, in sequence.json."""
        if displace_slot:
            d_num, d_email, d_org = displace_slot
            self._delete_account_files(d_num, d_email)
            self._sequence_store.save(
                self._sequence_store.load_or_empty().remove_slot(d_num)
            )
            self._prune_mappings(d_email, d_org)

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

    def add_account(
        self,
        slot: int | None = None,
        assume_yes: bool = False,
        alias: str | None = None,
    ) -> None:
        """Add current account to managed accounts.

        Args:
            slot: Specify the slot number to store the account in.
                  When None, auto-assigns the next available number.
                  When specified, prompts for confirmation if the slot
                  is already occupied by a different account.
            assume_yes: Skip that overwrite prompt (callers with their own
                  confirmation UI, e.g. the TUI, confirm before calling).
            alias: Optional short display alias to set on this account.
                  When omitted, an existing alias on the slot is preserved.
        """
        self._setup_directories()
        self._init_sequence_file()
        self._migrate_org_fields()

        if alias is not None:
            try:
                alias = normalize_alias(alias)
            except ValueError as e:
                raise ValidationError(str(e)) from e

        identity = self._get_current_account()
        if identity is None:
            raise ConfigError("No active Claude account found. Please log in first.")
        current_email, current_org_uuid = identity

        # When no slot specified and account already exists, refresh credentials in place
        if slot is None and self._account_exists(current_email, current_org_uuid):
            if alias is not None:
                seq = self._get_sequence_data() or {}
                conflict = self._alias_in_use(
                    alias,
                    exclude_num=self._find_account_slot(
                        seq, current_email, current_org_uuid
                    ),
                )
                if conflict is not None:
                    raise ValidationError(
                        f"Alias '{alias}' is already used by account {conflict}"
                    )

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
                if alias is not None:
                    data.accounts[account_num].raw["alias"] = alias
                self._sequence_store.save(data.set_active(int(account_num)))
            # A refreshed credential invalidates any dead-token quarantine on
            # this slot; otherwise the stale strike row keeps the account stuck
            # at "re-login needed" and it never fetches the new token.
            self._usage_store.clear_dead_token(
                [account_num], {account_num: (current_email, current_org_uuid)}
            )


            tag = self._get_display_tag(current_email, matched_org_name, current_org_uuid)
            self._logger.info(f"Updated credentials for account {account_num}: {current_email}")
            print(
                f"{accent('Updated credentials')} for Account {account_num} "
                f"({current_email} {muted(f'[{tag}]')})."
            )
            return

        # Determine slot number and collect confirmation decisions
        # (no destructive operations until new account is verified readable)
        resolved = self._resolve_target_slot(
            current_email, current_org_uuid, slot, assume_yes=assume_yes
        )
        if resolved is None:
            return
        account_num, displace_slot, migrate_from = resolved
        seq_raw = self._get_sequence_data() or {}

        # Capture any alias to carry forward before destructive cleanup below
        # deletes the old record (same account moving slots, or refreshing in place).
        existing_alias = None
        if slot is not None:
            prior = seq_raw.get("accounts", {}).get(account_num) or {}
            if (
                prior.get("email") == current_email
                and prior.get("organizationUuid", "") == current_org_uuid
            ):
                existing_alias = prior.get("alias")
            if migrate_from:
                existing_alias = (
                    seq_raw["accounts"][migrate_from].get("alias")
                    or existing_alias
                )

        if alias is not None:
            conflict = self._alias_in_use(alias, exclude_num=account_num)
            if conflict is not None:
                raise ValidationError(
                    f"Alias '{alias}' is already used by account {conflict}"
                )

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

            # Update sequence.json (carrying any alias forward: an explicit
            # --alias wins, else the alias the displaced/moved record held).
            record = AccountRecord.create(
                email=current_email,
                uuid=account_uuid,
                organization_uuid=organization_uuid,
                organization_name=organization_name,
            )
            carried_alias = alias if alias is not None else existing_alias
            if carried_alias:
                record.raw["alias"] = carried_alias
            self._register_account_slot(account_num, record, set_active=True)
        # Registering a slot with a fresh credential lifts any dead-token
        # quarantine carried by that slot's prior lineage.
        self._usage_store.clear_dead_token(
            [account_num], {account_num: (current_email, organization_uuid)}
        )
        tag = self._get_display_tag(current_email, organization_name, organization_uuid)
        self._logger.info(f"Added account {account_num}: {current_email} (org: {organization_uuid or 'personal'})")
        print(f"{accent('Added')} Account {account_num}: {current_email} {muted(f'[{tag}]')}")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
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
            assume_yes: Skip the occupied-slot overwrite prompt (callers with
                   their own confirmation UI, e.g. the TUI, confirm first).
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
            # A refreshed credential invalidates any dead-token quarantine on this
            # slot (mirrors ``add_account``); otherwise the stale strike row keeps
            # the account stuck at "re-login needed" and it never fetches the new
            # token. Token accounts are always personal, so org is "".
            self._usage_store.clear_dead_token(
                [account_num], {account_num: (email, "")}
            )
            self._logger.info(f"Updated {kind_label} for account {account_num}: {email}")
            print(
                f"{accent(f'Updated {kind_label}')} for Account {account_num} "
                f"({email} {muted('[personal]')})."
            )
            return

        resolved = self._resolve_target_slot(email, "", slot, assume_yes=assume_yes)
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
        # Reusing/overwriting a slot with a fresh credential lifts any dead-token
        # quarantine carried by that slot's prior lineage (mirrors ``add_account``).
        self._usage_store.clear_dead_token(
            [account_num], {account_num: (email, "")}
        )
        self._logger.info(f"Added account {account_num} from {source_label}: {email}")
        if migrate_from:
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")
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
            is_alias = self._find_account_by_alias(identifier) is not None
            if not is_alias and not self._validate_email(identifier):
                raise ValidationError(f"Invalid account identifier: {identifier}")

            # For email identifiers, handle ambiguous matches interactively.
            # Aliases are unique by construction, so they never hit this.
            if not is_alias:
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
        self._prune_mappings(email, account_info.get("organizationUuid", ""))

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

    def _persist_poll_plans(
        self,
        records: dict[str, FetchRecord],
        pre: dict[str, UsageEntry],
        post: dict[str, UsageEntry],
        info_by_num: dict[str, tuple[int, str, str, str, bool, str, str]],
        identities: dict[str, tuple[str, str]],
    ) -> None:
        """Adapt and persist the cadence of every slot just fetched
        successfully, so the next collector — whichever surface it runs in —
        inherits the plan. Failures are paced by the store's backoff instead
        and keep their (now past-due) plan for when the backoff lifts."""
        now = self._usage_store.clock()
        threshold, models = self._poll_policy_inputs()
        plans: dict[str, tuple[float | None, float | None]] = {}
        for num, rec in records.items():
            if rec.sentinel is not None or rec.error is not None:
                continue
            before, after = pre.get(num), post.get(num)
            if after is None or after.fetched_at is None:
                continue
            recent_429 = (
                before is not None
                and before.last_429_at is not None
                and (now - before.last_429_at) < poll_policy.RECENT_429_WINDOW_S
            )
            plans[num] = poll_policy.plan_after_fetch(
                prev_interval_s=before.poll_interval_s if before else None,
                prev_usage=before.last_good if before else None,
                new_usage=after.last_good,
                is_active=bool(info_by_num[num][4]),
                threshold=threshold,
                models=models,
                recent_429=recent_429,
                now=now,
            )
        if plans:
            self._usage_store.set_poll_plan(plans, identities)

    def _replan_new_active(self, number: str, email: str, org_uuid: str) -> None:
        """Pull the just-activated account's poll plan to the active floor.

        Its stored plan was computed while it was an idle candidate and may
        wait up to CANDIDATE_MAX_INTERVAL_S — too slow for the account whose
        usage is about to move. The deadline anchors on the last measurement
        (an already-old one comes due immediately, a never-measured account
        is left plan-less so nothing blocks its first fetch), and the next
        poll is only ever pulled earlier, never pushed later. Best-effort by
        contract: the switch this rides on has already committed, so a cache
        hiccup here must not surface as a switch failure."""
        try:
            identities = {number: (email, org_uuid or "")}
            now = self._usage_store.clock()
            entry = self._usage_store.entries(identities).get(number)
            if entry is None or entry.fetched_at is None:
                return
            next_poll = max(now, entry.fetched_at + poll_policy.MIN_INTERVAL_S)
            if entry.next_poll_at is not None and entry.next_poll_at <= next_poll:
                return
            self._usage_store.set_poll_plan(
                {number: (next_poll, poll_policy.MIN_INTERVAL_S)}, identities
            )
        except Exception as e:
            self._logger.warning(
                f"Post-switch poll re-plan failed (switch itself succeeded): {e}"
            )

    def _usage_by_account(self) -> dict[str, dict[str, Any] | str | None]:
        """Map account number → decision-grade usage value for managed accounts."""
        reporter = self._list_reporter()
        accounts_info = reporter.build_accounts_info()
        entries = reporter.collect_usage_entries(accounts_info)
        return {num: entry.decision_value() for num, entry in entries.items()}

    def _warn_inert_models(
        self,
        usage: dict[str, Any],
        models: tuple[str, ...],
        json_output: bool,
        warnings: list[str],
    ) -> None:
        """One-shot typo guard for --model on the manual strategies.

        A configured name that no account reports gates nothing while looking
        active. Only claimed when every account's usage is readable (an
        unreadable account could be the one carrying the window)."""
        wanted = {m.lower(): m for m in models if m.lower() != "all"}
        if not wanted or not usage:
            return
        if any(not isinstance(v, dict) for v in usage.values()):
            return
        seen = {
            s["name"].lower()
            for v in usage.values()
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        missing = [name for low, name in wanted.items() if low not in seen]
        if not missing:
            return
        msg = (
            f"model(s) {', '.join(missing)} match no account's usage windows "
            "(typo?)"
        )
        if json_output:
            warnings.append(msg)
        else:
            warning(msg)

    def _select_best_switchable(
        self,
        current_num: str | None,
        models: tuple[str, ...] = (),
        usage: dict[str, dict[str, Any] | str | None] | None = None,
    ) -> tuple[str | None, str]:
        """Decide the ``best`` strategy target relative to the current account.

        Compares the rate-limit headroom of every *other* switchable account
        against the current one and only recommends a switch it can *prove*
        lands on strictly more headroom — never onto an account worse than (or
        merely unverifiable against) where the user already is. When a switch
        can't be proven beneficial, it stays put; bare ``cswap --switch``
        remains the way to force a plain rotation. ``models`` folds the named
        per-model weekly windows into every headroom comparison (see
        ``oauth.account_headroom``). Returns ``(target, note)``:

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
            if str(n) != str(current_num)
            and self._account_is_switchable(str(n))
            and not self._disabled_from_data(data, str(n))
        ]
        if not others:
            return None, "none"

        if usage is None:
            usage = self._usage_by_account()
        current_value = usage.get(str(current_num))
        current_headroom = oauth.account_headroom(
            current_value if isinstance(current_value, dict) else None, models
        )
        if current_headroom is None:
            # Can't measure where the user is → can't prove any target is
            # better. Stay rather than risk moving onto a worse account.
            return None, "current-unavailable"

        scored = [
            (
                oauth.account_headroom(
                    v if isinstance(v := usage.get(num), dict) else None, models
                ),
                num,
            )
            for num in others
        ]
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
        models: tuple[str, ...] = (),
        usage: dict[str, dict[str, Any] | str | None] | None = None,
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

        # Only fetch usage when needed; an empty map means the headroom check
        # below is always None (skipped), preserving the non-usage-aware path.
        if usage is None:
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
            if self._disabled_from_data(
                self._get_sequence_data() or {}, candidate
            ):
                skip_notice(candidate, "disabled", "disabled")
                continue
            if not self._account_is_switchable(candidate):
                skip_notice(
                    candidate,
                    "no stored credentials/config",
                    f"no stored credentials/config, re-add with "
                    f"cswap --add-account --slot {candidate}",
                )
                continue
            if skip_exhausted:
                cand_value = usage.get(candidate)
                cand_usage = cand_value if isinstance(cand_value, dict) else None
                room = oauth.account_headroom(cand_usage, models)
                if room is not None and room <= 0:
                    hit_limit = True
                    label = "5h/7d"
                    if models:
                        # Name what actually binds ("Fable", "5h/Fable", ...)
                        # so a config-driven skip is never mysterious.
                        at = [
                            name
                            for name, pct, _ in oauth.relevant_windows(
                                cand_usage, models
                            )
                            if pct >= 100.0
                        ]
                        if at:
                            label = "/".join(at)
                    skip_notice(
                        candidate, f"at {label} limit", f"at {label} limit"
                    )
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
            is_alias = self._find_account_by_alias(identifier) is not None
            if not is_alias and not self._validate_email(identifier):
                raise ValidationError(f"Invalid account identifier: {identifier}")

            # For email identifiers, handle ambiguous matches interactively —
            # except in JSON mode, where we never prompt. There we fall through
            # to _resolve_account_identifier, which raises a ConfigError listing
            # the matching slots (+ org labels) → structured error envelope.
            # Aliases are unique by construction, so they never hit this.
            if not json_output and not is_alias:
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
        # from the stored backup. Provenance-aware (issue #117): the no-op is
        # only taken when the live credential matches the slot's backup or
        # the divergence can't be classified — pre-fix behavior, silent — and
        # a *resolved* divergence falls through so _perform_switch can
        # reconcile it.
        provenance: dict[str, Any] | None = None
        if not force and data:
            identity = self._get_current_account()
            if identity is not None:
                cur_slot = self._find_account_slot(data, identity[0], identity[1])
                if cur_slot == target_account:
                    action, provenance = self._self_switch_action(
                        target_account, identity[0]
                    )
                if cur_slot == target_account and action != "reconcile":
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
            target_account,
            emit_output=not json_output,
            force_activate=force,
            provenance=provenance,
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
        models: tuple[str, ...] = (),
        model_source: str | None = None,
    ) -> dict[str, Any] | bool | None:
        """Switch to another managed account.

        Returns ``True`` when credentials were activated on a different slot,
        ``False`` when no switch was needed.

        ``models`` folds the named per-model weekly windows into every usage
        comparison of the usage-aware strategies (see
        ``oauth.relevant_windows``); ``model_source`` records where they came
        from (``"cli"`` or ``"autoswitch.model"``) so the choice is announced,
        never silent.

        Pass an explicit intent:
          * ``ManualSwitchIntent()`` — interactive round-robin (default)
          * ``CliSwitchIntent(...)`` — CLI ``--switch`` (strategy / JSON)
        """
        if strategy is not None or json_output:
            return run_switch_cli(
                self, strategy=strategy, json_output=json_output,
                models=models, model_source=model_source,
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
                raise SwitchError(ONLY_ONE_ACCOUNT_MSG)
            print(dimmed(ONLY_ONE_ACCOUNT_MSG))
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
        warnings_out: list[str] | None = None,
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

        # Invariant II (issue #117): this path skips the backup step, so the
        # live credential it replaces would otherwise have no surviving copy —
        # stash it first. For an unmanaged login the stash is the only copy
        # anywhere; for --force it guards against the "stale" live login
        # actually being the fresher generation. A failed stash aborts,
        # except under --force where the user explicitly asked for the
        # overwrite.
        if (
            rollback_creds
            and rollback_creds != target_creds
            and current_identity is not None
        ):
            current_slot = self._find_account_slot(
                data, current_identity[0], current_identity[1]
            )
            try:
                self._stash_live_credential(
                    rollback_creds,
                    "displaced-live-login",
                    current_slot or "unmanaged",
                    None,
                )
            except Exception as e:
                if not force_activate:
                    raise SwitchError(
                        "Could not preserve the live credential before "
                        f"activation (safety-copy write failed: {e}); "
                        "aborting rather than destroying it"
                    )
                msg = (
                    "Could not preserve the replaced live credential "
                    f"(safety-copy write failed: {e}) — proceeding "
                    "because --force explicitly rewrites the live login."
                )
                if warnings_out is not None:
                    warnings_out.append(msg)
                elif quiet:
                    self._logger.warning(msg)
                else:
                    warning(msg)

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

    def _live_matches_slot_backup(self, slot: str, email: str) -> bool:
        """Whether the live credential is provably the slot's stored lineage.

        Byte or refresh-token-fingerprint equality against the slot's backup.
        Used to make self-switch short-circuits provenance-aware: a no-op is
        only safe when live state matches what the slot holds — when they
        have diverged, the switch should run so ``_perform_switch`` can
        classify the live bytes (re-sync or preserve) instead of silently
        leaving the divergence in place. Unreadable/empty live credentials
        return True (keep the no-op: forcing a switch on missing evidence
        would fail later anyway).
        """
        try:
            live = self._read_credentials()
        except Exception:
            return True
        if not live:
            return True
        backup = self._read_account_credentials(slot, email)
        if not backup:
            return False
        return live == backup or (
            oauth.credential_fingerprint(live)
            == oauth.credential_fingerprint(backup)
        )

    def _self_switch_action(
        self, slot: str, email: str
    ) -> tuple[str, dict[str, Any] | None]:
        """How to treat a switch that targets the already-active slot.

        Returns ``(action, provenance)``:

        - ``("noop", None)`` — live matches the slot's backup; nothing to do
          (issue #79's short-circuit).
        - ``("reconcile", provenance)`` — live diverged and its owner was
          resolved: run the full switch so ``_perform_switch`` can classify
          (re-sync a legitimate rotation, or preserve foreign bytes and
          restore the slot's stored credential).
        - ``("noop-diverged", None)`` — live diverged but cannot be
          classified (offline / endpoint failure / no profile access). Exact
          pre-fix behavior: an ordinary already-active no-op, silent to the
          user — endpoint trouble must never surface on the self-switch path
          either. Leaving everything untouched is also the safe write:
          activating the stored backup over an unverified live credential
          could replace a freshly rotated token with its consumed ancestor.
        """
        if self._live_matches_slot_backup(slot, email):
            return "noop", None
        provenance = self._prefetch_live_identity()
        if provenance.get("resolved") is None:
            self._logger.info(
                "Live credential diverges from Account-%s's stored backup "
                "and ownership could not be verified; self-switch left "
                "everything untouched (pre-fix no-op).",
                slot,
            )
            return "noop-diverged", None
        return "reconcile", provenance

    def _prefetch_live_identity(self) -> dict[str, Any]:
        """Resolve the live credential's owner BEFORE the locks are taken.

        The switch-time backup copies live credential bytes into the slot named
        by ``~/.claude.json`` — two files with independent writers. When they
        agree (bytes or refresh-token lineage match the slot's stored backup)
        no network is needed. When they diverge, only the API can say whose
        token the live bytes are (the credential blob carries no identity), and
        "no network while locks are held" forces that call to happen here.

        Returns ``{"live": str|None, "resolved": dict|None}``. ``resolved`` is
        only trustworthy while the live bytes haven't moved — the under-lock
        classifier re-checks byte equality before using it.
        """
        result: dict[str, Any] = {"live": None, "resolved": None}
        try:
            live = self._read_credentials()
        except Exception as e:
            self._logger.debug(f"Pre-lock live credential read failed: {e!r}")
            return result
        result["live"] = live
        if not live:
            return result
        identity = self._get_current_account()
        if identity is None:
            return result
        data = self._get_sequence_data() or {}
        slot = self._find_account_slot(data, identity[0], identity[1])
        if slot is None:
            return result
        backup = self._read_account_credentials(slot, identity[0])
        if backup == live or (
            oauth.credential_fingerprint(backup)
            == oauth.credential_fingerprint(live)
        ):
            return result  # provenance already established locally
        access_token = oauth.extract_access_token(live)
        if not access_token:
            return result  # raw API key / garbled JSON — nothing to resolve
        try:
            result["resolved"] = oauth.fetch_oauth_profile(access_token)
        except Exception as e:
            # fetch_oauth_profile swallows its own failures; this belt keeps
            # the invariant structural — the oracle is advisory and must
            # never fail a switch.
            self._logger.debug(f"Profile resolution raised: {e!r}")
        return result

    def _classify_outgoing_credential(
        self,
        current_account: str,
        current_email: str,
        original_creds: str,
        provenance: dict[str, Any],
        data: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Decide what the switch-time backup may do with the live credential.

        Returns ``(kind, foreign_slot)``:

        - ``"own-bytes"``      — byte-identical to the slot's stored backup;
          nothing changed, nothing to capture.
        - ``"own-family"``     — same refresh-token lineage (access token
          rotated); back up normally.
        - ``"own-rotated"``    — full rotation, but the profile endpoint
          resolved the live token to this slot's identity; back up normally
          (the live→backup re-sync that keeps slots alive across Claude
          Code's routine refresh-token rotations).
        - ``"foreign"``        — uuid-positively resolved to *another* managed
          slot (``foreign_slot``) holding a different lineage; backing it up
          here would destroy this slot's only refresh token (issue #117's
          poisoning). Preserved in a safety copy, never written into any
          slot: identity proves ownership, not generation freshness.
        - ``"foreign-synced"`` — resolved to another managed slot whose
          stored backup already holds this exact lineage; nothing needs
          preserving, nothing may be written.
        - ``"alien"``          — a *structurally complete* identity (uuid +
          email + organization) that matches no managed slot (unmanaged
          login, recycled email wearing a managed address, or an email+org
          match without uuid confirmation). Preserved in a safety copy.
        - ``"unresolved"``     — mismatch and identity could not be
          established (offline, endpoint failure, malformed response, no
          access token in the blob, bytes moved since the pre-lock read) —
          or was only *partially* established: a response missing email or
          organization matching nothing is indistinguishable from schema
          drift, and preserve-and-skip on drift would silently recreate the
          fail-closed behavior this design forbids. The caller falls back to
          the exact pre-fix backup: the identity oracle is advisory, and
          endpoint state must never change switch behavior beyond skipping
          the extra safety.
        """
        backup = self._read_account_credentials(current_account, current_email)
        if backup and backup == original_creds:
            return ("own-bytes", None)
        if backup and (
            oauth.credential_fingerprint(backup)
            == oauth.credential_fingerprint(original_creds)
        ):
            return ("own-family", None)
        resolved = provenance.get("resolved")
        if resolved is None or provenance.get("live") != original_creds:
            return ("unresolved", None)
        r_email = resolved.get("email") or ""
        r_org = resolved.get("organizationUuid") or ""
        r_uuid = (resolved.get("uuid") or "").strip()
        # Outgoing-slot uuid match first: robust to partial responses (a
        # drifted schema may drop email/organization) and to an account
        # whose email changed. Organization must agree only when both sides
        # record one — the codebase's usual leniency for org matching.
        own = data.get("accounts", {}).get(current_account, {})
        own_uuid = (own.get("uuid") or "").strip()
        own_org = own.get("organizationUuid", "") or ""
        if r_uuid and own_uuid and r_uuid == own_uuid and (
            not r_org or not own_org or r_org == own_org
        ):
            return ("own-rotated", None)
        slot = self._find_account_slot(data, r_email, r_org) if r_email else None
        if slot is not None and r_uuid:
            # When both sides carry a uuid it must agree: an email+org match
            # with a conflicting uuid is a *different* account wearing a
            # recycled email (e.g. deleted/recreated claude.ai account), and
            # treating it as the slot would poison the slot's backup.
            stored_uuid = (
                data.get("accounts", {}).get(slot, {}).get("uuid") or ""
            ).strip()
            if stored_uuid and stored_uuid != r_uuid:
                slot = None
        if slot is None and r_uuid:
            # Fall back to the account uuid (org-scoped) in case the slot's
            # stored email is stale or synthesized (add-token placeholder).
            for num, acct in data.get("accounts", {}).items():
                if (
                    acct.get("uuid")
                    and acct.get("uuid") == r_uuid
                    and (acct.get("organizationUuid", "") or "") == r_org
                ):
                    slot = num
                    break
        if slot == current_account:
            return ("own-rotated", None)
        if slot is None:
            # A positive "alien" needs a structurally complete identity —
            # email plus organization — matching nothing. A partial one is
            # indistinguishable from schema drift and must fail open like
            # any other oracle degradation, not preserve-and-skip.
            if r_email and resolved.get("organizationUuid") is not None:
                return ("alien", None)
            return ("unresolved", None)
        # A cross-slot attribution must be uuid-positive: an email+org match
        # against a slot with no recorded uuid (add-token placeholder) is not
        # evidence enough to name that slot in user output — treat as alien.
        stored_uuid = (
            data.get("accounts", {}).get(slot, {}).get("uuid") or ""
        ).strip()
        if not r_uuid or stored_uuid != r_uuid:
            return ("alien", None)
        foreign_email = data.get("accounts", {}).get(slot, {}).get("email", "")
        foreign_backup = self._read_account_credentials(slot, foreign_email)
        if foreign_backup and (
            foreign_backup == original_creds
            or oauth.credential_fingerprint(foreign_backup)
            == oauth.credential_fingerprint(original_creds)
        ):
            return ("foreign-synced", slot)
        return ("foreign", slot)

    def _stash_live_credential(
        self,
        original_creds: str,
        reason: str,
        current_account: str,
        resolved: dict[str, Any] | None,
    ) -> str:
        """Preserve an unowned live credential before it is overwritten.

        Raises on failure — a successful stash is the license to overwrite the
        live store (the bytes may be the only live copy of some account's
        refresh token). The logged evidence doubles as the instrumentation for
        identifying what wrote the credential (#117's writer is unidentified).
        """
        creds_mtime: str | None = None
        try:
            mtime = get_credentials_path().stat().st_mtime
            creds_mtime = datetime.fromtimestamp(
                mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            pass  # Keychain backend or file absent
        live_oauth_account: dict[str, Any] | None = None
        try:
            config = self._read_json(self._get_claude_config_path())
            if isinstance(config, dict):
                live_oauth_account = config.get("oauthAccount")
        except Exception:
            pass
        entry_id = self._store._write_unclaimed_credential(
            original_creds,
            {
                "reason": reason,
                "configSlot": current_account,
                "fingerprint": oauth.credential_fingerprint(original_creds),
                "liveOauthAccount": live_oauth_account,
                "resolvedIdentity": resolved,
                "credentialsMtime": creds_mtime,
            },
        )
        self._logger.warning(
            "Live credential does not belong to Account-%s (%s): stashed as %s "
            "(credentials mtime %s). Something outside cswap rewrote the live "
            "login after the last switch.",
            current_account,
            reason,
            entry_id,
            creds_mtime or "unknown",
        )
        return entry_id

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
        provenance: dict[str, Any] | None = None,
        quiet: bool = False,
        warnings_out: list[str] | None = None,
    ) -> None:
        """Back up the current account and activate the target with rollback."""
        # Create transaction for rollback capability
        try:
            original_creds = self._read_credentials()
            if original_creds is None:
                raise CredentialReadError("Failed to read current credentials")
            if not original_creds:
                # An empty read (e.g. a macOS Keychain `security` timeout,
                # which returns "" rather than raising) must NOT be written
                # over the departing account's backup — that would destroy
                # its stored credential. Fail the switch; the backup stays
                # intact and the caller can retry once the Keychain settles.
                raise CredentialReadError(
                    "Current account credential is empty (Keychain unreadable?); "
                    "refusing to overwrite its backup"
                )
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

        def emit_warning(msg: str) -> None:
            if warnings_out is not None:
                warnings_out.append(msg)
            elif quiet:
                self._logger.warning(msg)
            else:
                warning(msg)

        try:
            # Step 1: Backup current account (under the switch lock already).
            # Position in ~/.claude.json says which slot is active; only the
            # classification says who owns the live bytes (issue #117: an
            # external write here used to destroy the outgoing slot's refresh
            # token). The identity oracle is strictly advisory — "unresolved"
            # falls back to the exact pre-fix backup, so endpoint state never
            # decides whether a switch completes.
            kind, foreign_slot = self._classify_outgoing_credential(
                current_account, current_email, original_creds,
                provenance or {"live": None, "resolved": None}, data,
            )
            if kind in ("foreign", "alien"):
                # Positively not this slot's bytes: never into a slot; never
                # silently destroyed. The safety copy (which raises on
                # failure, aborting before the live store is overwritten) is
                # the license to proceed.
                self._stash_live_credential(
                    original_creds, kind, current_account,
                    (provenance or {}).get("resolved"),
                )
                if kind == "foreign":
                    emit_warning(
                        "Credential ownership mismatch detected. The live "
                        "credential was preserved and was not written "
                        f"into Account-{current_account}. If Account-"
                        f"{foreign_slot} later cannot authenticate, log "
                        "in as it and run: cswap add --slot "
                        f"{foreign_slot}"
                    )
                else:
                    emit_warning(
                        "The live login does not match a managed "
                        "account. It was preserved and not written into "
                        f"Account-{current_account}. If you need that "
                        "account, log in as it and run: cswap add"
                    )
            elif kind == "foreign-synced":
                # Another managed account's bytes, and that slot already
                # holds this lineage — nothing needs preserving, nothing
                # may be written.
                emit_warning(
                    "Credential ownership mismatch detected. The live "
                    f"credential already matches Account-{foreign_slot}'s "
                    "stored backup, so nothing was written into "
                    f"Account-{current_account}."
                )
            elif kind == "own-bytes":
                # Untouched since cswap wrote it — the slot already holds
                # these bytes. Refresh only the config backup.
                self._write_account_config(
                    current_account, current_email, original_config
                )
                self._logger.info(
                    f"Backed up account {current_account} (config only; "
                    "credentials unchanged)"
                )
            else:  # own-family / own-rotated / unresolved
                original_creds = self._write_verified_live_account_credentials(
                    current_account, current_email, original_creds,
                    assume_locked=True,
                )
                self._write_account_config(
                    current_account, current_email, original_config
                )
                if kind == "own-rotated":
                    # The profile call proved the identity; backfill a
                    # missing slot uuid (add-token placeholder) while the
                    # sequence file is being rewritten anyway.
                    resolved = (provenance or {}).get("resolved") or {}
                    acct = data.get("accounts", {}).get(current_account, {})
                    if not acct.get("uuid") and resolved.get("uuid"):
                        acct["uuid"] = resolved["uuid"]
                    self._logger.info(f"Backed up account {current_account}")
                elif kind == "unresolved":
                    self._logger.info(
                        f"Backed up account {current_account} (lineage "
                        "differs from the stored backup and ownership could "
                        "not be verified — pre-fix backup)"
                    )
                else:
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
        provenance: dict[str, Any] | None = None,
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

        # Pre-lock identity resolution (may hit the network — must happen
        # before the locks). Callers that already resolved (self-switch
        # reconciliation) pass it in; force activation never backs up the
        # live credential so it skips the lookup.
        if provenance is None:
            provenance = (
                {"live": None, "resolved": None}
                if force_activate
                else self._prefetch_live_identity()
            )

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
                direct_warnings: list[str] | None = (
                    [] if not emit_output else None
                )
                self._activate_target_directly(
                    data,
                    target_account,
                    target_email,
                    config_path,
                    current_identity,
                    quiet=quiet,
                    force_refresh=force_refresh,
                    force_activate=force_activate,
                    warnings_out=direct_warnings,
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
                        "warnings": direct_warnings or [],
                    }
                # Defer the human result to AFTER the lock: _print_switch_result
                # runs list_accounts(), whose persist callbacks re-acquire this
                # (non-reentrant) lock.
                direct_activation = True
            else:
                direct_activation = False
                current_email, _ = current_identity
                from_num = current_account
                # JSON mode collects classification warnings into the result
                # envelope; human mode prints them inline under the lock.
                swap_warnings: list[str] | None = (
                    [] if not emit_output else None
                )
                self._swap_target_transactional(
                    data,
                    target_account,
                    target_email,
                    current_account,
                    current_email,
                    config_path,
                    force_refresh=force_refresh,
                    provenance=provenance,
                    quiet=quiet,
                    warnings_out=swap_warnings,
                )

        # Lock released. Safe to do network I/O and let persist callbacks
        # re-acquire the lock from inside list_accounts().
        if direct_activation:
            # The just-activated account's candidate-era poll plan may be too
            # slow for the account whose usage is about to move.
            self._replan_new_active(
                target_account,
                target_email,
                (self._get_sequence_data() or {})
                .get("accounts", {})
                .get(target_account, {})
                .get("organizationUuid", ""),
            )
            if not quiet:
                self._print_switch_result(target_account, target_email)
            return None
        self._replan_new_active(
            target_account,
            target_email,
            (self._get_sequence_data() or {})
            .get("accounts", {})
            .get(target_account, {})
            .get("organizationUuid", ""),
        )
        if not emit_output:
            data = self._get_sequence_data() or {}
            to_email = data.get("accounts", {}).get(target_account, {}).get("email", target_email)
            return {
                "from": account_ref(int(from_num), current_email) if from_num else None,
                "to": account_ref(int(target_account), to_email),
                "warnings": swap_warnings or [],
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
