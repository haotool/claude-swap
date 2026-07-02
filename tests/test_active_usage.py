"""Active usage resolution, usage display, and cache freshness."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_swap import oauth
from claude_swap.credentials import ActiveCredentials
from claude_swap.json_output import USAGE_API_KEY, USAGE_KEYCHAIN_UNAVAILABLE
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.conftest import usage_payload as _usage_payload


class TestListAccountsUsage:
    """Test list_accounts shows usage info."""

    def test_list_shows_usage(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.oauth.urllib.request.urlopen", return_value=mock_response
            ),
        ):
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
        old_backup = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": 1,
                }
            }
        )
        refreshed_live = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "new-access",
                    "refreshToken": "new-refresh",
                    "expiresAt": 9_999_999_999_000,
                }
            }
        )

        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        switcher._write_account_credentials("1", "test@example.com", old_backup)

        with (
            patch.object(switcher, "_read_credentials", return_value=refreshed_live),
            patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
        ):
            switcher.list_accounts()

        stored = switcher._read_account_credentials("1", "test@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "new-refresh"

    def test_health_shows_ok_for_accounts_with_usage(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """Health output should align with the list/token formatting."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})
        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"}
        }

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=usage_result
            ),
        ):
            switcher.list_accounts(show_token_status=True, show_health=True)

        output = capsys.readouterr().out
        assert "health: ok" in output
        assert "oauth:" in output

    def test_health_refreshes_expiring_inactive_credentials(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """Health checks should refresh inactive backups before they expire."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        expiring_backup = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": 1,
                }
            }
        )
        refreshed_backup = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "new-access",
                    "refreshToken": "new-refresh",
                    "expiresAt": 9_999_999_999_000,
                }
            }
        )

        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        switcher._write_account_credentials(
            "2", "account2@example.com", expiring_backup
        )

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=refreshed_backup,
            ),
            patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
        ):
            switcher.list_accounts(show_token_status=True, show_health=True)

        output = capsys.readouterr().out
        stored = switcher._read_account_credentials("2", "account2@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "new-refresh"
        assert "health: token refreshed" in output

    def test_list_shows_usage_null_reset(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.oauth.urllib.request.urlopen", return_value=mock_response
            ),
        ):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "5h:   0%" in output
        assert "7d: 100%" in output
        assert "usage unavailable" not in output

    def test_list_no_credentials(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with (
            patch.object(switcher, "_read_credentials", return_value=""),
            patch.object(switcher, "_read_account_credentials", return_value=""),
        ):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "no credentials" in output

    def test_list_never_writes_live_while_claude_code_running(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """While Claude Code owns the active account, list never writes live creds.

        Refreshing the live credential in parallel would race with Claude Code's own
        refresh (which coordinates via a ~/.claude/ lockfile cswap doesn't honor) and
        could trip refresh-token reuse detection. The active row stays hands-off
        (is_active=True) whenever an owner is detected; only inactive backups refresh.
        """
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-backup",
                    "refreshToken": "rt-orig",
                },
            }
        )
        refreshed_creds = json.dumps(
            {
                "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new"},
            }
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        def mock_fetch(
            account_num, email, credentials, is_active, persist_credentials=None
        ):
            if not is_active and persist_credentials is not None:
                persist_credentials(account_num, email, refreshed_creds)
            return None

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch.object(switcher, "_write_credentials") as write_live,
            patch.object(switcher, "_write_account_credentials") as write_backup,
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            switcher.list_accounts()

        write_live.assert_not_called()
        write_backup.assert_called_once_with(
            "2", "account2@example.com", refreshed_creds
        )

    def test_usage_classification_parity_across_paths(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """list-JSON, list-human, and strategy paths classify accounts identically."""
        from claude_swap.json_output import (
            USAGE_API_KEY,
            usage_fields,
            usage_display_line,
        )

        API_KEY = "sk-ant-api03-abcdefghij1234567890XYZ"
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        sample_sequence_data["accounts"]["3"] = {
            "email": "api-key-3@token.local",
            "uuid": "uuid-3",
            "added": "2024-01-01T00:00:00Z",
            "kind": "api_key",
        }
        sample_sequence_data["sequence"].append(3)

        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        usage_ok = {
            "five_hour": {"pct": 10.0, "clock": "Jan 1 03:00", "countdown": "0m"}
        }

        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        switcher._write_account_credentials("3", "api-key-3@token.local", API_KEY)

        def mock_fetch(
            num, email, credentials, is_active=False, persist_credentials=None, **kwargs
        ):
            if str(num) == "1":
                return usage_ok
            if str(num) == "2":
                return None
            return None

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher,
                "_read_account_credentials",
                side_effect=lambda n, e: API_KEY if str(n) == "3" else "",
            ),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            json_payload = switcher.list_accounts(json_output=True)
            capsys.readouterr()
            strategy_usage = switcher._usage_by_account()
            switcher.list_accounts()
            human_out = capsys.readouterr().out

        json_by_num = {a["number"]: a["usageStatus"] for a in json_payload["accounts"]}
        strategy_by_num = {
            int(k): usage_fields(v)[0] for k, v in strategy_usage.items()
        }
        assert json_by_num == strategy_by_num
        assert json_by_num[1] == "ok"
        assert json_by_num[2] == "no_credentials"
        assert json_by_num[3] == "api_key"
        assert usage_display_line(USAGE_API_KEY) in human_out
        assert "no credentials" in human_out
        assert "10%" in human_out

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
        backup_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-backup",
                    "refreshToken": "rt-orig",
                },
            }
        )
        refreshed_creds = json.dumps(
            {
                "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new"},
            }
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        def mock_fetch(
            account_num, email, credentials, is_active, persist_credentials=None
        ):
            # Simulate a refresh on the inactive account only.
            if not is_active and persist_credentials is not None:
                persist_credentials(account_num, email, refreshed_creds)
            return None

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch.object(switcher, "_write_credentials") as write_live,
            patch.object(switcher, "_write_account_credentials") as write_backup,
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            switcher.list_accounts()

        # Live creds must never be written from list_accounts()
        write_live.assert_not_called()
        # Backup was written for the inactive account (2) only.
        write_backup.assert_called_once_with(
            "2", "account2@example.com", refreshed_creds
        )

    def test_list_shows_token_status_when_requested(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
            patch(
                "claude_swap.oauth.build_token_status",
                return_value="oauth: fresh, refresh token yes",
            ),
        ):
            switcher.list_accounts(show_token_status=True)

        output = capsys.readouterr().out
        assert "oauth: fresh, refresh token yes" in output

    def test_list_uses_cached_usage(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """When a fresh usage cache exists, list_accounts skips API calls."""
        from claude_swap.cache import write_cache
        from claude_swap.usage_cache import _usage_to_cache

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Pre-populate cache with usage data for both accounts
        cached_usage = {
            "1": {
                "five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"},
                "seven_day": {"pct": 60, "clock": "Jan 2 03:00", "countdown": "2d"},
            },
            "2": {
                "five_hour": {"pct": 80, "clock": "Jan 1 04:00", "countdown": "30m"},
                "seven_day": {"pct": 90, "clock": "Jan 3 03:00", "countdown": "3d"},
            },
        }
        write_cache(
            switcher.backup_dir / "cache" / "usage.json",
            {k: _usage_to_cache(v) for k, v in cached_usage.items()},
        )

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch("claude_swap.oauth.fetch_usage_for_account") as mock_fetch,
        ):
            switcher.list_accounts()

        # API should NOT have been called — data came from cache
        mock_fetch.assert_not_called()
        output = capsys.readouterr().out
        assert "25%" in output
        assert "80%" in output

    def test_list_ignores_cache_when_accounts_change(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=usage_result
            ),
        ):
            switcher.list_accounts()

        output = capsys.readouterr().out
        # Should show live data (10%), not cached data (25%)
        assert "10%" in output

    def test_list_preserves_previous_cached_usage_when_fetch_returns_none(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                side_effect=lambda num, *args, **kwargs: (
                    None
                    if str(num) == "1"
                    else {
                        "five_hour": {
                            "pct": 10,
                            "clock": "Jan 1 03:00",
                            "countdown": "0m",
                        },
                        "seven_day": {
                            "pct": 50,
                            "clock": "Jan 2 03:00",
                            "countdown": "0m",
                        },
                    }
                ),
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
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
        caplog,
    ):
        """A classified rate-limit failure should be visible without debug logs."""
        from claude_swap import oauth
        from claude_swap.cache import read_cache, MISSING

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        rate_limited = oauth.UsageFetchError(reason="rate_limited", status_code=429)
        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"}
        }

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            with (
                patch.object(switcher, "_read_credentials", return_value=active_creds),
                patch.object(
                    switcher, "_read_account_credentials", return_value=backup_creds
                ),
                patch(
                    "claude_swap.oauth.fetch_usage_for_account",
                    side_effect=lambda num, *args, **kwargs: (
                        rate_limited if str(num) == "1" else usage_result
                    ),
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
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
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

        with (
            patch.object(switcher, "_read_credentials", return_value=active_creds),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                side_effect=lambda num, *args, **kwargs: (
                    oauth.UsageFetchError(reason="rate_limited", status_code=429)
                    if str(num) == "1"
                    else {
                        "five_hour": {
                            "pct": 10,
                            "clock": "Jan 1 03:00",
                            "countdown": "0m",
                        }
                    }
                ),
            ),
        ):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "25%" in output
        assert "10%" in output
        assert "cached; live fetch usage unavailable (rate limited)" in output


class TestActiveAccountRefresh:
    """`_fetch_active_usage`: refresh the active token only when no owner is running."""

    _EXPIRED = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "sk-active",
                "refreshToken": "rt-orig",
                "expiresAt": 1000,
            }
        }
    )
    _REFRESHED = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "sk-new",
                "refreshToken": "rt-new",
                "expiresAt": 9999999999000,
            }
        }
    )

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    def test_no_owner_refreshes_and_writes_both_stores(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            assert is_active is False
            persist_credentials(account_num, email, self._REFRESHED)
            return usage_result

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=False,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials") as write_live,
            patch.object(switcher, "_write_account_credentials") as write_backup,
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )

        assert result == usage_result
        write_live.assert_called_once_with(self._REFRESHED)
        write_backup.assert_called_once_with("1", "test@example.com", self._REFRESHED)

    def test_cc_running_stays_handsoff_and_reports_token_expired(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED

        switcher = self._switcher(sample_sequence_data)

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials") as write_live,
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=None
            ) as mock_fetch,
        ):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )

        assert result == USAGE_TOKEN_EXPIRED
        assert mock_fetch.call_args.kwargs.get("is_active") is True
        write_live.assert_not_called()

    def test_live_session_blocks_refresh(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        switcher = self._switcher(sample_sequence_data)

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=False,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[4242]),
            patch.object(switcher, "_write_credentials") as write_live,
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=None
            ) as mock_fetch,
        ):
            switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert mock_fetch.call_args.kwargs.get("is_active") is True
        write_live.assert_not_called()

    def test_lineage_mismatch_skips_write_and_reports_token_expired(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED

        switcher = self._switcher(sample_sequence_data)
        live_changed = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-x",
                    "refreshToken": "rt-someone-else",
                },
            }
        )
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            persist_credentials(account_num, email, self._REFRESHED)
            return usage_result

        with (
            patch.object(switcher, "_read_credentials", return_value=live_changed),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=False,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials") as write_live,
            patch.object(switcher, "_write_account_credentials") as write_backup,
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )

        assert result == USAGE_TOKEN_EXPIRED
        write_live.assert_not_called()
        write_backup.assert_not_called()

    def test_owner_appears_mid_refresh_backs_up_rotation_keeps_live(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        # No owner at the pre-check, so the refresh runs and consumes the
        # single-use token; the owner then appears before persist. The rotated
        # credential must NOT be discarded (that would leave a dead token) — it
        # is backed up (recoverable on a later switch) while live is untouched.
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED

        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            assert is_active is False  # not owned at pre-check
            persist_credentials(account_num, email, self._REFRESHED)
            return usage_result

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                side_effect=[False, True],
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials") as write_live,
            patch.object(switcher, "_write_account_credentials") as write_backup,
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )

        assert result == USAGE_TOKEN_EXPIRED
        write_live.assert_not_called()  # do not clobber the owner's live store
        write_backup.assert_called_once_with("1", "test@example.com", self._REFRESHED)

    def test_write_failure_reports_token_expired(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED

        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            try:
                persist_credentials(account_num, email, self._REFRESHED)
            except Exception:
                pass
            return usage_result

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=False,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(
                switcher, "_write_credentials", side_effect=OSError("disk full")
            ),
            patch.object(switcher, "_write_account_credentials"),
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )

        assert result == USAGE_TOKEN_EXPIRED

    def test_resolve_active_usage_entry_refreshes_when_cache_missing(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            assert is_active is False
            persist_credentials(account_num, email, self._REFRESHED)
            return usage_result

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=False,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials"),
            patch.object(switcher, "_write_account_credentials"),
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            usage, note = switcher._resolve_active_usage_entry(
                "1",
                "test@example.com",
                creds=self._EXPIRED,
            )

        assert usage == usage_result
        assert note is None

    def test_detection_failure_fails_closed(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """If instance detection raises, assume an owner exists and do not refresh."""
        from claude_swap.list_reporter import ListReporter

        switcher = self._switcher(sample_sequence_data)

        with patch(
            "claude_swap.list_reporter.get_running_instances",
            side_effect=OSError("boom"),
        ):
            assert ListReporter(switcher)._active_cc_running() is True

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.get_running_instances",
                side_effect=OSError("boom"),
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials") as write_live,
            patch(
                "claude_swap.oauth.fetch_usage_for_account", return_value=None
            ) as mock_fetch,
        ):
            switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert mock_fetch.call_args.kwargs.get("is_active") is True
        write_live.assert_not_called()

    def test_refresh_network_call_does_not_hold_the_lock(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The lock must be free during the refresh network call (no a07c767 regression)."""
        from claude_swap.locking import FileLock

        switcher = self._switcher(sample_sequence_data)
        lock_free_during_fetch = {"ok": False}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            probe = FileLock(switcher.lock_file)
            lock_free_during_fetch["ok"] = probe.acquire(timeout=0.5)
            if lock_free_during_fetch["ok"]:
                probe.release()
            persist_credentials(account_num, email, self._REFRESHED)
            return {"five_hour": {"pct": 10}}

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=False,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials"),
            patch.object(switcher, "_write_account_credentials"),
            patch("claude_swap.oauth.fetch_usage_for_account", side_effect=mock_fetch),
        ):
            switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert lock_free_during_fetch["ok"] is True

    def test_no_token_returns_no_credentials(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Missing access token short-circuits before any owner check or fetch."""
        from claude_swap.json_output import USAGE_NO_CREDENTIALS

        switcher = self._switcher(sample_sequence_data)
        with patch("claude_swap.oauth.fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", "")
        assert result == USAGE_NO_CREDENTIALS
        mock_fetch.assert_not_called()

    def test_list_renders_token_expired_line(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """End-to-end: --list shows the intentional message for the active account."""
        switcher = self._switcher(sample_sequence_data)
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
        ):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "token expired — Claude Code refreshes the active account" in output


class TestSchemaDriftWarning:
    """When the usage API returns a dict that lacks the expected rate-limit
    windows, log a structured WARNING — distinguishes schema-break from
    transient network failure (general-purpose review HIGH).
    """

    def test_logs_warning_when_no_window_keys(self, temp_home: Path, caplog):

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "u@example.com",
                        "accountUuid": "uuid-x",
                    }
                }
            )
        )
        s._write_json(
            s.sequence_file,
            {
                "accounts": {"1": {"email": "u@example.com", "organizationUuid": ""}},
                "sequence": [1],
                "activeAccountNumber": 1,
            },
        )
        caplog.set_level(logging.WARNING, logger="claude-swap")

        # Empty usage dict reaches max_usage_pct → None, but our drift
        # detector should fire a WARNING first.
        with (
            patch.object(
                s,
                "_read_credentials",
                return_value='{"claudeAiOauth":{"accessToken":"sk-abc"}}',
            ),
            patch("claude_swap.oauth.extract_access_token", return_value="sk-abc"),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value={"new_unexpected_key": 42},
            ),
        ):
            result = s.get_active_usage_pct()

        assert result is None
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any(
            "no recognized rate-limit windows" in m and "new_unexpected_key" in m
            for m in warnings
        ), warnings


class TestUsageCacheFreshness:
    def test_usage_cache_fresh_requires_matching_keys_and_stamps(self, temp_home: Path):
        from claude_swap.usage_cache import _usage_to_cache

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

    def test_legacy_entry_without_cached_at_is_untrusted(
        self,
        temp_home: Path,
    ):
        # Per-row trust: a legacy row without ``_cached_at`` is treated as
        # untrusted so an unrelated cache write cannot extend its TTL. The
        # planner refreshes rather than acting on possibly-stale data.
        import json

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
            json.dumps(
                {
                    "timestamp": time.time(),
                    "data": {
                        "1": {"five_hour": {"pct": 30}},
                        "2": {"five_hour": {"pct": 40}},
                    },
                }
            ),
            encoding="utf-8",
        )

        with patch.object(s, "_account_is_switchable", return_value=True):
            snapshots = s._trusted_usage_snapshots()

        assert snapshots == {}

    def test_stale_slot_excluded_fresh_slot_retained(
        self,
        temp_home: Path,
    ):
        # A stale per-row slot is excluded; a fresh sibling is still returned
        # (partial trusted subset), so one stale slot can't block planning.
        import json

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
            json.dumps(
                {
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
                }
            ),
            encoding="utf-8",
        )

        with patch.object(s, "_account_is_switchable", return_value=True):
            assert s._trusted_usage_snapshots() == {"2": {"five_hour": {"pct": 40}}}

    def test_get_active_usage_pct_honors_per_slot_freshness(
        self,
        temp_home: Path,
    ):
        import json

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a1@example.com",
                        "accountUuid": "uuid-1",
                    },
                }
            )
        )
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
        cache_path.write_text(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "data": {
                        "1": {
                            "five_hour": {"pct": 50},
                            "_cached_at": time.time() - 9999,
                        },
                    },
                }
            )
        )
        live_usage = {"five_hour": {"pct": 96}, "seven_day": {"pct": 20}}

        with (
            patch.object(s, "_read_credentials", return_value=creds),
            patch("claude_swap.oauth.extract_access_token", return_value="tok"),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=live_usage,
            ) as mock_fetch,
        ):
            assert s.get_active_usage_pct() == 96.0

        mock_fetch.assert_called_once()

    def test_get_active_usage_breakdown_returns_per_window(
        self,
        temp_home: Path,
    ):
        """Breakdown exposes each window separately so the monitor
        can track 5h velocity independently of a flat 7d, and stays a strict
        superset of get_active_usage_pct (max of the same values)."""
        import json

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a1@example.com",
                        "accountUuid": "uuid-1",
                    },
                }
            )
        )
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

        with (
            patch.object(s, "_read_credentials", return_value=creds),
            patch("claude_swap.oauth.extract_access_token", return_value="tok"),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=live_usage,
            ),
        ):
            breakdown = s.get_active_usage_breakdown()

        assert breakdown == {"five_hour": 72.0, "seven_day": 87.0}
        assert max(breakdown.values()) == 87.0  # equals get_active_usage_pct

    def test_get_active_usage_breakdown_none_when_unavailable(
        self,
        temp_home: Path,
    ):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        with patch.object(s, "_get_current_account", return_value=None):
            assert s.get_active_usage_breakdown() is None

    def test_fetch_failure_does_not_restamp_stale_entry(self, temp_home: Path):
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

        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(
                s, "_trusted_usage_snapshots", side_effect=[{}, {"1": {}, "2": {}}]
            ),
            patch.object(s, "_refresh_switchable_usage_cache") as mock_refresh,
        ):
            s.build_auto_switch_decision(95, 99.0)

        mock_refresh.assert_called_once()

    def test_refresh_triggers_when_only_active_snapshot_is_trusted(
        self,
        temp_home: Path,
    ):
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

        active_only = {"1": {"five_hour": {"pct": 96}}}
        refreshed = {
            "1": {"five_hour": {"pct": 96}},
            "2": {"five_hour": {"pct": 10}},
        }

        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(
                s,
                "_trusted_usage_snapshots",
                side_effect=[active_only, refreshed],
            ),
            patch.object(s, "_refresh_switchable_usage_cache") as mock_refresh,
        ):
            decision = s.build_auto_switch_decision(95, 96.0)
            plan = s._plan_automated_switch(decision)

        mock_refresh.assert_called_once()
        assert plan.outcome == "chosen"
        assert plan.target == "2"

    def test_failed_refresh_leaves_expired_snapshots_untrusted(self, temp_home: Path):
        import json

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
            json.dumps(
                {
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
                }
            ),
            encoding="utf-8",
        )

        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=None,
            ),
        ):
            s._refresh_switchable_usage_cache()

        assert s._trusted_usage_snapshots() == {}


class TestListReporterKeychainFlag:
    """The keychain-unavailable flag must survive across facade calls (PR#77).

    ``_usage_by_account`` builds accounts info through one facade call and
    resolves usages through another; both must observe the same reporter
    state, or a locked Keychain shows the active account as "no credentials".
    """

    def test_usage_by_account_reports_keychain_unavailable(self, temp_home: Path):
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
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a1@example.com",
                        "accountUuid": "u1",
                    }
                }
            )
        )

        with patch.object(
            s,
            "_read_active_credentials",
            return_value=ActiveCredentials(None, True),
        ):
            usage = s._usage_by_account()

        assert usage["1"] == USAGE_KEYCHAIN_UNAVAILABLE

    def test_list_reporter_instance_is_reused(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        assert s._list_reporter() is s._list_reporter()


class TestRefreshGateResolvedSlots:
    """Peers that can never yield a trusted row must not refetch every cycle.

    The refresh gate treats a cache row that is a known usage sentinel, or an
    error row within the per-slot TTL, as already answered. Only slots with a
    missing or expired row trigger a network refresh.
    """

    def _switcher_with_peer(self, temp_home: Path) -> ClaudeAccountSwitcher:
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
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a1@example.com",
                        "accountUuid": "u1",
                    }
                }
            )
        )
        return s

    def _seed_usage_cache(self, s: ClaudeAccountSwitcher, rows: dict) -> None:
        cache_path = s.backup_dir / "cache" / "usage.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"timestamp": time.time(), "data": rows}),
            encoding="utf-8",
        )

    def _counting_refresh(self, s: ClaudeAccountSwitcher):
        calls: list[int] = []
        real_refresh = s._refresh_switchable_usage_cache

        def refresh() -> None:
            calls.append(1)
            real_refresh()

        return calls, refresh

    def test_api_key_peer_refetches_once_then_stays_quiet(self, temp_home: Path):
        """An API-key peer resolves to a sentinel and stops the refetch loop."""
        s = self._switcher_with_peer(temp_home)
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        live_usage = {"five_hour": {"pct": 96}, "seven_day": {"pct": 50}}
        calls, refresh = self._counting_refresh(s)

        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(s, "_read_credentials", return_value=active_creds),
            patch.object(
                s, "_read_account_credentials", return_value="sk-ant-api03-peer"
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=live_usage,
            ),
            patch.object(s, "_refresh_switchable_usage_cache", side_effect=refresh),
        ):
            s.build_auto_switch_decision(95, 96.0)
            assert calls == [1]
            # The single refresh persisted the API-key sentinel for the peer.
            cached = s._read_json(s.usage_cache_path) or {}
            assert cached.get("data", {}).get("2") == USAGE_API_KEY
            s.build_auto_switch_decision(95, 96.0)

        assert calls == [1]

    def test_error_row_within_ttl_suppresses_refetch(self, temp_home: Path):
        s = self._switcher_with_peer(temp_home)
        now = time.time()
        self._seed_usage_cache(
            s,
            {
                "1": {"five_hour": {"pct": 96}, "_cached_at": now},
                "2": {
                    "_type": "usage_fetch_error",
                    "reason": "network_error",
                    "status_code": None,
                    "message": "boom",
                    "retry_after": None,
                    "_cached_at": now,
                },
            },
        )

        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(s, "_refresh_switchable_usage_cache") as refresh,
        ):
            s.build_auto_switch_decision(95, 96.0)

        refresh.assert_not_called()

    def test_error_row_past_ttl_refetches_exactly_once(self, temp_home: Path):
        """An expired error row triggers one refetch, whose re-stamped error
        row answers the following cycle."""
        s = self._switcher_with_peer(temp_home)
        now = time.time()
        self._seed_usage_cache(
            s,
            {
                "1": {"five_hour": {"pct": 96}, "_cached_at": now},
                "2": {
                    "_type": "usage_fetch_error",
                    "reason": "network_error",
                    "status_code": None,
                    "message": "boom",
                    "retry_after": None,
                    "_cached_at": now - 9999,
                },
            },
        )
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        peer_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok2"}})
        calls, refresh = self._counting_refresh(s)

        def read_backup(num: str, email: str) -> str:
            return peer_creds

        with (
            patch.object(s, "_account_is_switchable", return_value=True),
            patch.object(s, "_read_credentials", return_value=active_creds),
            patch.object(s, "_read_account_credentials", side_effect=read_backup),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=oauth.UsageFetchError(
                    reason="network_error", message="still down"
                ),
            ),
            patch.object(s, "_refresh_switchable_usage_cache", side_effect=refresh),
        ):
            s.build_auto_switch_decision(95, 96.0)
            assert calls == [1]
            s.build_auto_switch_decision(95, 96.0)

        assert calls == [1]


class TestRefreshInactiveCredentialsIfNeeded:
    """Lock-acquired re-check in ``_refresh_inactive_credentials_if_needed``."""

    def _expired_creds(self) -> str:
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old",
                    "refreshToken": "rt-old",
                    "expiresAt": 0,
                },
            }
        )

    def _fresh_creds(self) -> str:
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "new",
                    "refreshToken": "rt-new",
                    "expiresAt": 4_070_908_800_000,
                },
            }
        )

    def test_refresh_inactive_skips_when_disk_already_fresh(self, temp_home: Path):
        """Lock-acquired re-check skips redundant refresh when disk is fresh."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        stale_creds = self._expired_creds()
        fresh_creds = self._fresh_creds()
        switcher._write_account_credentials("1", "x@y.z", fresh_creds)

        with patch("claude_swap.oauth.refresh_oauth_credentials") as mock_refresh:
            result, note = switcher._refresh_inactive_credentials_if_needed(
                "1",
                "x@y.z",
                stale_creds,
            )

        assert "fresh" in note.lower() or "skip" in note.lower()
        assert result == fresh_creds
        mock_refresh.assert_not_called()
