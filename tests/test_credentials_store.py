"""macOS Keychain fallback, _kc_* routing, and write-verify drift handling."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
    _KEYCHAIN_REPROBE_INTERVAL,
    ActiveCredentials,
)
from claude_swap.exceptions import (
    CredentialReadError,
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
        return json.dumps(
            {"claudeAiOauth": {"accessToken": token, "refreshToken": "rt"}}
        )

    def test_persistent_live_rotation_does_not_raise(
        self,
        temp_home: Path,
        monkeypatch,
        caplog,
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

        live_iter = iter(
            [
                self._creds("live-1"),
                self._creds("live-2"),
                self._creds("live-3"),
            ]
        )

        def read_live():
            return next(live_iter)

        monkeypatch.setattr(switcher, "_write_account_credentials", write_acct)
        monkeypatch.setattr(switcher, "_read_account_credentials", read_acct)
        monkeypatch.setattr(switcher, "_read_credentials", read_live)
        monkeypatch.setattr("claude_swap.credential_refresh.time.sleep", lambda *_: None)

        caplog.set_level(logging.WARNING, logger="claude-swap")

        result = switcher._write_verified_live_account_credentials(
            "2",
            "b@example.com",
            self._creds("intended"),
        )

        # Last sampled live state is what gets persisted as the backup.
        assert result == self._creds("live-3")
        assert store["backup"] == self._creds("live-3")

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any(
            "persistent in-flight Claude Code rotation" in m for m in warnings
        ), warnings

    def test_persistent_storage_write_failure_raises(
        self,
        temp_home: Path,
        monkeypatch,
    ):
        """If live_now is stable but our write never sticks, raise so the
        genuine storage failure surfaces — don't silently swallow it as a
        rotation event."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        # Our writes are no-ops — stored stays empty.  live_now is stable.
        monkeypatch.setattr(
            switcher,
            "_write_account_credentials",
            lambda *_: None,
        )
        monkeypatch.setattr(
            switcher,
            "_read_account_credentials",
            lambda *_: "",
        )
        stable_live = self._creds("stable")
        monkeypatch.setattr(switcher, "_read_credentials", lambda: stable_live)
        monkeypatch.setattr("claude_swap.credential_refresh.time.sleep", lambda *_: None)

        with pytest.raises(CredentialWriteError, match="did not match"):
            switcher._write_verified_live_account_credentials(
                "2",
                "b@example.com",
                stable_live,
            )

    def test_refuses_to_back_up_empty_credentials(
        self,
        temp_home: Path,
        monkeypatch,
    ):
        """An empty payload must be rejected before any backup write —
        persisting it clobbers the slot's ``.enc`` to 0 bytes and, in file
        mode, deletes the Keychain copy."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        writes: list = []
        monkeypatch.setattr(
            switcher, "_write_account_credentials",
            lambda *a: writes.append(a),
        )
        monkeypatch.setattr(switcher, "_read_account_credentials", lambda *_: "")
        monkeypatch.setattr(
            switcher, "_read_credentials", lambda: self._creds("live"),
        )

        with pytest.raises(CredentialWriteError, match="empty credentials"):
            switcher._write_verified_live_account_credentials(
                "1",
                "a@example.com",
                "",
            )
        assert writes == []

    def test_unreadable_live_raises_before_touching_backup(
        self,
        temp_home: Path,
        monkeypatch,
    ):
        """Live reads "" (e.g. Keychain pinned to file mode on a Keychain-only
        Mac): the verifier must raise BEFORE the first backup write, not after
        it has already clobbered the slot."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        writes: list = []
        monkeypatch.setattr(
            switcher, "_write_account_credentials",
            lambda *a: writes.append(a),
        )
        monkeypatch.setattr(switcher, "_read_account_credentials", lambda *_: "")
        monkeypatch.setattr(switcher, "_read_credentials", lambda: "")

        with pytest.raises(CredentialReadError, match="No live credentials"):
            switcher._write_verified_live_account_credentials(
                "1",
                "a@example.com",
                self._creds("intended"),
            )
        assert writes == []


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

    def test_capability_cache_reprobes_after_cooldown(
        self, temp_home: Path, monkeypatch
    ):
        """One transient failure must not pin a long-running process (`cswap
        auto` under `service install`) to file mode for its whole lifetime:
        after the cooldown the next op re-probes, and a success restores
        Keychain routing."""
        s = self._macos_switcher()
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        with pytest.raises(KeychainError):
            s._store._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._store._use_keychain() is False  # sticky inside the window

        base = time.monotonic()
        monkeypatch.setattr(
            "claude_swap.credentials.time.monotonic",
            lambda: base + _KEYCHAIN_REPROBE_INTERVAL + 1,
        )
        assert s._store._use_keychain() is True  # cooldown elapsed: re-probe

        monkeypatch.setattr(macos_keychain, "get_password", lambda *a, **k: "ok")
        s._store._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._store._use_keychain() is True  # success restores routing

    def test_reprobe_failure_restamps_the_cooldown(
        self, temp_home: Path, monkeypatch
    ):
        """A failed re-probe pins file mode again for a fresh full window."""
        s = self._macos_switcher()
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        with pytest.raises(KeychainError):
            s._store._kc_call(macos_keychain.get_password, "svc", "acct")

        now = {"t": time.monotonic() + _KEYCHAIN_REPROBE_INTERVAL + 1}
        monkeypatch.setattr(
            "claude_swap.credentials.time.monotonic", lambda: now["t"]
        )
        assert s._store._use_keychain() is True  # first window elapsed

        with pytest.raises(KeychainError):
            s._store._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._store._use_keychain() is False  # re-pinned, fresh stamp

        now["t"] += _KEYCHAIN_REPROBE_INTERVAL + 1
        assert s._store._use_keychain() is True  # second window elapsed

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

    def test_active_read_retries_transient_keychain_failure(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "FROM-KC"
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)

        calls = {"n": 0}
        real_get = macos_keychain.get_password

        def flaky_get(service, account):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeychainError("transient lock")
            return real_get(service, account)

        monkeypatch.setattr(macos_keychain, "get_password", flaky_get)

        result = s._read_active_credentials()
        assert result == ActiveCredentials("FROM-KC", False)
        assert calls["n"] == 2

        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "FROM-KC-2"
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")

        result = s._read_active_credentials()
        assert result == ActiveCredentials("FROM-KC-2", False)

    def test_active_read_keychain_unavailable_no_fallback(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        assert not get_credentials_path().exists()

        result = s._read_active_credentials()
        assert result == ActiveCredentials("", True)
        assert s._read_credentials() == ""

    def test_active_read_keychain_failure_covered_by_file_is_degraded(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        """A file read that covers a FAILED Keychain read is usable but degraded.

        Keychain-mode writes deliberately leave ``.credentials.json`` untouched
        (#1414), so after an A→B switch the file can still hold account A's
        credentials. The value is fine for display/usage, but the ``degraded``
        flag must mark it so persistence paths never treat it as the active
        account's live credential (cross-account backup poisoning).
        """
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)

        result = s._read_active_credentials()
        assert result == ActiveCredentials("FROM-FILE", False, degraded=True)

    def test_active_read_file_without_keychain_failure_is_not_degraded(
        self, temp_home: Path, block_real_keychain
    ):
        """The legit file fallback (Keychain item merely absent, rc-44) keeps
        its upstream semantics: not degraded, safe to sync (#60/#66 users)."""
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")

        result = s._read_active_credentials()
        assert result == ActiveCredentials("FROM-FILE", False, degraded=False)

    def test_active_read_absent_item_is_not_keychain_unavailable(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        assert not get_credentials_path().exists()

        result = s._read_active_credentials()
        assert result == ActiveCredentials("", False)

    def test_list_active_shows_keychain_unavailable(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        monkeypatch,
        block_real_keychain,
        capsys,
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = self._macos_switcher()
        s._write_json(s.sequence_file, sample_sequence_data)
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        assert not get_credentials_path().exists()

        payload = s.list_accounts(json_output=True)
        active = next(a for a in payload["accounts"] if a["number"] == 1)
        assert active["usageStatus"] == "keychain_unavailable"
        assert active["usage"] is None

        s.list_accounts()
        out = capsys.readouterr().out
        assert "test@example.com" in out
        assert "keychain unavailable — locked or in use; try again" in out

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

    def test_keychain_write_refreshes_existing_file(
        self, temp_home: Path, block_real_keychain
    ):
        # #86: an already-present shadow file must be rewritten (mtime bumped) so a
        # running Claude Code session invalidates its memoized token and hot-reloads.
        # #1414: it is rewritten, never deleted — a file-reading consumer stays valid.
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("OLD-CREDS")
        os.utime(cred, (1_000_000_000, 1_000_000_000))  # force an old mtime
        old_mtime_ns = cred.stat().st_mtime_ns

        s._write_credentials('{"fresh":1}')  # keychain usable → writes keychain

        assert s._store._last_active_credentials_backend == "keychain"
        assert cred.exists()  # never deleted (#1414)
        assert cred.read_text() == '{"fresh":1}'  # rewritten to the fresh account
        assert cred.stat().st_mtime_ns > old_mtime_ns  # the actual invalidation trigger

    def test_keychain_write_bumps_mtime_even_when_content_unchanged(
        self, temp_home: Path, block_real_keychain
    ):
        # The fix bumps mtime via atomic os.replace, so it fires even when the new
        # creds are byte-identical to the old — the purest test of the mechanism
        # (a content-only assertion would silently miss this).
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text('{"same":1}')
        os.utime(cred, (1_000_000_000, 1_000_000_000))
        old_mtime_ns = cred.stat().st_mtime_ns

        s._write_credentials('{"same":1}')  # identical content

        assert cred.stat().st_mtime_ns > old_mtime_ns

    def test_keychain_write_does_not_create_absent_file(
        self, temp_home: Path, block_real_keychain
    ):
        # Keychain-only users keep their fileless posture: no .credentials.json is
        # created, so no plaintext credential lands on their disk (#86).
        s = self._macos_switcher()
        cred = get_credentials_path()
        assert not cred.exists()

        s._write_credentials('{"fresh":1}')  # keychain usable → writes keychain

        assert s._store._last_active_credentials_backend == "keychain"
        assert not cred.exists()

    def test_refresh_stale_file_is_best_effort(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        # The Keychain write is authoritative and already succeeded; a failure to
        # refresh the shadow file must warn, not fail the switch.
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("OLD-CREDS")

        def boom(_credentials):
            raise OSError("disk full")

        monkeypatch.setattr(s._store, "_write_active_credentials_file", boom)

        s._write_credentials('{"fresh":1}')  # must not raise

        assert s._store._last_active_credentials_backend == "keychain"

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
        assert (
            SECURITY_SERVICE,
            "account-1-a@example.com",
        ) not in block_real_keychain.data

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
        assert (
            SECURITY_SERVICE,
            "account-1-a@example.com",
        ) not in block_real_keychain.data

    # -- write verification targets the backend actually written ----------

    def test_verify_ignores_stale_file_when_keychain_write_succeeded(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        """R2/Bugbot: a successful Keychain write must not be failed by the
        verify read-back hitting a transiently locked Keychain.

        An unreadable just-written backend is inconclusive, not a mismatch —
        without this, the fallthrough read could mismatch (e.g. no file at
        all) and abort a switch that actually succeeded. Since #86 the
        shadow file is refreshed on a successful Keychain write, so the
        fallthrough now reads the fresh creds rather than stale ones.
        """
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text('{"stale":"other-account"}')
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)

        real_get = macos_keychain.get_password

        def get_locked_after_write(service, account):
            if service == CLAUDE_CODE_KEYCHAIN_SERVICE:
                raise KeychainError("locked right after write")
            return real_get(service, account)

        monkeypatch.setattr(macos_keychain, "get_password", get_locked_after_write)

        s._write_credentials('{"fresh":1}', verify=True)  # must not raise

        acct = macos_keychain.keychain_account_name()
        assert block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] == '{"fresh":1}'
        assert cred.read_text() == '{"fresh":1}'  # refreshed, never deleted (#86)

    def test_verify_still_detects_silent_keychain_corruption(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        """A readable Keychain returning a different payload is a genuine
        verification failure and must still raise."""
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()

        real_set = macos_keychain.set_password

        def corrupting_set(service, account, password):
            if service == CLAUDE_CODE_KEYCHAIN_SERVICE:
                return real_set(service, account, "TAMPERED")
            return real_set(service, account, password)

        monkeypatch.setattr(macos_keychain, "set_password", corrupting_set)

        with pytest.raises(CredentialWriteError, match="verification failed"):
            s._write_credentials('{"fresh":1}', verify=True)
        assert block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] == "TAMPERED"

    def test_verify_file_backend_reads_the_file(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        """File-mode writes verify against the file just written."""
        s = self._macos_switcher()
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)

        s._write_credentials('{"fresh":1}', verify=True)  # must not raise

        assert s._store._last_active_credentials_backend == "file"
        assert get_credentials_path().read_text() == '{"fresh":1}'

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
