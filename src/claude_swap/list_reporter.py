"""Account list/status reporting for claude-swap.

Renders the ``--list`` / ``--status`` views for ``ClaudeAccountSwitcher`` —
it never imports ``switcher`` at runtime (type-only). "Read-only" means switch state:
listing never changes which account is active, but it is where opportunistic
credential maintenance happens (live-rotation sync-back, inactive-token
refresh, parked-rotation recovery), because a usage fetch can consume a
single-use refresh token (claude-code#24317). Usage resolution goes through
the shared per-account :class:`~claude_swap.usage_store.UsageStore`
(stale-on-error, failure backoff, staggered fetches), so ``--list``, the TUI,
and ``cswap auto`` all learn from each other's fetches.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_swap import oauth
from claude_swap.claude_locks import claude_config_lock, claude_credentials_lock
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
    empty_list_payload,
    list_payload,
    slot_for_identity,
    status_payload,
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
    format_age,
    ide_short_name,
    muted,
)
from claude_swap.process_detection import get_running_instances
from claude_swap.usage_store import FetchRecord, UsageEntry, with_sentinel

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Delay between successive usage-request launches in one collect pass, so N
# accounts never burst the shared usage endpoint from one IP in the same
# instant (request hygiene; see upstream issue #85).
_FETCH_STAGGER_S = 0.25

# Show an age note on displayed usage older than this. Below it the data is
# essentially current (auto refreshes every tick; --list on demand).
_USAGE_AGE_NOTE_S = 90.0

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
    # Collect (label, body) rows first, then pad every label to the widest one so
    # per-model names (e.g. "Fable") don't shift the columns of the other lines.
    rows: list[tuple[str, str]] = []
    spend = usage.get("spend")
    if spend:
        used = spend["used"]
        limit = spend["limit"]
        pct = spend["pct"]
        if "clock" in spend:
            rows.append(
                (
                    "$$",
                    f"{pct:>3.0f}%   resets {spend['clock']:<12}  "
                    f"${used:,.2f} / ${limit:,.2f}",
                )
            )
        else:
            rows.append(("$$", f"{pct:>3.0f}%   ${used:,.2f} / ${limit:,.2f}"))
    for label, w in (("5h", usage.get("five_hour")), ("7d", usage.get("seven_day"))):
        if w:
            if "clock" in w:
                rows.append(
                    (
                        label,
                        f"{w['pct']:>3.0f}%   resets {w['clock']:<12}  "
                        f"in {w['countdown']}",
                    )
                )
            else:
                rows.append((label, f"{w['pct']:>3.0f}%"))
    for w in usage.get("scoped") or []:
        # Per-model weekly limits (e.g. Fable). Flag ones at/over the limit so a
        # maxed model — the usual reason to switch — stands out.
        marker = "  (!)" if w["pct"] >= 100 else ""
        if "clock" in w:
            rows.append(
                (
                    w["name"],
                    f"{w['pct']:>3.0f}%   resets {w['clock']:<12}  "
                    f"in {w['countdown']}{marker}",
                )
            )
        else:
            rows.append((w["name"], f"{w['pct']:>3.0f}%{marker}"))
    width = max((len(label) for label, _ in rows), default=0) + 1  # label + ':'
    return [f"{label + ':':<{width}} {body}" for label, body in rows]


# Human notes for sentinel usage states (fallback: the raw sentinel string).
_SENTINEL_NOTES = {
    USAGE_TOKEN_EXPIRED: "token expired — Claude Code refreshes the active account",
    USAGE_API_KEY: "API key (no quota)",
    USAGE_KEYCHAIN_UNAVAILABLE: "keychain unavailable — locked or in use; try again",
}


def _last_seen_note(entry: UsageEntry) -> str | None:
    """"last seen 53% used · 12m ago" from an entry's last-good measurement."""
    if entry.last_good is None or entry.fetched_at is None:
        return None
    headroom = oauth.account_headroom(entry.last_good)
    if headroom is None:
        return None
    return (
        f"last seen {100 - headroom:.0f}% used · "
        f"{format_age(int(entry.fetched_at * 1000))}"
    )


def _usage_entry_lines(entry: UsageEntry) -> list[str]:
    """Styled usage lines (sans indent) for one account's entry.

    Sentinel states render their note first, with a supplementary "last seen"
    line when an older measurement exists. Measurements render as usual, age-
    annotated once older than ``_USAGE_AGE_NOTE_S`` (stale-served); an account
    with no measurement at all shows "usage unavailable" plus the last fetch
    error, so a failing endpoint is visible instead of a silent blank.
    """
    if entry.sentinel is not None:
        out = [dimmed(_SENTINEL_NOTES.get(entry.sentinel, entry.sentinel))]
        last_seen = _last_seen_note(entry)
        if last_seen is not None and entry.sentinel != USAGE_API_KEY:
            out.append(f"{dimmed('└')} {muted(last_seen)}")
        return out
    if entry.last_good is not None:
        lines = _format_usage_lines(entry.last_good)
        if (
            lines
            and entry.age_s is not None
            and entry.age_s > _USAGE_AGE_NOTE_S
            and entry.fetched_at is not None
        ):
            lines[-1] += f" · {format_age(int(entry.fetched_at * 1000))}"
        return [
            f"{dimmed('└' if j == len(lines) - 1 else '├')} {muted(line)}"
            for j, line in enumerate(lines)
        ]
    detail = "usage unavailable"
    if entry.last_error:
        detail += f" ({entry.last_error})"
    return [dimmed(detail)]


def run_list(
    host: ClaudeAccountSwitcher,
    *,
    show_token_status: bool = False,
    show_health: bool = False,
    json_output: bool = False,
    fetch: set[str] | None = None,
) -> dict[str, Any] | None:
    """List all managed accounts via *host*."""
    return ListReporter(host).list_accounts(
        show_token_status=show_token_status,
        show_health=show_health,
        json_output=json_output,
        fetch=fetch,
    )


def run_status(
    host: ClaudeAccountSwitcher,
    *,
    json_output: bool = False,
) -> dict[str, Any] | None:
    """Display current account status via *host*."""
    return ListReporter(host).status(json_output=json_output)


class ListReporter:
    """Read-only list/status renderer backed by the switcher host."""

    def __init__(self, host: ClaudeAccountSwitcher) -> None:
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
    def _logger(self) -> logging.Logger:
        return self._host._logger

    def list_accounts(
        self,
        *,
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
        if json_output:
            if not self.sequence_file.exists():
                return empty_list_payload()
            data = self._host._get_sequence_data_migrated() or {}
            current_identity = self._host._get_current_account()
            active_num = None
            if current_identity is not None:
                ce, ou = current_identity
                active_num = slot_for_identity(data.get("accounts", {}), ce, ou)
            accounts_info, _ = self.collect_accounts_info(data, active_num)
            entries = self.collect_usage_entries(accounts_info, fetch=fetch)
            return self.build_list_payload(accounts_info, entries)
        if not self.sequence_file.exists():
            print(dimmed("No accounts are managed yet."))
            self._host._first_run_setup()
            return None

        data = self._host._get_sequence_data_migrated() or {}
        current_identity = self._host._get_current_account()
        active_num = None
        if current_identity is not None:
            current_email, current_org_uuid = current_identity
            active_num = slot_for_identity(
                data.get("accounts", {}), current_email, current_org_uuid,
            )

        accounts_info, health_notes = self.collect_accounts_info(data, active_num)
        entries = self.collect_usage_entries(accounts_info, fetch=fetch)
        self.print_account_rows(
            accounts_info,
            entries,
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
            entry = self.active_usage_entry(
                account_num, current_email, current_org_uuid
            )
            for line in _usage_entry_lines(entry):
                print(f"  {line}")
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

    def _static_usage_sentinel(
        self, account_info: tuple[int, str, str, str, bool, str],
    ) -> str | None:
        """Sentinel state derivable without any network call, or ``None``.

        Re-derived on every collect pass (never persisted), so it can't
        outlive the condition that produced it.
        """
        num, email, _, _, is_active, creds = account_info
        if looks_like_api_key(creds):
            # Managed API-key account: no subscription quota to fetch.
            return USAGE_API_KEY
        if not creds or not oauth.extract_access_token(creds):
            if is_active and self._active_keychain_unavailable:
                return USAGE_KEYCHAIN_UNAVAILABLE
            return USAGE_NO_CREDENTIALS
        if is_active:
            # Owned + locally expired must be visible even when the fetch is
            # gated (fresh entry, failure backoff, concurrent claim) — the
            # auto engine's idle-hold keys on this sentinel, and it is provable
            # locally: only an owner (Claude Code / live session) may refresh
            # this credential, and it hasn't. The expiry check gates the
            # process scan, so the common non-expired path pays nothing.
            oauth_data = oauth.extract_oauth_data(creds)
            if (
                oauth_data
                and oauth.is_oauth_token_expired(oauth_data.get("expiresAt"))
                and (
                    self._active_cc_running()
                    or self._host._live_session_pids(str(num), email)
                )
            ):
                return USAGE_TOKEN_EXPIRED
        return None

    def _run_usage_fetches(
        self, infos: list[tuple[int, str, str, str, bool, str]],
    ) -> dict[str, FetchRecord]:
        """Fetch the given accounts in parallel, staggering request starts so
        N accounts never hit the endpoint in the same instant."""
        def fetch_one(
            idx_info: tuple[int, tuple[int, str, str, str, bool, str]],
        ) -> tuple[str, FetchRecord]:
            idx, info = idx_info
            if idx and _FETCH_STAGGER_S:
                time.sleep(idx * _FETCH_STAGGER_S)
            return str(info[0]), self.fetch_account_usage(info)

        with ThreadPoolExecutor() as executor:
            return dict(executor.map(fetch_one, enumerate(infos)))

    def collect_usage_entries(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str]],
        fetch: set[str] | None = None,
    ) -> dict[str, UsageEntry]:
        """Store-backed usage collection: one :class:`UsageEntry` per account.

        ``fetch=None`` (on-demand callers: ``--list``/``--status``/switch
        strategies) makes every account eligible; the auto engine passes an
        explicit set to restrict which accounts *may* be fetched this pass.
        Either way an account is skipped — its stored entry served instead —
        when a sentinel state applies, its entry is fresh (≤ ``SERVE_TTL_S``),
        it is inside failure backoff, or another collector claimed it moments
        ago. A failed fetch only updates the entry's error/backoff fields, so
        the last-good measurement keeps being served (stale-on-error).
        """
        store = self._host._usage_store
        now = store.clock()
        identities = {
            str(num): (email, org_uuid or "")
            for num, email, _org_name, org_uuid, _active, _creds in accounts_info
        }
        info_by_num = {str(info[0]): info for info in accounts_info}
        sentinels: dict[str, str] = {}
        for num, info in info_by_num.items():
            static = self._static_usage_sentinel(info)
            if static is not None:
                sentinels[num] = static

        entries = store.entries(identities)
        to_fetch = [
            num
            for num in info_by_num
            if num not in sentinels
            and (fetch is None or num in fetch)
            and not entries[num].fresh(now)
            and not entries[num].in_backoff(now)
            and not entries[num].claimed(now)
        ]

        if to_fetch:
            store.claim(to_fetch, identities)
            records = self._run_usage_fetches(
                [info_by_num[num] for num in to_fetch]
            )
            store.record(records, identities)
            for num, record in records.items():
                if record.sentinel is not None:
                    sentinels[num] = record.sentinel
            entries = store.entries(identities)

        return {
            num: with_sentinel(entries[num], sentinels.get(num))
            for num in info_by_num
        }

    def resolve_usages(
        self, accounts_info: list[tuple[int, str, str, str, bool, str]],
    ) -> dict[str, UsageEntry]:
        """Store-backed entries for every row (every stale account eligible)."""
        return self.collect_usage_entries(accounts_info)

    def fetch_account_usage(
        self, account_info: tuple[int, str, str, str, bool, str],
    ) -> FetchRecord:
        """One network fetch for one account. Never raises."""
        num, email, _, _, is_active, creds = account_info
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

        outcome = oauth.try_fetch_usage_for_account(
            str(num), email, creds,
            is_active=is_active or has_live_session,
            persist_credentials=persist,
        )
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
        )

    def fetch_active_usage(
        self, account_num: str, email: str, creds: str, *, degraded: bool = False,
    ) -> FetchRecord:
        """Usage fetch for the active/default account, refreshing its token only
        when safe."""
        oauth_data = oauth.extract_oauth_data(creds)
        if not oauth_data or not oauth_data.get("accessToken"):
            return FetchRecord(sentinel=USAGE_NO_CREDENTIALS)

        # A degraded active read (Keychain failed; a leftover plaintext file
        # covered it) may hold another account's credentials: fetching usage
        # with it is fine, but consuming its single-use refresh token — or
        # persisting the rotation into this account's live/backup stores —
        # would poison the slot. Route it through the fetch-only owner path.
        owned = degraded or self._active_cc_running() or bool(
            self._host._live_session_pids(account_num, email)
        )
        if owned:
            if oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
                # The request would just 401 (an owned credential may not be
                # refreshed), so skip it — Claude Code's own /usage does the
                # same on a locally-expired token.
                return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
            outcome = oauth.try_fetch_usage_for_account(
                account_num, email, creds, is_active=True,
            )
            if outcome.usage is None and oauth.is_oauth_token_expired(
                oauth_data.get("expiresAt")
            ):
                return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
            return FetchRecord(
                usage=outcome.usage,
                error=outcome.error,
                retry_after_s=outcome.retry_after_s,
            )

        original_refresh = oauth_data.get("refreshToken")
        persist_skipped = False

        def persist_active(num: str, acct_email: str, new_creds: str) -> None:
            nonlocal persist_skipped
            # Failing to acquire any lock means the rotated credential was NOT
            # persisted — mark it skipped (never show usage for it) before the
            # error propagates to oauth._persist's warning path. The Claude
            # Code locks matter here too: _write_credentials touches the
            # active store and (via _clear_managed_key) possibly ~/.claude.json,
            # and holding them closes the owner-check-to-write gap.
            try:
                with (
                    FileLock(self.lock_file),
                    claude_credentials_lock(),
                    claude_config_lock(),
                ):
                    live = self._host._read_credentials() or ""
                    live_oauth = oauth.extract_oauth_data(live) if live else None
                    live_refresh = (
                        live_oauth.get("refreshToken") if live_oauth else None
                    )
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
                            self._host._write_account_credentials(
                                num, acct_email, new_creds,
                            )
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
                    self._host._write_credentials(new_creds)
                    self._host._write_account_credentials(num, acct_email, new_creds)
            except Exception:
                if not persist_skipped:
                    persist_skipped = True
                raise

        outcome = oauth.try_fetch_usage_for_account(
            account_num, email, creds,
            is_active=False, persist_credentials=persist_active,
        )
        if persist_skipped:
            return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
        if outcome.usage is None and oauth.is_oauth_token_expired(
            oauth_data.get("expiresAt")
        ):
            return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
        )

    def active_usage_entry(
        self,
        account_num: str,
        current_email: str,
        org_uuid: str = "",
    ) -> UsageEntry:
        """Store-backed usage entry for just the active account.

        Builds a single-account info row instead of the full accounts list
        (``--status`` touches one slot) and runs it through the shared
        collector, so freshness/backoff/claim gating and the shared
        ``cache/usage.json`` table behave exactly as in ``--list``.
        """
        active = self._host._read_active_credentials()
        creds = active.value or ""
        self._active_keychain_unavailable = active.keychain_unavailable
        self._active_degraded = active.degraded
        info = (int(account_num), current_email, "", org_uuid or "", True, creds)
        return self.collect_usage_entries([info])[str(account_num)]

    def build_list_payload(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str]],
        entries: dict[str, UsageEntry],
    ) -> dict[str, Any]:
        return list_payload(accounts_info, entries)

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
        org_uuid = acct.get("organizationUuid", "") or ""
        return status_payload(
            identity=identity,
            account_num=account_num,
            account_record=acct,
            usage_entry=self.active_usage_entry(
                account_num, current_email, org_uuid
            ),
            total_managed=len(data.get("accounts", {})),
        )

    def print_account_rows(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str]],
        entries: dict[str, UsageEntry],
        health_notes: dict[str, list[str]],
        *,
        show_health: bool,
        show_token_status: bool,
    ) -> None:
        """Render the per-account usage/health/token block."""
        print(bolded("Accounts:"))
        for i, (num, email, org_name, org_uuid, is_active, _) in enumerate(
            accounts_info,
        ):
            tag = self._host._get_display_tag(email, org_name, org_uuid)
            # NOTE: the TUI watch view (tui._watch_account_rows) parses this
            # output to map rows to accounts for quick-switch: it relies on the
            # uncolored ``  {num}: `` prefix and the ``(active)`` marker below.
            # Keep them intact when tweaking this line, or update that parser.
            if is_active:
                marker = f" {bold_accent('(active)')}"
                print(f"  {num}: {email} {muted(f'[{tag}]')}{marker}")
            else:
                print(f"  {num}: {email} {muted(f'[{tag}]')}")
            entry = entries[str(num)]
            for line in _usage_entry_lines(entry):
                print(f"     {line}")
            if entry.sentinel is not None:
                health_notes.setdefault(str(num), []).append(
                    _SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
                )
            elif entry.last_good is None:
                note = "usage unavailable"
                if entry.last_error:
                    note += f" ({entry.last_error})"
                health_notes.setdefault(str(num), []).append(note)
            if show_health:
                notes = health_notes.get(str(num), [])
                health = "ok" if not notes else ", ".join(notes)
                print(f"     {dimmed('•')} {muted(f'health: {health}')}")

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
            # Fail closed: an undetectable owner means the live store may be
            # in use — never consume its single-use refresh token on a guess.
            return True
