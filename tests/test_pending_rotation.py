"""Parked credential rotations: probe5 (R2-M2 residue) must be eradicated.

The adversarial probe5 scenario: an inactive slot's usage fetch refreshes an
expired token over the network — consuming the single-use refresh token
(claude-code#24317) — and the persist callback cannot take the file lock
because another process is wedged holding it. Before the park/recover
machinery, the rotation was dropped after the bounded wait and the backup
kept the now-dead token: the slot silently degraded to manual re-login.

Now the timed-out persist parks the rotation in a slot-tagged pending file
next to the backup ``.enc`` (owner-only), and the next locked pass over the
slot — a list/status read or a switch activation — applies it. These tests
replay the probe end to end and pin the recovery contract.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.credential_refresh import (
    park_rotated_credential,
    recover_pending_rotation,
)
from claude_swap.credentials import pending_rotation_path
from claude_swap.exceptions import CredentialWriteError
from claude_swap.list_reporter import ListReporter
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _oauth_creds(tag: str, *, expired: bool = False) -> str:
    delta = -3_600_000 if expired else 3_600_000
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": f"at-{tag}",
                "refreshToken": f"rt-{tag}",
                "expiresAt": int(time.time() * 1000) + delta,
            }
        }
    )


def _seed_switcher(home: Path) -> ClaudeAccountSwitcher:
    """Two managed accounts; slot 1 active, slot 2 inactive with a dead token."""
    sw = ClaudeAccountSwitcher()
    sw.platform = Platform.LINUX
    sw._setup_directories()
    sw._write_json(
        sw.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2026-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "a@example.com", "uuid": "", "organizationUuid": "",
                      "organizationName": "", "added": ""},
                "2": {"email": "b@example.com", "uuid": "", "organizationUuid": "",
                      "organizationName": "", "added": ""},
            },
        },
    )
    (home / ".claude.json").write_text(
        json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "organizationUuid": "",
                "accountUuid": "u",
            }
        })
    )
    return sw


def _park_via_contended_fetch(
    sw: ClaudeAccountSwitcher, old: str, rotated: str, monkeypatch
) -> None:
    """Replay probe5: refresh succeeds, persist times out against a held lock."""
    monkeypatch.setattr(
        "claude_swap.list_reporter._ROTATED_PERSIST_LOCK_TIMEOUT", 0.2
    )
    blocker = FileLock(sw.lock_file)
    assert blocker.acquire()
    try:
        with (
            patch(
                "claude_swap.oauth.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(rotated, None),
            ),
            patch(
                "claude_swap.oauth.request_usage_data",
                side_effect=OSError("no network"),
            ),
            patch.object(sw, "_live_session_pids", return_value=[]),
        ):
            ListReporter(sw).fetch_account_usage(
                (2, "b@example.com", "", "", False, old, ""),
            )
    finally:
        blocker.release()


class TestProbe5Eradication:
    def test_probe5_contended_persist_no_longer_loses_the_rotation(
        self, temp_home: Path, monkeypatch, caplog
    ):
        """The probe5 assertion inverted: the rotated credential survives.

        The probe asserted the backup still held the dead token with only a
        log record left. Now the rotation is parked durably, and the very
        next list pass lands it in the backup.
        """
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)
        rotated = _oauth_creds("rotated")

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            _park_via_contended_fetch(sw, old, rotated, monkeypatch)

        # The rotation was not dropped: it is parked on disk, slot-tagged.
        pending = pending_rotation_path(sw.credentials_dir, "2", "b@example.com")
        assert pending.exists()
        assert json.loads(pending.read_text())["credentials"] == rotated
        assert any("Parked rotated OAuth token" in r.message for r in caplog.records)

        # The next list pass (lock now free) applies it to the backup.
        with patch.object(sw, "_live_session_pids", return_value=[]):
            infos = ListReporter(sw).build_accounts_info()
        stored = sw._read_account_credentials("2", "b@example.com")
        assert stored == rotated, "parked rotation must land in the backup"
        assert not pending.exists()
        # ... and the recovered credential is what the list row consumed.
        assert [info[5] for info in infos if info[0] == 2] == [rotated]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_pending_file_is_owner_only(self, temp_home: Path, monkeypatch):
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)

        _park_via_contended_fetch(sw, old, _oauth_creds("rotated"), monkeypatch)

        pending = pending_rotation_path(sw.credentials_dir, "2", "b@example.com")
        assert stat.S_IMODE(os.stat(pending).st_mode) == 0o600

    def test_switch_activation_applies_the_parked_rotation(
        self, temp_home: Path, monkeypatch
    ):
        """The switch path recovers too: activating the slot uses the parked
        credential instead of refreshing (and bricking) the dead stored one."""
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)
        rotated = _oauth_creds("rotated")
        park_rotated_credential(
            sw.credentials_dir, "2", "b@example.com", rotated, replaces=["rt-old"]
        )

        def must_not_refresh(_creds: str) -> str:
            raise AssertionError(
                "activation must use the parked rotation, not consume "
                "another refresh"
            )

        with (
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                side_effect=must_not_refresh,
            ),
            FileLock(sw.lock_file),
        ):
            result = sw._refresher.refresh_target_before_activation(
                "2", "b@example.com", old
            )

        assert result == rotated
        assert sw._read_account_credentials("2", "b@example.com") == rotated
        assert not pending_rotation_path(
            sw.credentials_dir, "2", "b@example.com"
        ).exists()

    def test_relogin_after_park_wins_over_the_parked_rotation(
        self, temp_home: Path
    ):
        """A re-login (new lineage) that lands after the park is newer than
        the parked rotation: recovery must keep it and discard the park."""
        sw = _seed_switcher(temp_home)
        relogged = _oauth_creds("relogin")
        sw._write_account_credentials("2", "b@example.com", relogged)
        park_rotated_credential(
            sw.credentials_dir, "2", "b@example.com",
            _oauth_creds("rotated"), replaces=["rt-old"],
        )

        recovered = recover_pending_rotation(sw, "2", "b@example.com")

        assert recovered is None
        assert sw._read_account_credentials("2", "b@example.com") == relogged
        assert not pending_rotation_path(
            sw.credentials_dir, "2", "b@example.com"
        ).exists()

    def test_recovery_leaves_the_park_in_place_when_the_lock_is_busy(
        self, temp_home: Path, monkeypatch
    ):
        """A busy lock defers recovery instead of losing the park."""
        monkeypatch.setattr(
            "claude_swap.credential_refresh._PENDING_RECOVERY_LOCK_TIMEOUT", 0.2
        )
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)
        park_rotated_credential(
            sw.credentials_dir, "2", "b@example.com",
            _oauth_creds("rotated"), replaces=["rt-old"],
        )

        blocker = FileLock(sw.lock_file)
        assert blocker.acquire()
        try:
            recovered = recover_pending_rotation(sw, "2", "b@example.com")
        finally:
            blocker.release()

        assert recovered is None
        assert sw._read_account_credentials("2", "b@example.com") == old
        assert pending_rotation_path(
            sw.credentials_dir, "2", "b@example.com"
        ).exists()

    def test_lost_recovery_race_reads_as_benign_not_unreadable(
        self, temp_home: Path, monkeypatch, caplog
    ):
        """Two concurrent passes can race to recover the same park; the loser
        reads a path the winner already consumed. That FileNotFoundError is a
        benign lost race — it must not surface as the scary "Discarding
        unreadable pending credential rotation" warning."""
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)
        park_rotated_credential(
            sw.credentials_dir, "2", "b@example.com",
            _oauth_creds("rotated"), replaces=["rt-old"],
        )
        pending = pending_rotation_path(sw.credentials_dir, "2", "b@example.com")

        class WinnerConsumesDuringLockWait(FileLock):
            # The concurrent winner applies and consumes the park while the
            # loser waits on the lock (its exists() probe already passed).
            def acquire(self, timeout: float | None = None) -> bool:
                got = super().acquire(timeout)
                pending.unlink(missing_ok=True)
                return got

        monkeypatch.setattr(
            "claude_swap.credential_refresh.FileLock", WinnerConsumesDuringLockWait
        )
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            recovered = recover_pending_rotation(sw, "2", "b@example.com")

        assert recovered is None
        assert sw._read_account_credentials("2", "b@example.com") == old
        assert not any(
            "unreadable pending credential rotation" in r.message
            for r in caplog.records
        ), "a lost recovery race must not be reported as an unreadable park"
        assert any(
            "consumed by a concurrent pass" in r.message
            and r.levelno == logging.DEBUG
            for r in caplog.records
        )

    def test_unreadable_pending_file_is_discarded_loudly(
        self, temp_home: Path, caplog
    ):
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)
        pending = pending_rotation_path(sw.credentials_dir, "2", "b@example.com")
        pending.write_text("{not json")

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            recovered = recover_pending_rotation(sw, "2", "b@example.com")

        assert recovered is None
        assert not pending.exists()
        assert sw._read_account_credentials("2", "b@example.com") == old
        assert any(
            "unreadable pending credential rotation" in r.message
            for r in caplog.records
        )

    def test_deleting_the_slot_deletes_its_parked_rotation(self, temp_home: Path):
        """A parked rotation holds a working credential — removing the slot
        must not leave it behind."""
        sw = _seed_switcher(temp_home)
        sw._write_account_credentials("2", "b@example.com", _oauth_creds("old"))
        park_rotated_credential(
            sw.credentials_dir, "2", "b@example.com",
            _oauth_creds("rotated"), replaces=["rt-old"],
        )

        sw._delete_account_credentials("2", "b@example.com")

        assert not pending_rotation_path(
            sw.credentials_dir, "2", "b@example.com"
        ).exists()


class TestVerifyMismatchParking:
    """A failed backup write verification must not cost the consumed rotation.

    ``refresh_inactive_if_needed`` raises ``CredentialWriteError`` when the
    read-back after persisting a refreshed token does not match — but the
    network refresh has already consumed the single-use refresh token, so the
    in-memory rotation is the slot's only working credential. It must be
    parked durably (same recovery path as a wedged lock) before the loud
    error surfaces.
    """

    def test_verify_mismatch_parks_the_rotation_instead_of_losing_it(
        self, temp_home: Path
    ):
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)
        rotated = _oauth_creds("rotated")

        # Reads keep returning the stale credential even after the write,
        # simulating a backup write that silently did not take (e.g. a
        # Keychain ACL hiccup).
        with (
            patch(
                "claude_swap.oauth.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(rotated, None),
            ),
            patch.object(sw, "_read_account_credentials", return_value=old),
            patch.object(sw, "_write_account_credentials"),
            pytest.raises(CredentialWriteError),
        ):
            sw._refresh_inactive_credentials_if_needed("2", "b@example.com", old)

        pending = pending_rotation_path(sw.credentials_dir, "2", "b@example.com")
        assert pending.exists()
        payload = json.loads(pending.read_text())
        assert payload["credentials"] == rotated
        assert payload["replaces"] == ["rt-old"]

    def test_parked_verify_mismatch_recovers_on_the_next_list_pass(
        self, temp_home: Path
    ):
        sw = _seed_switcher(temp_home)
        old = _oauth_creds("old", expired=True)
        sw._write_account_credentials("2", "b@example.com", old)
        rotated = _oauth_creds("rotated")

        with (
            patch(
                "claude_swap.oauth.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(rotated, None),
            ),
            patch.object(sw, "_read_account_credentials", return_value=old),
            patch.object(sw, "_write_account_credentials"),
            pytest.raises(CredentialWriteError),
        ):
            sw._refresh_inactive_credentials_if_needed("2", "b@example.com", old)

        # The next locked pass over the slot applies the parked rotation.
        with patch.object(sw, "_live_session_pids", return_value=[]):
            ListReporter(sw).build_accounts_info()

        assert sw._read_account_credentials("2", "b@example.com") == rotated
        assert not pending_rotation_path(
            sw.credentials_dir, "2", "b@example.com"
        ).exists()
