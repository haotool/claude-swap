"""Credential storage layer for Claude Code accounts.

Extracted from ``switcher.py`` (plan 020) so the switcher no longer owns the
keychain/file mechanics of reading and writing credentials. ``CredentialStore``
owns two stores:

- the **active** credential Claude Code itself reads (macOS Keychain service
  ``"Claude Code-credentials"``; elsewhere ``~/.claude/.credentials.json``), and
- the **per-account backup** credentials claude-swap keeps for inactive slots
  (macOS Keychain service ``"claude-swap"``; elsewhere base64 ``.enc`` files
  under ``credentials_dir``).

The store reads its live configuration (``platform``, ``credentials_dir``,
``_logger``) off a host via the ``_StoreHost`` Protocol at call time, so a
switcher that mutates those attributes post-construction (e.g. tests setting
``switcher.platform``) is honored. The store must not reach for any *method* on
the host — session-lifecycle side effects (invalidating a slot's session
profile after a backup write) stay in the switcher, which wraps the store's
pure write.

Interface and method names mirror upstream ``credentials.py`` to keep future
selective convergence a drop-in. ``_write_credentials``'s ``verify`` keyword is
a claude-swap addition (activation-path read-back verification) not present
upstream.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Protocol

from claude_swap import macos_keychain
from claude_swap.exceptions import CredentialWriteError
from claude_swap.models import Platform
from claude_swap.paths import (
    get_claude_config_home,
    get_credentials_path,
)

# Service name under which the legacy ``keyring`` backend stored per-account
# backup credentials lives in switcher.py (KEYRING_SERVICE) — it is a migration
# concern, not part of the active credential store, so it stays there.

# Service name for per-account backup credentials managed via the ``security``
# CLI on macOS. Deliberately distinct from the legacy keyring service so old
# keyring items and new security items coexist during migration.
SECURITY_SERVICE = "claude-swap"

# Service name of Claude Code's *active* credential in the macOS Keychain (read
# by Claude Code itself; we read/write it when switching accounts).
CLAUDE_CODE_KEYCHAIN_SERVICE = "Claude Code-credentials"


class _StoreHost(Protocol):
    """The live configuration view ``CredentialStore`` reads from its owner.

    Data only — the store reads these attributes at call time so post-construction
    overrides (e.g. tests setting ``switcher.platform``) are honored. The store
    must not reach for any *method* here.
    """

    platform: Platform
    credentials_dir: Path
    _logger: logging.Logger


class CredentialStore:
    """Owns the active and per-account backup credential stores.

    One store per switcher. Constructed with the switcher as its host; the
    switcher satisfies ``_StoreHost`` by exposing ``platform`` /
    ``credentials_dir`` / ``_logger``.
    """

    def __init__(self, host: _StoreHost):
        self._host = host

    # -- active credential (Claude Code's own store) ----------------------

    def _read_credentials(self) -> str | None:
        """Read credentials from Claude Code's storage.

        Claude Code stores credentials in:
        - macOS: Keychain with service "Claude Code-credentials"
        - Linux/WSL/Windows: File at ~/.claude/.credentials.json

        Returns:
            Credentials string if found, empty string if not found, None on error.
        """
        if self._host.platform == Platform.MACOS:
            try:
                val = macos_keychain.get_password(
                    CLAUDE_CODE_KEYCHAIN_SERVICE, os.environ.get("USER", "user")
                )
            except Exception as e:
                # rc-44 (not found) is returned as None by the wrapper, not raised;
                # anything raised here is a genuine error (locked / denied / etc.).
                self._host._logger.error(f"Failed to read credentials: {e}")
                return None
            return val if val is not None else ""
        else:  # Linux/WSL/Windows - credentials stored in file
            cred_file = get_credentials_path()
            if cred_file.exists():
                try:
                    return cred_file.read_text(encoding="utf-8")
                except Exception as e:
                    self._host._logger.error(f"Failed to read credentials file: {e}")
                    return None
            return ""

    def _write_credentials(self, credentials: str, *, verify: bool = False) -> None:
        """Write credentials to Claude Code's storage.

        Claude Code stores credentials in:
        - macOS: Keychain with service "Claude Code-credentials"
        - Linux/WSL/Windows: File at ~/.claude/.credentials.json

        Args:
            credentials: The credential payload to persist (raw string).
            verify: When True, immediately read the credentials back from the
                storage layer and confirm the readback matches what was
                written. Defends against silent Keychain ACL corruption and
                concurrent overwrites by other processes between our write and
                the next operation. Recommended for activation-path writes;
                left False on rollback writes where verification failure would
                mask the original cause of the rollback. (claude-swap addition;
                not present in upstream.)

        Raises:
            CredentialWriteError: If writing credentials fails, or if
                ``verify=True`` and the readback does not match the intended
                payload.
        """
        if self._host.platform == Platform.MACOS:
            try:
                macos_keychain.set_password(
                    CLAUDE_CODE_KEYCHAIN_SERVICE,
                    os.environ.get("USER", "user"),
                    credentials,
                )
            except Exception as e:
                raise CredentialWriteError(f"Failed to write credentials: {e}")
        else:  # Linux/WSL/Windows - credentials stored in file
            cred_dir = get_claude_config_home()
            cred_dir.mkdir(parents=True, exist_ok=True)
            cred_file = cred_dir / ".credentials.json"
            try:
                fd, tmp_path = tempfile.mkstemp(dir=str(cred_dir), suffix=".tmp")
                try:
                    os.write(fd, credentials.encode("utf-8"))
                    os.close(fd)
                    fd = -1
                    os.replace(tmp_path, str(cred_file))
                    if sys.platform != "win32":
                        os.chmod(str(cred_file), 0o600)
                except BaseException:
                    if fd >= 0:
                        os.close(fd)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            except Exception as e:
                raise CredentialWriteError(f"Failed to write credentials: {e}")

        if verify:
            readback = self._read_credentials()
            if readback != credentials:
                # We deliberately do NOT include credential payloads in the
                # error message (avoid leaking secrets into logs).
                raise CredentialWriteError(
                    "Credential write verification failed: readback differs "
                    "from intended payload. Possible silent Keychain corruption "
                    "or concurrent overwrite. Aborting switch."
                )

    # -- per-account backup credentials -----------------------------------

    def _uses_file_backup_backend(self) -> bool:
        """Whether per-account backup credentials live in files vs. the Keychain.

        Linux/WSL/Windows store them as base64 files under ``credentials_dir``;
        macOS (and any UNKNOWN platform) use the macOS Keychain (via the
        ``security`` CLI). Windows moved to files because the Windows Credential
        Manager rejects entries over ~2,500 bytes, which Claude Code session
        credentials can exceed (#45).
        """
        return self._host.platform in (Platform.LINUX, Platform.WSL, Platform.WINDOWS)

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        """Read account credentials from backup.

        On Linux/WSL/Windows: Uses file-based storage (base64 files under
        ``credentials_dir``). On macOS: Uses the Keychain via the ``security`` CLI.
        """
        if self._uses_file_backup_backend():
            cred_file = self._host.credentials_dir / f".creds-{account_num}-{email}.enc"
            if cred_file.exists():
                try:
                    encoded = cred_file.read_text(encoding="utf-8")
                    return base64.b64decode(encoded).decode("utf-8")
                except Exception as e:
                    self._host._logger.warning(f"Failed to read credentials file: {e}")
                    return ""
            return ""
        else:
            # macOS: per-account backup credentials in the Keychain via `security`.
            username = f"account-{account_num}-{email}"
            try:
                creds = macos_keychain.get_password(SECURITY_SERVICE, username)
                return creds if creds else ""
            except Exception as e:
                self._host._logger.warning(f"Failed to read credentials from Keychain: {e}")
                return ""

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup (pure storage).

        On Linux/WSL/Windows: Uses file-based storage (base64 files under
        ``credentials_dir``). On macOS: Uses the Keychain via the ``security`` CLI.

        Session-profile invalidation after a backup change is **not** done here
        — that is a switcher-owned lifecycle side effect (the store is data-only
        toward its host). The switcher's same-named wrapper adds it.
        """
        if self._uses_file_backup_backend():
            cred_file = self._host.credentials_dir / f".creds-{account_num}-{email}.enc"
            try:
                encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
                # Atomic 0o600 write: ``write_text`` would land the file with
                # the user's umask (typically 0o644) for the window before the
                # explicit ``chmod``, exposing the base64-encoded token to any
                # same-UID process that races a read.  ``mkstemp`` creates the
                # temp file with 0o600 directly, and ``os.replace`` is atomic
                # within the directory.
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self._host.credentials_dir), suffix=".tmp",
                )
                try:
                    os.write(fd, encoded.encode("utf-8"))
                    os.close(fd)
                    fd = -1
                    os.replace(tmp_path, str(cred_file))
                    if sys.platform != "win32":
                        os.chmod(str(cred_file), 0o600)
                except BaseException:
                    if fd >= 0:
                        os.close(fd)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            except Exception as e:
                self._host._logger.warning(f"Failed to write credentials file: {e}")
                raise
        else:
            # macOS: per-account backup credentials in the Keychain via `security`.
            username = f"account-{account_num}-{email}"
            try:
                macos_keychain.set_password(SECURITY_SERVICE, username, credentials)
            except Exception as e:
                self._host._logger.warning(f"Failed to write credentials to Keychain: {e}")
                raise

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        """Delete account credentials from backup.

        On Linux/WSL/Windows: Deletes file-based credential storage.
        On macOS: Removes from the Keychain via the ``security`` CLI.
        """
        if self._uses_file_backup_backend():
            cred_files = [self._host.credentials_dir / f".creds-{account_num}-{email}.enc"]
            if str(account_num) != "None":
                cred_files.append(self._host.credentials_dir / f".creds-None-{email}.enc")
            for cred_file in cred_files:
                try:
                    if cred_file.exists():
                        cred_file.unlink()
                except Exception as e:
                    self._host._logger.warning(f"Failed to delete credentials file: {e}")
        else:
            # macOS: per-account backup credentials in the Keychain via `security`.
            usernames = [f"account-{account_num}-{email}"]
            if str(account_num) != "None":
                usernames.append(f"account-None-{email}")
            for username in usernames:
                try:
                    macos_keychain.delete_password(SECURITY_SERVICE, username)
                except Exception as e:
                    self._host._logger.warning(f"Failed to delete credentials from Keychain: {e}")
