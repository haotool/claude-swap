"""Credential storage layer for claude-swap.

Owns *where* credentials live and *how* they are read/written — the macOS
Keychain-vs-file routing, per-process capability detection and sticky fallback,
and the ``.enc``-wins backup reconciliation that landed in #66. Split out of
``switcher.py`` so the switcher reads as account orchestration again.

``CredentialStore`` is a leaf collaborator: it imports only the OS-primitive and
path helpers (``macos_keychain``, ``paths``) and never imports ``switcher``. It
reads its live configuration (``platform``, ``_logger``, ``credentials_dir``)
from a host *view* — a small data-only window onto the switcher that constructs
it — and must never call a switcher *method* through that host, or storage and
orchestration would re-couple. The store owns only its two pieces of state:
``_keychain_usable_cache`` (sticky, process-local) and
``_last_active_credentials_backend`` (for the post-switch follow-up message).
"""

from __future__ import annotations

import base64
import json
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
    get_global_config_path,
)

# Service name for per-account backup credentials managed via the ``security``
# CLI on macOS. Deliberately distinct from the legacy keyring service so old
# keyring items and new security items coexist during migration.
SECURITY_SERVICE = "claude-swap"

# Service name of Claude Code's *active* OAuth credential in the macOS Keychain
# (read by Claude Code itself; we read/write it when switching accounts).
CLAUDE_CODE_KEYCHAIN_SERVICE = "Claude Code-credentials"

# Service name of Claude Code's *active* managed API key (``/login`` with an
# ``sk-ant-api…`` key) in the macOS Keychain. Distinct from the OAuth service
# above (no ``-credentials`` suffix); Claude Code resolves it on a separate auth
# axis. On non-macOS the managed key instead lives in ``~/.claude.json`` as
# ``primaryApiKey``.
CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE = "Claude Code"


def looks_like_api_key(credentials: str | None) -> bool:
    """Whether a stored active credential is a raw managed API key vs OAuth JSON.

    Strict on purpose: a managed key is a bare ``sk-ant-api…`` string, while every
    OAuth/setup-token credential is a JSON object (``{"claudeAiOauth": …}``).
    Requiring the ``sk-ant-api`` prefix (and that it isn't JSON) keeps a
    raw/garbled ``sk-ant-oat…`` setup token from ever being misclassified.
    """
    if not credentials:
        return False
    text = credentials.strip()
    return text.startswith("sk-ant-api") and not text.startswith("{")


def approved_form(api_key: str) -> str:
    """The value Claude Code stores in ``customApiKeyResponses.approved``.

    Mirrors Claude Code's ``normalizeApiKeyForConfig`` (``apiKey.slice(-20)``):
    the last 20 chars. Storing anything else makes Claude Code's "is this key
    approved?" check miss and re-prompt the user to approve the key.
    """
    return api_key.strip()[-20:]


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

    One store per switcher.
    """

    def __init__(self, host: _StoreHost):
        self._host = host

    def _read_credentials(self) -> str | None:
        """Read Claude Code's active credential — OAuth *or* managed API key.

        Tries the OAuth credential fully first (macOS Keychain
        "Claude Code-credentials", else ``~/.claude/.credentials.json``), then
        the managed-key locations (macOS Keychain "Claude Code", then
        ``~/.claude.json`` ``primaryApiKey``). Trying OAuth fully first means an
        OAuth login is never misread as an API key. A returned managed key is a
        raw ``sk-ant-api…`` string — callers distinguish it via
        ``looks_like_api_key``.

        Returns:
            Credential string if found, "" if not found, None on a read error.
        """
        oauth_val = self._read_oauth_credentials()
        if oauth_val is None:
            return None
        if oauth_val:
            return oauth_val
        return self._read_managed_key()

    def _read_oauth_credentials(self) -> str | None:
        """Read Claude Code's active OAuth credential (no managed-key fallback).

        Returns:
            Credentials string if found, empty string if not found, None on error.
        """
        if self._host.platform == Platform.MACOS:
            try:
                val = macos_keychain.get_password(
                    CLAUDE_CODE_KEYCHAIN_SERVICE, os.environ.get("USER", "user")
                )
            except Exception as e:
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

    def _read_managed_key(self) -> str:
        """Read the active managed API key, or "" when absent. Non-mutating.

        macOS Keychain "Claude Code" first, then ``~/.claude.json``
        ``primaryApiKey`` — mirroring Claude Code's resolution order.
        """
        if self._host.platform == Platform.MACOS:
            try:
                val = macos_keychain.get_password(
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    os.environ.get("USER", "user"),
                )
            except Exception as e:
                self._host._logger.warning(f"Managed-key Keychain read failed: {e}")
                val = None
            if val:
                return val
        cfg = self._read_global_config()
        if cfg:
            key = cfg.get("primaryApiKey")
            if isinstance(key, str) and key:
                return key
        return ""

    def _read_global_config(self) -> dict | None:
        """Read and parse ``~/.claude.json``, or None when absent/unreadable."""
        path = get_global_config_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._host._logger.warning(f"Failed to read global config: {e}")
            return None
        return data if isinstance(data, dict) else None

    def _update_global_config(self, mutator) -> None:
        """Atomically apply ``mutator(dict)`` to ``~/.claude.json``, key-scoped.

        Reads the current config, lets ``mutator`` change only the keys it owns
        (``primaryApiKey`` / ``customApiKeyResponses``), and writes it back
        atomically — preserving every other key (``oauthAccount``, projects,
        settings). 0o600 mirrors the switcher's write path.
        """
        path = get_global_config_path()
        try:
            data = self._read_global_config() or {}
        except Exception as e:  # pragma: no cover - defensive
            raise CredentialWriteError(f"Failed to read global config for update: {e}")
        mutator(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(path))
            if sys.platform != "win32":
                os.chmod(str(path), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _write_credentials(self, credentials: str, *, verify: bool = False) -> None:
        """Write Claude Code's active credential, enforcing a single auth axis.

        Detects the kind from the payload (raw ``sk-ant-api…`` key vs OAuth JSON)
        and mirrors Claude Code's own save/remove: activating one axis clears the
        other so a stale credential can't shadow the switch.

        - **OAuth** → write the OAuth credential, then clear any managed key.
        - **API key** → store the key (macOS Keychain "Claude Code", else
          ``~/.claude.json`` ``primaryApiKey``) and record ``key[-20:]`` in
          ``customApiKeyResponses.approved``, then clear the OAuth credential.

        ``verify=True`` (OAuth path only) read-backs and confirms the payload
        matches what was written — guards against silent Keychain corruption on
        the activation path.

        Raises:
            CredentialWriteError: If writing fails, or if ``verify=True`` (OAuth)
                and the readback does not match the intended payload.
        """
        if looks_like_api_key(credentials):
            self._write_managed_credentials(credentials.strip())
            return

        self._write_oauth_credentials(credentials)
        self._clear_managed_key()

        if verify:
            readback = self._read_credentials()
            if readback != credentials:
                raise CredentialWriteError(
                    "Credential write verification failed: readback differs "
                    "from intended payload. Possible silent Keychain corruption "
                    "or concurrent overwrite. Aborting switch."
                )

    def _write_oauth_credentials(self, credentials: str) -> None:
        """Write Claude Code's active OAuth credential (no axis clearing).

        - macOS: Keychain with service "Claude Code-credentials"
        - Linux/WSL/Windows: File at ~/.claude/.credentials.json
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

    def _write_managed_credentials(self, api_key: str) -> None:
        """Activate a managed API key, then clear OAuth (mutual exclusion).

        Always records ``key[-20:]`` in ``customApiKeyResponses.approved`` (Claude
        Code does this on every platform — otherwise it re-prompts to approve the
        key). Stores the key in the macOS Keychain when on macOS, else
        ``~/.claude.json`` ``primaryApiKey``. Finally clears the OAuth credential.

        Raises:
            CredentialWriteError: If persisting the key fails.
        """
        wrote_to_keychain = False
        if self._host.platform == Platform.MACOS:
            try:
                macos_keychain.set_password(
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    os.environ.get("USER", "user"),
                    api_key,
                )
            except Exception as e:
                self._host._logger.warning(
                    f"Managed-key Keychain write failed, falling back to config: {e}"
                )
            else:
                wrote_to_keychain = True

        approved = approved_form(api_key)

        def _mutate(cfg: dict) -> None:
            responses = cfg.get("customApiKeyResponses")
            if not isinstance(responses, dict):
                responses = {}
            approved_list = responses.get("approved")
            if not isinstance(approved_list, list):
                approved_list = []
            if approved not in approved_list:
                approved_list.append(approved)
            responses["approved"] = approved_list
            responses.setdefault("rejected", [])
            cfg["customApiKeyResponses"] = responses
            if wrote_to_keychain:
                cfg.pop("primaryApiKey", None)
            else:
                cfg["primaryApiKey"] = api_key

        try:
            self._update_global_config(_mutate)
        except CredentialWriteError:
            raise
        except Exception as e:
            raise CredentialWriteError(f"Failed to write managed API key: {e}")

        self._clear_oauth_credential()

    def _clear_managed_key(self) -> None:
        """Clear any active managed API key (Claude Code ``removeApiKey`` semantics).

        Deletes the macOS Keychain "Claude Code" item (best-effort) and drops
        ``primaryApiKey`` from ``~/.claude.json``. Leaves
        ``customApiKeyResponses.approved`` untouched — removing it would force
        recovering ``key[-20:]`` from the Keychain for no benefit. A no-op (no
        config rewrite) when no key is present.
        """
        if self._host.platform == Platform.MACOS:
            try:
                macos_keychain.delete_password(
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    os.environ.get("USER", "user"),
                )
            except Exception:
                pass  # best-effort; a down Keychain can't be cleaned now
        cfg = self._read_global_config()
        if cfg is not None and cfg.get("primaryApiKey") is not None:
            def _drop(c: dict) -> None:
                c.pop("primaryApiKey", None)

            try:
                self._update_global_config(_drop)
            except Exception as e:
                self._host._logger.warning(f"Failed to clear primaryApiKey: {e}")

    def _clear_oauth_credential(self) -> None:
        """Clear the active OAuth credential — Keychain item and plaintext file.

        Best-effort: a down Keychain or missing file is fine. Removing
        ``.credentials.json`` stops Claude Code from falling back to a stale
        OAuth login over the just-activated API key.
        """
        if self._host.platform == Platform.MACOS:
            try:
                macos_keychain.delete_password(
                    CLAUDE_CODE_KEYCHAIN_SERVICE, os.environ.get("USER", "user")
                )
            except Exception:
                pass  # best-effort
        cred_file = get_credentials_path()
        try:
            if cred_file.exists():
                cred_file.unlink()
        except OSError as e:
            self._host._logger.warning(f"Failed to remove credentials file: {e}")

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
        """
        if self._uses_file_backup_backend():
            cred_file = self._host.credentials_dir / f".creds-{account_num}-{email}.enc"
            try:
                encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
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
