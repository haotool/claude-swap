"""Rollback failure paths for ``--import`` (transfer.py).

Adversarial review R2, nit: the import rollback branches — undoing a
half-written entry and unwinding previously completed entries when a later
one fails — carried almost no coverage despite being the error handling that
keeps a failed multi-account import from leaving the store half-migrated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.exceptions import TransferError
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import _rollback_overwritten_slot, import_accounts

from tests.test_transfer import SAMPLE_CONFIG, _linux_switcher, _seed_account


def _entry(email: str, number: int, marker: str) -> dict:
    config = json.loads(json.dumps(SAMPLE_CONFIG))
    config["oauthAccount"]["emailAddress"] = email
    return {
        "number": number,
        "email": email,
        "uuid": f"u-{number}",
        "organizationUuid": "",
        "organizationName": "",
        "added": "2024-01-01T00:00:00Z",
        "credentials": {"accessToken": "tok", "_marker": marker},
        "config": config,
    }


def _write_envelope(path: Path, *entries: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "exportedAt": "2026-01-01T00:00:00Z",
                "exportedFrom": "linux",
                "swapVersion": "0.0.0",
                "encrypted": False,
                "activeAccountNumber": None,
                "accounts": list(entries),
            }
        )
    )


class TestImportRollback:
    def test_mid_import_failure_rolls_back_completed_entries(
        self, temp_home: Path, capsys
    ):
        """A failure on entry N unwinds entries 1..N-1 imported by this run."""
        s = _linux_switcher(temp_home)
        envelope = temp_home / "two.cswap"
        _write_envelope(
            envelope,
            _entry("alice@example.com", 1, "ALICE"),
            _entry("bob@example.com", 2, "BOB"),
        )

        real_write = ClaudeAccountSwitcher._write_account_credentials

        def failing_second_write(self, num, email, creds):
            if email == "bob@example.com":
                raise OSError("disk full")
            return real_write(self, num, email, creds)

        with (
            pytest.MonkeyPatch.context() as mp,
        ):
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                failing_second_write,
            )
            with pytest.raises(TransferError, match="rolled back 1 account"):
                import_accounts(s, str(envelope))

        # Alice's completed import was fully unwound: no slot record, no files.
        seq = s._get_sequence_data() or {}
        assert "1" not in seq.get("accounts", {})
        assert s._read_account_credentials("1", "alice@example.com") == ""
        assert s._read_account_config("1", "alice@example.com") == ""
        capsys.readouterr()

    def test_overwrite_failure_restores_previous_slot_contents(
        self, temp_home: Path, capsys
    ):
        """A half-done --force overwrite restores the pre-import slot state."""
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        creds_before = s._read_account_credentials("1", "alice@example.com")
        config_before = s._read_account_config("1", "alice@example.com")

        envelope = temp_home / "force.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE-NEW"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_config",
                lambda self, num, email, cfg: (_ for _ in ()).throw(
                    OSError("config write failed")
                ),
            )
            with pytest.raises(TransferError, match="import failed on alice"):
                import_accounts(s, str(envelope), force=True)

        assert s._read_account_credentials("1", "alice@example.com") == creds_before
        assert s._read_account_config("1", "alice@example.com") == config_before
        seq = s._get_sequence_data() or {}
        assert seq["accounts"]["1"]["email"] == "alice@example.com"
        capsys.readouterr()

    def test_rollback_failure_names_unrollable_accounts(
        self, temp_home: Path, capsys
    ):
        """When the rollback itself fails, the error says what was kept."""
        s = _linux_switcher(temp_home)
        envelope = temp_home / "two.cswap"
        _write_envelope(
            envelope,
            _entry("alice@example.com", 1, "ALICE"),
            _entry("bob@example.com", 2, "BOB"),
        )

        real_write = ClaudeAccountSwitcher._write_account_credentials
        real_delete = ClaudeAccountSwitcher._delete_account_credentials

        def failing_second_write(self, num, email, creds):
            if email == "bob@example.com":
                raise OSError("disk full")
            return real_write(self, num, email, creds)

        def failing_delete(self, num, email):
            if email == "alice@example.com":
                raise OSError("delete failed")
            return real_delete(self, num, email)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                failing_second_write,
            )
            mp.setattr(
                ClaudeAccountSwitcher,
                "_delete_account_credentials",
                failing_delete,
            )
            with pytest.raises(
                TransferError, match="could not roll back alice@example.com"
            ):
                import_accounts(s, str(envelope))
        capsys.readouterr()

    def test_later_failure_rolls_back_a_completed_overwrite(
        self, temp_home: Path, capsys
    ):
        """Entry N failing restores a slot that entry N-1 already overwrote."""
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        creds_before = s._read_account_credentials("1", "alice@example.com")
        config_before = s._read_account_config("1", "alice@example.com")
        record_before = (s._get_sequence_data() or {})["accounts"]["1"]

        envelope = temp_home / "two.cswap"
        _write_envelope(
            envelope,
            _entry("alice@example.com", 1, "ALICE-NEW"),
            _entry("bob@example.com", 2, "BOB"),
        )

        real_write = ClaudeAccountSwitcher._write_account_credentials

        def failing_second_write(self, num, email, creds):
            if email == "bob@example.com":
                raise OSError("disk full")
            return real_write(self, num, email, creds)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                failing_second_write,
            )
            with pytest.raises(TransferError, match="rolled back 1 account"):
                import_accounts(s, str(envelope), force=True)

        # Alice's overwrite was undone back to the pre-import contents.
        assert s._read_account_credentials("1", "alice@example.com") == creds_before
        assert s._read_account_config("1", "alice@example.com") == config_before
        seq = s._get_sequence_data() or {}
        assert seq["accounts"]["1"] == record_before
        assert 1 in seq["sequence"]
        assert "2" not in seq.get("accounts", {})
        capsys.readouterr()

    def test_overwrite_rollback_restores_a_slot_that_had_no_backups(
        self, temp_home: Path, capsys
    ):
        """Rolling back an overwrite of a record-only slot removes the new files.

        A slot can exist in sequence.json without stored backups or a rotation
        entry (e.g. a crash between an add's writes). Overwriting it and then
        failing must return the store to that state, not leave the imported
        files behind as phantom backups.
        """
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        s._delete_account_files("1", "alice@example.com")
        data = s._get_sequence_data() or {}
        data["sequence"] = []
        data["activeAccountNumber"] = None
        s._write_json(s.sequence_file, data)
        record_before = data["accounts"]["1"]

        envelope = temp_home / "two.cswap"
        _write_envelope(
            envelope,
            _entry("alice@example.com", 1, "ALICE-NEW"),
            _entry("bob@example.com", 2, "BOB"),
        )

        real_write = ClaudeAccountSwitcher._write_account_credentials

        def failing_second_write(self, num, email, creds):
            if email == "bob@example.com":
                raise OSError("disk full")
            return real_write(self, num, email, creds)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                failing_second_write,
            )
            with pytest.raises(TransferError, match="rolled back 1 account"):
                import_accounts(s, str(envelope), force=True)

        assert s._read_account_credentials("1", "alice@example.com") == ""
        assert s._read_account_config("1", "alice@example.com") == ""
        seq = s._get_sequence_data() or {}
        assert seq["accounts"]["1"] == record_before
        assert 1 not in seq["sequence"]
        capsys.readouterr()

    def test_fresh_import_config_failure_deletes_the_half_written_slot(
        self, temp_home: Path, capsys
    ):
        """Creds written + config failed on a new slot leaves no files behind."""
        s = _linux_switcher(temp_home)
        envelope = temp_home / "one.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_config",
                lambda self, num, email, cfg: (_ for _ in ()).throw(
                    OSError("config write failed")
                ),
            )
            with pytest.raises(TransferError, match="import failed on alice"):
                import_accounts(s, str(envelope))

        assert s._read_account_credentials("1", "alice@example.com") == ""
        assert "1" not in (s._get_sequence_data() or {}).get("accounts", {})
        capsys.readouterr()

    def test_overwrite_creds_failure_leaves_the_slot_untouched(
        self, temp_home: Path, capsys
    ):
        """When the first overwrite write fails there is nothing to undo."""
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        creds_before = s._read_account_credentials("1", "alice@example.com")
        config_before = s._read_account_config("1", "alice@example.com")

        envelope = temp_home / "force.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE-NEW"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                lambda self, num, email, creds: (_ for _ in ()).throw(
                    OSError("creds write failed")
                ),
            )
            with pytest.raises(TransferError, match="import failed on alice"):
                import_accounts(s, str(envelope), force=True)

        assert s._read_account_credentials("1", "alice@example.com") == creds_before
        assert s._read_account_config("1", "alice@example.com") == config_before
        capsys.readouterr()

    def test_overwrite_sequence_failure_restores_both_files(
        self, temp_home: Path, capsys
    ):
        """Creds + config written, sequence.json failed → both files restored."""
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        creds_before = s._read_account_credentials("1", "alice@example.com")
        config_before = s._read_account_config("1", "alice@example.com")

        envelope = temp_home / "force.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE-NEW"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                s,
                "_write_json",
                lambda path, data: (_ for _ in ()).throw(
                    OSError("sequence write failed")
                ),
            )
            with pytest.raises(TransferError, match="import failed on alice"):
                import_accounts(s, str(envelope), force=True)

        assert s._read_account_credentials("1", "alice@example.com") == creds_before
        assert s._read_account_config("1", "alice@example.com") == config_before
        capsys.readouterr()

    def test_overwrite_sequence_failure_removes_files_the_slot_never_had(
        self, temp_home: Path, capsys
    ):
        """Undoing an overwrite of a backup-less slot deletes both new files."""
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        s._delete_account_files("1", "alice@example.com")

        envelope = temp_home / "force.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE-NEW"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                s,
                "_write_json",
                lambda path, data: (_ for _ in ()).throw(
                    OSError("sequence write failed")
                ),
            )
            with pytest.raises(TransferError, match="import failed on alice"):
                import_accounts(s, str(envelope), force=True)

        assert s._read_account_credentials("1", "alice@example.com") == ""
        assert s._read_account_config("1", "alice@example.com") == ""
        capsys.readouterr()

    def test_import_undo_failure_does_not_mask_the_original_error(
        self, temp_home: Path, capsys
    ):
        """A failing undo of a half-written new slot must not shadow the cause."""
        s = _linux_switcher(temp_home)
        envelope = temp_home / "one.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_config",
                lambda self, num, email, cfg: (_ for _ in ()).throw(
                    OSError("config write failed")
                ),
            )
            mp.setattr(
                ClaudeAccountSwitcher,
                "_delete_account_files",
                lambda self, num, email: (_ for _ in ()).throw(
                    OSError("delete failed")
                ),
            )
            with pytest.raises(
                TransferError, match="import failed on alice.*config write failed"
            ):
                import_accounts(s, str(envelope))
        capsys.readouterr()

    def test_overwrite_undo_failure_does_not_mask_the_original_error(
        self, temp_home: Path, capsys
    ):
        """A failing overwrite undo must surface the import error, not its own."""
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")

        envelope = temp_home / "force.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE-NEW"))

        real_write = ClaudeAccountSwitcher._write_account_credentials
        calls = {"count": 0}

        def restore_fails(self, num, email, creds):
            calls["count"] += 1
            if calls["count"] > 1:  # first call = import write, second = undo
                raise OSError("restore failed")
            return real_write(self, num, email, creds)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                restore_fails,
            )
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_config",
                lambda self, num, email, cfg: (_ for _ in ()).throw(
                    OSError("config write failed")
                ),
            )
            with pytest.raises(
                TransferError, match="import failed on alice.*config write failed"
            ):
                import_accounts(s, str(envelope), force=True)
        capsys.readouterr()


class TestRollbackOverwrittenSlotContract:
    """Direct contract tests for the defensive branches of the slot rollback.

    Through import_accounts the snapshot always sees a record (the overwrite
    target was found in accounts) — these states only arise when sequence.json
    was mutated or corrupted between snapshot and rollback, so they are pinned
    here against the helper itself with real files and no mocks.
    """

    def test_snapshot_without_record_removes_the_imported_record(
        self, temp_home: Path
    ):
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")

        _rollback_overwritten_slot(
            s,
            "1",
            "alice@example.com",
            {
                "prev_creds": None,
                "prev_config": None,
                "prev_record": None,
                "prev_in_sequence": False,
            },
        )

        seq = s._get_sequence_data() or {}
        assert "1" not in seq.get("accounts", {})
        assert 1 not in seq.get("sequence", [])
        assert s._read_account_credentials("1", "alice@example.com") == ""
        assert s._read_account_config("1", "alice@example.com") == ""

    def test_snapshot_in_sequence_reappends_a_dropped_sequence_entry(
        self, temp_home: Path
    ):
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        data = s._get_sequence_data() or {}
        record = data["accounts"]["1"]
        data["sequence"] = []
        data["activeAccountNumber"] = None
        s._write_json(s.sequence_file, data)

        _rollback_overwritten_slot(
            s,
            "1",
            "alice@example.com",
            {
                "prev_creds": '{"accessToken": "prev"}',
                "prev_config": '{"oauthAccount": {}}',
                "prev_record": record,
                "prev_in_sequence": True,
            },
        )

        seq = s._get_sequence_data() or {}
        assert seq["accounts"]["1"] == record
        assert seq["sequence"] == [1]
        assert s._read_account_credentials("1", "alice@example.com") == (
            '{"accessToken": "prev"}'
        )
