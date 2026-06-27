"""Tests for the credentials module.

These prove the store is independently testable against a minimal ``_StoreHost``
— a plain object exposing ``platform`` / ``credentials_dir`` / ``_logger`` and
**no methods** — which is the whole point of the extraction: the credential
storage layer no longer needs a full ``ClaudeAccountSwitcher`` to exercise.

The file (Linux/WSL/Windows) backup backend is used because it depends only on
the injected ``credentials_dir`` — no real Keychain and no ``$HOME`` coupling —
so the Protocol boundary is exercised in isolation on every platform.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap.credentials import CredentialStore
from claude_swap.models import Platform


def _file_host(tmp_path: Path) -> SimpleNamespace:
    """A minimal data-only host for the file backup backend."""
    creds_dir = tmp_path / "credentials"
    creds_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        platform=Platform.LINUX,
        credentials_dir=creds_dir,
        _logger=logging.getLogger("test.credentials"),
    )


def test_store_constructs_from_data_only_host(tmp_path: Path):
    """A SimpleNamespace (no methods) satisfies the host contract."""
    store = CredentialStore(_file_host(tmp_path))
    assert store._uses_file_backup_backend() is True


def test_account_credentials_file_round_trip(tmp_path: Path):
    """write → read returns the same payload via the base64 .enc file."""
    store = CredentialStore(_file_host(tmp_path))
    store._write_account_credentials("1", "alice@example.com", "secret-token")
    assert store._read_account_credentials("1", "alice@example.com") == "secret-token"


def test_account_credentials_written_0600(tmp_path: Path):
    """Backup file lands with owner-only permissions (no umask window)."""
    host = _file_host(tmp_path)
    store = CredentialStore(host)
    store._write_account_credentials("2", "bob@example.com", "tok")
    enc = host.credentials_dir / ".creds-2-bob@example.com.enc"
    assert enc.exists()
    assert (enc.stat().st_mode & 0o777) == 0o600


def test_read_missing_account_returns_empty(tmp_path: Path):
    """A slot with no backup reads as "" (not an error / not None)."""
    store = CredentialStore(_file_host(tmp_path))
    assert store._read_account_credentials("9", "nobody@example.com") == ""


def test_delete_account_credentials_removes_file(tmp_path: Path):
    store = CredentialStore(_file_host(tmp_path))
    store._write_account_credentials("3", "carol@example.com", "tok")
    store._delete_account_credentials("3", "carol@example.com")
    assert store._read_account_credentials("3", "carol@example.com") == ""


def test_post_construction_platform_override_is_honored(tmp_path: Path):
    """The store reads ``platform`` off the host at call time, not construction.

    Mutating the host after construction flips the backend — this is why the
    Protocol is data-only and read lazily (mirrors tests setting
    ``switcher.platform`` post-init).
    """
    host = _file_host(tmp_path)
    store = CredentialStore(host)
    assert store._uses_file_backup_backend() is True
    host.platform = Platform.MACOS
    assert store._uses_file_backup_backend() is False


@pytest.mark.parametrize("platform", [Platform.LINUX, Platform.WSL, Platform.WINDOWS])
def test_file_backends(tmp_path: Path, platform: Platform):
    host = _file_host(tmp_path)
    host.platform = platform
    store = CredentialStore(host)
    assert store._uses_file_backup_backend() is True
