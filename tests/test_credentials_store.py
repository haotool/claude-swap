"""macOS Keychain fallback, _kc_* routing, and write-verify drift handling."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE, SECURITY_SERVICE
from claude_swap.exceptions import (
    CredentialWriteError,
)
from claude_swap.macos_keychain import KeychainError
from claude_swap.models import Platform
from claude_swap.paths import get_credentials_path
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.conftest import raise_locked as _raise_locked


class TestWriteVerifiedLiveDriftHandling:
    """Lock the two drift modes of ``_write_verified_live_account_credentials``:

    1. Persistent Claude Code rotation under us → log warning, persist last
       sampled live state, do NOT raise.
    2. Persistent storage write failure (live stable, our write never sticks)
       → raise ``CredentialWriteError`` so the genuine failure surfaces.
    """

    def _creds(self, token: str) -> str:
        return json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "rt"}})

    def test_persistent_live_rotation_does_not_raise(
        self, temp_home: Path, monkeypatch, caplog,
    ):
        """Simulates Claude Code refreshing its token on every verification
        attempt.  The function must terminate with a warning and persist the
        last sampled live state instead of raising CredentialWriteError."""

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        # Each iteration: live_now reads return a DIFFERENT token, simulating
        # Claude Code refreshing concurrently.  Our writes always "succeed"
        # via the in-memory store, but live_now never equals stored.
        store: dict = {}

        def write_acct(num, email, creds):
            store["backup"] = creds

        def read_acct(num, email):
            return store.get("backup", "")

        live_iter = iter([
            self._creds("live-1"),
            self._creds("live-2"),
            self._creds("live-3"),
        ])

        def read_live():
            return next(live_iter)

        monkeypatch.setattr(switcher, "_write_account_credentials", write_acct)
        monkeypatch.setattr(switcher, "_read_account_credentials", read_acct)
        monkeypatch.setattr(switcher, "_read_credentials", read_live)
        monkeypatch.setattr("claude_swap.switcher.time.sleep", lambda *_: None)

        caplog.set_level(logging.WARNING, logger="claude-swap")

        result = switcher._write_verified_live_account_credentials(
            "2", "b@example.com", self._creds("intended"),
        )

        # Last sampled live state is what gets persisted as the backup.
        assert result == self._creds("live-3")
        assert store["backup"] == self._creds("live-3")

        warnings = [
            r.getMessage() for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any(
            "persistent in-flight Claude Code rotation" in m for m in warnings
        ), warnings

    def test_persistent_storage_write_failure_raises(
        self, temp_home: Path, monkeypatch,
    ):
        """If live_now is stable but our write never sticks, raise so the
        genuine storage failure surfaces — don't silently swallow it as a
        rotation event."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        # Our writes are no-ops — stored stays empty.  live_now is stable.
        monkeypatch.setattr(
            switcher, "_write_account_credentials", lambda *_: None,
        )
        monkeypatch.setattr(
            switcher, "_read_account_credentials", lambda *_: "",
        )
        stable_live = self._creds("stable")
        monkeypatch.setattr(switcher, "_read_credentials", lambda: stable_live)
        monkeypatch.setattr("claude_swap.switcher.time.sleep", lambda *_: None)

        with pytest.raises(CredentialWriteError, match="did not match"):
            switcher._write_verified_live_account_credentials(
                "2", "b@example.com", stable_live,
            )


class TestMacosKeychainFallback:
    """macOS auto-fallback to file storage when the Keychain is unusable, plus the
    ``.enc``-wins backup reconciliation.

    The autouse ``block_real_keychain`` fixture fakes a *working* in-memory
    Keychain; individual tests force failures by patching the ``macos_keychain``
    wrapper to raise ``KeychainError`` (``_raise_locked``).

    Credential routing lives on ``CredentialStore`` (``switcher._store``).
    """

    def _macos_switcher(self) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        return s

    # -- capability cache -------------------------------------------------

    def test_non_macos_never_uses_keychain(self, temp_home: Path):
        for plat in (Platform.LINUX, Platform.WSL, Platform.WINDOWS):
            s = ClaudeAccountSwitcher()
            s.platform = plat
            assert s._store._use_keychain() is False
            assert s._uses_file_backup_backend() is True

    def test_capability_cache_sticky_false(self, temp_home: Path, monkeypatch):
        s = self._macos_switcher()
        assert s._store._use_keychain() is True

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        with pytest.raises(KeychainError):
            s._store._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._store._use_keychain() is False

        monkeypatch.setattr(macos_keychain, "get_password", lambda *a, **k: "ok")
        s._store._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._store._use_keychain() is False

    def test_item_exists_is_capability_neutral(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._store._keychain_usable_cache = False
        block_real_keychain.data[("svc", "acct")] = "x"
        assert macos_keychain.item_exists("svc", "acct") is True
        assert s._store._use_keychain() is False

    def test_capability_cache_is_process_local(self, temp_home: Path):
        s1 = self._macos_switcher()
        s1._store._keychain_usable_cache = False
        assert s1._store._use_keychain() is False
        s2 = self._macos_switcher()
        assert s2._store._keychain_usable_cache is None
        assert s2._store._use_keychain() is True

    def test_kc_call_propagates_programming_errors(self, temp_home: Path):
        s = self._macos_switcher()

        def boom(*a, **k):
            raise TypeError("bug")

        with pytest.raises(TypeError):
            s._store._kc_call(boom)
        assert s._store._keychain_usable_cache is None

    def test_active_write_does_not_swallow_programming_errors(
        self, temp_home: Path, monkeypatch
    ):
        s = self._macos_switcher()

        def boom(*a, **k):
            raise TypeError("bug")

        monkeypatch.setattr(macos_keychain, "set_password", boom)
        with pytest.raises(TypeError):
            s._write_credentials('{"x":1}')

    # -- active store -----------------------------------------------------

    def test_active_write_keys_keychain_by_account_name(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        monkeypatch.delenv("USER", raising=False)
        s = self._macos_switcher()
        s._write_credentials('{"x":1}')
        acct = macos_keychain.keychain_account_name()
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, acct) in block_real_keychain.data
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, "user") not in block_real_keychain.data
        assert s._store._last_active_credentials_backend == "keychain"

    def test_active_read_prefers_keychain_then_file(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "FROM-KC"
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")
        assert s._read_credentials() == "FROM-KC"
        del block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)]
        assert s._read_credentials() == "FROM-FILE"

    def test_active_write_falls_back_to_file_and_clears_stale_keychain(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "STALE"
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)

        s._write_credentials('{"fresh":1}')

        assert s._store._last_active_credentials_backend == "file"
        assert get_credentials_path().read_text() == '{"fresh":1}'
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, acct) not in block_real_keychain.data

    def test_keychain_write_leaves_existing_file_untouched(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("PRESERVE-ME")
        s._write_credentials('{"fresh":1}')
        assert s._store._last_active_credentials_backend == "keychain"
        assert cred.read_text() == "PRESERVE-ME"

    # -- backup store: .enc-wins -----------------------------------------

    def _no_session(self, s):
        return (
            patch.object(s, "_live_session_pids", return_value=[]),
            patch.object(s, "_invalidate_session_credentials"),
        )

    def test_backup_read_enc_wins_over_stale_keychain(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "STALE-KC")
        s._store._write_backup_enc("1", "a@example.com", "FRESH-FILE")
        assert s._read_account_credentials("1", "a@example.com") == "FRESH-FILE"

    def test_backup_keychain_write_deletes_enc(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._store._write_backup_enc("1", "a@example.com", "OLD-FILE")
        p1, p2 = self._no_session(s)
        with p1, p2:
            s._write_account_credentials("1", "a@example.com", "NEW-KC")
        assert not s._store._backup_enc_path("1", "a@example.com").exists()
        assert s._read_account_credentials("1", "a@example.com") == "NEW-KC"

    def test_backup_enc_unlink_failure_rewrites_fresh(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        s._store._write_backup_enc("1", "a@example.com", "OLD-FILE")
        enc = s._store._backup_enc_path("1", "a@example.com")

        orig_unlink = Path.unlink

        def flaky_unlink(self_path, *a, **k):
            if self_path == enc:
                raise OSError("cannot unlink")
            return orig_unlink(self_path, *a, **k)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)
        p1, p2 = self._no_session(s)
        with p1, p2:
            s._write_account_credentials("1", "a@example.com", "NEW-KC")
        monkeypatch.setattr(Path, "unlink", orig_unlink)

        assert base64.b64decode(enc.read_text()).decode() == "NEW-KC"
        assert s._read_account_credentials("1", "a@example.com") == "NEW-KC"

    def test_backup_file_mode_writes_enc_and_clears_keychain(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "STALE-KC")
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)
        p1, p2 = self._no_session(s)
        with p1, p2:
            s._write_account_credentials("1", "a@example.com", "FILE-CREDS")
        assert s._read_account_credentials("1", "a@example.com") == "FILE-CREDS"
        assert (SECURITY_SERVICE, "account-1-a@example.com") not in block_real_keychain.data

    @pytest.mark.parametrize("bad", ["corrupt", "", "!!!!", "   ", "\n"])
    def test_backup_bad_enc_falls_back_to_keychain(
        self, temp_home: Path, block_real_keychain, bad
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "FROM-KC")
        s._store._backup_enc_path("1", "a@example.com").write_text(bad)
        assert s._read_account_credentials("1", "a@example.com") == "FROM-KC"

    def test_backup_delete_removes_both_backends(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "KC")
        s._store._write_backup_enc("1", "a@example.com", "FILE")
        s._delete_account_credentials("1", "a@example.com")
        assert not s._store._backup_enc_path("1", "a@example.com").exists()
        assert (SECURITY_SERVICE, "account-1-a@example.com") not in block_real_keychain.data

    # -- healthy-Mac no-op guard & follow-up ------------------------------

    def test_healthy_mac_reads_create_no_files(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "KC")
        assert s._read_account_credentials("1", "a@example.com") == "KC"
        assert not s._store._backup_enc_path("1", "a@example.com").exists()
        assert s._read_credentials() == ""
        assert not get_credentials_path().exists()

    @pytest.mark.skip(reason="_print_switch_followup removed; backend-specific followup no longer exists")
    def test_switch_followup_reflects_recorded_backend(
        self, temp_home: Path, capsys
    ):
        s = self._macos_switcher()
        s._store._last_active_credentials_backend = "file"
        s._print_switch_followup()
        assert "next message" in capsys.readouterr().out
        s._store._last_active_credentials_backend = "keychain"
        s._print_switch_followup()
        assert "30 seconds" in capsys.readouterr().out

