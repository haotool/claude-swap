"""Tests for the credentials module.

These prove the store is independently testable against a minimal ``_StoreHost``
— a plain object exposing ``platform`` / ``credentials_dir`` / ``_logger`` and
**no methods** — which is the whole point of the extraction: the credential
storage layer no longer needs a full ``ClaudeAccountSwitcher`` to exercise.

``TestCredentialRefresherLocking`` uses the same pattern for ``RefreshHost``:
``_refresher_host`` is a ``SimpleNamespace`` fake that satisfies the Protocol
structurally (no inheritance, no full switcher).

The file (Linux/WSL/Windows) backup backend is used because it depends only on
the injected ``credentials_dir`` — no real Keychain and no ``$HOME`` coupling —
so the Protocol boundary is exercised in isolation on every platform.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap.credential_refresh import CredentialRefresher
from claude_swap.credentials import CredentialStore
from claude_swap.exceptions import CredentialWriteError, LockError
from claude_swap.locking import FileLock
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


def _oauth_creds(token: str = "live-token") -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": "rt",
                "expiresAt": 9_999_999_999_000,
            },
        }
    )


def _refresher_host(tmp_path: Path) -> SimpleNamespace:
    creds_dir = tmp_path / "credentials"
    creds_dir.mkdir(parents=True, exist_ok=True)
    lock_file = tmp_path / ".lock"
    store: dict[tuple[str, str], str] = {}
    live = {"creds": _oauth_creds()}

    host = SimpleNamespace(
        platform=Platform.LINUX,
        credentials_dir=creds_dir,
        lock_file=lock_file,
        _logger=logging.getLogger("test.credential_refresh"),
    )
    host._read_account_credentials = lambda num, email: store.get((num, email), "")
    host._write_account_credentials = lambda num, email, creds: store.__setitem__(
        (num, email),
        creds,
    )
    host._read_credentials = lambda: live["creds"]
    host._store = store
    host._live = live
    return host


class TestCredentialRefresherLocking:
    def test_write_verified_live_acquires_lock_when_uncontested(self, tmp_path: Path):
        host = _refresher_host(tmp_path)
        refresher = CredentialRefresher(host)
        acquires: list[bool] = []
        real_acquire = FileLock.acquire

        def track_acquire(lock_self, timeout=None):
            acquires.append(True)
            return real_acquire(lock_self, timeout)

        with patch.object(FileLock, "acquire", track_acquire):
            result = refresher.write_verified_live(
                "1", "a@example.com", host._live["creds"]
            )

        assert acquires == [True]
        assert result == host._live["creds"]
        assert host._store[("1", "a@example.com")] == host._live["creds"]

    def test_write_verified_live_skips_reentrant_lock_when_already_held(
        self,
        tmp_path: Path,
    ):
        host = _refresher_host(tmp_path)
        refresher = CredentialRefresher(host)

        with FileLock(host.lock_file):
            result = refresher.write_verified_live(
                "1",
                "a@example.com",
                host._live["creds"],
                assume_locked=True,
            )

        assert result == host._live["creds"]
        assert host._store[("1", "a@example.com")] == host._live["creds"]

    def test_write_verified_live_blocks_while_lock_held_by_other(self, tmp_path: Path):
        host = _refresher_host(tmp_path)
        refresher = CredentialRefresher(host)
        body_entered = threading.Event()
        original_body = refresher._write_verified_live_body

        def tracked_body(*args, **kwargs):
            body_entered.set()
            return original_body(*args, **kwargs)

        with patch.object(
            refresher, "_write_verified_live_body", side_effect=tracked_body
        ):
            with FileLock(host.lock_file):
                thread = threading.Thread(
                    target=refresher.write_verified_live,
                    args=("1", "a@example.com", host._live["creds"]),
                )
                thread.start()
                body_entered.wait(timeout=0.5)
                assert not body_entered.is_set()

            thread.join(timeout=5)

        assert body_entered.is_set()
        assert host._store[("1", "a@example.com")] == host._live["creds"]

    def test_sync_live_to_backup_holds_lock_for_read_compare_write(
        self,
        tmp_path: Path,
    ):
        host = _refresher_host(tmp_path)
        refresher = CredentialRefresher(host)
        host._live["creds"] = _oauth_creds("rotated-live")
        lock_held_during_write = {"value": False}

        original_write = refresher.write_verified_live

        def write_under_lock(num, email, creds, *, assume_locked=False):
            assert assume_locked is True
            lock = FileLock(host.lock_file)
            lock_held_during_write["value"] = lock.acquire(timeout=0) is False
            return original_write(num, email, creds, assume_locked=assume_locked)

        with patch.object(
            refresher, "write_verified_live", side_effect=write_under_lock
        ):
            refresher.sync_live_to_backup("1", "a@example.com", host._live["creds"])

        assert lock_held_during_write["value"] is True

    def test_sync_live_to_backup_waits_when_lock_contended(self, tmp_path: Path):
        host = _refresher_host(tmp_path)
        refresher = CredentialRefresher(host)
        host._live["creds"] = _oauth_creds("rotated-live")

        with FileLock(host.lock_file):
            with pytest.raises(LockError):
                refresher.sync_live_to_backup("1", "a@example.com", host._live["creds"])

    def test_refresh_inactive_does_not_hold_lock_during_network_refresh(
        self,
        tmp_path: Path,
    ):
        host = _refresher_host(tmp_path)
        refresher = CredentialRefresher(host)
        expired = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old",
                    "refreshToken": "rt-old",
                    "expiresAt": 0,
                },
            }
        )
        refreshed = _oauth_creds("new")
        host._store[("1", "a@example.com")] = expired
        lock_events: list[str] = []

        real_acquire = FileLock.acquire
        real_release = FileLock.release

        def track_acquire(lock_self, timeout=None):
            lock_events.append("acquire")
            return real_acquire(lock_self, timeout)

        def track_release(lock_self):
            lock_events.append("release")
            return real_release(lock_self)

        def refresh_outside_lock(creds):
            lock_events.append("network")
            probe = FileLock(host.lock_file)
            assert probe.acquire(timeout=0) is True
            probe.release()
            return refreshed

        with (
            patch.object(FileLock, "acquire", track_acquire),
            patch.object(FileLock, "release", track_release),
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                side_effect=refresh_outside_lock,
            ),
        ):
            result, note = refresher.refresh_inactive_if_needed(
                "1",
                "a@example.com",
                expired,
            )

        assert result == refreshed
        assert note == "token refreshed"
        assert lock_events.index("network") > lock_events.index("release")
        assert any(
            i > lock_events.index("network")
            for i, event in enumerate(lock_events)
            if event == "acquire"
        )

    def test_refresh_inactive_raises_when_persist_does_not_stick(
        self,
        tmp_path: Path,
    ):
        # A single-use refresh token is consumed by the network refresh. If the
        # backup write silently fails to persist (read-back mismatch), the slot
        # must surface CredentialWriteError, not return a stale/bricked backup.
        host = _refresher_host(tmp_path)
        refresher = CredentialRefresher(host)
        expired = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old",
                    "refreshToken": "rt-old",
                    "expiresAt": 0,
                },
            }
        )
        host._store[("1", "a@example.com")] = expired

        with (
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=_oauth_creds("new"),
            ),
            patch.object(host, "_write_account_credentials", lambda *a, **k: None),
            pytest.raises(CredentialWriteError, match="read-back mismatch"),
        ):
            refresher.refresh_inactive_if_needed("1", "a@example.com", expired)
