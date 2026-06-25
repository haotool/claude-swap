"""Tests for the ClaudeAccountSwitcher class."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    CredentialWriteError,
    ValidationError,
)
from claude_swap.models import Platform
from claude_swap.paths import get_backup_root
from claude_swap.switcher import ClaudeAccountSwitcher, SETUP_TOKEN_SCOPES


def _usage_payload(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "_cached_at"}


class TestEmailValidation:
    """Test email validation."""

    def test_valid_emails(self, temp_home: Path):
        """Test that valid emails pass validation."""
        switcher = ClaudeAccountSwitcher()
        valid_emails = [
            "user@example.com",
            "user.name@example.co.uk",
            "user+tag@example.org",
            "user123@test.io",
        ]
        for email in valid_emails:
            assert switcher._validate_email(email), f"Expected {email} to be valid"

    def test_invalid_emails(self, temp_home: Path):
        """Test that invalid emails fail validation."""
        switcher = ClaudeAccountSwitcher()
        invalid_emails = [
            "not-an-email",
            "@example.com",
            "user@",
            "user@.com",
            "",
            "user@com",
        ]
        for email in invalid_emails:
            assert not switcher._validate_email(email), f"Expected {email} to be invalid"


class TestPlatformDetection:
    """Test platform detection."""

    @patch("platform.system", return_value="Darwin")
    def test_macos_detection(self, mock_system, temp_home: Path):
        """Test macOS platform detection."""
        assert Platform.detect() == Platform.MACOS

    @patch("platform.system", return_value="Linux")
    @patch.dict(os.environ, {}, clear=False)
    def test_linux_detection(self, mock_system, temp_home: Path):
        """Test Linux platform detection."""
        # Ensure WSL_DISTRO_NAME is not set
        env = os.environ.copy()
        env.pop("WSL_DISTRO_NAME", None)
        with patch.dict(os.environ, env, clear=True):
            assert Platform.detect() == Platform.LINUX

    @patch("platform.system", return_value="Linux")
    @patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"})
    def test_wsl_detection(self, mock_system, temp_home: Path):
        """Test WSL platform detection."""
        assert Platform.detect() == Platform.WSL

    @patch("platform.system", return_value="Windows")
    def test_windows_detection(self, mock_system, temp_home: Path):
        """Test Windows platform detection."""
        assert Platform.detect() == Platform.WINDOWS

    @patch("platform.system", return_value="FreeBSD")
    def test_unknown_platform(self, mock_system, temp_home: Path):
        """Test unknown platform detection."""
        assert Platform.detect() == Platform.UNKNOWN


class TestJsonOperations:
    """Test JSON read/write operations."""

    def test_write_and_read_json(self, temp_home: Path):
        """Test writing and reading JSON files."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "test.json"
        test_data = {"key": "value", "number": 42, "nested": {"a": 1}}

        switcher._write_json(test_path, test_data)
        result = switcher._read_json(test_path)

        assert result == test_data

    def test_read_nonexistent_json(self, temp_home: Path):
        """Test reading non-existent JSON file returns None."""
        switcher = ClaudeAccountSwitcher()
        result = switcher._read_json(Path("/nonexistent/path.json"))
        assert result is None

    def test_read_invalid_json(self, temp_home: Path):
        """Test reading invalid JSON file returns None."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "invalid.json"
        test_path.write_text("not valid json {{{")

        result = switcher._read_json(test_path)
        assert result is None

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_json_file_permissions(self, temp_home: Path):
        """Test that JSON files are written with correct permissions."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "secure.json"
        switcher._write_json(test_path, {"secret": "data"})

        # Check file permissions (0o600 = owner read/write only)
        stat = test_path.stat()
        assert stat.st_mode & 0o777 == 0o600


class TestGetCurrentAccount:
    """Test getting current account."""

    def test_no_config_file(self, temp_home: Path):
        """Test when no config file exists."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None

    def test_with_valid_config(self, temp_home: Path, mock_claude_config: Path):
        """Test reading email from valid config."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() == ("test@example.com", "")

    def test_config_without_oauth(self, temp_home: Path):
        """Test config file without oauthAccount."""
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({"other": "data"}))

        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None

    def test_config_with_empty_email(self, temp_home: Path):
        """Test config with empty email address."""
        config_path = temp_home / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "", "accountUuid": "uuid"}})
        )

        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None


class TestGetClaudeConfigPathUtf8:
    """Regression: Windows default encoding must not break UTF-8 Claude configs."""

    def test_fallback_config_with_unicode_punctuation(self, temp_home: Path):
        """~/.claude.json with non-ASCII (e.g. smart quotes) must be readable."""
        config = {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "uuid-1",
                "displayName": "Name with \u201csmart\u201d quotes",
            }
        }
        fallback = temp_home / ".claude.json"
        fallback.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        switcher = ClaudeAccountSwitcher()
        resolved = switcher._get_claude_config_path()
        assert resolved == fallback


class TestAccountExists:
    """Test account existence checking."""

    def test_account_exists(self, temp_home: Path, sample_sequence_data: dict):
        """Test checking if account exists."""
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._account_exists("account1@example.com", "") is True
        assert switcher._account_exists("nonexistent@example.com", "") is False

    def test_no_sequence_file(self, temp_home: Path):
        """Test account exists when no sequence file."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._account_exists("any@example.com", "") is False


class TestResolveAccountIdentifier:
    """Test resolving account identifiers."""

    def test_resolve_by_number(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving account by number."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("1") == "1"
        assert switcher._resolve_account_identifier("2") == "2"

    def test_resolve_by_email(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving account by email."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("account1@example.com") == "1"
        assert switcher._resolve_account_identifier("account2@example.com") == "2"

    def test_resolve_nonexistent(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving non-existent account."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("nonexistent@example.com") is None
        assert switcher._resolve_account_identifier("999") == "999"  # Numbers pass through


class TestDirectorySetup:
    """Test directory setup."""

    def test_creates_directories(self, temp_home: Path):
        """Test that setup creates required directories."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        assert switcher.backup_dir.exists()
        assert switcher.configs_dir.exists()
        assert switcher.credentials_dir.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_directory_permissions(self, temp_home: Path):
        """Test that directories have correct permissions."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        for directory in [switcher.backup_dir, switcher.configs_dir, switcher.credentials_dir]:
            stat = directory.stat()
            assert stat.st_mode & 0o777 == 0o700


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
        with patch.object(switcher, "_read_credentials", return_value=old_creds), \
             patch.object(switcher, "_write_account_credentials", side_effect=mock_write_creds), \
             patch.object(switcher, "_read_account_credentials", side_effect=mock_read_creds):
            switcher.add_account()

        # Verify first add
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        assert data["accounts"]["1"]["email"] == "test@example.com"
        assert "old-token" in stored["creds"]

        # Re-add same account with new credentials
        with patch.object(switcher, "_read_credentials", return_value=new_creds), \
             patch.object(switcher, "_write_account_credentials", side_effect=mock_write_creds), \
             patch.object(switcher, "_read_account_credentials", side_effect=mock_read_creds):
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


class TestGetNextAccountNumber:
    """Test getting next account number."""

    def test_first_account(self, temp_home: Path):
        """Test first account number is 1."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        assert switcher._get_next_account_number() == 1

    def test_with_existing_accounts(self, temp_home: Path, sample_sequence_data: dict):
        """Test next number after existing accounts."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._get_next_account_number() == 3


class TestStatus:
    """Test status command."""

    def test_status_no_account(self, temp_home: Path):
        """Test status when no account is logged in."""
        switcher = ClaudeAccountSwitcher()
        # Should not raise, just print
        switcher.status()

    def test_status_unmanaged_account(
        self, temp_home: Path, mock_claude_config: Path
    ):
        """Test status with unmanaged account."""
        switcher = ClaudeAccountSwitcher()
        switcher.status()

    def test_status_managed_account(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Test status with managed account."""
        # Update sequence data to match mock config email
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        switcher.status()


class TestStatusCache:
    """status() shares the usage.json cache with list_accounts."""

    def test_status_uses_cached_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """A fresh cache entry for the active account skips the API call."""
        from claude_swap.cache import write_cache

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        cached_usage = {
            "1": {"five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"},
                  "seven_day": {"pct": 60, "clock": "Jan 2 03:00", "countdown": "2d"}},
        }
        write_cache(switcher.backup_dir / "cache" / "usage.json", cached_usage)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account") as mock_fetch:
            switcher.status()

        mock_fetch.assert_not_called()
        output = capsys.readouterr().out
        assert "25%" in output
        assert "60%" in output

    def test_status_fetches_on_cache_miss_with_is_active_true(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """On cache miss, fetch with is_active=True (never refresh active creds) and write back."""
        from claude_swap.cache import read_cache, MISSING

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=usage_result) as mock_fetch:
            switcher.status()

        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.kwargs.get("is_active") is True

        output = capsys.readouterr().out
        assert "10%" in output

        cache_path = switcher.backup_dir / "cache" / "usage.json"
        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == usage_result
        assert "_cached_at" in cached["1"]

    def test_status_preserves_other_accounts_in_cache(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A cache miss for the active account merges into existing entries instead of clobbering."""
        from claude_swap.cache import read_cache, write_cache, MISSING

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Cache has only account "2"; status() runs for account "1"
        existing = {"2": {"five_hour": {"pct": 80}}}
        cache_path = switcher.backup_dir / "cache" / "usage.json"
        write_cache(cache_path, existing)

        usage_result = {"five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"}}

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=usage_result):
            switcher.status()

        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == usage_result
        assert cached["2"] == {"five_hour": {"pct": 80}}

    def test_status_preserves_previous_cached_usage_when_fetch_returns_none(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Transient active-account fetch failures should keep the last known usage."""
        from claude_swap.cache import read_cache, MISSING

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        previous_usage = {
            "1": {"five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"}},
            "2": {"five_hour": {"pct": 80, "clock": "Jan 1 04:00", "countdown": "30m"}},
        }
        cache_path = switcher.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"timestamp": 0, "data": previous_usage}),
            encoding="utf-8",
        )

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.status()

        output = capsys.readouterr().out
        assert "25%" in output

        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == previous_usage["1"]
        assert _usage_payload(cached["2"]) == previous_usage["2"]

    def test_status_shows_cached_usage_with_rate_limit_note(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """A rate-limited status call should surface the reason and keep stale usage visible."""
        from claude_swap import oauth
        from claude_swap.cache import read_cache, MISSING

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        previous_usage = {
            "1": {"five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"}},
        }
        cache_path = switcher.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"timestamp": 0, "data": previous_usage}),
            encoding="utf-8",
        )

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=oauth.UsageFetchError(reason="rate_limited", status_code=429),
             ):
            switcher.status()

        output = capsys.readouterr().out
        assert "25%" in output
        assert "cached; live fetch usage unavailable (rate limited)" in output

        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == previous_usage["1"]


class TestListAccountsUsage:
    """Test list_accounts shows usage info."""

    def test_list_shows_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        usage_response = {
            "five_hour": {"utilization": 10.0, "resets_at": "2026-01-01T00:00:00Z"},
            "seven_day": {"utilization": 50.0, "resets_at": "2026-01-02T00:00:00Z"},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(usage_response).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "test@example.com [personal] (active)" in output
        assert "account2@example.com" in output
        assert "├ 5h:" in output
        assert "└ 7d:" in output
        assert "10%" in output
        assert "50%" in output

    def test_list_syncs_refreshed_active_credentials_to_backup(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Active Claude Code refreshes must not leave cswap's backup token stale."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        old_backup = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 1,
            }
        })
        refreshed_live = json.dumps({
            "claudeAiOauth": {
                "accessToken": "new-access",
                "refreshToken": "new-refresh",
                "expiresAt": 9_999_999_999_000,
            }
        })

        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        switcher._write_account_credentials("1", "test@example.com", old_backup)

        with patch.object(switcher, "_read_credentials", return_value=refreshed_live), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.list_accounts()

        stored = switcher._read_account_credentials("1", "test@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "new-refresh"

    def test_health_shows_ok_for_accounts_with_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Health output should align with the list/token formatting."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})
        usage_result = {"five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"}}

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=usage_result):
            switcher.list_accounts(show_token_status=True, show_health=True)

        output = capsys.readouterr().out
        assert "health: ok" in output
        assert "oauth:" in output

    def test_health_refreshes_expiring_inactive_credentials(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Health checks should refresh inactive backups before they expire."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        expiring_backup = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 1,
            }
        })
        refreshed_backup = json.dumps({
            "claudeAiOauth": {
                "accessToken": "new-access",
                "refreshToken": "new-refresh",
                "expiresAt": 9_999_999_999_000,
            }
        })

        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        switcher._write_account_credentials("2", "account2@example.com", expiring_backup)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch("claude_swap.oauth.refresh_oauth_credentials", return_value=refreshed_backup), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.list_accounts(show_token_status=True, show_health=True)

        output = capsys.readouterr().out
        stored = switcher._read_account_credentials("2", "account2@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "new-refresh"
        assert "health: token refreshed" in output

    def test_list_shows_usage_null_reset(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """When five_hour.resets_at is null and seven_day is at 100%, display both correctly."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        usage_response = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 100.0, "resets_at": "2026-04-03T02:59:59Z"},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(usage_response).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "5h:   0%" in output
        assert "7d: 100%" in output
        assert "usage unavailable" not in output

    def test_list_no_credentials(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=""), \
             patch.object(switcher, "_read_account_credentials", return_value=""):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "no credentials" in output

    def test_list_persist_writes_only_backup_never_live(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Inactive account refresh persists to backup only — never touches live.

        Regression guard for the design drift where the persist closure used
        to rewrite live credentials for the active account. Per
        OAUTH_REFRESH_REDESIGN.md, cswap must never write to live creds — that
        would race with Claude Code's own refresh (which coordinates via a
        ~/.claude/ lockfile cswap doesn't honor).
        """
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-backup", "refreshToken": "rt-orig"},
        })
        refreshed_creds = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new"},
        })

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            # Simulate a refresh on the inactive account only.
            if not is_active:
                persist_credentials(account_num, email, refreshed_creds)
            return None

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch):
            switcher.list_accounts()

        # Live creds must never be written from list_accounts()
        write_live.assert_not_called()
        # Backup was written for the inactive account (2) only.
        write_backup.assert_called_once_with("2", "account2@example.com", refreshed_creds)

    def test_list_shows_token_status_when_requested(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=None), \
             patch("claude_swap.oauth.build_token_status", return_value="oauth: fresh, refresh token yes"):
            switcher.list_accounts(show_token_status=True)

        output = capsys.readouterr().out
        assert "oauth: fresh, refresh token yes" in output

    def test_list_uses_cached_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """When a fresh usage cache exists, list_accounts skips API calls."""
        from claude_swap.cache import write_cache
        from claude_swap.switcher import _usage_to_cache

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Pre-populate cache with usage data for both accounts
        cached_usage = {
            "1": {"five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"},
                   "seven_day": {"pct": 60, "clock": "Jan 2 03:00", "countdown": "2d"}},
            "2": {"five_hour": {"pct": 80, "clock": "Jan 1 04:00", "countdown": "30m"},
                   "seven_day": {"pct": 90, "clock": "Jan 3 03:00", "countdown": "3d"}},
        }
        write_cache(
            switcher.backup_dir / "cache" / "usage.json",
            {k: _usage_to_cache(v) for k, v in cached_usage.items()},
        )

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account") as mock_fetch:
            switcher.list_accounts()

        # API should NOT have been called — data came from cache
        mock_fetch.assert_not_called()
        output = capsys.readouterr().out
        assert "25%" in output
        assert "80%" in output

    def test_list_ignores_cache_when_accounts_change(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Cache is invalidated when the account set doesn't match."""
        from claude_swap.cache import write_cache

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Cache has only account "1" but the switcher has accounts "1" and "2"
        cached_usage = {
            "1": {"five_hour": {"pct": 25}},
        }
        write_cache(switcher.backup_dir / "cache" / "usage.json", cached_usage)

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=usage_result):
            switcher.list_accounts()

        output = capsys.readouterr().out
        # Should show live data (10%), not cached data (25%)
        assert "10%" in output

    def test_list_preserves_previous_cached_usage_when_fetch_returns_none(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Transient fetch failures should keep the last known usage instead of clobbering it."""
        from claude_swap.cache import read_cache, MISSING

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        previous_usage = {
            "1": {"five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"}},
            "2": {"five_hour": {"pct": 80, "clock": "Jan 1 04:00", "countdown": "30m"}},
        }
        cache_path = switcher.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"timestamp": 0, "data": previous_usage}),
            encoding="utf-8",
        )

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 side_effect=lambda num, *args, **kwargs: (
                     None
                     if str(num) == "1"
                     else {
                         "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
                         "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
                     }
                 ),
             ):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "25%" in output
        assert "10%" in output

        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == previous_usage["1"]

    def test_list_shows_rate_limit_when_no_previous_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys, caplog
    ):
        """A classified rate-limit failure should be visible without debug logs."""
        import logging
        from claude_swap import oauth
        from claude_swap.cache import read_cache, MISSING

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        rate_limited = oauth.UsageFetchError(reason="rate_limited", status_code=429)
        usage_result = {"five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"}}

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            with patch.object(switcher, "_read_credentials", return_value=active_creds), \
                 patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
                 patch(
                     "claude_swap.oauth.fetch_usage_for_account",
                     side_effect=lambda num, *args, **kwargs: (
                         rate_limited if str(num) == "1" else usage_result
                     ),
                 ):
                switcher.list_accounts()

        output = capsys.readouterr().out
        assert "usage unavailable (rate limited)" in output
        assert "10%" in output

        cached = read_cache(switcher.backup_dir / "cache" / "usage.json", 300)
        assert cached is not MISSING
        assert cached["1"]["_type"] == "usage_fetch_error"
        assert cached["1"]["reason"] == "rate_limited"
        assert "Usage fetch unavailable: account=1" in caplog.text
        assert "reason=rate_limited" in caplog.text

    def test_list_shows_cached_usage_with_rate_limit_note(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Stale usage should remain visible when a live refresh is rate-limited."""
        from claude_swap import oauth

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        previous_usage = {
            "1": {"five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"}},
            "2": {"five_hour": {"pct": 80, "clock": "Jan 1 04:00", "countdown": "30m"}},
        }
        cache_path = switcher.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"timestamp": 0, "data": previous_usage}),
            encoding="utf-8",
        )

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 side_effect=lambda num, *args, **kwargs: (
                     oauth.UsageFetchError(reason="rate_limited", status_code=429)
                     if str(num) == "1"
                     else {"five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"}}
                 ),
             ):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "25%" in output
        assert "10%" in output
        assert "cached; live fetch usage unavailable (rate limited)" in output


class TestPerformSwitchPostDisplay:
    """Regression tests for the post-switch display running outside the lock."""

    @staticmethod
    def _background_intent() -> "BackgroundAutoSwitchIntent":
        from claude_swap.models import (
            AutoSwitchDecisionContext,
            BackgroundAutoSwitchIntent,
        )

        return BackgroundAutoSwitchIntent(
            decision=AutoSwitchDecisionContext(
                threshold=95,
                active_usage_pct=None,
                live_active_slot="1",
                sequence_active_slot="1",
                usage_by_slot={},
            ),
        )

    def _setup_two_accounts(
        self,
        temp_home: Path,
        sample_sequence_data: dict,
    ) -> tuple[ClaudeAccountSwitcher, dict, dict]:
        """Set up a switcher with two managed accounts using in-memory
        credential and config stores.

        This bypasses the real macOS Keychain / Windows Credential Manager
        completely so tests never prompt the user for "restore to defaults"
        on macOS and never leak credentials into the developer's keyring.

        Returns (switcher, creds_store, configs_store). Live credentials for
        the active account are written to the temp-home credentials file
        (safe — that file lives in the test's tmp_path).
        """
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Live credentials for active account 1 (file under temp_home).
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)

        # Expired backup credentials for account 2 — forces refresh in
        # list_accounts() proactive path.
        expired_2 = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-stale-2",
                "refreshToken": "rt-orig-2",
                "expiresAt": 0,
                "scopes": ["user:profile"],
            },
        })

        # In-memory stores keyed by (num, email).
        creds_store: dict[tuple[str, str], str] = {
            ("2", "account2@example.com"): expired_2,
        }
        configs_store: dict[tuple[str, str], str] = {
            ("2", "account2@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "account2@example.com",
                    "accountUuid": "uuid-2",
                },
            }),
        }
        return switcher, creds_store, configs_store

    @staticmethod
    def _install_store_patches(
        switcher: ClaudeAccountSwitcher,
        creds_store: dict[tuple[str, str], str],
        configs_store: dict[tuple[str, str], str],
        live_state: dict,
    ) -> list:
        """Patch credential/config read/write to use in-memory stores.

        Critically, this also stubs _read_credentials/_write_credentials so
        nothing touches the real macOS Keychain (which would prompt the user
        with "Claude wants to use the confidential information stored in your
        keychain" during the test run).
        """
        def read_creds(num, email):
            return creds_store.get((str(num), email), "")

        def write_creds(num, email, creds):
            creds_store[(str(num), email)] = creds

        def read_cfg(num, email):
            return configs_store.get((str(num), email), "")

        def write_cfg(num, email, cfg):
            configs_store[(str(num), email)] = cfg

        def read_live():
            return live_state.get("creds", "")

        def write_live(creds, *, verify: bool = False) -> None:
            live_state["creds"] = creds
            # Honour the production contract: verify=True must validate the
            # readback. Since both read/write target the same in-memory dict
            # in these tests, the check is trivially satisfied — but the stub
            # must still accept the kwarg or _perform_switch crashes.
            if verify and read_live() != creds:
                # Match the real CredentialWriteError message shape.
                from claude_swap.exceptions import CredentialWriteError
                raise CredentialWriteError(
                    "Credential write verification failed (test stub)"
                )

        patches = [
            patch.object(switcher, "_read_account_credentials", side_effect=read_creds),
            patch.object(switcher, "_write_account_credentials", side_effect=write_creds),
            patch.object(switcher, "_read_account_config", side_effect=read_cfg),
            patch.object(switcher, "_write_account_config", side_effect=write_cfg),
            patch.object(switcher, "_read_credentials", side_effect=read_live),
            patch.object(switcher, "_write_credentials", side_effect=write_live),
        ]
        for p in patches:
            p.start()
        return patches

    def test_switch_persists_rotated_refresh_token_to_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Regression: _perform_switch must persist refreshed credentials to backup.

        Prior to the fix, _perform_switch held the outer FileLock around
        list_accounts(). Inside list_accounts(), the persist closure tried to
        re-acquire the same file lock (different FD, so fcntl.flock is NOT
        re-entrant), spun to the 10s timeout, raised LockError, and the
        refreshed credentials were silently dropped at debug level. If
        Anthropic rotated the refresh token on that request, the backup
        retained the old (now-invalid) refresh token and the only recovery
        was a re-login.

        This test exercises the full _perform_switch path with account 2
        needing a refresh, and verifies the rotated refresh token actually
        landed on disk. Against main this fails; against the fix it passes.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        # The currently-active account 1's creds carry an expired expiresAt.
        # After the swap, account 1 becomes *inactive* and its just-backed-up
        # credentials are eligible for proactive refresh inside the
        # post-switch list_accounts() call. This is the scenario that
        # triggers the original deadlock bug.
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-orig-1",
                "expiresAt": 0,
                "scopes": ["user:profile"],
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        # Monkeypatch refresh_oauth_credentials to simulate a server-side
        # refresh-token rotation (rt-orig-1 -> rt-rotated-1).
        rotated_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-rotated-1",
                "refreshToken": "rt-rotated-1",
                "expiresAt": 9_999_999_999_000,
                "scopes": ["user:profile"],
            },
        })

        try:
            with patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=rotated_creds,
            ), patch(
                "claude_swap.oauth.request_usage_data",
                return_value={
                    "five_hour": {"utilization": 12.0, "resets_at": None},
                    "seven_day": {"utilization": 34.0, "resets_at": None},
                },
            ):
                switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # After switch, backup for account 1 (now inactive) must contain the
        # rotated refresh token — confirming the persist inside list_accounts()
        # actually fired and didn't hit the lock deadlock.
        backup_after = creds_store.get(("1", "test@example.com"), "")
        assert backup_after, "backup credentials for account 1 are missing"
        backup_oauth = json.loads(backup_after)["claudeAiOauth"]
        assert backup_oauth["refreshToken"] == "rt-rotated-1", (
            f"Expected rotated refresh token on disk, got "
            f"{backup_oauth.get('refreshToken')!r} — lock deadlock regression"
        )
        assert backup_oauth["accessToken"] == "sk-rotated-1"

    def test_quiet_switch_suppresses_banners_and_followup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """BackgroundAutoSwitchIntent suppresses banners and followup:
        launchd's stdout/stderr should not collect interactive banner text or
        the platform-specific 'next message / 30s' followup line.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=creds_store[("2", "account2@example.com")],
            ), patch.object(
                switcher, "list_accounts"
            ) as mock_list:
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        # Commit happened — sequence advanced.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 2

        # Output stays empty: no "Switched to", no followup, no list_accounts().
        output = capsys.readouterr().out
        assert "Switched to" not in output
        assert "New account active" not in output
        assert "restart Claude Code" not in output
        mock_list.assert_not_called()

    def test_force_refresh_threads_through_perform_switch(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """BackgroundAutoSwitchIntent must forward force_refresh to
        _refresh_target_credentials_before_activation as force=True.
        Otherwise the monitor's "fresh token after handoff" guarantee is broken.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(
                switcher,
                "_refresh_target_credentials_before_activation",
                wraps=switcher._refresh_target_credentials_before_activation,
            ) as spy, patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=creds_store[("2", "account2@example.com")],
            ), patch.object(
                switcher, "list_accounts"
            ):
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        # The spy should have seen force=True.
        spy.assert_called_once()
        assert spy.call_args.kwargs.get("force") is True

    def test_activation_followup_text_is_platform_aware(self, temp_home: Path):
        """README documents the platform difference; the followup line must
        reflect it so the user-visible message stays honest with reality."""
        from claude_swap.models import Platform

        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS
        mac_text = switcher._activation_followup_text()
        assert "Keychain" in mac_text and "30s" in mac_text

        switcher.platform = Platform.LINUX
        linux_text = switcher._activation_followup_text()
        assert "next message" in linux_text
        assert "Keychain" not in linux_text

    def test_write_credentials_verify_failure_aborts_switch(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Defensive readback: when the storage layer silently returns
        different bytes than we wrote, ``_perform_switch`` must abort and
        roll back rather than commit a corrupt swap.

        Simulates the silent-Keychain-overwrite scenario by making
        ``_read_credentials`` return a stale payload after our write.
        """
        from claude_swap.exceptions import CredentialWriteError

        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        # Inject a verify mismatch: _read_credentials returns a tampered
        # payload after the write, simulating a silent Keychain overwrite.
        def write_then_corrupt(creds, *, verify=False):
            live_state["creds"] = creds
            if verify:
                # Pretend readback returned something else entirely.
                raise CredentialWriteError(
                    "Credential write verification failed: readback differs "
                    "from intended payload."
                )

        try:
            with patch.object(
                switcher, "_write_credentials", side_effect=write_then_corrupt,
            ), patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=creds_store[("2", "account2@example.com")],
            ):
                with pytest.raises(Exception) as exc_info:
                    switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # Either CredentialWriteError directly or SwitchError wrapping it.
        msg = str(exc_info.value)
        assert "verification failed" in msg or "readback" in msg

        # Sequence must NOT have advanced.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 1

    def test_multi_session_race_warning_logged_when_two_plus_running(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        caplog,
    ):
        """When >1 default-mode Claude Code processes are running, the
        switch must log a structured warning naming the PIDs and the
        underlying claude-code#24317 race condition. The switch still
        proceeds — the warning is informational."""
        import logging as _logging

        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        caplog.set_level(_logging.WARNING, logger="claude-swap")
        try:
            with patch.object(
                switcher,
                "_live_default_mode_claude_pids",
                return_value=[101, 202, 303],
            ), patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=creds_store[("2", "account2@example.com")],
            ), patch.object(switcher, "list_accounts"):
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        warnings = [
            r.getMessage() for r in caplog.records
            if r.name == "claude-swap" and r.levelno == _logging.WARNING
        ]
        assert any(
            "multi-session race" in m and "101" in m and "303" in m
            and "24317" in m
            for m in warnings
        ), warnings

        # Switch still committed — the warning is non-blocking.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 2

    def test_multi_session_race_warning_silent_with_single_session(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        caplog,
    ):
        """With <=1 live Claude Code process the warning must not fire —
        log noise here would train users to ignore real signals."""
        import logging as _logging

        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        caplog.set_level(_logging.WARNING, logger="claude-swap")
        try:
            with patch.object(
                switcher,
                "_live_default_mode_claude_pids",
                return_value=[101],
            ), patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=creds_store[("2", "account2@example.com")],
            ), patch.object(switcher, "list_accounts"):
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        warnings = [
            r.getMessage() for r in caplog.records
            if r.name == "claude-swap" and r.levelno == _logging.WARNING
        ]
        assert not any("multi-session race" in m for m in warnings), warnings

    def test_switch_survives_post_display_failure(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """Regression: a failure inside post-switch list_accounts() must not
        propagate as a switch failure. The swap already committed; the display
        is best-effort.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(
                switcher,
                "list_accounts",
                side_effect=RuntimeError("boom"),
            ), patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=creds_store[("2", "account2@example.com")],
            ):
                # Must not raise
                switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # Switch actually committed: sequence now points at account 2.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 2

        output = capsys.readouterr().out
        assert "Switched to" in output
        assert "usage display unavailable" in output
        # Followup line is platform-aware; both variants reference activation.
        assert "New account active" in output

    def test_switch_with_unset_active_account_does_not_write_none_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
    ):
        """purge -> add-token -> switch-to must not back up live creds as None."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": None,
                    "expiresAt": None,
                    "scopes": ["user:inference"],
                    "subscriptionType": None,
                    "rateLimitTier": None,
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "existing-live-token",
                "refreshToken": "existing-refresh",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert not any(num == "None" for num, _ in creds_store)
        assert not any(num == "None" for num, _ in configs_store)
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == (
            "target-token"
        )
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_switch_uses_live_identity_for_current_backup_slot(
        self,
        temp_home: Path,
    ):
        """Do not trust stale activeAccountNumber when backing up live creds."""
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "realiti44@gmail.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        }))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 3,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [3, 4],
            "accounts": {
                "3": {
                    "email": "onurcetinkol@gmail.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
                "4": {
                    "email": "realiti44@gmail.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
            },
        })
        target_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "target-token",
                "refreshToken": "target-refresh",
            }
        })
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "realiti-live-token",
                "refreshToken": "realiti-live-refresh",
            }
        })
        creds_store = {
            ("3", "onurcetinkol@gmail.com"): target_creds,
            ("4", "realiti44@gmail.com"): "old-realiti-backup",
        }
        configs_store = {
            ("3", "onurcetinkol@gmail.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "onurcetinkol@gmail.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
            ("4", "realiti44@gmail.com"): "old-realiti-config",
        }
        live_state = {"creds": live_creds}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(switcher, "list_accounts"):
                switcher._perform_switch("3")
        finally:
            for p in patches:
                p.stop()

        assert creds_store[("4", "realiti44@gmail.com")] == live_creds
        assert ("3", "realiti44@gmail.com") not in creds_store
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == (
            "target-token"
        )

    def test_direct_activation_rolls_back_live_creds_on_sequence_write_failure(
        self,
        temp_home: Path,
    ):
        """Live creds must be restored if a write fails after they were swapped."""
        config_path = temp_home / ".claude.json"
        original_config_text = json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })
        config_path.write_text(original_config_text)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        original_live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "live-untracked-token",
                "refreshToken": "live-untracked-refresh",
            }
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": "target-refresh",
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": original_live_creds}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json

        def failing_write_json(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 1:
                raise OSError("disk full")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=failing_write_json,
            ), pytest.raises(OSError, match="disk full"):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == original_live_creds
        assert config_path.read_text() == original_config_text

    def test_direct_activation_fails_fast_when_live_creds_unreadable(
        self,
        temp_home: Path,
    ):
        """Refuse to overwrite live creds we couldn't snapshot for rollback."""
        config_path = temp_home / ".claude.json"
        original_config_text = json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })
        config_path.write_text(original_config_text)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": "target-refresh",
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": "live-creds-that-we-cannot-read"}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(
                switcher, "_read_credentials", return_value=None,
            ), pytest.raises(CredentialReadError, match="snapshot"):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == "live-creds-that-we-cannot-read"
        assert config_path.read_text() == original_config_text


# ── Task 1: AccountInfo org fields ───────────────────────────────────────────

class TestAccountInfoOrgFields:
    def test_account_info_includes_org_fields(self):
        """AccountInfo should store organization UUID and name."""
        from claude_swap.models import AccountInfo
        info = AccountInfo(
            email="user@example.com",
            uuid="user-uuid",
            organization_uuid="org-uuid-123",
            organization_name="Acme Corp",
            added="2024-01-01T00:00:00Z",
            number=1,
        )
        assert info.organization_uuid == "org-uuid-123"
        assert info.organization_name == "Acme Corp"

    def test_account_info_personal_account_has_empty_org(self):
        """Personal accounts should have empty string for organization fields."""
        from claude_swap.models import AccountInfo
        info = AccountInfo.from_dict(1, {
            "email": "user@example.com",
            "uuid": "user-uuid",
            "added": "2024-01-01T00:00:00Z",
        })
        assert info.organization_uuid == ""
        assert info.organization_name == ""

    def test_account_info_to_dict_includes_org_fields(self):
        """to_dict() should include organization fields."""
        from claude_swap.models import AccountInfo
        info = AccountInfo(
            email="user@example.com",
            uuid="user-uuid",
            organization_uuid="org-uuid",
            organization_name="Acme",
            added="2024-01-01T00:00:00Z",
            number=1,
        )
        d = info.to_dict()
        assert d["organizationUuid"] == "org-uuid"
        assert d["organizationName"] == "Acme"

    def test_account_info_is_organization_property(self):
        """is_organization should be determined by organizationUuid presence."""
        from claude_swap.models import AccountInfo
        org = AccountInfo.from_dict(1, {"email": "u@e.com", "uuid": "u", "added": "", "organizationUuid": "o"})
        personal = AccountInfo.from_dict(2, {"email": "u@e.com", "uuid": "u", "added": ""})
        assert org.is_organization is True
        assert personal.is_organization is False

    def test_account_info_display_label(self):
        """display_label should include org name or personal tag."""
        from claude_swap.models import AccountInfo
        org = AccountInfo(email="u@e.com", uuid="u", organization_uuid="o",
                          organization_name="Acme", added="", number=1)
        personal = AccountInfo(email="u@e.com", uuid="u", organization_uuid="",
                               organization_name="", added="", number=2)
        assert org.display_label == "u@e.com [Acme]"
        assert personal.display_label == "u@e.com [personal]"


# ── Task 3: _account_exists composite key ────────────────────────────────────

class TestAccountExistsCompositeKey:
    def test_distinguishes_org_and_personal(self, temp_home, mock_credentials_file):
        """Accounts with same email but different organizationUuid should be treated as distinct."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps({
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "user@example.com",
                    "uuid": "user-uuid",
                    "organizationUuid": "org-uuid-A",
                    "organizationName": "Acme",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        }))
        switcher = ClaudeAccountSwitcher()
        assert switcher._account_exists("user@example.com", "org-uuid-A") is True
        assert switcher._account_exists("user@example.com", "") is False
        assert switcher._account_exists("user@example.com", "org-uuid-B") is False


# ── Task 4: _get_current_account returns tuple ───────────────────────────────

class TestGetCurrentAccountOrgSupport:
    def test_returns_org_info(self, temp_home, mock_org_claude_config):
        """_get_current_account should return (email, organization_uuid) tuple."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        switcher = ClaudeAccountSwitcher()
        result = switcher._get_current_account()
        assert result == ("user@example.com", "org-uuid-5678")

    def test_returns_empty_org_for_personal(self, temp_home, mock_personal_claude_config):
        """Personal account should return tuple with empty string for organization_uuid."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        switcher = ClaudeAccountSwitcher()
        result = switcher._get_current_account()
        assert result == ("user@example.com", "")

    def test_returns_none_when_no_config(self, temp_home):
        """Should return None when config file does not exist."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        switcher = ClaudeAccountSwitcher()
        result = switcher._get_current_account()
        assert result is None


# ── Task 5: add_account with org fields ──────────────────────────────────────

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
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account()

        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
            }
        }))
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
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
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account()

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        config_path.write_text(json.dumps(org_config))
        with redirect_stdout(f), \
             patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
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
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account()

        seq = json.loads((get_backup_root() / "sequence.json").read_text())
        assert seq["accounts"]["1"]["organizationName"] == "My Org"
        assert seq["accounts"]["1"]["organizationUuid"] == "org-uuid"


# ── Task 6: _resolve_account_identifier ambiguity ────────────────────────────

class TestResolveIdentifierAmbiguity:
    def test_by_number_always_works(self, temp_home, sample_sequence_data_with_org):
        """Account number identifier should always resolve correctly."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))
        switcher = ClaudeAccountSwitcher()
        assert switcher._resolve_account_identifier("1") == "1"
        assert switcher._resolve_account_identifier("2") == "2"

    def test_raises_on_ambiguous_email(self, temp_home, sample_sequence_data_with_org):
        """Should raise ConfigError when email matches multiple accounts."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from claude_swap.exceptions import ConfigError
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))
        switcher = ClaudeAccountSwitcher()
        with pytest.raises(ConfigError, match="ambiguous"):
            switcher._resolve_account_identifier("user@example.com")

    def test_unique_email_still_works(self, temp_home, sample_sequence_data):
        """Unique email should still resolve to the correct account number."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data))
        switcher = ClaudeAccountSwitcher()
        assert switcher._resolve_account_identifier("account1@example.com") == "1"


# ── Task 7: list_accounts org display ────────────────────────────────────────

class TestListAccountsOrgDisplay:
    def test_shows_org_name_and_personal(self, temp_home, mock_credentials_file,
                                         sample_sequence_data_with_org, capsys):
        """list_accounts should display org name and personal tag."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from unittest.mock import patch

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid-5678",
                "organizationName": "Acme Corp",
            }
        }))

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.list_accounts()

        out = capsys.readouterr().out
        assert "Acme Corp" in out
        assert "personal" in out
        assert "(active)" in out

    def test_active_account_detected_by_org_uuid(self, temp_home, mock_credentials_file,
                                                   sample_sequence_data_with_org, capsys):
        """Only the account matching current org_uuid should be marked (active)."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from unittest.mock import patch

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
            }
        }))

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.list_accounts()

        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if "(active)" in ln]
        assert len(lines) == 1
        assert "personal" in lines[0]


# ── Task 8: backward compatibility ───────────────────────────────────────────

class TestBackwardCompatibility:
    def test_old_sequence_json_without_org_fields(self, temp_home, sample_sequence_data, capsys):
        """Old sequence.json without organizationUuid should work correctly."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from unittest.mock import patch

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "account1@example.com",
                "accountUuid": "uuid-1",
            }
        }))
        (temp_home / ".claude" / ".credentials.json").write_text('{"accessToken": "tok"}')

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.list_accounts()

        out = capsys.readouterr().out
        assert "account1@example.com" in out
        assert "personal" in out

    def test_status_with_old_sequence_json(self, temp_home, sample_sequence_data, capsys):
        """status should display personal for old sequence.json entries."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "account1@example.com",
                "accountUuid": "uuid-1",
            }
        }))

        switcher = ClaudeAccountSwitcher()
        switcher.status()

        out = capsys.readouterr().out
        assert "account1@example.com" in out
        assert "personal" in out


class TestUpgradeMigration:
    """Test upgrade path from pre-v0.6.0 (no org fields) to v0.6.0+."""

    def _setup_pre_v06(self, temp_home, sequence_data, live_config):
        """Helper to set up pre-v0.6.0 state with a live config."""
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sequence_data))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps(live_config))

    def test_status_after_upgrade_with_org_uuid(
        self, temp_home, sample_sequence_data_pre_v06, capsys
    ):
        """status() should detect managed account after auto-migration."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        switcher.status()

        out = capsys.readouterr().out
        assert "Account-1" in out
        assert "not managed" not in out

    def test_list_after_upgrade_marks_active(
        self, temp_home, sample_sequence_data_pre_v06, capsys
    ):
        """list_accounts() should mark the active account after auto-migration."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        )

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            switcher.list_accounts()

        out = capsys.readouterr().out
        assert "(active)" in out

    def test_migration_uses_live_config_over_backup(
        self, temp_home, sample_sequence_data_pre_v06
    ):
        """Migration should prefer live config org fields for the active account."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        data = switcher._get_sequence_data_migrated()

        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-live"
        assert data["accounts"]["1"]["organizationName"] == "Live Org"

    def test_migration_idempotent(
        self, temp_home, sample_sequence_data_pre_v06
    ):
        """Running migration twice should not change the result."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        data1 = switcher._get_sequence_data_migrated()
        data2 = switcher._get_sequence_data_migrated()

        assert data1["accounts"]["1"]["organizationUuid"] == data2["accounts"]["1"]["organizationUuid"]
        assert data1["accounts"]["2"]["organizationUuid"] == data2["accounts"]["2"]["organizationUuid"]

    def test_migration_skips_already_migrated(
        self, temp_home, sample_sequence_data_pre_v06
    ):
        """Accounts that already have org fields should not be changed."""
        sample_sequence_data_pre_v06["accounts"]["1"]["organizationUuid"] = "existing-org"
        sample_sequence_data_pre_v06["accounts"]["1"]["organizationName"] = "Existing Org"

        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "different-org",
                "organizationName": "Different Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        data = switcher._get_sequence_data_migrated()

        assert data["accounts"]["1"]["organizationUuid"] == "existing-org"
        assert data["accounts"]["1"]["organizationName"] == "Existing Org"
        assert data["accounts"]["2"]["organizationUuid"] == ""

    def test_switch_after_upgrade_no_duplicate(
        self, temp_home, sample_sequence_data_pre_v06, capsys
    ):
        """switch() on pre-v0.6.0 data should not auto-add a duplicate account."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        )

        switcher = ClaudeAccountSwitcher()
        backup_dir = get_backup_root()
        creds_dir = backup_dir / "credentials"
        creds_dir.mkdir(exist_ok=True)
        import base64
        encoded = base64.b64encode(
            json.dumps({"claudeAiOauth": {"accessToken": "token-2"}}).encode()
        ).decode()
        (creds_dir / ".creds-2-other@example.com.enc").write_text(encoded)

        configs_dir = backup_dir / "configs"
        configs_dir.mkdir(exist_ok=True)
        (configs_dir / ".claude-config-2-other@example.com.json").write_text(
            json.dumps({"oauthAccount": {
                "emailAddress": "other@example.com",
                "accountUuid": "other-uuid-5678",
            }})
        )

        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "token-2"}})
        with patch.object(switcher, "_write_credentials"), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch.object(switcher, "_read_account_config", return_value=json.dumps({
                 "oauthAccount": {
                     "emailAddress": "other@example.com",
                     "accountUuid": "other-uuid-5678",
                 }
             })):
            switcher.switch()

        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 2
        assert "auto" not in capsys.readouterr().out.lower()


# ── --slot option for add_account ──────────────────────────────────────────────

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

        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
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

        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]

    def test_slot_occupied_cancel(self, temp_home, capsys):
        """When slot is occupied and user cancels, nothing should change."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account A to slot 3
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account(slot=3)

        # Try to add account B to slot 3, answer "n"
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds), \
             patch("builtins.input", return_value="n"):
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
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(slot=3)

        # Add account B to slot 3, answer "y"
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds), \
             patch.object(switcher, "_delete_account_credentials"), \
             patch("builtins.input", return_value="y"):
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
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]

        # Move to slot 5
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds), \
             patch.object(switcher, "_delete_account_credentials"):
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
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account(slot=1)

        # Add account B to slot 3
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account(slot=3)

        # Try to move A from slot 1 → slot 3, cancel
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds), \
             patch("builtins.input", return_value="n"):
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
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account(slot=5)

        # Add to slot 2
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_credentials", return_value=fake_creds), \
             patch.object(switcher, "_write_verified_live_account_credentials", return_value=fake_creds):
            switcher.add_account(slot=2)

        data = switcher._get_sequence_data()
        assert data["sequence"] == [2, 5]

    def test_add_account_retries_until_backup_matches_live_credentials(self, temp_home):
        fake_creds_old = json.dumps({"claudeAiOauth": {"accessToken": "tok-old"}})
        fake_creds_new = json.dumps({"claudeAiOauth": {"accessToken": "tok-new"}})
        switcher = self._make_switcher(temp_home)

        with patch.object(
            switcher,
            "_read_credentials",
            side_effect=[fake_creds_old, fake_creds_new, fake_creds_new],
        ), patch.object(
            switcher,
            "_read_account_credentials",
            side_effect=[fake_creds_old, fake_creds_new],
        ), patch.object(
            switcher,
            "_write_account_credentials",
        ) as write_creds, patch(
            "claude_swap.switcher.time.sleep",
        ):
            switcher.add_account(slot=1)

        assert write_creds.call_count == 2
        assert write_creds.call_args_list[0].args == ("1", "test@example.com", fake_creds_old)
        assert write_creds.call_args_list[1].args == ("1", "test@example.com", fake_creds_new)

    def test_add_account_raises_when_backup_never_matches_live_credentials(self, temp_home):
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok-live"}})
        switcher = self._make_switcher(temp_home)

        with patch.object(
            switcher,
            "_read_credentials",
            side_effect=[fake_creds, fake_creds, fake_creds, fake_creds],
        ), patch.object(
            switcher,
            "_read_account_credentials",
            side_effect=["stale-1", "stale-2", "stale-3"],
        ), patch.object(
            switcher,
            "_write_account_credentials",
        ), patch(
            "claude_swap.switcher.time.sleep",
        ), pytest.raises(
            CredentialWriteError,
            match="Stored backup credentials did not match live credentials",
        ):
            switcher.add_account(slot=1)


class TestPurgeLegacyCleanup:
    """``purge`` must remove a stale legacy directory if it ever reappears.

    Migration normally consumes the legacy path on init, but a partial
    pre-migration state or external recreation could leave it behind.
    Purge is the user's last-resort "remove everything" hammer, so it must
    cover that case explicitly.
    """

    def _ensure_linux_layout(self, monkeypatch):
        # Tests must observe the post-migration two-path world. On macOS in
        # CI the backup root and the legacy root are the same directory, so
        # there's nothing distinct to clean — pin to LINUX semantics.
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))

    def _make_switcher_then_recreate_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[ClaudeAccountSwitcher, Path, Path]:
        """Construct a switcher with no legacy present, then recreate it.

        Mirrors the realistic state where migration completed (or never had
        anything to migrate) and a stale legacy directory subsequently
        reappeared — e.g. a user manually backing up to the old path, or a
        third-party tool restoring a snapshot.
        """
        from claude_swap.paths import get_backup_root, get_legacy_backup_root

        self._ensure_linux_layout(monkeypatch)
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Instantiate while legacy is absent → init succeeds.
        switcher = ClaudeAccountSwitcher()

        # Now legacy reappears after init.
        legacy = get_legacy_backup_root()
        legacy.mkdir(parents=True, exist_ok=True)
        return switcher, backup_dir, legacy

    def test_purge_removes_stale_legacy_directory(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        switcher, backup_dir, legacy = self._make_switcher_then_recreate_legacy(monkeypatch)
        (legacy / "ghost.txt").write_text("should be removed")

        with patch("builtins.input", return_value="y"):
            switcher.purge()

        assert not legacy.exists()
        assert not backup_dir.exists()

    def test_purge_prompt_lists_legacy_when_present(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        switcher, backup_dir, legacy = self._make_switcher_then_recreate_legacy(monkeypatch)

        with patch("builtins.input", return_value="n"):
            switcher.purge()

        out = capsys.readouterr().out
        assert str(backup_dir) in out
        assert str(legacy) in out

    def test_purge_prompt_omits_legacy_when_absent(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        from claude_swap.paths import get_backup_root, get_legacy_backup_root

        self._ensure_linux_layout(monkeypatch)
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        legacy = get_legacy_backup_root()
        assert not legacy.exists()

        switcher = ClaudeAccountSwitcher()
        with patch("builtins.input", return_value="n"):
            switcher.purge()

        out = capsys.readouterr().out
        assert "Legacy backup directory" not in out


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
        with patch.object(switcher, "_write_account_credentials") as mock_creds, \
             patch.object(switcher, "_write_account_config"):
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


class TestPurge:
    """Tests for purge cleanup."""

    def test_purge_removes_legacy_none_keychain_entry(self, temp_home):
        """Purge should clean account-None-* entries from older buggy runs — from
        the new security service and best-effort from the legacy keyring."""
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "user@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })

        mock_keyring = MagicMock()
        with patch("builtins.input", return_value="y"), \
             patch("claude_swap.switcher.macos_keychain") as mock_kc, \
             patch.dict(sys.modules, {"keyring": mock_keyring}):
            switcher.purge()

        # New security service: account + legacy account-None both cleaned.
        mock_kc.delete_password.assert_has_calls([
            call("claude-swap", "account-1-user@example.com"),
            call("claude-swap", "account-None-user@example.com"),
        ])
        # Best-effort legacy keyring cleanup of the old claude-code service.
        mock_keyring.delete_password.assert_has_calls([
            call("claude-code", "account-1-user@example.com"),
            call("claude-code", "account-None-user@example.com"),
        ])


# ---------------------------------------------------------------------------
# Issue #41: tolerate broken slots in switch/switch_to
# ---------------------------------------------------------------------------


class TestSwitchSkipsBrokenSlots:
    """Issue #41: --switch must skip slots whose stored creds or config are
    missing rather than aborting. --switch-to N must keep failing but with an
    actionable, accurate message."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(
        self,
        s: ClaudeAccountSwitcher,
        num: int,
        email: str,
        creds: bool = True,
        config: bool = True,
    ) -> None:
        if creds:
            s._write_account_credentials(
                str(num),
                email,
                json.dumps({
                    "claudeAiOauth": {
                        "accessToken": f"sk-{num}",
                        "refreshToken": f"rt-{num}",
                    },
                }),
            )
        if config:
            s._write_account_config(
                str(num),
                email,
                json.dumps({
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": f"uuid-{num}",
                    },
                }),
            )

        data = s._get_sequence_data() or {
            "activeAccountNumber": None,
            "lastUpdated": "",
            "sequence": [],
            "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def test_account_is_switchable_helper(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        self._seed(s, 3, "c@example.com", config=False)

        assert s._account_is_switchable("1") is True
        assert s._account_is_switchable("2") is False
        assert s._account_is_switchable("3") is False
        # Stale sequence reference to a missing account record.
        assert s._account_is_switchable("99") is False

    def test_rotation_skips_broken_next_slot(self, temp_home: Path, capsys):
        """Three accounts, active=1, slot 2 broken — rotation must land on 3."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        self._seed(s, 3, "c@example.com")

        # Active account 1 is the live identity.
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with patch.object(s, "list_accounts"):
            s.switch()

        out = capsys.readouterr().out
        assert "Skipping Account-2" in out

        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == 3

    def test_rotation_no_valid_targets_returns_without_error(
        self, temp_home: Path, capsys
    ):
        """All non-active slots are broken — print a message, no exception."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        s.switch()  # must not raise

        out = capsys.readouterr().out
        assert "Skipping Account-2" in out
        assert "No other accounts have valid" in out

        # Active account unchanged.
        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_switch_to_missing_credentials_actionable_error(self, temp_home: Path):
        """switch_to a broken target raises with the new credentials message."""
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with pytest.raises(SwitchError, match="has no stored credentials"):
            s.switch_to("2")

    def test_switch_to_missing_config_actionable_error(self, temp_home: Path):
        """switch_to a target with creds but no config raises a distinct error."""
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", config=False)

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with pytest.raises(SwitchError, match="has no stored config backup"):
            s.switch_to("2")

    def test_switch_to_refreshes_expired_target_before_activation(self, temp_home: Path):
        """Expired inactive backup credentials are refreshed before becoming live."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
                "expiresAt": 9_999_999_999_000,
            },
        })
        expired_target = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-expired-2",
                "refreshToken": "rt-expired-2",
                "expiresAt": 1,
            },
        })
        refreshed_target = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-refreshed-2",
                "refreshToken": "rt-refreshed-2",
                "expiresAt": 9_999_999_999_000,
            },
        })
        s._write_account_credentials("2", "b@example.com", expired_target)
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with patch(
            "claude_swap.oauth.refresh_oauth_credentials",
            return_value=refreshed_target,
        ), patch.object(s, "list_accounts"):
            s.switch_to("2")

        live_after = json.loads((temp_home / ".claude" / ".credentials.json").read_text())
        backup_after = json.loads(s._read_account_credentials("2", "b@example.com"))
        assert live_after["claudeAiOauth"]["accessToken"] == "sk-refreshed-2"
        assert backup_after["claudeAiOauth"]["refreshToken"] == "rt-refreshed-2"

    def test_switch_to_expired_target_refresh_failure_is_actionable(self, temp_home: Path):
        """Do not activate an expired backup when its refresh token is already invalid."""
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
                "expiresAt": 9_999_999_999_000,
            },
        })
        expired_target = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-expired-2",
                "refreshToken": "rt-expired-2",
                "expiresAt": 1,
            },
        })
        s._write_account_credentials("2", "b@example.com", expired_target)
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with patch(
            "claude_swap.oauth.refresh_oauth_credentials",
            return_value=None,
        ), pytest.raises(SwitchError, match="stored OAuth token is expired"):
            s.switch_to("2")

        live_after = json.loads((temp_home / ".claude" / ".credentials.json").read_text())
        assert live_after["claudeAiOauth"]["accessToken"] == "sk-live-1"

    def test_fresh_machine_skips_broken_preferred_target(self, temp_home: Path, capsys):
        """No live session — picks first switchable slot if the recorded
        activeAccountNumber is broken (e.g., right after import)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com", creds=False)
        self._seed(s, 2, "b@example.com")
        # Mark account 1 as the recorded active (broken) — simulates a stale
        # state after import + later corruption.
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        # No live config — fresh-machine branch.
        with patch.object(s, "list_accounts"):
            s.switch()

        out = capsys.readouterr().out
        assert "Skipping Account-1" in out

        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == 2

    def test_fresh_machine_all_broken_raises(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com", creds=False)
        self._seed(s, 2, "b@example.com", config=False)

        with pytest.raises(ConfigError, match="No managed accounts have valid"):
            s.switch()


class TestSwitchQuietGuardsRaise:
    """Background auto-switch must raise ``SwitchError`` for "nothing to switch
    to" cases, not silently return — otherwise the monitor logs a false
    "switched account" on every threshold crossing.

    Interactive callers (``ManualSwitchIntent``) still see friendly print + return.
    """

    def _bootstrap(self, temp_home: Path, num_accounts: int) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        accounts: dict = {}
        sequence: list[int] = []
        for i in range(1, num_accounts + 1):
            accounts[str(i)] = {"email": f"a{i}@example.com"}
            sequence.append(i)
        data = {
            "accounts": accounts,
            "sequence": sequence,
            "activeAccountNumber": 1 if sequence else None,
        }
        s._write_json(s.sequence_file, data)
        # Pretend there's a current login for account 1 so we get past
        # the fresh-machine path and into the len(sequence)<2 guard.
        (temp_home / ".claude").mkdir(parents=True, exist_ok=True)
        (temp_home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {
                "emailAddress": "a1@example.com",
                "accountUuid": "uuid-1",
            }})
        )
        return s

    def test_quiet_single_account_raises(self, temp_home: Path):
        from claude_swap.exceptions import SwitchError
        from claude_swap.models import BackgroundAutoSwitchIntent

        s = self._bootstrap(temp_home, num_accounts=1)
        decision = s.build_auto_switch_decision(95, 99.0)
        with pytest.raises(SwitchError, match="Only one account"):
            s.switch(BackgroundAutoSwitchIntent(decision=decision))

    def test_interactive_single_account_still_silent(self, temp_home: Path, capsys):
        s = self._bootstrap(temp_home, num_accounts=1)
        s.switch()
        out = capsys.readouterr().out
        assert "Only one account" in out

    def test_quiet_automated_no_trusted_signal_raises_without_stdout_leak(
        self, temp_home: Path, capsys,
    ):
        """Unattended path must fail closed without polluting launchd stdout."""
        from claude_swap.exceptions import SwitchError
        from claude_swap.models import BackgroundAutoSwitchIntent

        s = self._bootstrap(temp_home, num_accounts=3)
        decision = s.build_auto_switch_decision(95, 99.0)
        with pytest.raises(SwitchError, match="Cannot choose auto-switch target"):
            s.switch(BackgroundAutoSwitchIntent(decision=decision))

        out = capsys.readouterr().out
        assert "Skipping" not in out


class TestSchemaDriftWarning:
    """When the usage API returns a dict that lacks the expected rate-limit
    windows, log a structured WARNING — distinguishes schema-break from
    transient network failure (general-purpose review HIGH).
    """

    def test_logs_warning_when_no_window_keys(self, temp_home: Path, caplog):
        import logging

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        (temp_home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {
                "emailAddress": "u@example.com",
                "accountUuid": "uuid-x",
            }})
        )
        caplog.set_level(logging.WARNING, logger="claude-swap")

        # Empty usage dict reaches _max_usage_pct → None, but our drift
        # detector should fire a WARNING first.
        with patch.object(s, "_read_credentials", return_value='{"claudeAiOauth":{"accessToken":"sk-abc"}}'), \
             patch("claude_swap.oauth.extract_access_token", return_value="sk-abc"), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value={"new_unexpected_key": 42}):
            result = s.get_active_usage_pct()

        assert result is None
        warnings = [
            r.getMessage() for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any(
            "no recognized rate-limit windows" in m and "new_unexpected_key" in m
            for m in warnings
        ), warnings


class TestSlotSwitchScore:
    """Cooldown-aware target picker — pure scoring function.

    Lock the score's total order: unsaturated (bucket 0) < saturated with
    known reset (bucket 1) < saturated unknown reset (bucket 1, inf) <
    unknown usage (bucket 2).  Within bucket 0, lower pct wins.  Within
    bucket 1 with known resets, sooner timestamp wins.
    """

    def test_unknown_usage_is_worst(self):
        from claude_swap.switcher import _slot_switch_score

        # Non-dict / empty / no recognised keys all collapse to bucket 2.
        for value in (None, {}, "not a dict", {"unexpected_field": 42}):
            bucket, _ = _slot_switch_score(value, 95)
            assert bucket == 2, f"{value!r} should be bucket 2, got {bucket}"

    def test_unsaturated_prefers_lower_pct(self):
        from claude_swap.switcher import _slot_switch_score

        low = _slot_switch_score({"five_hour": {"pct": 30}}, 95)
        mid = _slot_switch_score({"five_hour": {"pct": 60}}, 95)
        high = _slot_switch_score({"five_hour": {"pct": 80}}, 95)
        assert low < mid < high

    def test_unsaturated_takes_max_of_5h_and_7d(self):
        from claude_swap.switcher import _slot_switch_score

        # The blocking limit is the higher of the two; score must reflect it.
        out = _slot_switch_score(
            {"five_hour": {"pct": 20}, "seven_day": {"pct": 70}}, 95,
        )
        assert out == (0, 70.0)

    def test_saturated_with_resets_prefers_soonest(self):
        from claude_swap.switcher import _slot_switch_score

        soon = _slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}}, 95,
        )
        later = _slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}}, 95,
        )
        assert soon < later
        assert soon[0] == 1 and later[0] == 1  # both saturated bucket

    def test_saturated_without_resets_ranks_last_in_bucket(self):
        from claude_swap.switcher import _slot_switch_score
        import math

        with_reset = _slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "2099-12-31T00:00:00+00:00"}}, 95,
        )
        no_reset = _slot_switch_score({"five_hour": {"pct": 100}}, 95)
        assert with_reset < no_reset
        assert no_reset == (1, math.inf)

    def test_total_order_across_all_buckets(self):
        """The whole point: tuple-sort gives the right global ranking."""
        from claude_swap.switcher import _slot_switch_score

        candidates = [
            ("A-unsat-low", {"five_hour": {"pct": 30}}),
            ("B-unsat-mid", {"five_hour": {"pct": 80}}),
            ("C-sat-soon", {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}}),
            ("D-sat-late", {"five_hour": {"pct": 100, "resets_at": "2026-06-14T18:00:00+00:00"}}),
            ("E-sat-no-reset", {"five_hour": {"pct": 100}}),
            ("F-unknown", {}),
        ]
        scored = sorted(
            (_slot_switch_score(u, 95), name) for name, u in candidates
        )
        names_in_order = [name for _, name in scored]
        # Loose contract: unsat comes before sat; sat-with-reset before
        # sat-without; unknown is last.  Exact pct ordering matters within bucket.
        assert names_in_order[:2] == ["A-unsat-low", "B-unsat-mid"]
        assert names_in_order[2:4] == ["C-sat-soon", "D-sat-late"]
        assert names_in_order[-2] == "E-sat-no-reset"
        assert names_in_order[-1] == "F-unknown"

    def test_invalid_resets_at_falls_to_no_reset_bucket(self):
        """Malformed timestamps must not raise — they degrade the slot to
        'saturated without reset' so the picker still ranks it sensibly."""
        from claude_swap.switcher import _slot_switch_score
        import math

        out = _slot_switch_score(
            {"five_hour": {"pct": 100, "resets_at": "not-a-timestamp"}}, 95,
        )
        assert out == (1, math.inf)


class TestPickBestSwitchTarget:
    """Cache-first picker — integration with the on-disk usage cache."""

    def _bootstrap(self, temp_home: Path, num_accounts: int = 3) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        accounts: dict = {}
        for i in range(1, num_accounts + 1):
            accounts[str(i)] = {"email": f"a{i}@example.com"}
        data = {
            "accounts": accounts,
            "sequence": list(range(1, num_accounts + 1)),
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)
        return s

    def _seed_cache(self, switcher: ClaudeAccountSwitcher, payload: dict):
        from claude_swap.cache import write_cache
        from claude_swap.switcher import _usage_to_cache

        cache_path = switcher.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_cache(
            cache_path,
            {k: _usage_to_cache(v) for k, v in payload.items()},
        )

    def test_cold_cache_returns_none(self, temp_home: Path):
        """No usage cache → return None so caller falls back to round-robin.
        This is the load-bearing 'first run' contract."""
        s = self._bootstrap(temp_home)
        # All switchable but no cache → all bucket-2 → return None
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._pick_best_switch_target(95) is None

    def test_picks_unsaturated_over_saturated(self, temp_home: Path):
        """When at least one slot is unsaturated, it wins regardless of
        how soon a saturated slot would free up."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {
            "1": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
            "2": {"five_hour": {"pct": 30}},  # the winner
            "3": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T13:01:00+00:00"}},
        })
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._pick_best_switch_target(95, exclude="1") == "2"

    def test_picks_soonest_reset_when_all_saturated(self, temp_home: Path):
        """The headline use case: all accounts at limit, pick the one that
        will free up first.  This is what the user explicitly asked for."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {
            "1": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}},
            "2": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T13:30:00+00:00"}},  # soonest
            "3": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
        })
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._pick_best_switch_target(95) == "2"
            assert s._pick_best_switch_target(95, exclude="1") == "2"

    def test_global_pick_orders_saturated_by_cooldown(self, temp_home: Path):
        """From any non-optimal saturated slot, the picker targets the global
        soonest ``resets_at`` (Account-2), not round-robin sequence order."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {
            "1": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}},
            "2": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T13:30:00+00:00"}},
            "3": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
        })
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._pick_best_switch_target(95) == "2"
            # Active on soonest → global optimum is self.
            assert s._pick_best_switch_target(95) == "2"

    def test_switch_stays_when_already_on_soonest_saturated(self, temp_home: Path):
        """Automated path must not rotate away from the soonest-to-free slot."""
        from claude_swap.models import BackgroundAutoSwitchIntent

        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {
            "1": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}},
            "2": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T13:30:00+00:00"}},
            "3": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T14:00:00+00:00"}},
        })
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 2
        s._write_json(s.sequence_file, data)
        with patch.object(s, "_account_is_switchable", return_value=True), \
             patch.object(s, "_get_current_account", return_value=("a2@example.com", "uuid-2")), \
             patch.object(s, "_account_exists", return_value=True), \
             patch.object(s, "_perform_switch") as mock_perform:
            decision = s.build_auto_switch_decision(95, 100.0)
            switched = s.switch(BackgroundAutoSwitchIntent(decision=decision))
        assert switched is False
        mock_perform.assert_not_called()

    def test_automated_plan_rejects_stale_cache(self, temp_home: Path):
        """Expired cache entries must not drive unattended target planning."""
        import json
        import time

        s = self._bootstrap(temp_home, num_accounts=2)
        cache_path = s.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({
                "timestamp": time.time(),
                "data": {
                    "1": {
                        "five_hour": {"pct": 30},
                        "_cached_at": time.time() - 9999,
                    },
                    "2": {
                        "five_hour": {"pct": 40},
                        "_cached_at": time.time() - 9999,
                    },
                },
            }),
            encoding="utf-8",
        )
        decision = s.build_auto_switch_decision(95, 99.0)
        plan = s._plan_automated_switch(decision)
        assert plan.outcome == "no_trusted_signal"

    def test_automated_plan_uses_live_active_slot(self, temp_home: Path):
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {
            "1": {"five_hour": {"pct": 100, "resets_at": "2026-06-14T16:00:00+00:00"}},
            "2": {"five_hour": {"pct": 30}},
            "3": {"five_hour": {"pct": 80}},
        })
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        with patch.object(s, "_account_is_switchable", return_value=True), \
             patch.object(s, "_get_current_account", return_value=("a3@example.com", "")):
            decision = s.build_auto_switch_decision(95, 96.0)
            assert decision.live_active_slot == "3"
            plan = s._plan_automated_switch(decision)
        assert plan.outcome == "chosen"
        assert plan.target == "2"

    def test_plan_stays_put_when_both_accounts_saturated_similar_resets(
        self, temp_home: Path,
    ):
        """When BOTH accounts are saturated and the target resets at most
        _SATURATED_SWITCH_MARGIN_S=300s sooner, the plan must return
        already_optimal to prevent indefinite oscillation.

        Root cause: continued use on the active account advances its resets_at
        forward on every poll, making the idle account always appear marginally
        'better'.  Without the margin guard the monitor switches back and forth
        every 60s triggering the multi-session race warning on every swap."""
        s = self._bootstrap(temp_home, num_accounts=2)
        # Both saturated; Account-2 resets only 60s sooner — within 300s margin.
        self._seed_cache(s, {
            "1": {
                "five_hour": {
                    "pct": 100,
                    "resets_at": "2026-06-14T16:01:00+00:00",
                }
            },
            "2": {
                "five_hour": {
                    "pct": 100,
                    "resets_at": "2026-06-14T16:00:00+00:00",  # 60s sooner
                }
            },
        })
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        with patch.object(s, "_account_is_switchable", return_value=True), \
             patch.object(
                 s, "_get_current_account", return_value=("a1@example.com", ""),
             ):
            decision = s.build_auto_switch_decision(95, 100.0)
            plan = s._plan_automated_switch(decision)
        assert plan.outcome == "already_optimal", (
            "must not oscillate when target resets < 300s sooner"
        )

    def test_plan_switches_when_target_resets_meaningfully_sooner(
        self, temp_home: Path,
    ):
        """When the best target is saturated but resets >300s sooner than the
        active account, the switch IS worth making — the user will get capacity
        back meaningfully earlier on the target account."""
        s = self._bootstrap(temp_home, num_accounts=2)
        # Account-2 resets 10 minutes (600s) sooner — outside the 300s margin.
        self._seed_cache(s, {
            "1": {
                "five_hour": {
                    "pct": 100,
                    "resets_at": "2026-06-14T16:10:00+00:00",
                }
            },
            "2": {
                "five_hour": {
                    "pct": 100,
                    "resets_at": "2026-06-14T16:00:00+00:00",  # 600s sooner
                }
            },
        })
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        with patch.object(s, "_account_is_switchable", return_value=True), \
             patch.object(
                 s, "_get_current_account", return_value=("a1@example.com", ""),
             ):
            decision = s.build_auto_switch_decision(95, 100.0)
            plan = s._plan_automated_switch(decision)
        assert plan.outcome == "chosen"
        assert plan.target == "2"

    def test_trusted_snapshots_require_all_switchable_slots(self, temp_home: Path):
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {"2": {"five_hour": {"pct": 40}}})
        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._trusted_usage_snapshots() == {}

    def test_excludes_specified_slot(self, temp_home: Path):
        """The active account is excluded by the caller; without exclusion
        a soonest-reset active account would otherwise re-pick itself."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {
            "1": {"five_hour": {"pct": 30}},   # active, best score
            "2": {"five_hour": {"pct": 60}},
            "3": {"five_hour": {"pct": 80}},
        })
        with patch.object(s, "_account_is_switchable", return_value=True):
            # active=1 excluded → best of {2,3} is 2
            assert s._pick_best_switch_target(95, exclude="1") == "2"

    def test_skips_non_switchable_slots(self, temp_home: Path):
        """A slot with great usage but no stored credentials must never
        be returned — we'd raise SwitchError trying to activate it."""
        s = self._bootstrap(temp_home, num_accounts=3)
        self._seed_cache(s, {
            "1": {"five_hour": {"pct": 80}},
            "2": {"five_hour": {"pct": 10}},   # best by score, but unswitchable
            "3": {"five_hour": {"pct": 50}},
        })
        def switchable(num):
            return num != "2"
        with patch.object(s, "_account_is_switchable", side_effect=switchable):
            assert s._pick_best_switch_target(95, exclude="1") == "3"

    def test_cold_cache_partial_falls_back_to_signal(self, temp_home: Path):
        """When some slots have cache data and others don't, we still pick
        the best of what we know — only ALL-cold returns None."""
        s = self._bootstrap(temp_home, num_accounts=3)
        # Only slot 2 has cached usage
        self._seed_cache(s, {"2": {"five_hour": {"pct": 40}}})
        with patch.object(s, "_account_is_switchable", return_value=True):
            # Slot 2 has signal (bucket 0); slots 1 & 3 are bucket 2.
            # exclude=1 (active), so candidates are {2, 3} → 2 wins.
            assert s._pick_best_switch_target(95, exclude="1") == "2"


class TestUsageCacheFreshness:
    def test_usage_cache_fresh_requires_matching_keys_and_stamps(self, temp_home: Path):
        import time
        from claude_swap.switcher import _usage_to_cache

        s = ClaudeAccountSwitcher()
        now = time.time()
        fresh = {
            "1": _usage_to_cache({"five_hour": {"pct": 10}}),
            "2": _usage_to_cache({"five_hour": {"pct": 20}}),
        }
        assert s._usage_cache_fresh(fresh, {"1", "2"}) is True

        stale = dict(fresh)
        stale["1"] = {**fresh["1"], "_cached_at": now - 9999}
        assert s._usage_cache_fresh(stale, {"1", "2"}) is False
        assert s._usage_cache_fresh(fresh, {"1"}) is False

    def test_legacy_entry_without_cached_at_inherits_file_timestamp(
        self, temp_home: Path,
    ):
        import json
        import time

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        data = {
            "accounts": {
                "1": {"email": "a1@example.com"},
                "2": {"email": "a2@example.com"},
            },
            "sequence": [1, 2],
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)
        cache_path = s.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({
                "timestamp": time.time(),
                "data": {
                    "1": {"five_hour": {"pct": 30}},
                    "2": {"five_hour": {"pct": 40}},
                },
            }),
            encoding="utf-8",
        )

        with patch.object(s, "_account_is_switchable", return_value=True):
            snapshots = s._trusted_usage_snapshots()

        assert snapshots == {
            "1": {"five_hour": {"pct": 30}},
            "2": {"five_hour": {"pct": 40}},
        }

    def test_stale_slot_untrusted_despite_fresh_file_timestamp(
        self, temp_home: Path,
    ):
        import json
        import time

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        data = {
            "accounts": {
                "1": {"email": "a1@example.com"},
                "2": {"email": "a2@example.com"},
            },
            "sequence": [1, 2],
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)
        cache_path = s.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({
                "timestamp": time.time(),
                "data": {
                    "1": {
                        "five_hour": {"pct": 99},
                        "_cached_at": time.time() - 9999,
                    },
                    "2": {
                        "five_hour": {"pct": 40},
                        "_cached_at": time.time(),
                    },
                },
            }),
            encoding="utf-8",
        )

        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._trusted_usage_snapshots() == {}

    def test_get_active_usage_pct_honors_per_slot_freshness(
        self, temp_home: Path,
    ):
        import json
        import time

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a1@example.com",
                "accountUuid": "uuid-1",
            },
        }))
        data = {
            "accounts": {
                "1": {"email": "a1@example.com", "organizationUuid": "uuid-1"},
            },
            "sequence": [1],
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        cache_path = s.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "timestamp": time.time(),
            "data": {
                "1": {"five_hour": {"pct": 50}, "_cached_at": time.time() - 9999},
            },
        }))
        live_usage = {"five_hour": {"pct": 96}, "seven_day": {"pct": 20}}

        with patch.object(s, "_read_credentials", return_value=creds), \
             patch("claude_swap.oauth.extract_access_token", return_value="tok"), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=live_usage,
             ) as mock_fetch:
            assert s.get_active_usage_pct() == 96.0

        mock_fetch.assert_called_once()

    def test_get_active_usage_breakdown_returns_per_window(
        self, temp_home: Path,
    ):
        """Breakdown exposes each window separately (plan 019) so the monitor
        can track 5h velocity independently of a flat 7d, and stays a strict
        superset of get_active_usage_pct (max of the same values)."""
        import json

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a1@example.com",
                "accountUuid": "uuid-1",
            },
        }))
        data = {
            "accounts": {
                "1": {"email": "a1@example.com", "organizationUuid": "uuid-1"},
            },
            "sequence": [1],
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        live_usage = {"five_hour": {"pct": 72}, "seven_day": {"pct": 87}}

        with patch.object(s, "_read_credentials", return_value=creds), \
             patch("claude_swap.oauth.extract_access_token", return_value="tok"), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=live_usage,
             ):
            breakdown = s.get_active_usage_breakdown()

        assert breakdown == {"five_hour": 72.0, "seven_day": 87.0}
        assert max(breakdown.values()) == 87.0  # equals get_active_usage_pct

    def test_get_active_usage_breakdown_none_when_unavailable(
        self, temp_home: Path,
    ):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        with patch.object(s, "_get_current_account", return_value=None):
            assert s.get_active_usage_breakdown() is None

    def test_fetch_failure_does_not_restamp_stale_entry(self, temp_home: Path):
        import time
        from claude_swap import oauth
        from claude_swap.switcher import _persist_usage_cache_entry

        old_ts = time.time() - 9999
        previous = {"five_hour": {"pct": 25}, "_cached_at": old_ts}
        existing: dict = {"1": dict(previous)}

        _persist_usage_cache_entry(existing, "1", None, previous)
        assert existing["1"]["_cached_at"] == old_ts

        _persist_usage_cache_entry(
            existing,
            "1",
            oauth.UsageFetchError(reason="rate_limited", status_code=429),
            previous,
        )
        assert existing["1"]["_cached_at"] == old_ts

    def test_refresh_triggers_when_snapshots_incomplete(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        data = {
            "accounts": {
                "1": {"email": "a1@example.com"},
                "2": {"email": "a2@example.com"},
            },
            "sequence": [1, 2],
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)

        with patch.object(s, "_account_is_switchable", return_value=True), \
             patch.object(s, "_trusted_usage_snapshots", side_effect=[{}, {"1": {}, "2": {}}]), \
             patch.object(s, "_refresh_switchable_usage_cache") as mock_refresh:
            s.build_auto_switch_decision(95, 99.0)

        mock_refresh.assert_called_once()

    def test_failed_refresh_leaves_expired_snapshots_untrusted(self, temp_home: Path):
        import json
        import time
        from claude_swap import oauth

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        data = {
            "accounts": {
                "1": {"email": "a1@example.com"},
                "2": {"email": "a2@example.com"},
            },
            "sequence": [1, 2],
            "activeAccountNumber": 1,
        }
        s._write_json(s.sequence_file, data)
        cache_path = s.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({
                "timestamp": time.time(),
                "data": {
                    "1": {"five_hour": {"pct": 30}, "_cached_at": time.time() - 9999},
                    "2": {"five_hour": {"pct": 40}, "_cached_at": time.time() - 9999},
                },
            }),
            encoding="utf-8",
        )

        with patch.object(s, "_account_is_switchable", return_value=True), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=None,
             ):
            s._refresh_switchable_usage_cache()

        assert s._trusted_usage_snapshots() == {}


class TestRefreshTargetBeforeActivation:
    """Lock both branches of ``_refresh_target_credentials_before_activation``:
    when a stored OAuth token is expired and the network refresh fails, the
    method must raise SwitchError if no live session is detected, but must
    tolerate the failure (return the unrefreshed credentials unchanged) when
    a live session-mode instance is still using the token."""

    def _expired_creds(self) -> str:
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-expired",
                "refreshToken": "rt-expired",
                "expiresAt": 1,
            },
        })

    def test_raises_when_no_live_session_and_refresh_fails(self, temp_home: Path):
        from claude_swap.exceptions import SwitchError

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        with patch("claude_swap.oauth.refresh_oauth_credentials", return_value=None), \
             patch.object(ClaudeAccountSwitcher, "_live_session_pids", return_value=[]):
            with pytest.raises(SwitchError, match="stored OAuth token is expired"):
                s._refresh_target_credentials_before_activation(
                    "2", "b@example.com", self._expired_creds()
                )

    def test_returns_unchanged_when_live_session_present(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        creds = self._expired_creds()
        with patch("claude_swap.oauth.refresh_oauth_credentials", return_value=None), \
             patch.object(ClaudeAccountSwitcher, "_live_session_pids", return_value=[1234]):
            result = s._refresh_target_credentials_before_activation(
                "2", "b@example.com", creds
            )
        assert result == creds

    def _fresh_creds(self) -> str:
        """Token with a long-into-the-future expiry — not expired."""
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-fresh",
                "refreshToken": "rt-fresh",
                # Year 2099 in epoch ms — guaranteed not expired.
                "expiresAt": 4_070_908_800_000,
            },
        })

    def test_force_refresh_on_fresh_token_triggers_refresh(self, temp_home: Path):
        """force=True refreshes even when the token has not expired yet.

        This is the production-grade seamless-handoff path used by the
        background auto-switch monitor: after activation, Claude Code's first
        API call against the new account must use a freshly-issued token, not
        a backup token that could be minutes from expiry.
        """
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        fresh_creds = self._fresh_creds()
        refreshed_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-refreshed",
                "refreshToken": "rt-refreshed",
                "expiresAt": 4_070_908_800_000,
            },
        })
        with patch(
            "claude_swap.oauth.refresh_oauth_credentials",
            return_value=refreshed_creds,
        ) as mock_refresh, patch.object(
            ClaudeAccountSwitcher, "_write_account_credentials"
        ) as mock_write:
            result = s._refresh_target_credentials_before_activation(
                "2", "b@example.com", fresh_creds, force=True,
            )
        mock_refresh.assert_called_once_with(fresh_creds)
        mock_write.assert_called_once()
        assert result == refreshed_creds

    def test_force_refresh_failure_on_fresh_token_falls_back(self, temp_home: Path):
        """When force=True and refresh fails but the existing token is still
        valid, return the existing creds — degrading gracefully rather than
        blocking the switch on a transient network error."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        fresh = self._fresh_creds()
        with patch("claude_swap.oauth.refresh_oauth_credentials", return_value=None), \
             patch.object(ClaudeAccountSwitcher, "_live_session_pids", return_value=[]):
            result = s._refresh_target_credentials_before_activation(
                "2", "b@example.com", fresh, force=True,
            )
        assert result == fresh

    def test_no_force_skips_refresh_when_not_expired(self, temp_home: Path):
        """The default (interactive) path saves a network call when the token
        is still good — preserves the historic fast-path behaviour."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        fresh = self._fresh_creds()
        with patch(
            "claude_swap.oauth.refresh_oauth_credentials", return_value=None,
        ) as mock_refresh:
            result = s._refresh_target_credentials_before_activation(
                "2", "b@example.com", fresh,
            )
        mock_refresh.assert_not_called()
        assert result == fresh


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
        import logging

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
