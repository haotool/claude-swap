"""Tests for the ClaudeAccountSwitcher class."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from claude_swap.exceptions import (
    ConfigError,
)
from claude_swap.models import Platform

from claude_swap.credentials import ActiveCredentials
from claude_swap.sequence_store import AccountRecord
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.conftest import usage_payload as _usage_payload


class TestEmailValidation:
    """Test email validation."""

    @pytest.mark.parametrize(
        "email",
        [
            "user@example.com",
            "user.name@example.co.uk",
            "user+tag@example.org",
            "user123@test.io",
        ],
    )
    def test_valid_emails(self, temp_home: Path, email: str):
        """Test that valid emails pass validation."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._validate_email(email), f"Expected {email} to be valid"

    @pytest.mark.parametrize(
        "email",
        [
            "not-an-email",
            "@example.com",
            "user@",
            "user@.com",
            "",
            "user@com",
        ],
    )
    def test_invalid_emails(self, temp_home: Path, email: str):
        """Test that invalid emails fail validation."""
        switcher = ClaudeAccountSwitcher()
        assert not switcher._validate_email(email), f"Expected {email} to be invalid"


class TestFindAccountSlot:
    """Test the (email, organizationUuid) -> slot composite-key lookup."""

    DATA = {
        "accounts": {
            "1": {"email": "user@example.com", "organizationUuid": ""},
            "2": {"email": "user@example.com", "organizationUuid": "org-123"},
            "3": {"email": "other@example.com"},  # legacy record, no org field
        }
    }

    @pytest.mark.parametrize(
        ("email", "org_uuid", "expected"),
        [
            pytest.param("user@example.com", "org-123", "2", id="composite-identity"),
            pytest.param("user@example.com", "org-999", None, id="same-email-wrong-org"),
            pytest.param("nobody@example.com", "", None, id="absent-email"),
            pytest.param("user@example.com", "", "1", id="empty-org-matches-empty-field"),
            pytest.param("other@example.com", "", "3", id="empty-org-matches-missing-field"),
        ],
    )
    def test_composite_key_lookup(
        self, email: str, org_uuid: str, expected: str | None
    ):
        assert (
            ClaudeAccountSwitcher._find_account_slot(self.DATA, email, org_uuid)
            == expected
        )

    def test_empty_data_is_no_match(self):
        assert ClaudeAccountSwitcher._find_account_slot({}, "user@example.com", "") is None


class TestPlatformDetection:
    """Test platform detection."""

    @pytest.mark.parametrize(
        ("sys_platform", "wsl_distro", "expected"),
        [
            pytest.param("darwin", None, Platform.MACOS, id="macos"),
            pytest.param("linux", None, Platform.LINUX, id="linux"),
            pytest.param("linux", "Ubuntu", Platform.WSL, id="wsl"),
            pytest.param("win32", None, Platform.WINDOWS, id="windows"),
            pytest.param("freebsd13", None, Platform.UNKNOWN, id="unknown"),
        ],
    )
    def test_detects_platform(
        self,
        temp_home: Path,
        sys_platform: str,
        wsl_distro: str | None,
        expected: Platform,
    ):
        env = {k: v for k, v in os.environ.items() if k != "WSL_DISTRO_NAME"}
        if wsl_distro is not None:
            env["WSL_DISTRO_NAME"] = wsl_distro
        with (
            patch("sys.platform", sys_platform),
            patch.dict(os.environ, env, clear=True),
        ):
            assert Platform.detect() == expected


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


class TestMutationLocking:
    """add_account/remove_account must hold the cross-process FileLock around
    their sequence.json writes, matching _perform_switch, so a concurrent
    auto-switch can't silently lose the update."""

    @staticmethod
    def _spy_filelock(monkeypatch):
        from claude_swap.locking import FileLock as RealFileLock

        calls = {"acquired": 0}

        class SpyLock(RealFileLock):
            def __enter__(self):
                calls["acquired"] += 1
                return super().__enter__()

        monkeypatch.setattr("claude_swap.switcher.FileLock", SpyLock)
        return calls

    def test_add_account_new_slot_holds_lock(
        self, temp_home: Path, mock_claude_config: Path, monkeypatch
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        calls = self._spy_filelock(monkeypatch)

        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        stored: dict = {}
        with (
            patch.object(switcher, "_read_credentials", return_value=creds),
            patch.object(
                switcher,
                "_write_account_credentials",
                side_effect=lambda n, e, c: stored.update(creds=c),
            ),
            patch.object(
                switcher,
                "_read_account_credentials",
                side_effect=lambda n, e: stored.get("creds", ""),
            ),
        ):
            switcher.add_account()

        assert calls["acquired"] >= 1

    def test_add_account_from_token_holds_lock(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        calls = self._spy_filelock(monkeypatch)

        with (
            patch.object(switcher, "_write_account_credentials"),
            patch.object(switcher, "_write_account_config"),
        ):
            switcher.add_account_from_token(
                "sk-ant-api03-abcdefgh",
                email="tok@example.com",
            )

        assert calls["acquired"] >= 1
        assert "1" in switcher._get_sequence_data()["accounts"]

    def test_add_account_from_token_refresh_in_place_holds_lock(
        self, temp_home: Path, monkeypatch
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        switcher._register_account_slot(
            "1",
            AccountRecord.create(
                email="tok@example.com",
                added="2024-01-01T00:00:00Z",
                is_api_key=True,
            ),
            set_active=True,
        )
        calls = self._spy_filelock(monkeypatch)

        with (
            patch.object(switcher, "_write_account_credentials"),
            patch.object(switcher, "_write_account_config"),
        ):
            switcher.add_account_from_token(
                "sk-ant-api03-refresh",
                email="tok@example.com",
            )

        assert calls["acquired"] >= 1

    def test_add_account_refresh_reresolves_slot_under_lock(
        self, temp_home: Path, mock_claude_config: Path
    ):
        # add_account refresh-in-place must re-resolve the slot INSIDE the lock;
        # if the account was removed concurrently it raises cleanly rather than
        # writing a stale slot id.
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        creds = json.dumps(
            {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rt"}}
        )
        with (
            patch.object(
                switcher,
                "_get_current_account",
                return_value=("a@example.com", ""),
            ),
            patch.object(switcher, "_account_exists", return_value=True),
            patch.object(switcher, "_read_credentials", return_value=creds),
            patch.object(
                switcher,
                "_get_sequence_data",
                return_value={"accounts": {}, "sequence": []},
            ),
            pytest.raises(ConfigError, match="no longer managed"),
        ):
            switcher.add_account()

    def test_remove_account_holds_lock(self, temp_home: Path, monkeypatch):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        switcher._register_account_slot(
            "1",
            AccountRecord.create(
                email="a@example.com",
                uuid="u",
                added="2024-01-01T00:00:00Z",
            ),
            set_active=True,
        )
        calls = self._spy_filelock(monkeypatch)

        with (
            patch.object(switcher, "_ensure_no_live_session"),
            patch.object(switcher, "_delete_account_files"),
        ):
            switcher.remove_account("1", assume_yes=True)

        assert calls["acquired"] >= 1
        assert switcher._get_sequence_data()["accounts"] == {}


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
        import time

        from claude_swap.cache import write_cache

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Production cache rows carry a per-row ``_cached_at`` (via
        # usage_cache._usage_to_cache); include it so the row is trusted.
        cached_usage = {
            "1": {
                "five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"},
                "seven_day": {"pct": 60, "clock": "Jan 2 03:00", "countdown": "2d"},
                "_cached_at": time.time(),
            },
        }
        write_cache(switcher.backup_dir / "cache" / "usage.json", cached_usage)

        with (
            patch.object(switcher, "_read_active_credentials",
                         return_value=ActiveCredentials(active_creds, False)),
            patch("claude_swap.oauth.fetch_usage_for_account") as mock_fetch,
        ):
            switcher.status()

        mock_fetch.assert_not_called()
        output = capsys.readouterr().out
        assert "25%" in output
        assert "60%" in output

    def test_status_fetches_on_cache_miss_with_is_active_true(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_active_credentials",
                         return_value=ActiveCredentials(active_creds, False)),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=usage_result
            ) as mock_fetch,
        ):
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

    def test_status_fetches_with_is_active_true_when_cc_running(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """When Claude Code is running, fetch with is_active=True (never refresh live creds)."""
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

        with (
            patch.object(switcher, "_read_active_credentials",
                         return_value=ActiveCredentials(active_creds, False)),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=usage_result
            ) as mock_fetch,
        ):
            switcher.status()

        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.kwargs.get("is_active") is True

        output = capsys.readouterr().out
        assert "10%" in output

        cache_path = switcher.backup_dir / "cache" / "usage.json"
        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == usage_result

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

        with (
            patch.object(switcher, "_read_active_credentials",
                         return_value=ActiveCredentials(active_creds, False)),
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=usage_result
            ),
        ):
            switcher.status()

        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == usage_result
        assert cached["2"] == {"five_hour": {"pct": 80}}

    def test_status_preserves_previous_cached_usage_when_fetch_returns_none(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_active_credentials",
                         return_value=ActiveCredentials(active_creds, False)),
            patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
        ):
            switcher.status()

        output = capsys.readouterr().out
        assert "25%" in output

        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == previous_usage["1"]
        assert _usage_payload(cached["2"]) == previous_usage["2"]

    def test_status_shows_cached_usage_with_rate_limit_note(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_active_credentials",
                         return_value=ActiveCredentials(active_creds, False)),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=oauth.UsageFetchError(
                    reason="rate_limited", status_code=429
                ),
            ),
        ):
            switcher.status()

        output = capsys.readouterr().out
        assert "25%" in output
        assert "cached; live fetch usage unavailable (rate limited)" in output

        cached = read_cache(cache_path, 300)
        assert cached is not MISSING
        assert _usage_payload(cached["1"]) == previous_usage["1"]


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


class TestPurge:
    """Tests for purge cleanup."""

    @staticmethod
    def _macos_switcher_with_one_account(temp_home) -> ClaudeAccountSwitcher:
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
        return switcher

    def test_purge_removes_legacy_none_keychain_entry(self, temp_home):
        """Purge should clean account-None-* entries from older buggy runs — from
        the new security service and best-effort from the legacy keyring."""
        switcher = self._macos_switcher_with_one_account(temp_home)

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

    def test_purge_in_file_fallback_mode_still_clears_macos_keychain(
        self, temp_home
    ):
        """A Keychain flipped to file mode this process must not skip Keychain
        cleanup: items written by earlier keychain-mode runs live outside
        backup_dir, so nothing else removes them (upstream sweeps both)."""
        switcher = self._macos_switcher_with_one_account(temp_home)
        switcher._store._keychain_usable_cache = False

        mock_keyring = MagicMock()
        with patch("builtins.input", return_value="y"), \
             patch("claude_swap.switcher.macos_keychain") as mock_kc, \
             patch.dict(sys.modules, {"keyring": mock_keyring}):
            switcher.purge()

        mock_kc.delete_password.assert_has_calls([
            call("claude-swap", "account-1-user@example.com"),
            call("claude-swap", "account-None-user@example.com"),
        ])
        mock_keyring.delete_password.assert_has_calls([
            call("claude-code", "account-1-user@example.com"),
            call("claude-code", "account-None-user@example.com"),
        ])

    def test_purge_credential_sweep_removes_fallback_enc_in_keychain_mode(
        self, temp_home
    ):
        """The credential sweep itself must unlink fallback .enc files even in
        Keychain mode — reads are .enc-wins, so a leftover fallback file is a
        live credential, not cruft, and the rmtree backstop can fail partway."""
        switcher = self._macos_switcher_with_one_account(temp_home)
        enc = switcher.credentials_dir / ".creds-1-user@example.com.enc"
        enc.write_text("b64-credential-payload")

        removed: list[str] = []
        switcher._purge_remove_account_credentials(
            switcher._get_sequence_data(), removed,
        )

        assert not enc.exists()
        assert f"Credential file: {enc.name}" in removed
