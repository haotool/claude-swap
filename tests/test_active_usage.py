"""Active usage resolution, usage display, and cache freshness."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import oauth
from claude_swap.credentials import ActiveCredentials, pending_rotation_path
from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap import list_reporter
from claude_swap.list_reporter import ListReporter
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord, UsageStore



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

    def test_list_renders_per_model_scoped_rows_end_to_end(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """A weekly_scoped limit flows API → cache → _format_usage_lines → --list."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        usage_response = {
            "five_hour": {"utilization": 10.0, "resets_at": "2026-01-01T00:00:00Z"},
            "seven_day": {"utilization": 50.0, "resets_at": "2026-01-02T00:00:00Z"},
            "limits": [
                {
                    "kind": "weekly_scoped",
                    "percent": 100,
                    "resets_at": "2026-01-02T00:00:00Z",
                    "scope": {"model": {"display_name": "Fable"}},
                },
            ],
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
        assert "Fable:" in output
        assert "(!)" in output  # 100% → at-limit marker
        # The scoped label widens every label column, 5h/7d included.
        assert "5h:" in output and "7d:" in output

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
            patch.object(
                switcher,
                "_read_active_credentials",
                return_value=ActiveCredentials(refreshed_live, False),
            ),
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(None),
            ),
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
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(usage_result),
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
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(None),
            ),
        ):
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
        backup_creds = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-backup", "refreshToken": "rt-orig"},
        })
        refreshed_creds = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new"},
        })

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials=None):
            # Simulate a refresh on the inactive account only.
            if not is_active and persist_credentials is not None:
                persist_credentials(account_num, email, refreshed_creds)
            return oauth.UsageOutcome(None)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=True,
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            switcher.list_accounts()

        # Live creds must never be written while Claude Code is running.
        write_live.assert_not_called()
        # Backup was written for the inactive account (2) only.
        write_backup.assert_called_once_with("2", "account2@example.com", refreshed_creds)

    def test_usage_classification_parity_across_paths(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """list-JSON, list-human, and strategy paths classify accounts identically."""
        from claude_swap.json_output import usage_fields
        from claude_swap.list_reporter import _SENTINEL_NOTES

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
                return oauth.UsageOutcome(usage_ok)
            return oauth.UsageOutcome(None)

        with (
            patch.object(
                switcher,
                "_read_active_credentials",
                return_value=ActiveCredentials(active_creds, False),
            ),
            patch.object(
                switcher,
                "_read_account_credentials",
                side_effect=lambda n, e: API_KEY if str(n) == "3" else "",
            ),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                side_effect=mock_fetch,
            ),
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
        assert _SENTINEL_NOTES[USAGE_API_KEY] in human_out
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
            account_num, email, credentials, is_active, persist_credentials=None, **kw
        ):
            # Simulate a refresh on the inactive account only.
            if not is_active and persist_credentials is not None:
                persist_credentials(account_num, email, refreshed_creds)
            return oauth.UsageOutcome(None)

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
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                side_effect=mock_fetch,
            ),
        ):
            switcher.list_accounts()

        # Live creds must never be written from list_accounts()
        write_live.assert_not_called()
        # Backup was written for the inactive account (2) only.
        write_backup.assert_called_once_with(
            "2", "account2@example.com", refreshed_creds
        )

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
             patch("claude_swap.oauth.try_fetch_usage_for_account", return_value=oauth.UsageOutcome(None)), \
             patch("claude_swap.oauth.build_token_status", return_value="oauth: fresh, refresh token yes"):
            switcher.list_accounts(show_token_status=True)

        output = capsys.readouterr().out
        assert "oauth: fresh, refresh token yes" in output

    def test_list_uses_cached_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """When fresh store entries exist, list_accounts skips API calls."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Pre-populate the store with fresh usage data for both accounts
        UsageStore(switcher.backup_dir / "cache").record(
            {
                "1": FetchRecord(usage={
                    "five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"},
                    "seven_day": {"pct": 60, "clock": "Jan 2 03:00", "countdown": "2d"},
                }),
                "2": FetchRecord(usage={
                    "five_hour": {"pct": 80, "clock": "Jan 1 04:00", "countdown": "30m"},
                    "seven_day": {"pct": 90, "clock": "Jan 3 03:00", "countdown": "3d"},
                }),
            },
            {"1": ("test@example.com", ""), "2": ("account2@example.com", "")},
        )

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            switcher.list_accounts()

        # API should NOT have been called — data came from the store
        mock_fetch.assert_not_called()
        output = capsys.readouterr().out
        assert "25%" in output
        assert "80%" in output

    def test_list_refetches_stale_entries(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, capsys,
    ):
        """Entries older than the serve TTL are refetched, not served."""
        import time as time_mod

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Store has a 100s-old entry for account "1" (past SERVE_TTL_S) and
        # nothing for "2" — both must be fetched live.
        backdated = UsageStore(
            switcher.backup_dir / "cache", clock=lambda: time_mod.time() - 100
        )
        backdated.record(
            {"1": FetchRecord(usage={"five_hour": {"pct": 25}})},
            {"1": ("test@example.com", "")},
        )

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }

        with (
            patch.object(switcher, "_read_active_credentials",
                         return_value=ActiveCredentials(active_creds, False)),
            patch.object(
                switcher, "_read_account_credentials", return_value=backup_creds
            ),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(usage_result),
            ) as mock_fetch,
        ):
            switcher.list_accounts()

        assert mock_fetch.call_count == 2
        output = capsys.readouterr().out
        # Should show live data (10%), not the stale 25%
        assert "10%" in output
        assert "25%" not in output

    def test_list_fetch_set_restricts_fetches(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """``fetch`` caps which accounts may be fetched (the TUI watch view's
        adaptive set); the default ``None`` keeps every stale account eligible
        (covered by test_list_refetches_stale_entries)."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.list_reporter.ListReporter._active_cc_running",
                   return_value=True), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)) as mock_fetch:
            switcher.list_accounts(fetch=set())
        # Both accounts are stale (nothing stored) yet nobody may be fetched.
        mock_fetch.assert_not_called()

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.list_reporter.ListReporter._active_cc_running",
                   return_value=True), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)) as mock_fetch:
            switcher.list_accounts(fetch={"2"})
        # Only the allowed slot is fetched.
        assert mock_fetch.call_count == 1
        assert mock_fetch.call_args.args[0] == "2"


class TestRotatedTokenPersistContention:
    """R2-M2: a consumed single-use refresh token must never be dropped silently.

    The inactive-slot usage fetch refreshes an expired token over the network
    (consuming the single-use refresh token, claude-code#24317) and persists
    the rotation via a callback that takes the file lock. With the default 10s
    timeout, a switch holding the lock through its own in-lock network refresh
    made the persist raise LockError, which oauth swallowed as a warning — the
    backup kept the now-dead token and the slot needed a manual re-login.
    """

    _ROTATED = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "sk-rotated",
                "refreshToken": "rt-rotated",
                "expiresAt": 9_999_999_999_000,
            }
        }
    )

    def _seed(self, temp_home: Path, sample_sequence_data: dict):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        expired = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-old",
                    "refreshToken": "rt-old",
                    "expiresAt": 1000,
                }
            }
        )
        switcher._write_account_credentials("2", "account2@example.com", expired)
        return switcher, expired

    def _fetch_inactive_row(self, switcher, creds: str):
        with (
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=self._ROTATED,
            ),
            patch("claude_swap.oauth.request_usage_data", side_effect=OSError("no net")),
            patch.object(switcher, "_live_session_pids", return_value=[]),
        ):
            return ListReporter(switcher).fetch_account_usage(
                (2, "account2@example.com", "", "", False, creds),
            )

    def test_persist_waits_out_a_held_lock_and_lands_the_rotation(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A lock held past the old 10s default but released within the
        bounded persist window must not cost the rotated token."""
        switcher, expired = self._seed(temp_home, sample_sequence_data)
        blocker = FileLock(switcher.lock_file)
        assert blocker.acquire()
        release_timer = threading.Timer(0.5, blocker.release)

        with patch(
            "claude_swap.list_reporter.FileLock",
            side_effect=lambda p: FileLock(p, timeout=0.1),
        ):
            # Constructor timeout is irrelevant: persist must acquire with its
            # own bounded budget, which outlives this 0.5s contention.
            release_timer.start()
            try:
                self._fetch_inactive_row(switcher, expired)
            finally:
                release_timer.cancel()
                blocker.release()

        stored = switcher._read_account_credentials("2", "account2@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "rt-rotated"

    def test_persist_timeout_parks_the_rotation_instead_of_dropping_it(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        caplog,
        capsys,
        monkeypatch,
    ):
        """A wedged lock holder must not cost the rotation: it is parked on
        disk for the next locked pass, and no failure is reported."""
        switcher, expired = self._seed(temp_home, sample_sequence_data)
        monkeypatch.setattr(
            "claude_swap.list_reporter._ROTATED_PERSIST_LOCK_TIMEOUT", 0.2
        )
        blocker = FileLock(switcher.lock_file)
        assert blocker.acquire()
        try:
            with caplog.at_level(logging.WARNING, logger="claude-swap"):
                self._fetch_inactive_row(switcher, expired)
        finally:
            blocker.release()

        pending = pending_rotation_path(
            switcher.credentials_dir, "2", "account2@example.com"
        )
        assert pending.exists()
        payload = json.loads(pending.read_text())
        assert payload["credentials"] == self._ROTATED
        assert payload["replaces"] == ["rt-old"]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "Parked rotated OAuth token for account 2" in r.getMessage()
            for r in warnings
        ), [r.getMessage() for r in warnings]
        # Nothing was lost, so oauth's persist wrapper prints no failure.
        assert "failed to save refreshed token" not in capsys.readouterr().out

    def test_persist_timeout_with_failed_park_logs_error_with_recovery_hint(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        caplog,
        capsys,
        monkeypatch,
    ):
        """Only when parking itself fails is the token truly lost — that must
        surface as an error-level record naming the slot and the re-add
        recovery, plus the user-facing warning."""
        switcher, expired = self._seed(temp_home, sample_sequence_data)
        monkeypatch.setattr(
            "claude_swap.list_reporter._ROTATED_PERSIST_LOCK_TIMEOUT", 0.2
        )
        monkeypatch.setattr(
            "claude_swap.list_reporter.park_rotated_credential",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        blocker = FileLock(switcher.lock_file)
        assert blocker.acquire()
        try:
            with caplog.at_level(logging.ERROR, logger="claude-swap"):
                self._fetch_inactive_row(switcher, expired)
        finally:
            blocker.release()

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "rotated OAuth token for account 2" in r.getMessage()
            and "--add-account --slot 2" in r.getMessage()
            for r in errors
        ), [r.getMessage() for r in errors]
        # oauth's persist wrapper still prints the user-facing warning.
        assert "failed to save refreshed token for account 2" in capsys.readouterr().out

    def test_persist_keeps_a_relogged_slot_intact(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A re-login that lands while our refresh is in flight wins: the
        rotated old-lineage token must not clobber the fresh login."""
        switcher, expired = self._seed(temp_home, sample_sequence_data)
        relogged = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-relogin",
                    "refreshToken": "rt-relogin",
                    "expiresAt": 9_999_999_999_000,
                }
            }
        )

        def refresh_and_relogin(_creds: str) -> str:
            # The slot is re-added (new lineage) while our HTTP refresh runs.
            switcher._write_account_credentials(
                "2", "account2@example.com", relogged
            )
            return self._ROTATED

        with (
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                side_effect=refresh_and_relogin,
            ),
            patch("claude_swap.oauth.request_usage_data", side_effect=OSError("no net")),
            patch.object(switcher, "_live_session_pids", return_value=[]),
        ):
            ListReporter(switcher).fetch_account_usage(
                (2, "account2@example.com", "", "", False, expired),
            )

        stored = switcher._read_account_credentials("2", "account2@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "rt-relogin"


class TestActiveAccountRefresh:
    """`fetch_active_usage`: refresh the active token only when no owner is running."""

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
        """No Claude Code / session running → refresh and persist to live + backup."""
        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            assert is_active is False  # no owner → refresh enabled
            persist_credentials(account_num, email, self._REFRESHED)
            return oauth.UsageOutcome(usage_result)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=False,
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            result = switcher._list_reporter().fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.usage == usage_result
        assert result.sentinel is None
        write_live.assert_called_once_with(self._REFRESHED)
        write_backup.assert_called_once_with("1", "test@example.com", self._REFRESHED)

    def test_cc_running_stays_handsoff_and_reports_token_expired(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Claude Code running + expired token → no refresh, returns the sentinel."""
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED

        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=True,
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._list_reporter().fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        # Owned + locally expired → the request would just 401, so none is made.
        mock_fetch.assert_not_called()
        write_live.assert_not_called()

    def test_live_session_blocks_refresh(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A live `cswap run` session for the same account blocks active refresh."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=False,
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[4242]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._list_reporter().fetch_active_usage("1", "test@example.com", self._EXPIRED)

        # Session owns the credential + token expired → sentinel, no request,
        # and certainly no refresh write.
        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_fetch.assert_not_called()
        write_live.assert_not_called()

    def test_lineage_mismatch_skips_write_and_reports_token_expired(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """If the live refresh token changes between read and persist, discard the write."""
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED

        switcher = self._switcher(sample_sequence_data)
        # Live store now holds a *different* refresh token (e.g. user re-logged in).
        live_changed = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-x", "refreshToken": "rt-someone-else"},
        })
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            persist_credentials(account_num, email, self._REFRESHED)
            return oauth.UsageOutcome(usage_result)  # in-memory token would fetch fine...

        with patch.object(switcher, "_read_credentials", return_value=live_changed), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=False,
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            result = switcher._list_reporter().fetch_active_usage("1", "test@example.com", self._EXPIRED)

        # ...but we discarded the rotated credential, so never show its usage.
        assert result.sentinel == USAGE_TOKEN_EXPIRED
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

        def mock_fetch(
            account_num, email, credentials, is_active, persist_credentials, **kw
        ):
            assert is_active is False  # not owned at pre-check
            persist_credentials(account_num, email, self._REFRESHED)
            return oauth.UsageOutcome(usage_result)

        with (
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                side_effect=[False, True],
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials") as write_live,
            patch.object(switcher, "_write_account_credentials") as write_backup,
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                side_effect=mock_fetch,
            ),
        ):
            result = switcher._list_reporter().fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        write_live.assert_not_called()  # do not clobber the owner's live store
        write_backup.assert_called_once_with("1", "test@example.com", self._REFRESHED)

    def test_write_failure_reports_token_expired(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """If persisting the rotated credential raises, never show usage for it."""
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED

        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(account_num, email, credentials, is_active, persist_credentials):
            # oauth._persist swallows the write error after logging — mirror that.
            try:
                persist_credentials(account_num, email, self._REFRESHED)
            except Exception:
                pass
            return oauth.UsageOutcome(usage_result)  # refreshed in-memory token still fetches fine

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=False,
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials", side_effect=OSError("disk full")), \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            result = switcher._list_reporter().fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED

    def test_active_usage_entry_refreshes_when_store_missing(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}

        def mock_fetch(
            account_num, email, credentials, is_active, persist_credentials, **kw
        ):
            assert is_active is False
            persist_credentials(account_num, email, self._REFRESHED)
            return oauth.UsageOutcome(usage_result)

        with (
            patch.object(
                switcher,
                "_read_active_credentials",
                return_value=ActiveCredentials(self._EXPIRED, False),
            ),
            patch.object(switcher, "_read_credentials", return_value=self._EXPIRED),
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=False,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch.object(switcher, "_write_credentials"),
            patch.object(switcher, "_write_account_credentials"),
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                side_effect=mock_fetch,
            ),
        ):
            entry = switcher._list_reporter().active_usage_entry(
                "1", "test@example.com"
            )

        assert entry.last_good == usage_result
        assert entry.sentinel is None

    def test_detection_failure_fails_closed(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """If instance detection raises, assume an owner exists and do not refresh."""
        switcher = self._switcher(sample_sequence_data)

        with patch("claude_swap.list_reporter.get_running_instances",
                   side_effect=OSError("boom")):
            assert switcher._list_reporter()._active_cc_running() is True

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch("claude_swap.list_reporter.get_running_instances", side_effect=OSError("boom")), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._list_reporter().fetch_active_usage("1", "test@example.com", self._EXPIRED)

        # Fails closed: assumed owner + expired token → sentinel, no request,
        # no refresh write.
        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_fetch.assert_not_called()
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
            return oauth.UsageOutcome({"five_hour": {"pct": 10}})

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=False,
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials"), \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            switcher._list_reporter().fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert lock_free_during_fetch["ok"] is True

    def test_no_token_returns_no_credentials(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Missing access token short-circuits before any owner check or fetch."""
        from claude_swap.json_output import USAGE_NO_CREDENTIALS

        switcher = self._switcher(sample_sequence_data)
        with patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._list_reporter().fetch_active_usage("1", "test@example.com", "")
        assert result.sentinel == USAGE_NO_CREDENTIALS
        mock_fetch.assert_not_called()

    def test_list_renders_token_expired_line(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """End-to-end: --list shows the intentional message for the active account."""
        switcher = self._switcher(sample_sequence_data)
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(self._EXPIRED, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch(
                 "claude_swap.list_reporter.ListReporter._active_cc_running",
                 return_value=True,
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(None)):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "token expired — Claude Code refreshes the active account" in output

    def test_expired_owned_sentinel_wins_over_stored_entry(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The owned+expired sentinel is derived statically, so a fresh store
        entry (or a backoff/claim gate skipping the fetch) can't hide it."""
        switcher = self._switcher(sample_sequence_data)
        UsageStore(switcher.backup_dir / "cache").record(
            {"1": FetchRecord(usage={"five_hour": {"pct": 25.0}})},
            {"1": ("test@example.com", "")},
        )
        info = (1, "test@example.com", "", "", True, self._EXPIRED)

        with (
            patch(
                "claude_swap.list_reporter.ListReporter._active_cc_running",
                return_value=True,
            ),
            patch.object(switcher, "_live_session_pids", return_value=[]),
            patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch,
        ):
            entry = switcher._list_reporter().collect_usage_entries([info])["1"]

        assert entry.sentinel == USAGE_TOKEN_EXPIRED
        assert entry.decision_value() == USAGE_TOKEN_EXPIRED
        assert entry.last_good == {"five_hour": {"pct": 25.0}}  # last-seen kept
        mock_fetch.assert_not_called()


class TestSchemaDriftWarning:
    """When the usage API answers but nothing in the payload is recognized,
    log a structured WARNING — distinguishes schema-break from transient
    network failure (general-purpose review HIGH). Lives in
    ``build_usage_result`` so every consumer (engine, list, TUI) is covered.
    """

    def test_logs_warning_when_no_window_keys(self, temp_home: Path, caplog):
        from claude_swap.oauth import build_usage_result

        caplog.set_level(logging.WARNING, logger="claude-swap")
        assert build_usage_result({"new_unexpected_key": 42}) is None
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any(
            "no recognized rate-limit windows" in m and "new_unexpected_key" in m
            for m in warnings
        ), warnings

    def test_no_warning_for_recognized_or_empty_payloads(
        self, temp_home: Path, caplog
    ):
        from claude_swap.oauth import build_usage_result

        caplog.set_level(logging.WARNING, logger="claude-swap")
        assert build_usage_result({"five_hour": {"utilization": 10}}) is not None
        assert build_usage_result({}) is None
        assert not [
            r
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]


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


class TestFormatUsageLines:
    """Test _format_usage_lines rendering, including per-model scoped windows."""

    def test_scoped_lines_render_per_model_with_at_limit_marker(self):
        usage = {
            "five_hour": {"pct": 7.0, "clock": "20:39", "countdown": "1h 30m"},
            "seven_day": {"pct": 72.0, "clock": "21:59", "countdown": "3h"},
            "scoped": [
                {"name": "Fable", "pct": 100.0, "clock": "21:59", "countdown": "3h"},
            ],
        }
        lines = list_reporter._format_usage_lines(usage)
        assert lines[0].startswith("5h:")
        assert lines[1].startswith("7d:")
        fable = lines[2]
        assert fable.startswith("Fable:")
        assert "100%" in fable
        assert fable.rstrip().endswith("(!)")  # at/over limit marker

    def test_scoped_under_limit_has_no_marker(self):
        usage = {"scoped": [{"name": "Fable", "pct": 40.0, "clock": "21:59", "countdown": "3h"}]}
        lines = list_reporter._format_usage_lines(usage)
        assert len(lines) == 1
        assert lines[0].startswith("Fable:")
        assert "40%" in lines[0]
        assert "resets 21:59" in lines[0]
        assert "in 3h" in lines[0]
        assert not lines[0].rstrip().endswith("(!)")

    def test_scoped_without_clock_renders_pct_only(self):
        usage = {"scoped": [{"name": "Fable", "pct": 100.0}]}
        lines = list_reporter._format_usage_lines(usage)
        assert lines == ["Fable: 100%  (!)"]

    @pytest.mark.parametrize(
        ("pct", "flagged"),
        [
            (99.0, False),
            (99.9, False),
            (100.0, True),
            (120.0, True),
        ],
    )
    def test_at_limit_marker_boundary(self, pct: float, flagged: bool):
        lines = list_reporter._format_usage_lines({"scoped": [{"name": "Fable", "pct": pct}]})
        assert lines[0].rstrip().endswith("(!)") is flagged

    def test_no_scoped_key_renders_only_standard_windows(self):
        usage = {"five_hour": {"pct": 7.0}, "seven_day": {"pct": 72.0}}
        lines = list_reporter._format_usage_lines(usage)
        assert all(not line.startswith("Fable:") for line in lines)

    def test_scoped_labels_align_columns_with_standard_windows(self):
        usage = {
            "five_hour": {"pct": 0.0},
            "seven_day": {"pct": 62.0, "clock": "Jul 5 08:59", "countdown": "1d 19h"},
            "scoped": [
                {"name": "Fable", "pct": 100.0, "clock": "Jul 5 08:59", "countdown": "1d 19h"},
            ],
        }
        lines = list_reporter._format_usage_lines(usage)
        # Labels are padded to the widest ("Fable:"), so the % column lines up.
        assert lines[0] == "5h:      0%"
        assert lines[1].startswith("7d:     62%   resets Jul 5 08:59")
        assert lines[2].startswith("Fable: 100%   resets Jul 5 08:59")
        assert len({line.index("%") for line in lines}) == 1

    def test_standard_windows_alone_keep_legacy_layout(self):
        usage = {"five_hour": {"pct": 7.0, "clock": "20:39", "countdown": "1h 30m"}}
        lines = list_reporter._format_usage_lines(usage)
        assert lines == ["5h:   7%   resets 20:39         in 1h 30m"]
