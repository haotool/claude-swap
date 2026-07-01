"""OAuth credential-freshness layer for claude-swap.

Owns read-modify-write paths that keep backup OAuth tokens fresh and verified
— refresh before activation, sync Claude-Code-rotated tokens back to backup,
verify a write matches the live store. Split out of ``switcher.py`` so the
switcher reads as account orchestration again.

``CredentialRefresher`` is a leaf collaborator: it depends on ``RefreshHost``
for credential primitives (``_read_credentials``, ``_read_account_credentials``,
``_write_account_credentials``) and ``lock_file`` / ``_live_session_pids`` /
``_logger``, but never owns storage routing — that stays in ``CredentialStore``.
"""

from __future__ import annotations

import time

from claude_swap import oauth
from claude_swap.exceptions import (
    CredentialReadError,
    CredentialWriteError,
    SwitchError,
)
from claude_swap.locking import FileLock
from claude_swap.protocols import RefreshHost

_BACKUP_CREDENTIAL_VERIFY_ATTEMPTS = 3
_BACKUP_CREDENTIAL_VERIFY_DELAY_SECONDS = 0.5


class CredentialRefresher:
    """Owns OAuth token freshness/verification for backup credentials."""

    def __init__(self, host: RefreshHost):
        self._sw = host

    def write_verified_live(
        self,
        account_num: str,
        email: str,
        credentials: str,
        *,
        assume_locked: bool = False,
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

        When ``assume_locked`` is True, the caller already holds
        ``FileLock(self._sw.lock_file)`` and this method runs the verification
        loop without acquiring again. ``FileLock`` is not re-entrant — callers
        inside an existing lock (e.g. ``sync_live_to_backup``) must pass
        ``assume_locked=True``.

        When ``assume_locked`` is False (default), this method acquires the
        lock itself, blocking until it is available.
        """
        if assume_locked:
            return self._write_verified_live_body(account_num, email, credentials)

        with FileLock(self._sw.lock_file):
            return self._write_verified_live_body(account_num, email, credentials)

    def _write_verified_live_body(
        self,
        account_num: str,
        email: str,
        credentials: str,
    ) -> str:
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

            if previous_live is not None and live_now != previous_live:
                live_keeps_changing = True
            previous_live = live_now

            if attempt == _BACKUP_CREDENTIAL_VERIFY_ATTEMPTS - 1:
                if live_keeps_changing:
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

        raise CredentialWriteError("backup credential verification fell through unexpectedly")

    def _persist_account_verified(
        self,
        account_num: str,
        email: str,
        credentials: str,
    ) -> None:
        """Write an inactive backup slot and read it back to confirm it stuck.

        Unlike ``write_verified_live`` (which reconciles against the *live*
        active credential), this verifies the backup slot against the exact
        payload written — correct for an inactive target whose creds are not
        the live store. Raises ``CredentialWriteError`` if the read-back does
        not match, so a refresh that consumed a single-use token cannot leave a
        silently stale/bricked backup.
        """
        self._sw._write_account_credentials(account_num, email, credentials)
        stored = self._sw._read_account_credentials(account_num, email)
        if stored != credentials:
            raise CredentialWriteError(
                f"Backup credential read-back mismatch for account-{account_num} "
                "after OAuth refresh"
            )

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
            with FileLock(self._sw.lock_file):
                stored = self._sw._read_account_credentials(account_num, email)
                if stored == credentials:
                    return
                self.write_verified_live(
                    account_num,
                    email,
                    credentials,
                    assume_locked=True,
                )
            self._sw._logger.info("Synced refreshed live credentials for account %s", account_num)
        except (CredentialReadError, CredentialWriteError, OSError) as exc:
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

        Only called from ``_perform_switch`` while ``FileLock`` is already
        held; network refresh here stays under that caller lock (switcher track).
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

        # Anthropic refresh tokens are single-use: the network refresh above has
        # already invalidated the old one. A write that returns success but did
        # not durably store (e.g. a Keychain ACL hiccup) would leave the backup
        # holding the now-dead pre-refresh token — a silently bricked slot.
        # Read-back verify so that surfaces as CredentialWriteError instead.
        self._persist_account_verified(account_num, email, refreshed)
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

        Re-reads on disk under ``FileLock`` before and after the OAuth
        network round-trip (which runs outside the lock). If another writer
        already refreshed this slot, the on-disk token is returned without a
        redundant network call or overwrite. Anthropic refresh tokens are
        single-use (claude-code#24317); a double refresh would brick the
        slot with invalid_grant.
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
            original_refresh = latest_oauth.get("refreshToken") if latest_oauth else None

        refreshed = oauth.refresh_oauth_credentials(latest)
        if not refreshed:
            # Another process may have refreshed this slot while our network
            # call was failing — re-read under the lock before reporting a
            # stale/expired token, so we don't show "refresh failed" over a
            # good on-disk backup.
            with FileLock(self._sw.lock_file):
                current = (
                    self._sw._read_account_credentials(account_num, email)
                    or credentials
                )
                current_oauth = oauth.extract_oauth_data(current)
                if (
                    current_oauth
                    and current_oauth.get("accessToken")
                    and not oauth.is_oauth_token_expired(
                        current_oauth.get("expiresAt")
                    )
                ):
                    self._sw._logger.info(
                        "OAuth refresh skipped (fresh on disk after failure): "
                        "account=%s",
                        account_num,
                    )
                    return current, "token already fresh on disk"
            self._sw._logger.info(
                "OAuth refresh unavailable: account=%s email=%s",
                account_num,
                email,
            )
            return latest, "token refresh failed"

        with FileLock(self._sw.lock_file):
            current = self._sw._read_account_credentials(account_num, email) or credentials
            current_oauth = oauth.extract_oauth_data(current)
            if (
                current_oauth
                and current_oauth.get("accessToken")
                and not oauth.is_oauth_token_expired(current_oauth.get("expiresAt"))
            ):
                self._sw._logger.info(
                    "OAuth refresh skipped (already fresh on disk): account=%s",
                    account_num,
                )
                return current, "token already fresh on disk"
            current_refresh = current_oauth.get("refreshToken") if current_oauth else None
            if original_refresh and current_refresh != original_refresh:
                self._sw._logger.info(
                    "OAuth refresh skipped (refresh token changed on disk): account=%s",
                    account_num,
                )
                return current, "token already fresh on disk"

            self._persist_account_verified(account_num, email, refreshed)
            self._sw._logger.info("Refreshed inactive credentials for account %s", account_num)
            return refreshed, "token refreshed"
