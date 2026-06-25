"""OAuth credential-freshness layer for Claude Code accounts.

Extracted from ``switcher.py`` (plan 020 follow-on) so the switcher no longer
owns the multi-step "keep a backup token fresh / verified" logic. This is the
read-modify-write side of credentials (refresh expiring OAuth tokens, verify a
backup matches the live store, sync a Claude-Code-rotated token back to backup)
— distinct from ``CredentialStore``'s pure storage mechanics.

``CredentialRefresher`` is a collaborator that holds the switcher (composition
with back-reference) and calls its credential primitives
(``_read_credentials`` / ``_read_account_credentials`` /
``_write_account_credentials`` — the last of which carries the switcher's
session-invalidation side effect), plus ``lock_file`` / ``_live_session_pids``
/ ``_logger``. The switcher keeps thin delegators under the original method
names so every caller (and the existing test call-sites) is unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from claude_swap import oauth
from claude_swap.exceptions import (
    CredentialReadError,
    CredentialWriteError,
    SwitchError,
)
from claude_swap.locking import FileLock

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Backup-credential read-back verification tuning. Only this module uses them.
_BACKUP_CREDENTIAL_VERIFY_ATTEMPTS = 3
_BACKUP_CREDENTIAL_VERIFY_DELAY_SECONDS = 0.5


class CredentialRefresher:
    """Owns OAuth token freshness/verification for backup credentials."""

    def __init__(self, switcher: ClaudeAccountSwitcher):
        self._sw = switcher

    def write_verified_live(
        self,
        account_num: str,
        email: str,
        credentials: str,
    ) -> str:
        """Persist live credentials and verify the stored backup matches.

        On macOS in particular, the live Claude credential can lag or be
        concurrently mutated around login/switch boundaries. Writing a backup
        without read-back verification can silently preserve stale tokens.
        Returns the credential string actually persisted to backup.

        Two distinct drift modes are disambiguated:

        1. **Our write didn't take** (e.g. Keychain ACL hiccup): ``stored``
           never matches ``expected`` even when ``live_now`` is stable. After
           ``_BACKUP_CREDENTIAL_VERIFY_ATTEMPTS`` tries we raise
           ``CredentialWriteError`` — this is a genuine storage failure.

        2. **Claude Code is rotating tokens under us** during the verification
           window (its own refresh fired concurrently): ``live_now`` keeps
           changing across attempts. Looping forever is pointless; on the
           final attempt we log a warning and persist whatever ``live_now``
           sampled last. Backup is at most one rotation stale, which the
           normal refresh-before-activation path resolves on the next switch.
        """
        expected = credentials
        previous_live: str | None = None
        live_keeps_changing = False

        for attempt in range(_BACKUP_CREDENTIAL_VERIFY_ATTEMPTS):
            self._sw._write_account_credentials(account_num, email, expected)
            stored = self._sw._read_account_credentials(account_num, email)
            live_now = self._sw._read_credentials()
            if live_now is None:
                raise CredentialReadError("Failed to re-read live credentials for verification")
            if not live_now:
                raise CredentialReadError("No live credentials found during verification")
            if stored == live_now:
                return live_now

            # Track whether the drift is "live moving" or "our write failing".
            if previous_live is not None and live_now != previous_live:
                live_keeps_changing = True
            previous_live = live_now

            if attempt == _BACKUP_CREDENTIAL_VERIFY_ATTEMPTS - 1:
                if live_keeps_changing:
                    # Claude Code is actively refreshing tokens during our
                    # verification window. Accept the latest sample rather
                    # than fighting a moving target — the backup will be at
                    # most one rotation stale, and the refresh-before-
                    # activation path handles that on the next switch.
                    self._sw._logger.warning(
                        "persistent in-flight Claude Code rotation during "
                        "backup verification for account-%s after %d attempts; "
                        "persisting last sampled live state",
                        account_num,
                        _BACKUP_CREDENTIAL_VERIFY_ATTEMPTS,
                    )
                    self._sw._write_account_credentials(account_num, email, live_now)
                    return live_now
                raise CredentialWriteError(
                    "Stored backup credentials did not match live credentials"
                )

            expected = live_now
            time.sleep(_BACKUP_CREDENTIAL_VERIFY_DELAY_SECONDS)

        # Unreachable: the loop either returns or raises on every path above.
        raise CredentialWriteError("backup credential verification fell through unexpectedly")

    def sync_live_to_backup(
        self,
        account_num: str,
        email: str,
        credentials: str,
    ) -> None:
        """Best-effort sync for live credentials Claude Code may have refreshed."""
        oauth_data = oauth.extract_oauth_data(credentials)
        if (
            not oauth_data
            or not oauth_data.get("refreshToken")
            or not isinstance(oauth_data.get("expiresAt"), (int, float))
        ):
            return
        try:
            stored = self._sw._read_account_credentials(account_num, email)
            if stored == credentials:
                return
            self.write_verified_live(
                account_num,
                email,
                credentials,
            )
            self._sw._logger.info("Synced refreshed live credentials for account %s", account_num)
        except (CredentialReadError, CredentialWriteError, OSError) as exc:
            # Narrow catch: these are the credential-store failure modes that
            # are acceptable to swallow on a best-effort sync hot path.
            # ``KeyboardInterrupt`` and other base-exception subclasses must
            # propagate so the user can still Ctrl-C out of list_accounts().
            self._sw._logger.warning(
                "Failed to sync live credentials for account %s (%s): %r",
                account_num,
                email,
                exc,
            )

    def refresh_target_before_activation(
        self,
        account_num: str,
        email: str,
        credentials: str,
        *,
        force: bool = False,
    ) -> str:
        """Refresh an inactive backup's OAuth token before making it live.

        With ``force=False`` (default, interactive callers): refresh only when
        the stored access token has already expired. Saves a network round-trip
        when the cached token is still valid.

        With ``force=True`` (background auto-switch): refresh unconditionally so
        Claude Code's first API call against the newly-active account gets a
        token with maximum remaining lifetime, removing the "stale but valid"
        window. A failed forced refresh on a still-valid token is non-fatal —
        we fall back to the existing token rather than blocking the switch.
        """
        oauth_data = oauth.extract_oauth_data(credentials)
        if not oauth_data or not oauth_data.get("accessToken"):
            return credentials
        if not oauth_data.get("refreshToken"):
            return credentials

        expired = oauth.is_oauth_token_expired(oauth_data.get("expiresAt"))
        if not force and not expired:
            return credentials

        refreshed = oauth.refresh_oauth_credentials(credentials)
        if not refreshed:
            # Forced refresh on a still-valid token: degrade gracefully.
            if not expired:
                self._sw._logger.info(
                    "forced pre-activation refresh failed for account-%s "
                    "(existing token still valid; using it)",
                    account_num,
                )
                return credentials
            if self._sw._live_session_pids(account_num, email):
                self._sw._logger.warning(
                    "pre-activation refresh failed for account-%s; "
                    "live session-mode instance present, switching anyway",
                    account_num,
                )
                return credentials
            raise SwitchError(
                f"Account-{account_num} stored OAuth token is expired and "
                f"refresh failed. Re-add with: cswap --add-account --slot {account_num}"
            )

        self._sw._write_account_credentials(account_num, email, refreshed)
        self._sw._logger.info(
            "Refreshed target credentials for account %s (force=%s, was_expired=%s)",
            account_num,
            force,
            expired,
        )
        return refreshed

    def refresh_inactive_if_needed(
        self,
        account_num: str,
        email: str,
        credentials: str,
    ) -> tuple[str, str | None]:
        """Refresh an inactive backup token before it reaches expiry.

        Acquires the file lock for the refresh + persist step and re-reads
        the on-disk credentials under the lock. If another process already
        refreshed this slot since the caller's read, the on-disk fresh
        token is returned without a redundant network call. Anthropic's
        refresh tokens are single-use (claude-code#24317); a double
        refresh would brick the slot with invalid_grant.
        """
        oauth_data = oauth.extract_oauth_data(credentials)
        if (
            not oauth_data
            or not oauth_data.get("accessToken")
            or not oauth_data.get("refreshToken")
            or not oauth.is_oauth_token_expired(oauth_data.get("expiresAt"))
        ):
            return credentials, None

        with FileLock(self._sw.lock_file):
            # Re-read under the lock — another process may have refreshed
            # while we were waiting.
            latest = self._sw._read_account_credentials(account_num, email) or credentials
            latest_oauth = oauth.extract_oauth_data(latest)
            if (
                latest_oauth
                and latest_oauth.get("accessToken")
                and not oauth.is_oauth_token_expired(latest_oauth.get("expiresAt"))
            ):
                self._sw._logger.info(
                    "OAuth refresh skipped (already fresh on disk): account=%s",
                    account_num,
                )
                return latest, "token already fresh on disk"

            refreshed = oauth.refresh_oauth_credentials(latest)
            if not refreshed:
                self._sw._logger.info(
                    "OAuth refresh unavailable: account=%s email=%s",
                    account_num,
                    email,
                )
                return latest, "token refresh failed"

            self._sw._write_account_credentials(account_num, email, refreshed)
            self._sw._logger.info("Refreshed inactive credentials for account %s", account_num)
            return refreshed, "token refreshed"
