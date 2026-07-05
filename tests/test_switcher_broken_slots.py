"""Broken-slot resilience at switch time: skip-unswitchable rotation and
pre-activation refresh failure handling (issue #41)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


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

    def test_perform_switch_unknown_target_raises_configerror(self, temp_home: Path):
        """A target slot absent from accounts (corrupt/out-of-sync sequence)
        raises ConfigError, not an unhandled KeyError/TypeError."""
        from claude_swap.exceptions import ConfigError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")

        with pytest.raises(ConfigError, match="not found in managed accounts"):
            s._perform_switch("99")

    def test_switch_to_missing_config_actionable_error(self, temp_home: Path):
        """switch_to a target with creds but no config raises a distinct error."""
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", config=False)

        live_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-live-1",
                    "refreshToken": "rt-live-1",
                },
            }
        )
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a@example.com",
                        "accountUuid": "uuid-1",
                    },
                }
            )
        )

        with pytest.raises(SwitchError, match="has no stored config backup"):
            s.switch_to("2")

    def test_switch_to_refreshes_expired_target_before_activation(
        self, temp_home: Path
    ):
        """Expired inactive backup credentials are refreshed before becoming live."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        live_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-live-1",
                    "refreshToken": "rt-live-1",
                    "expiresAt": 9_999_999_999_000,
                },
            }
        )
        expired_target = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-expired-2",
                    "refreshToken": "rt-expired-2",
                    "expiresAt": 1,
                },
            }
        )
        refreshed_target = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-refreshed-2",
                    "refreshToken": "rt-refreshed-2",
                    "expiresAt": 9_999_999_999_000,
                },
            }
        )
        s._write_account_credentials("2", "b@example.com", expired_target)
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a@example.com",
                        "accountUuid": "uuid-1",
                    },
                }
            )
        )

        with (
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=refreshed_target,
            ),
            patch.object(s, "list_accounts"),
        ):
            s.switch_to("2")

        live_after = json.loads(
            (temp_home / ".claude" / ".credentials.json").read_text()
        )
        backup_after = json.loads(s._read_account_credentials("2", "b@example.com"))
        assert live_after["claudeAiOauth"]["accessToken"] == "sk-refreshed-2"
        assert backup_after["claudeAiOauth"]["refreshToken"] == "rt-refreshed-2"

    def test_switch_to_expired_target_refresh_failure_is_actionable(
        self, temp_home: Path
    ):
        """Do not activate an expired backup when its refresh token is already invalid."""
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        live_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-live-1",
                    "refreshToken": "rt-live-1",
                    "expiresAt": 9_999_999_999_000,
                },
            }
        )
        expired_target = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-expired-2",
                    "refreshToken": "rt-expired-2",
                    "expiresAt": 1,
                },
            }
        )
        s._write_account_credentials("2", "b@example.com", expired_target)
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with (
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=None,
            ),
            pytest.raises(SwitchError, match="stored OAuth token is expired"),
        ):
            s.switch_to("2")

        live_after = json.loads(
            (temp_home / ".claude" / ".credentials.json").read_text()
        )
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


class TestRefreshTargetBeforeActivation:
    """Lock both branches of ``_refresh_target_credentials_before_activation``:
    when a stored OAuth token is expired and the network refresh fails, the
    method must raise SwitchError if no live session is detected, but must
    tolerate the failure (return the unrefreshed credentials unchanged) when
    a live session-mode instance is still using the token."""

    def _expired_creds(self) -> str:
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-expired",
                    "refreshToken": "rt-expired",
                    "expiresAt": 1,
                },
            }
        )

    def test_raises_when_no_live_session_and_refresh_fails(self, temp_home: Path):
        from claude_swap.exceptions import SwitchError

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        with (
            patch("claude_swap.oauth.refresh_oauth_credentials", return_value=None),
            patch.object(ClaudeAccountSwitcher, "_live_session_pids", return_value=[]),
        ):
            with pytest.raises(SwitchError, match="stored OAuth token is expired"):
                s._refresh_target_credentials_before_activation(
                    "2", "b@example.com", self._expired_creds()
                )

    def test_returns_unchanged_when_live_session_present(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        creds = self._expired_creds()
        with (
            patch("claude_swap.oauth.refresh_oauth_credentials", return_value=None),
            patch.object(
                ClaudeAccountSwitcher, "_live_session_pids", return_value=[1234]
            ),
        ):
            result = s._refresh_target_credentials_before_activation(
                "2", "b@example.com", creds
            )
        assert result == creds

    def _fresh_creds(self) -> str:
        """Token with a long-into-the-future expiry — not expired."""
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-fresh",
                    "refreshToken": "rt-fresh",
                    # Year 2099 in epoch ms — guaranteed not expired.
                    "expiresAt": 4_070_908_800_000,
                },
            }
        )

    def test_force_refresh_on_fresh_token_triggers_refresh(self, temp_home: Path):
        """force=True refreshes even when the token has not expired yet.

        This is the production-grade seamless-handoff path used by the
        auto-switch engine: after activation, Claude Code's first API call
        against the new account must use a freshly-issued token, not a
        backup token that could be minutes from expiry.
        """
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        fresh_creds = self._fresh_creds()
        refreshed_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-refreshed",
                    "refreshToken": "rt-refreshed",
                    "expiresAt": 4_070_908_800_000,
                },
            }
        )
        # The persist is read-back verified, so the mock must round-trip
        # write→read (a no-op write would correctly raise CredentialWriteError).
        store: dict[tuple[str, str], str] = {}
        with (
            patch(
                "claude_swap.oauth.refresh_oauth_credentials",
                return_value=refreshed_creds,
            ) as mock_refresh,
            patch.object(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                side_effect=lambda num, email, creds: store.__setitem__(
                    (num, email), creds
                ),
            ) as mock_write,
            patch.object(
                ClaudeAccountSwitcher,
                "_read_account_credentials",
                side_effect=lambda num, email: store.get((num, email), ""),
            ),
        ):
            result = s._refresh_target_credentials_before_activation(
                "2",
                "b@example.com",
                fresh_creds,
                force=True,
            )
        mock_refresh.assert_called_once_with(fresh_creds)
        mock_write.assert_called_once()
        assert store[("2", "b@example.com")] == refreshed_creds
        assert result == refreshed_creds

    def test_force_refresh_failure_on_fresh_token_falls_back(self, temp_home: Path):
        """When force=True and refresh fails but the existing token is still
        valid, return the existing creds — degrading gracefully rather than
        blocking the switch on a transient network error."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        fresh = self._fresh_creds()
        with (
            patch("claude_swap.oauth.refresh_oauth_credentials", return_value=None),
            patch.object(ClaudeAccountSwitcher, "_live_session_pids", return_value=[]),
        ):
            result = s._refresh_target_credentials_before_activation(
                "2",
                "b@example.com",
                fresh,
                force=True,
            )
        assert result == fresh

    def test_no_force_skips_refresh_when_not_expired(self, temp_home: Path):
        """The default (interactive) path saves a network call when the token
        is still good — preserves the historic fast-path behaviour."""
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        fresh = self._fresh_creds()
        with patch(
            "claude_swap.oauth.refresh_oauth_credentials",
            return_value=None,
        ) as mock_refresh:
            result = s._refresh_target_credentials_before_activation(
                "2",
                "b@example.com",
                fresh,
            )
        mock_refresh.assert_not_called()
        assert result == fresh
