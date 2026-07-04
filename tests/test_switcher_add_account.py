"""add_account / add_account_from_token: slot assignment, refresh-in-place,
org-field capture, and API-key token registration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import (
    ConfigError,
    CredentialWriteError,
    ValidationError,
)
from claude_swap.paths import get_backup_root
from claude_swap.switcher import ClaudeAccountSwitcher, SETUP_TOKEN_SCOPES



class TestAddAccountRefresh:
    """Test refreshing credentials for an existing account."""

    def test_readd_existing_account_updates_credentials(
        self, temp_home: Path, mock_claude_config: Path, capsys
    ):
        """Re-adding an existing account should update its credentials, not duplicate it."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        old_creds = json.dumps({"claudeAiOauth": {"accessToken": "old-token"}})
        new_creds = json.dumps({"claudeAiOauth": {"accessToken": "new-token"}})

        # Track what was written to credential storage
        stored = {}

        def mock_write_creds(num, email, creds):
            stored["creds"] = creds

        def mock_read_creds(num, email):
            return stored.get("creds", "")

        # First add
        with (
            patch.object(switcher, "_read_credentials", return_value=old_creds),
            patch.object(
                switcher, "_write_account_credentials", side_effect=mock_write_creds
            ),
            patch.object(
                switcher, "_read_account_credentials", side_effect=mock_read_creds
            ),
        ):
            switcher.add_account()

        # Verify first add
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        assert data["accounts"]["1"]["email"] == "test@example.com"
        assert "old-token" in stored["creds"]

        # Re-add same account with new credentials
        with (
            patch.object(switcher, "_read_credentials", return_value=new_creds),
            patch.object(
                switcher, "_write_account_credentials", side_effect=mock_write_creds
            ),
            patch.object(
                switcher, "_read_account_credentials", side_effect=mock_read_creds
            ),
        ):
            switcher.add_account()

        # Should still have only 1 account
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        assert len(data["sequence"]) == 1

        # Should have printed update message
        output = capsys.readouterr().out
        assert "Updated credentials" in output

        # Verify credentials were actually updated
        assert "new-token" in stored["creds"]


class TestAddAccountOrgFields:
    def test_allows_same_email_different_org(self, temp_home):
        """Should allow adding same-email account if organizationUuid differs."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        config_path = temp_home / ".claude.json"

        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid-A",
                "organizationName": "Acme",
            }
        }))
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account()

        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "user@example.com",
                        "accountUuid": "user-uuid",
                    }
                }
            )
        )
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account()

        seq = json.loads((get_backup_root() / "sequence.json").read_text())
        assert len(seq["accounts"]) == 2
        assert seq["accounts"]["1"]["organizationUuid"] == "org-uuid-A"
        assert seq["accounts"]["2"]["organizationUuid"] == ""

    def test_blocks_true_duplicate(self, temp_home):
        """Should block adding an account with identical (email, organizationUuid) combination."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        config_path = temp_home / ".claude.json"
        org_config = {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid-A",
                "organizationName": "Acme",
            }
        }
        config_path.write_text(json.dumps(org_config))
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account()

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        config_path.write_text(json.dumps(org_config))
        with (
            redirect_stdout(f),
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account()
        assert "Updated credentials" in f.getvalue()

        seq = json.loads((get_backup_root() / "sequence.json").read_text())
        assert len(seq["accounts"]) == 1

    def test_stores_org_name_in_sequence(self, temp_home):
        """add_account should store organizationName in sequence.json."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid",
                "organizationName": "My Org",
            }
        }))
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account()

        seq = json.loads((get_backup_root() / "sequence.json").read_text())
        assert seq["accounts"]["1"]["organizationName"] == "My Org"
        assert seq["accounts"]["1"]["organizationUuid"] == "org-uuid"


# ── Task 6: _resolve_account_identifier ambiguity ────────────────────────────

class TestAddAccountSlot:
    """Test add_account with --slot option."""

    def _make_switcher(self, temp_home, email="test@example.com", org_uuid="", org_name=""):
        """Helper: write a claude config and return a switcher instance."""
        config = {
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "uuid-" + email,
                "organizationUuid": org_uuid,
                "organizationName": org_name,
            }
        }
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_add_to_specific_empty_slot(self, temp_home, capsys):
        """Adding to an empty slot should place the account there."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._make_switcher(temp_home)

        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account(slot=5)

        data = switcher._get_sequence_data()
        assert "5" in data["accounts"]
        assert data["accounts"]["5"]["email"] == "test@example.com"
        assert data["activeAccountNumber"] == 5
        assert 5 in data["sequence"]
        assert "Added" in capsys.readouterr().out

    def test_add_without_slot_auto_assigns(self, temp_home):
        """Without --slot, should auto-assign next number (original behavior)."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._make_switcher(temp_home)

        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]

    def test_slot_occupied_cancel(self, temp_home, capsys):
        """When slot is occupied and user cancels, nothing should change."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account A to slot 3
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account(slot=3)

        # Try to add account B to slot 3, answer "n"
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
            patch("builtins.input", return_value="n"),
        ):
            switcher.add_account(slot=3)

        # Slot 3 should still be account A
        data = switcher._get_sequence_data()
        assert data["accounts"]["3"]["email"] == "a@example.com"
        assert "Cancelled" in capsys.readouterr().out

    def test_slot_occupied_overwrite(self, temp_home, capsys):
        """When slot is occupied and user confirms, should overwrite."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account A to slot 3
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
            patch.object(switcher, "_delete_account_credentials"),
        ):
            switcher.add_account(slot=3)

        # Add account B to slot 3, answer "y"
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
            patch.object(switcher, "_delete_account_credentials"),
            patch("builtins.input", return_value="y"),
        ):
            switcher.add_account(slot=3)

        data = switcher._get_sequence_data()
        assert data["accounts"]["3"]["email"] == "b@example.com"
        assert len(data["accounts"]) == 1
        assert "Added" in capsys.readouterr().out

    def test_migrate_account_to_different_slot(self, temp_home, capsys):
        """Moving an existing account to a new slot should clean up the old slot."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account to slot 1 (auto)
        switcher = self._make_switcher(temp_home, email="user@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
            patch.object(switcher, "_delete_account_credentials"),
        ):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]

        # Move to slot 5
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
            patch.object(switcher, "_delete_account_credentials"),
        ):
            switcher.add_account(slot=5)

        data = switcher._get_sequence_data()
        assert "1" not in data["accounts"]
        assert "5" in data["accounts"]
        assert data["accounts"]["5"]["email"] == "user@example.com"
        assert 1 not in data["sequence"]
        assert 5 in data["sequence"]
        out = capsys.readouterr().out
        assert "Moved from slot 1" in out

    def test_migrate_with_occupied_target_cancel_preserves_old_slot(self, temp_home, capsys):
        """If migration target is occupied and user cancels, old slot must survive."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account A to slot 1
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account(slot=1)

        # Add account B to slot 3
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account(slot=3)

        # Try to move A from slot 1 → slot 3, cancel
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
            patch("builtins.input", return_value="n"),
        ):
            switcher.add_account(slot=3)

        # Both slots should be untouched
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "a@example.com"
        assert data["accounts"]["3"]["email"] == "b@example.com"
        assert "Cancelled" in capsys.readouterr().out

    def test_slot_must_be_positive(self, temp_home):
        """Slot number must be >= 1."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._make_switcher(temp_home)

        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             pytest.raises(ConfigError, match="must be >= 1"):
            switcher.add_account(slot=0)

    def test_sequence_stays_sorted(self, temp_home):
        """Sequence list should remain sorted when using --slot."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add to slot 5
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account(slot=5)

        # Add to slot 2
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with (
            patch.object(switcher, "_read_credentials", return_value=fake_creds),
            patch.object(
                switcher,
                "_write_verified_live_account_credentials",
                return_value=fake_creds,
            ),
        ):
            switcher.add_account(slot=2)

        data = switcher._get_sequence_data()
        assert data["sequence"] == [2, 5]

    def test_add_account_retries_until_backup_matches_live_credentials(self, temp_home):
        fake_creds_old = json.dumps({"claudeAiOauth": {"accessToken": "tok-old"}})
        fake_creds_new = json.dumps({"claudeAiOauth": {"accessToken": "tok-new"}})
        switcher = self._make_switcher(temp_home)

        with (
            patch.object(
                switcher,
                "_read_credentials",
                side_effect=[fake_creds_old, fake_creds_new, fake_creds_new],
            ),
            patch.object(
                switcher,
                "_read_account_credentials",
                side_effect=[fake_creds_old, fake_creds_new],
            ),
            patch.object(
                switcher,
                "_write_account_credentials",
            ) as write_creds,
            patch(
                "claude_swap.switcher.time.sleep",
            ),
        ):
            switcher.add_account(slot=1)

        assert write_creds.call_count == 2
        assert write_creds.call_args_list[0].args == (
            "1",
            "test@example.com",
            fake_creds_old,
        )
        assert write_creds.call_args_list[1].args == (
            "1",
            "test@example.com",
            fake_creds_new,
        )

    def test_add_account_raises_when_backup_never_matches_live_credentials(
        self, temp_home
    ):
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok-live"}})
        switcher = self._make_switcher(temp_home)

        with (
            patch.object(
                switcher,
                "_read_credentials",
                side_effect=[fake_creds, fake_creds, fake_creds, fake_creds],
            ),
            patch.object(
                switcher,
                "_read_account_credentials",
                side_effect=["stale-1", "stale-2", "stale-3"],
            ),
            patch.object(
                switcher,
                "_write_account_credentials",
            ),
            patch(
                "claude_swap.switcher.time.sleep",
            ),
            pytest.raises(
                CredentialWriteError,
                match="Stored backup credentials did not match live credentials",
            ),
        ):
            switcher.add_account(slot=1)


class TestAddAccountFromToken:
    """Tests for add_account_from_token (--add-token flow)."""

    def _make_switcher(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_basic_add_stores_account(self, temp_home, capsys):
        """A valid token + email should store the account and print 'Added'."""
        switcher = self._make_switcher(temp_home)
        with (
            patch.object(switcher, "_write_account_credentials"),
            patch.object(switcher, "_write_account_config"),
        ):
            switcher.add_account_from_token("sk-ant-oat01-abc", "user@example.com")

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]
        assert data["accounts"]["1"]["email"] == "user@example.com"
        assert 1 in data["sequence"]
        out = capsys.readouterr().out
        assert "Added" in out
        assert "user@example.com" in out

    def test_credentials_blob_format(self, temp_home):
        """Stored credentials must wrap the token in claudeAiOauth and seed default scopes."""
        switcher = self._make_switcher(temp_home)
        stored_creds = None

        def capture_creds(num, email, creds):
            nonlocal stored_creds
            stored_creds = creds

        with patch.object(switcher, "_write_account_credentials", side_effect=capture_creds), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("mytoken", "user@example.com")

        oauth_blob = json.loads(stored_creds)["claudeAiOauth"]
        assert oauth_blob["accessToken"] == "mytoken"
        assert oauth_blob["scopes"] == list(SETUP_TOKEN_SCOPES)

    def test_config_blob_contains_email(self, temp_home):
        """Stored config must contain oauthAccount.emailAddress."""
        switcher = self._make_switcher(temp_home)
        stored_config = None

        def capture_config(num, email, cfg):
            nonlocal stored_config
            stored_config = cfg

        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config", side_effect=capture_config):
            switcher.add_account_from_token("mytoken", "user@example.com")

        cfg = json.loads(stored_config)
        assert cfg["oauthAccount"]["emailAddress"] == "user@example.com"

    def test_explicit_slot(self, temp_home):
        """--slot should place the account in the specified slot."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", "user@example.com", slot=7)

        data = switcher._get_sequence_data()
        assert "7" in data["accounts"]
        assert "1" not in data["accounts"]
        assert 7 in data["sequence"]

    def test_update_in_place_same_email(self, temp_home, capsys):
        """Calling add_account_from_token again for the same email refreshes in place."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v1", "user@example.com")
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v2", "user@example.com")

        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        out = capsys.readouterr().out
        assert "Updated token" in out

    def test_update_in_place_writes_scopes(self, temp_home):
        """Refreshing an existing account in place must also seed default scopes."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v1", "user@example.com")

        stored_creds = None

        def capture_creds(num, email, creds):
            nonlocal stored_creds
            stored_creds = creds

        with patch.object(switcher, "_write_account_credentials", side_effect=capture_creds), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v2", "user@example.com")

        oauth_blob = json.loads(stored_creds)["claudeAiOauth"]
        assert oauth_blob["accessToken"] == "token-v2"
        assert oauth_blob["scopes"] == list(SETUP_TOKEN_SCOPES)

    def test_update_in_place_rejects_inconsistent_metadata(self, temp_home):
        """Never write account-None-* credentials if sequence lookup is corrupt."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_account_exists", return_value=True), \
             patch.object(switcher, "_write_account_credentials") as write_creds, \
             pytest.raises(ConfigError, match="metadata.*inconsistent"):
            switcher.add_account_from_token("token-v2", "user@example.com")

        write_creds.assert_not_called()

    def test_invalid_email_raises(self, temp_home):
        """A malformed email should raise ValidationError."""
        switcher = self._make_switcher(temp_home)
        with pytest.raises(ValidationError, match="Invalid email"):
            switcher.add_account_from_token("tok", "not-an-email")

    def test_empty_token_raises(self, temp_home):
        """An empty token string should raise ValidationError."""
        switcher = self._make_switcher(temp_home)
        with pytest.raises(ValidationError, match="empty"):
            switcher.add_account_from_token("   ", "user@example.com")

    def test_stdin_token(self, temp_home, capsys):
        """Token='-' should read from stdin."""
        switcher = self._make_switcher(temp_home)
        import io
        fake_stdin = io.StringIO("stdin-token\n")
        with patch("sys.stdin", fake_stdin), \
             patch.object(switcher, "_write_account_credentials") as mock_creds, \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("-", "user@example.com")

        stored = mock_creds.call_args[0][2]
        oauth_blob = json.loads(stored)["claudeAiOauth"]
        assert oauth_blob["accessToken"] == "stdin-token"
        assert oauth_blob["scopes"] == list(SETUP_TOKEN_SCOPES)

    def test_slot_zero_raises(self, temp_home):
        """Slot 0 should raise ConfigError."""
        switcher = self._make_switcher(temp_home)
        with pytest.raises(ConfigError, match=">= 1"):
            switcher.add_account_from_token("tok", "user@example.com", slot=0)

    def test_sequence_sorted_after_add(self, temp_home):
        """Sequence must remain sorted when using an explicit slot."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", "a@example.com", slot=5)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", "b@example.com", slot=2)

        data = switcher._get_sequence_data()
        assert data["sequence"] == [2, 5]

    def test_default_email_when_omitted(self, temp_home, capsys):
        """Omitting email should synthesize setup-token-{slot}@token.local."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok")

        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "setup-token-1@token.local"
        out = capsys.readouterr().out
        assert "setup-token-1@token.local" in out

    def test_default_email_with_explicit_slot(self, temp_home):
        """Default email should derive from explicit --slot when one is given."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", slot=7)

        data = switcher._get_sequence_data()
        assert data["accounts"]["7"]["email"] == "setup-token-7@token.local"

    def test_default_email_writes_to_config_blob(self, temp_home):
        """Defaulted email must propagate into the oauthAccount.emailAddress field."""
        switcher = self._make_switcher(temp_home)
        stored_config = None

        def capture_config(num, email, cfg):
            nonlocal stored_config
            stored_config = cfg

        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config", side_effect=capture_config):
            switcher.add_account_from_token("tok", slot=3)

        cfg = json.loads(stored_config)
        assert cfg["oauthAccount"]["emailAddress"] == "setup-token-3@token.local"

    def test_default_email_unique_per_slot(self, temp_home):
        """Two default-email registrations to different slots must coexist."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok-a", slot=4)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok-b", slot=8)

        data = switcher._get_sequence_data()
        emails = {data["accounts"][n]["email"] for n in ("4", "8")}
        assert emails == {
            "setup-token-4@token.local",
            "setup-token-8@token.local",
        }

    def test_explicit_email_not_overridden_by_default(self, temp_home):
        """Explicit --email must win over the auto-default."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", email="me@example.com", slot=2)

        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "me@example.com"

