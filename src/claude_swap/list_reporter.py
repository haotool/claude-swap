"""Read-only account LIST/STATUS reporting for ``ClaudeAccountSwitcher``.

Renders managed-account rows, usage, health, and running-instance blocks without
touching switch-transaction state.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from claude_swap import oauth
from claude_swap.cache import read_cache_data, read_cache_with_timestamp
from claude_swap.credential_refresh import (
    park_rotated_credential,
    recover_pending_rotation,
)
from claude_swap.credentials import looks_like_api_key
from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_NO_CREDENTIALS,
    USAGE_TOKEN_EXPIRED,
    UsageEntry,
    _KNOWN_USAGE_SENTINELS,
    _slot_for_identity,
    empty_list_payload,
    list_payload,
    status_payload,
    usage_display_line,
)
from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock
from claude_swap.printer import (
    abbreviate_path,
    accent,
    bold_accent,
    bolded,
    dimmed,
    entrypoint_label,
    ide_short_name,
    muted,
)
from claude_swap.process_detection import get_running_instances
from claude_swap.usage_cache import (
    _merge_usage_with_previous,
    _usage_from_cache,
    _usage_slot_trusted,
)

if TYPE_CHECKING:
    from claude_swap.protocols import ListHost

# How long a persist of a just-rotated OAuth credential may wait for the file
# lock. Anthropic refresh tokens are single-use (claude-code#24317): by the
# time the persist callback runs, the old token is already consumed. The
# default 10s FileLock timeout loses that race against a switch holding the
# lock through its in-lock network refresh (~10s) plus write verification;
# 30s outlasts any legitimate transaction while still bounding a wedged
# holder — past it the rotation is parked on disk for the next locked pass
# (park_rotated_credential) instead of being dropped.
_ROTATED_PERSIST_LOCK_TIMEOUT = 30.0


def _format_usage_lines(usage: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    spend = usage.get("spend")
    if spend:
        used = spend["used"]
        limit = spend["limit"]
        pct = spend["pct"]
        if "clock" in spend:
            lines.append(
                f"$$: {pct:>3.0f}%   resets {spend['clock']:<12}  "
                f"${used:,.2f} / ${limit:,.2f}"
            )
        else:
            lines.append(f"$$: {pct:>3.0f}%   ${used:,.2f} / ${limit:,.2f}")
    h5 = usage.get("five_hour")
    if h5:
        if "clock" in h5:
            lines.append(
                f"5h: {h5['pct']:>3.0f}%   resets {h5['clock']:<12}  "
                f"in {h5['countdown']}"
            )
        else:
            lines.append(f"5h: {h5['pct']:>3.0f}%")
    d7 = usage.get("seven_day")
    if d7:
        if "clock" in d7:
            lines.append(
                f"7d: {d7['pct']:>3.0f}%   resets {d7['clock']:<12}  "
                f"in {d7['countdown']}"
            )
        else:
            lines.append(f"7d: {d7['pct']:>3.0f}%")
    return lines


def run_list(
    host: ListHost,
    *,
    show_token_status: bool = False,
    show_health: bool = False,
    json_output: bool = False,
) -> dict[str, Any] | None:
    """List all managed accounts via *host*."""
    return ListReporter(host).list_accounts(
        show_token_status=show_token_status,
        show_health=show_health,
        json_output=json_output,
    )


def run_status(
    host: ListHost,
    *,
    json_output: bool = False,
) -> dict[str, Any] | None:
    """Display current account status via *host*."""
    return ListReporter(host).status(json_output=json_output)


class ListReporter:
    """Read-only list/status renderer backed by a narrow ``ListHost``."""

    def __init__(self, host: ListHost) -> None:
        self._host = host
        self._active_keychain_unavailable = False
        self._active_degraded = False

    @property
    def sequence_file(self) -> Path:
        return self._host.sequence_file

    @property
    def lock_file(self) -> Path:
        return self._host.lock_file

    @property
    def usage_cache_path(self) -> Path:
        return self._host.usage_cache_path

    @property
    def _logger(self) -> logging.Logger:
        return self._host._logger

    def list_accounts(
        self,
        *,
        show_token_status: bool = False,
        show_health: bool = False,
        json_output: bool = False,
    ) -> dict[str, Any] | None:
        """List all managed accounts."""
        if json_output:
            if not self.sequence_file.exists():
                return empty_list_payload()
            data = self._host._get_sequence_data_migrated() or {}
            current_identity = self._host._get_current_account()
            active_num = None
            if current_identity is not None:
                ce, ou = current_identity
                active_num = _slot_for_identity(data.get("accounts", {}), ce, ou)
            accounts_info, _ = self.collect_accounts_info(data, active_num)
            usages, _ = self.resolve_usages(accounts_info)
            return self.build_list_payload(accounts_info, usages)
        if not self.sequence_file.exists():
            print(dimmed("No accounts are managed yet."))
            self._host._first_run_setup()
            return None

        data = self._host._get_sequence_data_migrated() or {}
        current_identity = self._host._get_current_account()
        active_num = None
        if current_identity is not None:
            current_email, current_org_uuid = current_identity
            active_num = _slot_for_identity(
                data.get("accounts", {}), current_email, current_org_uuid,
            )

        accounts_info, health_notes = self.collect_accounts_info(data, active_num)
        usages, usage_notes = self.resolve_usages(accounts_info)
        self.print_account_rows(
            accounts_info,
            usages,
            usage_notes,
            health_notes,
            show_health=show_health,
            show_token_status=show_token_status,
        )
        self.print_running_instances()
        return None

    def status(self, *, json_output: bool = False) -> dict[str, Any] | None:
        """Display current account status."""
        if json_output:
            return self.build_status_payload()
        identity = self._host._get_current_account()
        if identity is None:
            print(f"{bolded('Status:')} {dimmed('No active Claude account')}")
            return None
        current_email, current_org_uuid = identity

        data = self._host._get_sequence_data_migrated()
        if not data:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
            return None

        account_num = None
        org_name = ""
        for num, info in data.get("accounts", {}).items():
            if (
                info.get("email") == current_email
                and info.get("organizationUuid", "") == current_org_uuid
            ):
                account_num = num
                org_name = info.get("organizationName", "") or ""
                break

        if account_num:
            tag = self._host._get_display_tag(current_email, org_name, current_org_uuid)
            total = len(data.get("accounts", {}))
            print(
                f"{bolded('Status:')} {accent(f'Account-{account_num}')} "
                f"({current_email} {muted(f'[{tag}]')})"
            )
            print(f"  {dimmed(f'Total managed accounts: {total}')}")
            active = self._host._read_active_credentials()
            creds = active.value or ""
            active_keychain_unavailable = active.keychain_unavailable
            if creds and not active.degraded:
                # A degraded read (Keychain failed, plaintext file covered it)
                # may hold ANOTHER account's leftover credentials; syncing it
                # would poison this slot's backup. The sync is an optimization,
                # so skipping the degraded case is safe.
                self._host._sync_live_account_credentials_to_backup(
                    account_num,
                    current_email,
                    creds,
                )
            usage, usage_note = self.resolve_active_usage_entry(
                account_num, current_email, creds=creds,
                keychain_unavailable=active_keychain_unavailable,
                degraded=active.degraded,
            )
            if isinstance(usage, dict):
                lines = _format_usage_lines(usage)
                for j, line in enumerate(lines):
                    connector = "└" if j == len(lines) - 1 else "├"
                    print(f"  {dimmed(connector)} {muted(line)}")
            else:
                display_line = usage_display_line(usage)
                if display_line:
                    print(f"  {dimmed(display_line)}")
                elif usage is None:
                    print(f"  {dimmed('usage unavailable')}")
            if isinstance(usage_note, oauth.UsageFetchError):
                print(
                    f"  {dimmed('•')} "
                    f"{muted(f'cached; live fetch {oauth.describe_usage_error(usage_note)}')}"
                )
        else:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
        return None

    def collect_accounts_info(
        self, data: dict[str, Any], active_num: str | None,
    ) -> tuple[list[tuple[int, str, str, str, bool, str]], dict[str, list[str]]]:
        """Build per-account rows, syncing the live account and refreshing
        inactive backups. Returns (accounts_info, health_notes)."""
        accounts_info = self.build_accounts_info(data, active_num)
        health_notes: dict[str, list[str]] = {}
        updated: list[tuple[int, str, str, str, bool, str]] = []
        for num, email, org_name, org_uuid, is_active, creds in accounts_info:
            if is_active:
                # A degraded active read (Keychain failed, a leftover file
                # covered it) may belong to another account; syncing it would
                # poison this slot's backup. The sync is an optimization, so
                # skipping the degraded case is safe.
                if creds and not self._active_degraded:
                    self._host._sync_live_account_credentials_to_backup(
                        str(num),
                        email,
                        creds,
                    )
            elif creds and not self._host._live_session_pids(str(num), email):
                creds, refresh_note = self._host._refresh_inactive_credentials_if_needed(
                    str(num),
                    email,
                    creds,
                )
                if refresh_note:
                    health_notes.setdefault(str(num), []).append(refresh_note)
            updated.append((num, email, org_name, org_uuid, is_active, creds))
        return updated, health_notes

    def build_accounts_info(
        self,
        data: dict[str, Any] | None = None,
        active_num: str | None = None,
    ) -> list[tuple[int, str, str, str, bool, str]]:
        """Build per-account (num, email, org_name, org_uuid, is_active, creds)."""
        if data is None:
            data = self._host._get_sequence_data_migrated() or {}
        if active_num is None:
            current_identity = self._host._get_current_account()
            if current_identity is not None:
                current_email, current_org_uuid = current_identity
                active_num = self._host._find_account_slot(
                    data, current_email, current_org_uuid,
                )

        accounts_info: list[tuple[int, str, str, str, bool, str]] = []
        self._active_keychain_unavailable = False
        self._active_degraded = False
        for num in data.get("sequence", []):
            account = data.get("accounts", {}).get(str(num), {})
            email = account.get("email", "unknown")
            org_name = account.get("organizationName", "") or ""
            org_uuid = account.get("organizationUuid", "") or ""
            is_active = str(num) == active_num

            if is_active:
                active = self._host._read_active_credentials()
                creds = active.value or ""
                self._active_keychain_unavailable = active.keychain_unavailable
                self._active_degraded = active.degraded
            else:
                creds = self._host._read_account_credentials(str(num), email)
                # A parked rotation (persist lost the lock race) supersedes
                # the stored credential — the stored refresh token is already
                # consumed. No-op unless the slot has a pending file.
                recovered = recover_pending_rotation(self._host, str(num), email)
                if recovered is not None:
                    creds = recovered

            accounts_info.append((num, email, org_name, org_uuid, is_active, creds))
        return accounts_info

    def resolve_usages(
        self, accounts_info: list[tuple[int, str, str, str, bool, str]],
    ) -> tuple[list[UsageEntry], list[oauth.UsageFetchError | None]]:
        """Return (usages, usage_notes): the fresh cache when trusted, else a
        live fetch that is merged back into the cache."""
        cached_data, _ = read_cache_with_timestamp(self.usage_cache_path)
        previous_cached = cached_data if cached_data is not None else {}
        account_keys = {str(info[0]) for info in accounts_info}
        if self._host._usage_cache_fresh(previous_cached, account_keys):
            cached_data = previous_cached
            usages = [
                cast(UsageEntry, _usage_from_cache(cached_data.get(str(info[0]))))
                for info in accounts_info
            ]
            usage_notes: list[oauth.UsageFetchError | None] = [
                None for _ in accounts_info
            ]
        else:
            with ThreadPoolExecutor() as executor:
                usages = list(executor.map(self.fetch_account_usage, accounts_info))
            usage_notes = []
            updates: dict[str, object] = {}
            for info, usage in zip(accounts_info, usages):
                key = str(info[0])
                previous = previous_cached.get(key)
                _, note = _merge_usage_with_previous(usage, previous)
                usage_notes.append(note)
                updates[key] = usage
            self._host._merge_usage_cache(updates)
            merged_raw = read_cache_data(self.usage_cache_path, default={}) or {}
            merged: dict[str, Any] = (
                merged_raw if isinstance(merged_raw, dict) else {}
            )
            usages = [
                cast(UsageEntry, _usage_from_cache(merged.get(str(info[0]))))
                for info in accounts_info
            ]
        return usages, usage_notes

    def fetch_account_usage(
        self, account_info: tuple[int, str, str, str, bool, str],
    ) -> UsageEntry:
        """Fetch live usage for one account row (used by the thread pool)."""
        num, email, _, _, is_active, creds = account_info
        if looks_like_api_key(creds):
            return USAGE_API_KEY
        if not creds or not oauth.extract_access_token(creds):
            if is_active and self._active_keychain_unavailable:
                return USAGE_KEYCHAIN_UNAVAILABLE
            return USAGE_NO_CREDENTIALS
        if is_active:
            return self.fetch_active_usage(
                str(num), email, creds, degraded=self._active_degraded,
            )

        original_oauth = oauth.extract_oauth_data(creds)
        # Refresh-token lineage this fetch is allowed to overwrite; grows as
        # rotations persist so a 401-triggered second rotation still lands.
        own_lineage = {original_oauth.get("refreshToken")} if original_oauth else set()

        def persist(acct_num: str, acct_email: str, new_creds: str) -> None:
            new_oauth = oauth.extract_oauth_data(new_creds)
            new_refresh = new_oauth.get("refreshToken") if new_oauth else None
            lock = FileLock(self.lock_file)
            if not lock.acquire(timeout=_ROTATED_PERSIST_LOCK_TIMEOUT):
                # A wedged holder must not cost the rotation: park it on disk
                # for the next locked pass over this slot to apply.
                try:
                    park_rotated_credential(
                        self._host.credentials_dir, acct_num, acct_email,
                        new_creds,
                        replaces=[t for t in own_lineage if isinstance(t, str)],
                    )
                except Exception:
                    self._logger.error(
                        "Could not persist rotated OAuth token for account %s (%s): "
                        "file lock still held after %.0fs and parking the rotation "
                        "failed. The previous refresh token is already consumed; "
                        "if the next refresh fails with invalid_grant, re-add "
                        "with `cswap --add-account --slot %s`.",
                        acct_num, acct_email, _ROTATED_PERSIST_LOCK_TIMEOUT,
                        acct_num,
                    )
                    raise LockError(
                        f"persist of rotated token for account {acct_num} timed out"
                    )
                own_lineage.add(new_refresh)
                self._logger.warning(
                    "Parked rotated OAuth token for account %s (%s): file lock "
                    "still held after %.0fs. It will be applied automatically "
                    "by the next list/switch that touches the slot.",
                    acct_num, acct_email, _ROTATED_PERSIST_LOCK_TIMEOUT,
                )
                return
            try:
                stored = self._host._read_account_credentials(acct_num, acct_email)
                stored_oauth = oauth.extract_oauth_data(stored) if stored else None
                stored_refresh = (
                    stored_oauth.get("refreshToken") if stored_oauth else None
                )
                if (
                    stored_refresh is not None
                    and stored_refresh not in own_lineage
                    and stored_refresh != new_refresh
                ):
                    # The slot was re-logged or re-imported while we were
                    # refreshing: the on-disk credential is from a newer login
                    # action, so ours is moot — keep theirs.
                    self._logger.warning(
                        "Discarding rotated OAuth token for account %s (%s): "
                        "slot credentials changed while refreshing.",
                        acct_num, acct_email,
                    )
                    return
                self._host._write_account_credentials(acct_num, acct_email, new_creds)
                own_lineage.add(new_refresh)
            finally:
                lock.release()

        has_live_session = bool(self._host._live_session_pids(str(num), email))

        result = oauth.fetch_usage_for_account(
            str(num), email, creds,
            is_active=is_active or has_live_session,
            persist_credentials=persist,
        )
        if isinstance(result, oauth.UsageFetchError):
            self._log_usage_fetch_error(num, email, is_active, result)
        return result

    def fetch_active_usage(
        self, account_num: str, email: str, creds: str, *, degraded: bool = False,
    ) -> UsageEntry:
        """Usage for the active/default account, refreshing its token only when safe."""
        oauth_data = oauth.extract_oauth_data(creds)
        if not oauth_data or not oauth_data.get("accessToken"):
            return USAGE_NO_CREDENTIALS

        # A degraded active read (Keychain failed; a leftover plaintext file
        # covered it) may hold another account's credentials: fetching usage
        # with it is fine, but consuming its single-use refresh token — or
        # persisting the rotation into this account's live/backup stores —
        # would poison the slot. Route it through the fetch-only owner path.
        owned = degraded or self._active_cc_running() or bool(
            self._host._live_session_pids(account_num, email)
        )
        if owned:
            usage = oauth.fetch_usage_for_account(
                account_num, email, creds, is_active=True,
            )
            if isinstance(usage, oauth.UsageFetchError):
                self._log_usage_fetch_error(account_num, email, True, usage)
            if usage is None and oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
                return USAGE_TOKEN_EXPIRED
            return usage

        original_refresh = oauth_data.get("refreshToken")
        persist_skipped = False

        def persist_active(num: str, acct_email: str, new_creds: str) -> None:
            nonlocal persist_skipped
            with FileLock(self.lock_file):
                live = self._host._read_credentials() or ""
                live_oauth = oauth.extract_oauth_data(live) if live else None
                live_refresh = live_oauth.get("refreshToken") if live_oauth else None
                if live_refresh != original_refresh:
                    # Someone else already rotated the live token while we were
                    # refreshing; ours is stale — drop it (no back-up either).
                    persist_skipped = True
                    self._logger.warning(
                        "Active-account refresh for %s (%s): refresh token changed "
                        "mid-refresh; discarding rotated credential.",
                        num, acct_email,
                    )
                    return
                if self._active_cc_running() or self._host._live_session_pids(
                    num, acct_email
                ):
                    # Claude Code appeared mid-refresh and owns the live store.
                    # We already consumed the single-use refresh token, so do NOT
                    # discard the rotation (that would leave a dead token on disk):
                    # back it up so a later switch recovers it, but leave the live
                    # credentials untouched to avoid clobbering the owner.
                    persist_skipped = True
                    try:
                        self._host._write_account_credentials(num, acct_email, new_creds)
                    except Exception:
                        self._logger.warning(
                            "Active-account refresh for %s (%s): owner appeared and "
                            "backing up the rotated credential failed.",
                            num, acct_email, exc_info=True,
                        )
                    else:
                        self._logger.warning(
                            "Active-account refresh for %s (%s): owner appeared "
                            "mid-refresh; kept live, backed up rotated credential.",
                            num, acct_email,
                        )
                    return
                try:
                    self._host._write_credentials(new_creds)
                    self._host._write_account_credentials(num, acct_email, new_creds)
                except Exception:
                    persist_skipped = True
                    raise

        usage = oauth.fetch_usage_for_account(
            account_num, email, creds,
            is_active=False, persist_credentials=persist_active,
        )
        if isinstance(usage, oauth.UsageFetchError):
            self._log_usage_fetch_error(account_num, email, True, usage)
        if persist_skipped:
            return USAGE_TOKEN_EXPIRED
        if usage is None and oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
            return USAGE_TOKEN_EXPIRED
        return usage

    def resolve_active_usage_entry(
        self,
        account_num: str,
        email: str,
        *,
        creds: str | None = None,
        keychain_unavailable: bool = False,
        degraded: bool = False,
    ) -> tuple[UsageEntry, oauth.UsageFetchError | None]:
        """Usage entry for the active account (cache-first, owner-aware refresh)."""
        if creds is None:
            active = self._host._read_active_credentials()
            creds = active.value or ""
            keychain_unavailable = active.keychain_unavailable
            degraded = active.degraded
        if looks_like_api_key(creds):
            return USAGE_API_KEY, None
        if not creds or not oauth.extract_access_token(creds):
            if keychain_unavailable:
                return USAGE_KEYCHAIN_UNAVAILABLE, None
            return USAGE_NO_CREDENTIALS, None

        cached_data, _ = read_cache_with_timestamp(self.usage_cache_path)
        previous_cached = cached_data if cached_data is not None else {}
        if account_num in previous_cached:
            usage = _usage_from_cache(previous_cached[account_num])
            if isinstance(usage, str) and usage in _KNOWN_USAGE_SENTINELS:
                return usage, None
            if isinstance(usage, dict) and _usage_slot_trusted(usage, time.time()):
                return usage, None

        fetched = self.fetch_active_usage(account_num, email, creds, degraded=degraded)
        previous = previous_cached.get(account_num)
        display, note = _merge_usage_with_previous(fetched, previous)
        self._host._merge_usage_cache({account_num: fetched})
        if isinstance(display, dict):
            return display, note
        if isinstance(display, (str, oauth.UsageFetchError)):
            return display, note
        cached: UsageEntry = cast(UsageEntry, _usage_from_cache(display))
        return cached, note

    def active_account_usage(
        self, account_num: str, current_email: str,
    ) -> dict[str, Any] | str | oauth.UsageFetchError | None:
        """Usage for the active account via the shared per-slot usage cache."""
        usage, _ = self.resolve_active_usage_entry(account_num, current_email)
        if isinstance(usage, oauth.UsageFetchError):
            return usage
        return usage

    def build_list_payload(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str]],
        usages: list[UsageEntry],
    ) -> dict[str, Any]:
        return list_payload(accounts_info, usages)

    def build_status_payload(self) -> dict[str, Any]:
        identity = self._host._get_current_account()
        if identity is None:
            return status_payload(
                identity=None,
                account_num=None,
                account_record=None,
                usage_entry=None,
            )
        current_email, current_org_uuid = identity
        data = self._host._get_sequence_data_migrated()
        if not data:
            return status_payload(
                identity=identity,
                account_num=None,
                account_record=None,
                usage_entry=None,
            )
        account_num = self._host._find_account_slot(
            data, current_email, current_org_uuid,
        )
        if not account_num:
            return status_payload(
                identity=identity,
                account_num=None,
                account_record=None,
                usage_entry=None,
            )
        acct = data["accounts"][account_num]
        return status_payload(
            identity=identity,
            account_num=account_num,
            account_record=acct,
            usage_entry=self.active_account_usage(account_num, current_email),
            total_managed=len(data.get("accounts", {})),
        )

    def print_account_rows(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str]],
        usages: list[UsageEntry],
        usage_notes: list[oauth.UsageFetchError | None],
        health_notes: dict[str, list[str]],
        *,
        show_health: bool,
        show_token_status: bool,
    ) -> None:
        """Render the per-account usage/health/token block."""
        print(bolded("Accounts:"))
        for i, ((num, email, org_name, org_uuid, is_active, _), usage) in enumerate(
            zip(accounts_info, usages),
        ):
            tag = self._host._get_display_tag(email, org_name, org_uuid)
            if is_active:
                marker = f" {bold_accent('(active)')}"
                print(f"  {num}: {email} {muted(f'[{tag}]')}{marker}")
            else:
                print(f"  {num}: {email} {muted(f'[{tag}]')}")
            if isinstance(usage, str):
                line = usage_display_line(usage) or usage
                print(f"     {dimmed(line)}")
                health_notes.setdefault(str(num), []).append(line)
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
                print(
                    f"     {dimmed('•')} "
                    f"{muted(f'cached; live fetch {oauth.describe_usage_error(note)}')}"
                )

            if show_token_status:
                token_status = oauth.build_token_status(accounts_info[i][5])
                if token_status:
                    print(f"     {dimmed('•')} {muted(token_status)}")
            if i < len(accounts_info) - 1:
                print()

    def print_running_instances(self) -> None:
        """Render the grouped 'Running instances' block (best-effort)."""
        try:
            sessions, ide_instances = get_running_instances()

            if sessions or ide_instances:
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
                    print(
                        f"  {dimmed('●')} {muted(label)}   {muted(cwd)}  "
                        f"{dimmed(f'({", ".join(parts)})')}"
                    )
        except Exception:
            self._logger.debug("Failed to detect running instances", exc_info=True)

    def _active_cc_running(self) -> bool:
        """Whether any default-profile Claude Code instance is running."""
        try:
            sessions, ides = get_running_instances()
            return bool(sessions or ides)
        except Exception:
            self._logger.debug(
                "Failed to detect running Claude instances", exc_info=True,
            )
            return True

    def _log_usage_fetch_error(
        self,
        account_num: str | int,
        email: str,
        active: bool,
        result: oauth.UsageFetchError,
    ) -> None:
        self._logger.info(
            "Usage fetch unavailable: account=%s email=%s active=%s reason=%s status=%s",
            account_num,
            email,
            active,
            result.reason,
            result.status_code,
        )
