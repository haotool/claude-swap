"""Tests for the auto-switch (Beta) feature.

Covers the switcher-side config/usage helpers and the TUI monitor logic.
Curses primitives are mocked exactly as in ``test_tui.py``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import monitor, oauth, tui
from claude_swap.credentials import ActiveCredentials
from claude_swap.locking import FileLock
from claude_swap.usage_policy import binding_pct as max_usage_pct
from claude_swap.exceptions import ClaudeSwitchError, SwitchError, ValidationError
from claude_swap.models import (
    AutoSwitchDecisionContext,
    BackgroundAutoSwitchIntent,
    InteractiveAutoSwitchIntent,
    SwitchPlanResult,
)
from claude_swap.sequence_store import AutoSwitchConfig
from claude_swap.switcher import (
    DEFAULT_AUTO_SWITCH_THRESHOLD,
    ClaudeAccountSwitcher,
)

from tests.conftest import bootstrap_switchable_accounts, stub_screen


def _login(temp_home: Path, email: str = "u@example.com") -> None:
    config = {"oauthAccount": {"emailAddress": email}}
    (temp_home / ".claude.json").write_text(json.dumps(config))


def _already_optimal_plan(switcher: ClaudeAccountSwitcher):
    return patch.object(
        switcher,
        "plan_automated_switch",
        return_value=SwitchPlanResult(outcome="already_optimal"),
    )


# --------------------------------------------------------------------------- #
# SwitchIntent contract (Background vs Interactive)                             #
# --------------------------------------------------------------------------- #


class TestSwitchIntentContract:
    """Product SSOT: background monitor fails closed; TUI monitor prints."""

    @staticmethod
    def _single_account_decision() -> AutoSwitchDecisionContext:
        return AutoSwitchDecisionContext(
            threshold=95,
            active_usage_pct=99.0,
            live_active_slot="1",
            sequence_active_slot="1",
            usage_by_slot={},
        )

    def test_background_single_account_raises(self, temp_home: Path, capsys):
        switcher = bootstrap_switchable_accounts(temp_home, num_accounts=1)
        decision = self._single_account_decision()
        with pytest.raises(SwitchError, match="Only one account"):
            switcher.switch(BackgroundAutoSwitchIntent(decision=decision))
        assert capsys.readouterr().out == ""

    def test_interactive_single_account_prints_and_returns(
        self,
        temp_home: Path,
        capsys,
    ):
        switcher = bootstrap_switchable_accounts(temp_home, num_accounts=1)
        decision = self._single_account_decision()
        switched = switcher.switch(InteractiveAutoSwitchIntent(decision=decision))
        out = capsys.readouterr().out
        assert switched is False
        assert "Only one account" in out

    def test_background_no_trusted_signal_stays_put_without_stdout(
        self,
        temp_home: Path,
        capsys,
    ):
        # Background auto-switch treats a planning/cache miss as benign: it stays
        # put (returns False) rather than raising, matching upstream's never-raise
        # headroom picker. Interactive still prints + raises (see below).
        switcher = bootstrap_switchable_accounts(temp_home, num_accounts=3)
        decision = switcher.build_auto_switch_decision(95, 99.0)
        assert switcher.switch(BackgroundAutoSwitchIntent(decision=decision)) is False
        assert capsys.readouterr().out == ""

    def test_interactive_no_trusted_signal_prints_then_raises(
        self,
        temp_home: Path,
        capsys,
    ):
        switcher = bootstrap_switchable_accounts(temp_home, num_accounts=3)
        decision = switcher.build_auto_switch_decision(95, 99.0)
        with pytest.raises(SwitchError, match="Cannot choose auto-switch target"):
            switcher.switch(InteractiveAutoSwitchIntent(decision=decision))
        out = capsys.readouterr().out
        assert "Cannot choose auto-switch target" in out


# --------------------------------------------------------------------------- #
# max_usage_pct                                                               #
# --------------------------------------------------------------------------- #


class TestMaxUsagePct:
    def test_none_when_no_usage(self):
        assert max_usage_pct(None) is None
        assert max_usage_pct({}) is None
        assert max_usage_pct("no credentials") is None

    def test_returns_highest_of_5h_7d(self):
        usage = {"five_hour": {"pct": 40}, "seven_day": {"pct": 95}}
        assert max_usage_pct(usage) == 95.0

    def test_ignores_spend_entry(self):
        usage = {"five_hour": {"pct": 10}, "spend": {"pct": 99}}
        assert max_usage_pct(usage) == 10.0

    def test_handles_missing_pct(self):
        assert max_usage_pct({"five_hour": {}}) is None


# --------------------------------------------------------------------------- #
# Config persistence                                                          #
# --------------------------------------------------------------------------- #


class TestAutoSwitchConfig:
    def test_default_is_disabled(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        cfg = switcher.get_auto_switch_config()
        assert cfg == AutoSwitchConfig(
            enabled=False,
            threshold=DEFAULT_AUTO_SWITCH_THRESHOLD,
        )

    def test_enable_and_persist(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True)
        # A fresh instance reads the persisted value.
        assert ClaudeAccountSwitcher().get_auto_switch_config().enabled is True

    def test_set_threshold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        cfg = switcher.set_auto_switch_config(threshold=80)
        assert cfg.threshold == 80
        assert switcher.get_auto_switch_config().threshold == 80

    def test_partial_update_keeps_other_field(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True, threshold=70)
        switcher.set_auto_switch_config(threshold=60)
        cfg = switcher.get_auto_switch_config()
        assert cfg == AutoSwitchConfig(enabled=True, threshold=60)

    @pytest.mark.parametrize("bad", [0, -5, 101, 999])
    def test_invalid_threshold_rejected(self, temp_home: Path, bad: int):
        switcher = ClaudeAccountSwitcher()
        with pytest.raises(ValidationError):
            switcher.set_auto_switch_config(threshold=bad)

    def test_does_not_clobber_accounts(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {"email": "a@x.com"}
        switcher._write_json(switcher.sequence_file, data)
        switcher.set_auto_switch_config(enabled=True)
        assert "1" in switcher._get_sequence_data()["accounts"]


# --------------------------------------------------------------------------- #
# get_active_usage_pct                                                        #
# --------------------------------------------------------------------------- #


class TestActiveUsagePct:
    def test_none_without_login(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert switcher.get_active_usage_pct() is None

    def test_none_without_credentials(self, temp_home: Path):
        _login(temp_home)
        switcher = ClaudeAccountSwitcher()
        with patch.object(switcher, "_read_credentials", return_value=""):
            assert switcher.get_active_usage_pct() is None

    def test_returns_pct_from_usage_api(self, temp_home: Path):
        _login(temp_home)
        switcher = ClaudeAccountSwitcher()
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        usage = {"five_hour": {"pct": 96}, "seven_day": {"pct": 20}}
        with (
            patch.object(
                switcher,
                "_read_active_credentials",
                return_value=ActiveCredentials(creds, False),
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=usage,
            ),
        ):
            assert switcher.get_active_usage_pct() == 96.0

    def test_none_when_api_unavailable(self, temp_home: Path):
        _login(temp_home)
        switcher = ClaudeAccountSwitcher()
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with (
            patch.object(
                switcher,
                "_read_active_credentials",
                return_value=ActiveCredentials(creds, False),
            ),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=None,
            ),
        ):
            assert switcher.get_active_usage_pct() is None


# --------------------------------------------------------------------------- #
# Monitor decision core                                                       #
# --------------------------------------------------------------------------- #


class TestShouldSwitch:
    def test_at_threshold_switches(self):
        assert monitor.should_switch(95, 95) is True

    def test_above_threshold_switches(self):
        assert monitor.should_switch(99.5, 95) is True

    def test_below_threshold_holds(self):
        assert monitor.should_switch(94.9, 95) is False

    def test_none_usage_holds(self):
        assert monitor.should_switch(None, 95) is False


# --------------------------------------------------------------------------- #
# TUI settings sub-flow                                                       #
# --------------------------------------------------------------------------- #


class TestDoAutoSwitch:
    def test_toggle_enables(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        # Enter on "Enable" (idx 0), then Esc to leave the settings screen.
        screen.getch.side_effect = [10, 27]
        tui._do_auto_switch(screen, switcher)
        assert switcher.get_auto_switch_config().enabled is True

    def test_back_does_nothing(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [27]  # Esc immediately
        tui._do_auto_switch(screen, switcher)
        assert switcher.get_auto_switch_config().enabled is False

    def test_set_threshold_via_prompt(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        # Select by menu value, not KEY_DOWN index — survives label reordering.
        screen.getch.side_effect = [ord("8"), ord("0"), 10, 27]
        with (
            patch("claude_swap.tui.curses.curs_set"),
            patch(
                "claude_swap.tui._select_from",
                side_effect=["threshold", None],
            ),
        ):
            tui._do_auto_switch(screen, switcher)
        assert switcher.get_auto_switch_config().threshold == 80

    def test_service_toggle_installs_on_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()

        with (
            patch("claude_swap.tui.sys.platform", "darwin"),
            patch("claude_swap.tui._service_state", return_value="not installed"),
            patch(
                "claude_swap.tui._select_from",
                side_effect=["service-toggle", None],
            ),
            patch("claude_swap.tui._shell_out") as mock_shell,
        ):
            tui._do_auto_switch(screen, switcher)

        _stdscr_arg, fn = mock_shell.call_args.args
        assert _stdscr_arg is screen
        with patch("claude_swap.tui.service.install", return_value=0) as mock_install:
            fn()
        mock_install.assert_called_once_with(switcher)

    def test_service_toggle_uninstalls_on_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()

        with (
            patch("claude_swap.tui.sys.platform", "darwin"),
            patch("claude_swap.tui._service_state", return_value="loaded"),
            patch(
                "claude_swap.tui._select_from",
                side_effect=["service-toggle", None],
            ),
            patch("claude_swap.tui._shell_out") as mock_shell,
        ):
            tui._do_auto_switch(screen, switcher)

        _stdscr_arg, fn = mock_shell.call_args.args
        assert _stdscr_arg is screen
        with patch(
            "claude_swap.tui.service.uninstall", return_value=0
        ) as mock_uninstall:
            fn()
        mock_uninstall.assert_called_once_with(switcher)

    def test_service_status_shells_out(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()

        with (
            patch(
                "claude_swap.tui._select_from",
                side_effect=["service-status", None],
            ),
            patch("claude_swap.tui._shell_out") as mock_shell,
        ):
            tui._do_auto_switch(screen, switcher)

        _stdscr_arg, fn = mock_shell.call_args.args
        assert _stdscr_arg is screen
        with patch("claude_swap.tui.service.status", return_value=0) as mock_status:
            fn()
        mock_status.assert_called_once_with(switcher)

    def test_service_status_shows_error_off_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()

        with (
            patch("claude_swap.tui.sys.platform", "linux"),
            patch("claude_swap.tui._service_state", return_value="unsupported"),
            patch(
                "claude_swap.tui._select_from",
                side_effect=["service-status", None],
            ),
            patch("claude_swap.tui._show_message") as mock_message,
            patch("claude_swap.tui._shell_out") as mock_shell,
        ):
            tui._do_auto_switch(screen, switcher)

        mock_message.assert_called_once()
        mock_shell.assert_not_called()

    def test_service_toggle_shows_error_off_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()

        with (
            patch("claude_swap.tui.sys.platform", "linux"),
            patch("claude_swap.tui._service_state", return_value="unsupported"),
            patch(
                "claude_swap.tui._select_from",
                side_effect=["service-toggle", None],
            ),
            patch("claude_swap.tui._show_message") as mock_message,
        ):
            tui._do_auto_switch(screen, switcher)

        mock_message.assert_called_once()

    def test_service_menu_label_for_installed_but_not_loaded(self):
        assert tui._service_menu_label("installed but not loaded") == (
            "Background service: Uninstall"
        )


# --------------------------------------------------------------------------- #
# TUI monitor loop                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("stub_live_claude")
class TestRunAutoMonitor:
    def test_quits_without_switching_below_threshold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [ord("q")]
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=10.0),
            patch("claude_swap.tui._auto_perform_switch") as mock_switch,
            patch("claude_swap.tui.acquire_pid", return_value=None),
            patch("claude_swap.tui.release_pid"),
            patch("claude_swap.tui.curses.curs_set"),
        ):
            tui._run_auto_monitor(screen, switcher, threshold=95)
        mock_switch.assert_not_called()

    def test_switches_when_threshold_reached(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [ord("q")]
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch(
                "claude_swap.tui._auto_perform_switch", return_value=True
            ) as mock_switch,
            patch("claude_swap.tui.acquire_pid", return_value=None),
            patch("claude_swap.tui.release_pid"),
            patch("claude_swap.tui.curses.curs_set"),
        ):
            tui._run_auto_monitor(screen, switcher, threshold=95)
        mock_switch.assert_called_once()

    def test_tui_adapter_surfaces_switch_failed_on_error(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        def boom(_decision):
            raise ClaudeSwitchError("planning failed")

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=boom,
            )

        assert result.kind == "switch_failed"
        assert "planning failed" in (result.switch_error or "")

    def test_tui_uses_engine_adaptive_interval(self, temp_home: Path):
        """TUI adapter must sleep using engine-provided next_interval, not a
        fixed 60s cadence."""
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [ord("q")]
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=91.0),
            patch(
                "claude_swap.tui.monitor_step",
                return_value=monitor.MonitorStepResult(
                    kind="polled",
                    threshold=95,
                    pct=91.0,
                    next_interval=12,
                    pct_text="91%",
                    user_message="Monitoring active account.",
                ),
            ) as mock_step,
            patch("claude_swap.tui.acquire_pid", return_value=None),
            patch("claude_swap.tui.release_pid"),
            patch("claude_swap.tui.curses.curs_set"),
        ):
            tui._run_auto_monitor(screen, switcher, threshold=95)
        mock_step.assert_called_once()
        drawn = screen.addstr.call_args_list
        assert any("Next check in" in str(c) for c in drawn)

    def test_s_key_forces_immediate_poll(self, temp_home: Path):
        """Pressing ``s`` zeroes the countdown so monitor_step runs again."""
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [ord("s"), ord("q")]
        polled = monitor.MonitorStepResult(
            kind="polled",
            threshold=95,
            pct=50.0,
            next_interval=60,
            pct_text="50%",
            user_message="Monitoring active account.",
        )
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch(
                "claude_swap.tui.monitor_step",
                return_value=polled,
            ) as mock_step,
            patch("claude_swap.tui.acquire_pid", return_value=None),
            patch("claude_swap.tui.release_pid"),
            patch("claude_swap.tui.curses.curs_set"),
        ):
            tui._run_auto_monitor(screen, switcher, threshold=95)
        assert mock_step.call_count == 2

    def test_blocks_when_cli_monitor_already_running(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        screen = stub_screen()
        screen.getch.return_value = ord("q")

        with (
            patch("claude_swap.tui.acquire_pid", return_value=12345),
            patch("claude_swap.tui._show_message") as mock_message,
            patch("claude_swap.tui.monitor_step") as mock_step,
            patch("claude_swap.tui.curses.curs_set"),
        ):
            tui._run_auto_monitor(screen, switcher, threshold=95)

        mock_step.assert_not_called()
        mock_message.assert_called_once()
        assert "12345" in mock_message.call_args.args[1]

    def test_draws_threshold_from_engine_result(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [ord("q")]
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=91.0),
            patch(
                "claude_swap.tui.monitor_step",
                return_value=monitor.MonitorStepResult(
                    kind="polled",
                    threshold=80,
                    pct=91.0,
                    next_interval=12,
                    pct_text="91%",
                    user_message="Monitoring active account.",
                ),
            ),
            patch("claude_swap.tui.acquire_pid", return_value=None),
            patch("claude_swap.tui.release_pid"),
            patch("claude_swap.tui.curses.curs_set"),
        ):
            tui._run_auto_monitor(screen, switcher, threshold=95)

        header_calls = [
            str(c)
            for c in screen.addstr.call_args_list
            if "threshold" in str(c).lower()
        ]
        assert any("threshold 80%" in c for c in header_calls)

    def test_ctrl_c_during_switch_is_not_already_optimal(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        def cancel(_decision):
            raise monitor.SwitchCancelled

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=cancel,
            )

        assert result.kind == "switch_cancelled"
        assert "cancelled" in result.user_message.lower()
        assert result.kind != "already_optimal"


@pytest.mark.usefixtures("stub_live_claude")
class TestMonitorEngine:
    def test_step_already_optimal_does_not_call_switch(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with (
            _already_optimal_plan(switcher),
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert result.kind == "already_optimal"
        perform.assert_called_once()
        decision = perform.call_args[0][0]
        assert decision.threshold == 95
        assert decision.active_usage_pct == 96.0

    def test_step_honours_masked_retry_after_when_pct_present(self, temp_home: Path):
        # A trusted prior usage row masks an active 429: get_active_usage_pct
        # returns a stale pct (not None), so the poll takes the pct-present
        # path — which must still honour the server Retry-After for its next
        # interval instead of polling straight through the rate-limit window.
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=50.0),
            patch.object(
                switcher,
                "get_active_usage_breakdown",
                return_value={"max": 50.0},
            ),
            patch.object(
                switcher,
                "get_active_usage_retry_after",
                return_value=120,
            ),
        ):
            result = monitor.monitor_step(switcher, state, poll_seconds=30)

        assert result.next_interval == 120  # max(normal<=30, min(120, cap=300))

    def test_step_retry_after_capped_when_pct_present(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=50.0),
            patch.object(
                switcher,
                "get_active_usage_breakdown",
                return_value={"max": 50.0},
            ),
            patch.object(
                switcher,
                "get_active_usage_retry_after",
                return_value=99999,
            ),
        ):
            result = monitor.monitor_step(switcher, state, poll_seconds=30)

        assert result.next_interval == monitor.MONITOR_RETRY_AFTER_CAP

    def test_step_no_trusted_signal_is_not_already_optimal(self, temp_home: Path):
        switcher = bootstrap_switchable_accounts(temp_home, num_accounts=3)
        state = monitor.MonitorRuntimeState()

        def perform(decision: AutoSwitchDecisionContext) -> bool:
            return switcher.switch(BackgroundAutoSwitchIntent(decision=decision))

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert result.kind == "no_trusted_signal"
        assert result.kind != "already_optimal"
        assert "no trusted usage signal" in result.user_message.lower()
        assert state.saturated_hold is True

    def test_step_threshold_without_handler(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=99.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=None,
            )

        assert result.kind == "threshold_no_handler"
        assert "no switch handler" in result.user_message

    def test_noop_cycle_plans_once_in_monitor(self, temp_home: Path):
        """A stay-put threshold cycle must not replan in the finalize step.

        Each plan_automated_switch call reads every slot's credential backend
        (a `security` subprocess per slot on macOS), so a saturated hold must
        not pay it more than once inside the monitor engine.
        """
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with (
            _already_optimal_plan(switcher) as plan,
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert result.kind == "already_optimal"
        assert plan.call_count == 1

    def test_noop_cycle_plans_at_most_twice_end_to_end(self, temp_home: Path):
        """Full no-op cycle budget: one plan inside switch(), one in the monitor."""
        switcher = bootstrap_switchable_accounts(temp_home, num_accounts=3)
        state = monitor.MonitorRuntimeState()

        def perform(decision: AutoSwitchDecisionContext) -> bool:
            return switcher.switch(BackgroundAutoSwitchIntent(decision=decision))

        # Count at the single chokepoint: both switch() and the monitor plan
        # through plan_automated_switch.
        with (
            patch.object(
                switcher,
                "plan_automated_switch",
                return_value=SwitchPlanResult(outcome="already_optimal"),
            ) as plan,
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert result.kind == "already_optimal"
        assert plan.call_count <= 2

    def test_step_switch_failed_dedups_log(self, temp_home: Path, caplog):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        caplog.set_level(logging.DEBUG, logger="claude-swap")

        def boom(_decision) -> bool:
            raise ClaudeSwitchError("same error")

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=99.0),
            patch("claude_swap.monitor.time.time", return_value=1_000_000.0),
        ):
            monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=boom,
            )
            monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=boom,
            )

        warnings = [
            r
            for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.WARNING
            and "switch failed" in r.getMessage()
        ]
        debugs = [
            r
            for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.DEBUG
            and "switch failed (repeat)" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert len(debugs) == 1

    def test_threshold_on_masked_stale_pct_requires_fresh_fetch(
        self, temp_home: Path
    ):
        """A trusted prior cache row can mask a failed fetch, so the pct the
        threshold sees may be arbitrarily old. The trigger signal joins the
        no-trusted-signal philosophy: hold, and only switch once a poll's
        fetch actually succeeds."""
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=True)

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch.object(
                switcher,
                "active_usage_is_masked_failure",
                return_value=True,
            ),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert result.kind == "no_trusted_signal"
        perform.assert_not_called()
        assert state.saturated_hold is True

        # The next poll's fetch succeeds: the gate opens and the switch runs.
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch.object(
                switcher,
                "active_usage_is_masked_failure",
                return_value=False,
            ),
        ):
            result2 = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert result2.kind == "switched"
        perform.assert_called_once()

    def test_switch_failure_backoff_grows_and_caps(self, temp_home: Path):
        """Consecutive switch failures back off exponentially instead of
        retrying at the near-threshold t_min: every failed attempt pays a
        full plan (a `security` subprocess per slot on macOS) plus a forced
        token refresh, so the retry cadence must grow and cap."""
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        def boom(_decision) -> bool:
            raise ClaudeSwitchError("switch keeps failing")

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            intervals: list[int] = []
            for _ in range(8):
                result = monitor.monitor_step(
                    switcher,
                    state,
                    poll_seconds=monitor.MONITOR_POLL_SECONDS,
                    perform_switch=boom,
                )
                assert result.kind == "switch_failed"
                intervals.append(result.next_interval)

        base = monitor.MONITOR_FAILURE_BACKOFF_BASE
        cap = monitor.MONITOR_SWITCH_FAILURE_BACKOFF_CAP
        assert intervals == [min(base * 2**n, cap) for n in range(8)]
        assert intervals[-1] == cap

    def test_switch_failure_backoff_resets_after_success(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        outcomes = iter(
            [
                ClaudeSwitchError("boom"),
                ClaudeSwitchError("boom"),
                True,
                ClaudeSwitchError("boom"),
            ]
        )

        def perform(_decision) -> bool:
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
        ):
            kinds: list[str] = []
            intervals: list[int] = []
            for _ in range(4):
                result = monitor.monitor_step(
                    switcher,
                    state,
                    poll_seconds=monitor.MONITOR_POLL_SECONDS,
                    perform_switch=perform,
                )
                kinds.append(result.kind)
                intervals.append(result.next_interval)

        base = monitor.MONITOR_FAILURE_BACKOFF_BASE
        assert kinds == [
            "switch_failed",
            "switch_failed",
            "switched",
            "switch_failed",
        ]
        assert intervals[0] == base
        assert intervals[1] == base * 2
        # The successful switch reset the failure streak.
        assert intervals[3] == base

    def test_step_decision_error_returns_switch_failed(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch.object(
                switcher,
                "build_auto_switch_decision",
                side_effect=OSError("planning lock failed"),
            ),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=MagicMock(),
            )

        assert result.kind == "switch_failed"
        assert "planning lock failed" in (result.switch_error or "")

    def test_saturated_hold_replans_each_poll(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with (
            _already_optimal_plan(switcher),
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch("claude_swap.monitor.time.time", return_value=1_000_000.0),
        ):
            result1 = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )
            result2 = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert result1.kind == "already_optimal"
        assert result2.kind == "already_optimal"
        assert perform.call_count == 2

    def test_saturated_hold_uses_t_max_not_near_trigger_floor(
        self,
        temp_home: Path,
    ):
        """saturated_hold must poll at t_max (60s), not at the 5s near-trigger
        floor.  Bug: both the first already_optimal result and subsequent
        saturated-hold results returned next_interval=interval, which collapsed
        to t_min=5 when pct>=threshold*NEAR_TRIGGER_RATIO.  Fix: both branches
        now return poll_seconds (t_max)."""
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with (
            _already_optimal_plan(switcher),
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=100.0),
            patch("claude_swap.monitor.time.time", return_value=1_000_000.0),
        ):
            # First call: perform_switch returns False → sets saturated_hold=True
            result1 = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=60,
                perform_switch=perform,
            )
            # Second call: saturated_hold is already True → skips switch
            result2 = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=60,
                perform_switch=perform,
            )

        assert result1.kind == "already_optimal"
        assert result1.next_interval == 60, (
            f"first already_optimal must use t_max=60, got {result1.next_interval}"
        )
        assert result2.kind == "already_optimal"
        assert result2.next_interval == 60, (
            f"saturated-hold path must use t_max=60, got {result2.next_interval}"
        )
        assert perform.call_count == 2

    def test_threshold_hold_honours_masked_retry_after(self, temp_home: Path):
        # Staying put at threshold while a masked 429 is active must wait out the
        # server Retry-After (capped), not just the t_max hold cadence.
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with (
            _already_optimal_plan(switcher),
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=100.0),
            patch.object(
                switcher,
                "get_active_usage_retry_after",
                return_value=120,
            ),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=60,
                perform_switch=perform,
            )

        assert result.kind == "already_optimal"
        assert result.next_interval == 120  # max(t_max=60, min(120, cap))

    def test_saturated_hold_poll_log_reflects_real_cadence(
        self,
        temp_home: Path,
        caplog,
    ):
        """During a saturated hold the poll log must report the real t_max
        cadence (60s) and must not emit the "— switching" line, since hold
        replans without treating the cycle as a fresh threshold crossing."""
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        state.saturated_hold = True  # as if a prior poll already saturated
        perform = MagicMock(return_value=False)

        caplog.set_level(logging.INFO, logger="claude-swap")
        with (
            _already_optimal_plan(switcher),
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=100.0),
            patch("claude_swap.monitor.time.time", return_value=1_000_000.0),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=60,
                perform_switch=perform,
            )

        assert result.kind == "already_optimal"
        perform.assert_called_once()
        messages = [r.getMessage() for r in caplog.records if r.name == "claude-swap"]
        poll_lines = [m for m in messages if m.startswith("monitor poll:")]
        assert poll_lines and "next_poll=60s" in poll_lines[-1], (
            f"poll log must show the real 60s cadence, got {poll_lines}"
        )
        assert not any("— switching" in m for m in messages), (
            "saturated hold must not log '— switching' (replan, not fresh switch)"
        )
        assert any("replanning at 60s" in m for m in messages)

    def test_saturated_hold_clears_when_below_threshold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with (
            _already_optimal_plan(switcher),
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(
                switcher,
                "get_active_usage_pct",
                side_effect=[96.0, 90.0, 96.0],
            ),
            patch("claude_swap.monitor.time.time", return_value=1_000_000.0),
        ):
            monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )
            monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )
            monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert perform.call_count == 2

    def test_saturated_hold_replans_when_peer_account_becomes_unsaturated(
        self,
        temp_home: Path,
    ):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(side_effect=[False, True])

        with (
            _already_optimal_plan(switcher),
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch.object(
                switcher,
                "build_auto_switch_decision",
                side_effect=[
                    AutoSwitchDecisionContext(
                        threshold=95,
                        active_usage_pct=96.0,
                        live_active_slot="1",
                        sequence_active_slot="1",
                        usage_by_slot={},
                    ),
                    AutoSwitchDecisionContext(
                        threshold=95,
                        active_usage_pct=96.0,
                        live_active_slot="1",
                        sequence_active_slot="1",
                        usage_by_slot={"2": {"five_hour": {"pct": 10}}},
                    ),
                ],
            ) as mock_decision,
            patch("claude_swap.monitor.time.time", return_value=1_000_000.0),
        ):
            result1 = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=60,
                perform_switch=perform,
            )
            result2 = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=60,
                perform_switch=perform,
            )

        assert result1.kind == "already_optimal"
        assert result2.kind == "switched"
        assert mock_decision.call_count == 2
        assert perform.call_count == 2

    def test_idle_clears_saturated_hold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        state.saturated_hold = True
        perform = MagicMock(return_value=True)

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(
                ClaudeAccountSwitcher,
                "_live_default_mode_claude_pids",
                side_effect=[[], [99999]],
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch("claude_swap.monitor.time.time", return_value=1_000_000.0),
        ):
            idle_result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )
            assert idle_result.kind == "idle"
            assert state.saturated_hold is False

            switch_result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        assert switch_result.kind == "switched"
        perform.assert_called_once()

    def test_step_usage_fetch_error_returns_unavailable_backoff(
        self,
        temp_home: Path,
    ):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        _login(temp_home)
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "_read_credentials", return_value=creds),
            patch(
                "claude_swap.oauth.fetch_usage_for_account",
                return_value=oauth.UsageFetchError(
                    reason="rate_limited",
                    status_code=429,
                ),
            ),
        ):
            result = monitor.monitor_step(
                switcher,
                state,
                poll_seconds=60,
                perform_switch=MagicMock(),
            )

        assert result.kind == "usage_unavailable"
        assert result.consecutive_failures == 1
        assert result.next_interval == monitor.MONITOR_FAILURE_BACKOFF_BASE
        assert "unavailable" in result.user_message.lower()

    def test_warms_usage_cache_on_first_poll_only(
        self,
        temp_home: Path,
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "accounts": {
                    "1": {"email": "a1@example.com"},
                    "2": {"email": "a2@example.com"},
                },
                "sequence": [1, 2],
                "activeAccountNumber": 1,
            },
        )
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=10.0),
            patch.object(switcher, "_account_is_switchable", return_value=True),
            patch.object(switcher, "_trusted_usage_snapshots", return_value={}),
            patch.object(
                switcher,
                "_refresh_switchable_usage_cache",
            ) as mock_refresh,
        ):
            monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )
            monitor.monitor_step(
                switcher,
                state,
                poll_seconds=0,
                perform_switch=perform,
            )

        mock_refresh.assert_called_once()
        assert state.usage_cache_warmed is True


@pytest.mark.usefixtures("stub_live_claude")
class TestMonitorStepSelfHeals:
    """Environmental failures in the poll body map to backoff, never escape.

    The adapters (CLI loop, TUI loop, service) treat an exception from
    ``monitor_step`` as fatal. A concurrent switch legitimately holds the
    file lock past the 10s default (in-lock network refresh), and on Windows
    a store read can race an ``os.replace`` writer into a transient OSError —
    neither may kill the monitor.
    """

    def test_wedged_lock_during_usage_resolution_backs_off(
        self, temp_home: Path, caplog
    ):
        """A held FileLock reached via the usage-cache merge must degrade to
        the usage-unavailable backoff (it used to escape as LockError)."""
        switcher = bootstrap_switchable_accounts(temp_home, num_accounts=1)
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "at-live",
                        "refreshToken": "rt-live",
                        "expiresAt": 9_999_999_999_000,
                    }
                }
            )
        )
        switcher.set_auto_switch_config(enabled=True, threshold=95)
        state = monitor.MonitorRuntimeState()

        blocker = FileLock(switcher.lock_file)
        assert blocker.acquire()
        try:
            with (
                patch(
                    "claude_swap.switcher.FileLock",
                    side_effect=lambda p: FileLock(p, timeout=0.1),
                ),
                patch(
                    "claude_swap.oauth.fetch_usage_for_account",
                    return_value={"five_hour": {"pct": 50.0}},
                ),
                caplog.at_level(logging.WARNING, logger="claude-swap"),
            ):
                result = monitor.monitor_step(switcher, state, poll_seconds=0)
        finally:
            blocker.release()

        assert result.kind == "usage_unavailable"
        assert state.consecutive_failures == 1
        assert any(
            "monitor poll failed" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_transient_read_error_backs_off_then_recovers(self, temp_home: Path):
        """A sharing-violation-shaped OSError (Windows os.replace race on
        sequence.json) must cost one backoff cycle, not the process."""
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        with patch.object(
            switcher,
            "get_auto_switch_config",
            side_effect=PermissionError("sharing violation"),
        ):
            result = monitor.monitor_step(switcher, state, poll_seconds=0)

        assert result.kind == "usage_unavailable"
        assert state.consecutive_failures == 1

        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                return_value=AutoSwitchConfig(enabled=True, threshold=95),
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=42.0),
        ):
            result = monitor.monitor_step(switcher, state, poll_seconds=0)

        assert result.kind == "polled"
        assert state.consecutive_failures == 0


# --------------------------------------------------------------------------- #
# Monitor PID lifecycle (launchd exclusivity)                                  #
# --------------------------------------------------------------------------- #


class TestMonitorPidLifecycle:
    def test_acquire_overwrites_stale_dead_pid(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("99999", encoding="utf-8")

        with patch("claude_swap.monitor._pid_is_running", return_value=False):
            existing = monitor._acquire_monitor_pid(pid_path)

        assert existing is None
        assert pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_read_running_pid_rejects_live_non_monitor_process(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("4242", encoding="utf-8")

        with (
            patch("claude_swap.monitor.os.kill"),
            patch(
                "claude_swap.monitor._pid_command",
                return_value="/usr/bin/python sleep 999",
            ),
        ):
            assert monitor._read_running_pid(pid_path) is None

    def test_pid_is_running_accepts_cli_monitor_entrypoint(self):
        with (
            patch("claude_swap.monitor.os.kill"),
            patch(
                "claude_swap.monitor._pid_command",
                return_value="/usr/local/bin/cswap --monitor",
            ),
        ):
            assert monitor._pid_is_running(4242) is True

    def test_pid_is_running_accepts_bare_tui_entrypoint(self):
        # The TUI in-process monitor's argv is just ``cswap`` (no --monitor),
        # yet it writes the PID file and must be visible to the guard so a
        # later CLI/launchd run does not start a second monitor.
        with (
            patch("claude_swap.monitor.os.kill"),
            patch(
                "claude_swap.monitor._pid_command",
                return_value="/usr/local/bin/cswap",
            ),
        ):
            assert monitor._pid_is_running(4242) is True

    def test_pid_is_running_rejects_unrelated_reused_pid(self):
        with (
            patch("claude_swap.monitor.os.kill"),
            patch(
                "claude_swap.monitor._pid_command",
                return_value="vim notes.txt",
            ),
        ):
            assert monitor._pid_is_running(4242) is False

    @pytest.mark.parametrize(
        "cmdline",
        [
            # R2 minor: fuzzy substring matching mistook these recycled PIDs
            # for the monitor holder and refused to start a real monitor.
            "vim claude-swap.py",
            "less notes-on-monitor.txt",
            "docker run monitoring-stack",
            "/usr/bin/python sleep 999",
            "python -m http.server 8000",
        ],
    )
    def test_pid_is_running_rejects_lookalike_cmdlines(self, cmdline: str):
        with (
            patch("claude_swap.monitor.os.kill"),
            patch("claude_swap.monitor._pid_command", return_value=cmdline),
        ):
            assert monitor._pid_is_running(4242) is False

    @pytest.mark.parametrize(
        "cmdline",
        [
            "/usr/bin/python3.12 -m claude_swap --monitor",
            "python -m claude_swap --monitor --service-monitor",
            '"C:\\Program Files\\Python312\\pythonw.exe" -m claude_swap --monitor',
        ],
    )
    def test_pid_is_running_accepts_module_entrypoints(self, cmdline: str):
        with (
            patch("claude_swap.monitor.os.kill"),
            patch("claude_swap.monitor._pid_command", return_value=cmdline),
        ):
            assert monitor._pid_is_running(4242) is True

    def test_pid_is_running_windows_uses_tasklist_not_os_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # On Windows os.kill(pid, 0) would TerminateProcess, so the guard must
        # route through tasklist instead of the POSIX os.kill/ps path.
        monkeypatch.setattr(monitor.os, "name", "nt")
        kill = MagicMock(side_effect=AssertionError("os.kill must not run on nt"))
        monkeypatch.setattr(monitor.os, "kill", kill)
        with patch(
            "claude_swap.monitor._tasklist_image",
            return_value=(True, "cswap.exe"),
        ):
            assert monitor._pid_is_running(4242) is True

    def test_pid_is_running_windows_rejects_absent_pid(self):
        # tasklist ran, no process owns the PID → not running.
        with patch("claude_swap.monitor._tasklist_image", return_value=(True, None)):
            assert monitor._pid_is_running_windows(4242) is False

    def test_pid_is_running_windows_rejects_unrelated_image(self):
        with patch(
            "claude_swap.monitor._tasklist_image",
            return_value=(True, "notepad.exe"),
        ):
            assert monitor._pid_is_running_windows(4242) is False

    def test_pid_is_running_windows_python_host_checked_by_cmdline(self):
        # R2 minor: a python.exe image alone must not be treated as the
        # holder — the argv decides.
        with (
            patch(
                "claude_swap.monitor._tasklist_image",
                return_value=(True, "python.exe"),
            ),
            patch(
                "claude_swap.monitor._windows_cmdline",
                return_value=(True, "python.exe -m claude_swap --monitor"),
            ),
        ):
            assert monitor._pid_is_running_windows(4242) is True
        with (
            patch(
                "claude_swap.monitor._tasklist_image",
                return_value=(True, "python.exe"),
            ),
            patch(
                "claude_swap.monitor._windows_cmdline",
                return_value=(True, "python.exe -m http.server"),
            ),
        ):
            assert monitor._pid_is_running_windows(4242) is False

    def test_pid_is_running_windows_python_host_kept_when_cmdline_unavailable(self):
        # Command line undeterminable: keep the conservative bias rather than
        # allow a second monitor.
        with (
            patch(
                "claude_swap.monitor._tasklist_image",
                return_value=(True, "python.exe"),
            ),
            patch(
                "claude_swap.monitor._windows_cmdline",
                return_value=(False, None),
            ),
        ):
            assert monitor._pid_is_running_windows(4242) is True

    def test_pid_is_running_windows_assumes_holder_when_tasklist_unavailable(
        self,
    ):
        # tasklist missing → liveness undeterminable → conservatively the holder.
        with patch("claude_swap.monitor._tasklist_image", return_value=(False, None)):
            assert monitor._pid_is_running_windows(4242) is True

    def test_tasklist_image_parses_csv_and_no_task_line(self):
        running = MagicMock(returncode=0, stdout='"cswap.exe","4242","Console"\n')
        with patch("claude_swap.monitor.subprocess.run", return_value=running):
            assert monitor._tasklist_image(4242) == (True, "cswap.exe")
        absent = MagicMock(
            returncode=0,
            stdout="INFO: No tasks are running which match the specified criteria.\n",
        )
        with patch("claude_swap.monitor.subprocess.run", return_value=absent):
            assert monitor._tasklist_image(4242) == (True, None)
        with patch("claude_swap.monitor.subprocess.run", side_effect=OSError):
            assert monitor._tasklist_image(4242) == (False, None)

    def test_tasklist_image_handles_quoted_comma_fields(self):
        # CSV semantics: a quoted image name containing a comma must not
        # shear the row apart (a naive split returned a fragment as the image).
        running = MagicMock(
            returncode=0,
            stdout='"my, app.exe","4242","Console","1","10,000 K"\n',
        )
        with patch("claude_swap.monitor.subprocess.run", return_value=running):
            assert monitor._tasklist_image(4242) == (True, "my, app.exe")

    @pytest.mark.parametrize(
        "notice",
        [
            # tasklist localizes its no-match notice; only English says INFO:.
            "INFORMATION: Es werden keine Aufgaben mit den angegebenen "
            "Kriterien ausgeführt.\n",
            "情報: 指定された条件に一致するタスクは実行されていません。\n",
        ],
    )
    def test_tasklist_image_no_match_is_structural_not_localized(
        self, notice: str
    ):
        # "No process owns the PID" must be decided by the absence of a data
        # row carrying the queried PID, not by an English text prefix.
        absent = MagicMock(returncode=0, stdout=notice)
        with patch("claude_swap.monitor.subprocess.run", return_value=absent):
            assert monitor._tasklist_image(4242) == (True, None)

    def test_tasklist_timeout_keeps_conservative_holder_bias(self):
        # A hung tasklist (WMI-backed and able to stall forever) must map to
        # "undeterminable" instead of wedging the supervised monitor at
        # startup, where IgnoreNew would swallow every watchdog re-fire.
        with patch(
            "claude_swap.monitor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tasklist", timeout=10),
        ):
            assert monitor._tasklist_image(4242) == (False, None)
            assert monitor._pid_is_running_windows(4242) is True

    def test_windows_cmdline_timeout_maps_to_undeterminable(self):
        # (False, None) is the "query never ran" shape; the caller keeps the
        # conservative holder bias for it (covered above).
        with patch(
            "claude_swap.monitor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=10),
        ):
            assert monitor._windows_cmdline(4242) == (False, None)

    def test_windows_pid_probes_use_system_root_binaries(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Bare tasklist/powershell names resolve through PATH, which a
        # user-writable PATH entry can hijack — resolve under %SystemRoot%
        # like launchd resolves launchctl absolutely.
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        argvs: list[list[str]] = []

        def fake_run(argv, **kwargs):
            argvs.append(list(argv))
            return MagicMock(returncode=0, stdout="")

        with patch("claude_swap.monitor.subprocess.run", side_effect=fake_run):
            monitor._tasklist_image(4242)
            monitor._windows_cmdline(4242)

        assert argvs[0][0] == r"C:\Windows\System32\tasklist.exe"
        assert argvs[1][0] == (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        )

    @pytest.mark.parametrize(
        "probe",
        [monitor._tasklist_image, monitor._windows_cmdline],
    )
    def test_windows_pid_probes_bounded_and_windowless(
        self, monkeypatch: pytest.MonkeyPatch, probe
    ):
        # Simulate the win32-only constant so the flag plumbing is asserted on
        # every platform; windows-latest exercises the real value.
        monkeypatch.setattr(monitor, "_NO_WINDOW", 0x08000000)
        captured: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return MagicMock(returncode=0, stdout="")

        with patch("claude_swap.monitor.subprocess.run", side_effect=fake_run):
            probe(4242)

        assert captured["timeout"] == monitor._WINDOWS_PID_PROBE_TIMEOUT
        assert captured["creationflags"] == 0x08000000

    def test_run_cli_monitor_starts_when_pidfile_has_reused_pid(
        self,
        temp_home: Path,
        capsys,
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher.set_auto_switch_config(enabled=True)
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("4242", encoding="utf-8")

        with (
            patch("claude_swap.monitor.os.kill"),
            patch(
                "claude_swap.monitor._pid_command",
                return_value="/usr/bin/python sleep 999",
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=10.0),
        ):
            code = monitor.run_cli_monitor(switcher, poll_seconds=1, once=True)

        out = capsys.readouterr().out
        assert code == 0
        assert "Auto-switch monitor (Beta)" in out
        assert "already running" not in out
        assert "threshold 95%" in out

    def test_acquire_monitor_pid_handles_concurrent_create(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("12345", encoding="utf-8")

        with (
            patch("claude_swap.monitor._read_running_pid", side_effect=[None, 12345]),
            patch("claude_swap.monitor._pid_is_running", return_value=False),
            patch("claude_swap.monitor.os.open", side_effect=FileExistsError),
        ):
            existing = monitor._acquire_monitor_pid(pid_path)

        assert existing == 12345

    def test_acquire_stale_cleanup_preserves_concurrent_winner(
        self, temp_home: Path
    ):
        """R2-M3: two starters race on a stale PID file; the loser's cleanup
        must not delete the winner's freshly written PID file.

        Reproduces the TOCTOU: process A judges the file stale, process B then
        completes the whole acquisition (cleanup + O_EXCL create), and A
        resumes with its own cleanup. The old unconditional unlink deleted B's
        fresh file and let A's O_EXCL succeed — two monitor singletons. The
        read-verify-unlink cleanup sees content that no longer matches what A
        judged stale and leaves B's file alone; A then reports B as the owner.
        """
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("99999999", encoding="utf-8")  # stale, dead owner

        real_cleanup = monitor._remove_stale_pid_file
        b_won = {"done": False}

        def cleanup_with_b_winning_first(path):
            # B runs its entire acquisition inside A's window between the
            # staleness read and the stale-file cleanup.
            if not b_won["done"]:
                b_won["done"] = True
                assert monitor._acquire_monitor_pid(path) is None
                assert path.read_text(encoding="utf-8") == str(os.getpid())
            return real_cleanup(path)

        with (
            patch(
                "claude_swap.monitor._pid_is_running",
                side_effect=lambda pid: pid == os.getpid(),
            ),
            patch(
                "claude_swap.monitor._remove_stale_pid_file",
                side_effect=cleanup_with_b_winning_first,
            ),
        ):
            owner_seen_by_a = monitor._acquire_monitor_pid(pid_path)

        # A must defer to B — not believe it owns the singleton too.
        assert owner_seen_by_a == os.getpid()
        assert pid_path.read_text(encoding="utf-8") == str(os.getpid())

    def test_remove_stale_pid_file_only_removes_verified_content(
        self, temp_home: Path, monkeypatch
    ):
        """The reclaim discards the file only when the captured bytes match
        the ones it judged stale; a concurrent winner's fresh file — landed
        inside the reclaim window — is put back."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("424242", encoding="utf-8")

        # Dead owner, stable content: removed.
        with patch("claude_swap.monitor._pid_is_running", return_value=False):
            monitor._remove_stale_pid_file(pid_path)
        assert not pid_path.exists()

        # Dead owner, but a concurrent winner replaces the file inside the
        # reclaim window (after the staleness read): the winner's file
        # survives with its content intact.
        pid_path.write_text("424242", encoding="utf-8")
        real_read_text = Path.read_text
        swapped = {"done": False}

        def racing_read_text(self_path, *args, **kwargs):
            text = real_read_text(self_path, *args, **kwargs)
            if self_path == pid_path and not swapped["done"]:
                swapped["done"] = True
                self_path.unlink()
                self_path.write_text("31337", encoding="utf-8")
            return text

        monkeypatch.setattr(Path, "read_text", racing_read_text)
        with patch("claude_swap.monitor._pid_is_running", return_value=False):
            monitor._remove_stale_pid_file(pid_path)
        monkeypatch.setattr(Path, "read_text", real_read_text)
        assert pid_path.read_text(encoding="utf-8") == "31337"

        # Live owner: never removed.
        with patch("claude_swap.monitor._pid_is_running", return_value=True):
            monitor._remove_stale_pid_file(pid_path)
        assert pid_path.exists()

    def test_reclaim_captures_atomically_instead_of_unlinking_in_place(
        self, temp_home: Path, monkeypatch
    ):
        """The stale file must leave the contended path via an atomic rename
        (claim), never an in-place unlink: the unlink is what left a window
        — between the verify read and the unlink — where a concurrent
        winner's fresh PID file could still be deleted."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("424242", encoding="utf-8")

        real_unlink = Path.unlink
        unlinked: list[Path] = []

        def recording_unlink(self_path, *args, **kwargs):
            unlinked.append(self_path)
            return real_unlink(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", recording_unlink)
        with patch("claude_swap.monitor._pid_is_running", return_value=False):
            monitor._remove_stale_pid_file(pid_path)

        assert not pid_path.exists()
        assert pid_path not in unlinked

    def test_reclaim_loser_exits_quietly_on_lost_rename(self, temp_home: Path):
        """Two reclaimers race: the loser's rename raises FileNotFoundError
        and it must exit the race without touching anything."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("424242", encoding="utf-8")

        with (
            patch("claude_swap.monitor._pid_is_running", return_value=False),
            patch(
                "claude_swap.monitor.os.rename",
                side_effect=FileNotFoundError,
            ),
        ):
            monitor._remove_stale_pid_file(pid_path)

        assert pid_path.read_text(encoding="utf-8") == "424242"

    def test_reclaim_restores_via_rename_on_windows(
        self, temp_home: Path, monkeypatch
    ):
        """The restore path uses os.rename on nt (no-overwrite semantics
        there); the captured fresh file must land back on the pid path."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("424242", encoding="utf-8")
        monkeypatch.setattr(monitor.os, "name", "nt")

        real_read_text = Path.read_text
        swapped = {"done": False}

        def racing_read_text(self_path, *args, **kwargs):
            text = real_read_text(self_path, *args, **kwargs)
            if self_path == pid_path and not swapped["done"]:
                swapped["done"] = True
                self_path.unlink()
                self_path.write_text("31337", encoding="utf-8")
            return text

        monkeypatch.setattr(Path, "read_text", racing_read_text)
        with patch("claude_swap.monitor._pid_is_running", return_value=False):
            monitor._remove_stale_pid_file(pid_path)
        monkeypatch.setattr(Path, "read_text", real_read_text)

        assert pid_path.read_text(encoding="utf-8") == "31337"
        assert not list(switcher.backup_dir.glob("*.reclaim-*"))

    def test_reclaim_restore_defers_to_newer_winner(
        self, temp_home: Path, monkeypatch
    ):
        """Restore refuses to overwrite: when yet another starter recreated
        the path before the restore lands, the captured copy is dropped and
        no reclaim temp file is left behind."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("424242", encoding="utf-8")

        real_read_text = Path.read_text
        swapped = {"done": False}

        def racing_read_text(self_path, *args, **kwargs):
            text = real_read_text(self_path, *args, **kwargs)
            if self_path == pid_path and not swapped["done"]:
                swapped["done"] = True
                self_path.unlink()
                self_path.write_text("31337", encoding="utf-8")
            return text

        monkeypatch.setattr(Path, "read_text", racing_read_text)
        with (
            patch("claude_swap.monitor._pid_is_running", return_value=False),
            patch(
                "claude_swap.monitor.os.link",
                side_effect=FileExistsError,
            ),
        ):
            monitor._remove_stale_pid_file(pid_path)
        monkeypatch.setattr(Path, "read_text", real_read_text)

        assert not list(switcher.backup_dir.glob("*.reclaim-*"))

    def test_run_cli_monitor_releases_pid_in_finally(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher.set_auto_switch_config(enabled=True)
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"

        with (
            patch.object(
                ClaudeAccountSwitcher,
                "_live_default_mode_claude_pids",
                return_value=[99999],
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=10.0),
        ):
            monitor.run_cli_monitor(switcher, poll_seconds=1, once=True)

        assert not pid_path.exists()


# --------------------------------------------------------------------------- #
# CLI monitor                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("stub_live_claude")
class TestCliAutoMonitor:
    def test_does_not_start_when_existing_pid_is_running(self, temp_home: Path, capsys):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("12345", encoding="utf-8")

        with patch("claude_swap.monitor._pid_is_running", return_value=True):
            code = monitor.run_cli_monitor(switcher, once=True)

        out = capsys.readouterr().out
        assert code == 0
        assert "Status:" in out
        assert "Auto-switch monitor (Beta)" in out
        assert "already running (pid 12345)" in out

    def test_service_monitor_pid_collision_is_retryable_failure(
        self,
        temp_home: Path,
        capsys,
        monkeypatch: pytest.MonkeyPatch,
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
        pid_path.write_text("12345", encoding="utf-8")
        monkeypatch.setenv(monitor.SERVICE_MONITOR_ENV_KEY, "1")

        with patch("claude_swap.monitor._pid_is_running", return_value=True):
            code = monitor.run_cli_monitor(switcher, once=True)

        out = capsys.readouterr().out
        assert code == 75
        assert "already running (pid 12345)" in out

    def test_once_switches_when_threshold_reached(self, temp_home: Path, capsys):
        switcher = ClaudeAccountSwitcher()

        with (
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch.object(switcher, "switch") as mock_switch,
        ):
            code = monitor.run_cli_monitor(
                switcher,
                poll_seconds=1,
                once=True,
            )

        out = capsys.readouterr().out
        assert code == 0
        assert "Auto-switch monitor (Beta)" in out
        assert "threshold 95%" in out
        assert "active usage:" in out
        mock_switch.assert_called_once()

    def test_once_e2e_holds_below_threshold(self, temp_home: Path, capsys):
        switcher = ClaudeAccountSwitcher()

        with (
            patch.object(switcher, "get_active_usage_pct", return_value=40.0),
            patch.object(switcher, "switch") as mock_switch,
        ):
            code = monitor.run_cli_monitor(switcher, poll_seconds=1, once=True)

        out = capsys.readouterr().out
        assert code == 0
        assert "active usage:" in out
        assert "40%" in out
        assert "switching account" not in out
        mock_switch.assert_not_called()

    def test_restores_sigterm_handler_after_once_run(self, temp_home: Path):
        import signal

        switcher = ClaudeAccountSwitcher()
        original = signal.getsignal(signal.SIGTERM)

        with patch.object(switcher, "get_active_usage_pct", return_value=10.0):
            monitor.run_cli_monitor(switcher, poll_seconds=1, once=True)

        assert signal.getsignal(signal.SIGTERM) == original

    def test_run_cli_monitor_exits_143_on_sigterm(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """SIGTERM during the loop raises _MonitorStopped → return code 143.

        Locks in the launchd-bootout contract: `launchctl bootout` sends
        SIGTERM, and the supervised monitor must exit cleanly (not be
        force-killed). If a future refactor removes the signal.signal()
        installation, the previously-passing tests in this class would
        still pass — this one would not.
        """
        import signal

        switcher = ClaudeAccountSwitcher()
        installed: dict[str, object] = {}
        original = signal.getsignal(signal.SIGTERM)
        real_signal = signal.signal

        def capture_signal(signum, handler):
            if signum == signal.SIGTERM:
                installed["handler"] = handler
            return real_signal(signum, handler)

        # Fire SIGTERM at the first time.sleep — the captured handler
        # raises _MonitorStopped inside the try block.
        def fire_sigterm_then_noop(_seconds):
            handler = installed.pop("handler", None)
            if handler is not None:
                handler(signal.SIGTERM, None)

        monkeypatch.setattr("claude_swap.monitor.signal.signal", capture_signal)
        monkeypatch.setattr("claude_swap.monitor.time.sleep", fire_sigterm_then_noop)

        with patch.object(switcher, "get_active_usage_pct", return_value=10.0):
            rc = monitor.run_cli_monitor(switcher, poll_seconds=1, once=False)

        assert rc == 143
        # finally-block restored the prior handler (not the test's stop_monitor).
        assert signal.getsignal(signal.SIGTERM) == original

    def test_logs_poll_and_switch_at_threshold(self, temp_home: Path, caplog):
        switcher = ClaudeAccountSwitcher()
        switched = {"n": 0, "intent": None}

        def _do_switch(intent) -> bool:
            switched["n"] += 1
            switched["intent"] = intent
            return True

        caplog.set_level(logging.INFO, logger="claude-swap")
        with (
            patch.object(switcher, "get_active_usage_pct", return_value=96.0),
            patch.object(switcher, "switch", side_effect=_do_switch),
        ):
            monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        records = [r for r in caplog.records if r.name == "claude-swap"]
        msgs = [r.getMessage() for r in records]
        assert any("monitor poll" in m and "96" in m for m in msgs), msgs
        assert any("monitor threshold reached" in m for m in msgs), msgs
        assert any("monitor switched account" in m for m in msgs), msgs
        assert switched["n"] == 1
        from claude_swap.models import BackgroundAutoSwitchIntent

        assert isinstance(switched["intent"], BackgroundAutoSwitchIntent)
        assert switched["intent"].decision.threshold == 95
        assert switched["intent"].decision.active_usage_pct == 96.0

    def test_logs_warning_when_switch_fails(self, temp_home: Path, caplog):
        switcher = ClaudeAccountSwitcher()

        def _raise(_intent) -> bool:
            raise ClaudeSwitchError("boom")

        caplog.set_level(logging.INFO, logger="claude-swap")
        with (
            patch.object(switcher, "get_active_usage_pct", return_value=99.0),
            patch.object(switcher, "switch", side_effect=_raise),
        ):
            monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        warnings = [
            r
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert warnings, [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
        assert any("monitor switch failed" in r.getMessage() for r in warnings)
        assert any("boom" in r.getMessage() for r in warnings)

    def test_cli_stdout_omits_switching_before_switch_failed(
        self,
        temp_home: Path,
        capsys,
    ):
        switcher = ClaudeAccountSwitcher()

        def _raise(_intent) -> bool:
            raise ClaudeSwitchError("boom")

        with (
            patch.object(switcher, "get_active_usage_pct", return_value=99.0),
            patch.object(switcher, "switch", side_effect=_raise),
        ):
            monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        out = capsys.readouterr().out
        assert "switch failed: boom" in out
        assert "switching account" not in out

    def test_monitor_stops_when_config_disabled_mid_cycle(self, temp_home: Path):
        """Config is re-read each cycle: disabling via TUI stops switching."""
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                side_effect=[
                    AutoSwitchConfig(enabled=True, threshold=98),
                    AutoSwitchConfig(enabled=False, threshold=98),
                ],
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=99.0),
            patch.object(switcher, "switch") as mock_switch,
        ):
            code = monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        assert code == 0
        mock_switch.assert_not_called()

    def test_monitor_picks_up_threshold_change_at_poll_time(self, temp_home: Path):
        """Config is re-read each cycle: lowering threshold takes effect immediately."""
        switcher = ClaudeAccountSwitcher()
        # Startup: threshold=98; in-loop: threshold lowered to 50.
        # Usage=60% → below 98 (no switch), but above 50 (switch).
        with (
            patch.object(
                switcher,
                "get_auto_switch_config",
                side_effect=[
                    AutoSwitchConfig(enabled=True, threshold=98),
                    AutoSwitchConfig(enabled=True, threshold=50),
                ],
            ),
            patch.object(switcher, "get_active_usage_pct", return_value=60.0),
            patch.object(switcher, "switch") as mock_switch,
        ):
            code = monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        assert code == 0
        mock_switch.assert_called_once()

    def test_monitor_idles_when_no_live_claude_sessions(
        self,
        temp_home: Path,
        caplog,
    ):
        """With zero default-mode Claude Code processes running there is
        nothing burning tokens, so the monitor skips the usage API call
        entirely and idles at the polling ceiling.  Override the class-level
        fixture's return value to simulate the idle state.
        """
        switcher = ClaudeAccountSwitcher()
        caplog.set_level(logging.INFO, logger="claude-swap")

        with (
            patch.object(
                ClaudeAccountSwitcher,
                "_live_default_mode_claude_pids",
                return_value=[],
            ),
            patch.object(switcher, "get_active_usage_pct") as mock_usage,
            patch.object(switcher, "switch") as mock_switch,
        ):
            monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        # Usage API is NOT consulted while idle — that's the whole point of
        # the optimisation; otherwise we'd waste an HTTP call per cycle.
        mock_usage.assert_not_called()
        mock_switch.assert_not_called()
        msgs = [r.getMessage() for r in caplog.records if r.name == "claude-swap"]
        assert any("no live Claude Code sessions" in m for m in msgs), msgs

    def test_monitor_dedups_repeating_switch_errors(self, temp_home, caplog):
        """A permanently broken slot must not spam an identical WARNING every
        poll cycle — only the first occurrence is logged at WARNING; repeats
        drop to DEBUG so launchd's monitor.err stays scannable."""
        switcher = ClaudeAccountSwitcher()
        caplog.set_level(logging.DEBUG, logger="claude-swap")

        sleeps: list[int] = []

        def fake_sleep(_seconds):
            sleeps.append(_seconds)
            if len(sleeps) >= 3:
                raise monitor._MonitorStopped

        def boom(_intent):
            raise ClaudeSwitchError("slot 2 token expired and refresh failed")

        with (
            patch.object(switcher, "get_active_usage_pct", return_value=99.0),
            patch.object(switcher, "switch", side_effect=boom),
            patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep),
        ):
            # Explicit poll_seconds=60 so the test does not silently depend on
            # the module-level default; the value itself doesn't matter here
            # because time.sleep is mocked.
            monitor.run_cli_monitor(switcher, poll_seconds=60)

        warnings = [
            r
            for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.WARNING
            and "switch failed" in r.getMessage()
        ]
        debugs = [
            r
            for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.DEBUG
            and "switch failed (repeat)" in r.getMessage()
        ]
        # First identical failure surfaces; subsequent ones drop to debug.
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert len(debugs) >= 1, [r.getMessage() for r in debugs]

    def test_monitor_backs_off_on_consecutive_usage_failures(
        self,
        temp_home: Path,
        caplog,
    ):
        """When the usage API returns None, the failure counter increments and
        the next poll interval grows exponentially.  We exercise the in-process
        counter by running the loop multiple times through the (mocked) sleep
        boundary, verifying logged backoff values.
        """
        switcher = ClaudeAccountSwitcher()
        caplog.set_level(logging.WARNING, logger="claude-swap")

        sleeps: list[int] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            # Bail out after 3 backoffs so the test always terminates.
            if len(sleeps) >= 3:
                raise monitor._MonitorStopped

        with (
            patch.object(switcher, "get_active_usage_pct", return_value=None),
            patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep),
        ):
            # Explicit poll_seconds=60 (the production default) so we are not
            # silently coupled to a module-level constant.
            monitor.run_cli_monitor(switcher, poll_seconds=60)

        # Backoffs follow BASE * 2^(n-1) clamped at MAX:
        # n=1 → 5, n=2 → 10, n=3 → 20.
        assert sleeps == [5, 10, 20]

        # Each failure is logged as a warning naming the consecutive count.
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any("failures=1" in m for m in warnings), warnings
        assert any("failures=3" in m for m in warnings), warnings


# --------------------------------------------------------------------------- #
# Adaptive polling pure functions                                              #
# --------------------------------------------------------------------------- #


class TestNextPollInterval:
    """Lock the behaviour contract of the velocity-based interval picker.

    These tests cover the design assumptions that callers (the monitor loop)
    can safely rely on: bounds, idle handling, near-trigger override, and
    the test-friendly t_max=0 degradation.
    """

    def test_returns_zero_when_t_max_zero(self):
        """Test contract: poll_seconds=0 propagates as a 0-second sleep so
        existing once=True fixtures finish in milliseconds."""
        assert monitor._next_poll_interval(50.0, 40.0, 1.0, 95, t_max=0) == 0

    def test_returns_t_max_without_baseline(self):
        """First iteration, no previous sample → no velocity → idle at max."""
        assert (
            monitor._next_poll_interval(50.0, None, 0.0, 95)
            == monitor.MONITOR_POLL_SECONDS
        )

    def test_returns_t_max_when_velocity_zero(self):
        """User idle: same pct, no token consumption — slow poll is correct."""
        out = monitor._next_poll_interval(50.0, 50.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_returns_t_max_when_velocity_negative(self):
        """Post-switch drop: clamps to idle rather than computing negative ETA."""
        out = monitor._next_poll_interval(20.0, 50.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_returns_t_min_at_near_trigger_ratio(self):
        """At ≥ NEAR_TRIGGER_RATIO * threshold, ignore velocity and force the
        floor.  For threshold=95 and ratio=0.95 the override fires at 90.25%.
        """
        out = monitor._next_poll_interval(91.0, 50.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_near_trigger_override_fires_even_when_velocity_zero(self):
        """The override doesn't care about velocity — at the final approach a
        single bursty prompt can blow through the remaining budget."""
        out = monitor._next_poll_interval(94.0, 94.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_near_trigger_after_baseline_reset(self):
        """Post-switch baseline reset at high usage must not skip the floor."""
        out = monitor._next_poll_interval(96.0, None, 0.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_predicted_interval_within_bounds(self):
        """Positive velocity: schedule the next poll well before predicted
        threshold crossing.  Result must land inside [MIN, MAX]."""
        out = monitor._next_poll_interval(60.0, 50.0, 60.0, 95, t_max=120)
        assert monitor.MONITOR_POLL_SECONDS_MIN <= out <= 120

    def test_high_velocity_shrinks_to_t_min(self):
        """Pathological velocity (10% in 1s) → ETA tiny → clamped to floor."""
        out = monitor._next_poll_interval(60.0, 50.0, 1.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_low_velocity_caps_at_t_max(self):
        """Very slow burn → ETA huge → clamped to ceiling."""
        out = monitor._next_poll_interval(50.0, 49.9, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_respects_custom_t_max(self):
        """Test fixtures override t_max via the run_cli_monitor poll_seconds
        kwarg; the picker must honour the smaller ceiling."""
        out = monitor._next_poll_interval(50.0, 49.9, 60.0, 95, t_max=15)
        assert out == 15


class TestNextPollIntervalMulti:
    """Per-window interval picker — the most-urgent window wins."""

    def test_fast_window_not_masked_by_flat_higher_window(self):
        """The 2026-06-24 bug: a flat, higher 7d window must not hide a fast 5h
        climb. The aggregate max would sit at 87% (flat → t_max); per-window the
        rising 5h must drive a short interval."""
        current = {"seven_day": 87.0, "five_hour": 90.0}
        last = {"seven_day": 87.0, "five_hour": 75.0}  # 5h +15 over the gap
        out = monitor._next_poll_interval_multi(current, last, 60.0, 98)
        # The old collapsed max(5h,7d)=87 (flat) would have returned t_max(60).
        assert out < monitor.MONITOR_POLL_SECONDS, (
            f"fast 5h climb must shorten the interval, got {out}"
        )

    def test_matches_single_window_when_one_window(self):
        """With a lone aggregate window it equals the scalar picker."""
        multi = monitor._next_poll_interval_multi(
            {"max": 60.0},
            {"max": 50.0},
            60.0,
            95,
        )
        single = monitor._next_poll_interval(60.0, 50.0, 60.0, 95)
        assert multi == single

    def test_empty_falls_back_to_t_max(self):
        out = monitor._next_poll_interval_multi({}, {}, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_near_trigger_in_any_window_forces_floor(self):
        """A 5h in the near-trigger band forces the floor even if 7d is calm."""
        current = {"seven_day": 40.0, "five_hour": 94.0}
        out = monitor._next_poll_interval_multi(current, {}, 0.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN


@pytest.mark.usefixtures("stub_live_claude")
class TestSleepWakeAndHeartbeat:
    """Sleep/wake baseline reset and idle heartbeat for the schema-break safety
    net.  Both are operational guardrails for monitor.err observability.
    """

    def test_wake_gap_resets_baseline_and_last_switch_error(
        self,
        temp_home: Path,
        caplog,
    ):
        """A wall-clock gap > WAKE_GAP_MULTIPLIER * poll_seconds indicates
        sleep/wake.  Baselines and last_switch_error must reset so a stale
        velocity track doesn't bias the next interval and a new failure is
        not masked as a "repeat" of a pre-sleep failure.

        Concretely: drive an identical switch failure pre-sleep and
        post-sleep.  Without the reset, the post-sleep failure would drop
        to DEBUG via the dedup logic; with the reset, it MUST re-surface
        at WARNING so the on-call sees the new occurrence.
        """
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True, threshold=95)
        caplog.set_level(logging.DEBUG, logger="claude-swap")

        sleeps: list[int] = []

        def fake_sleep(_seconds):
            sleeps.append(_seconds)
            if len(sleeps) >= 2:
                raise monitor._MonitorStopped

        def fake_wall_time():
            # Pre-sleep ticks share the same wall value; after the first
            # sleep, jump forward by 10 hours to simulate macOS sleep/wake.
            return 1_000_000.0 if not sleeps else 1_000_000.0 + 10 * 3600

        # Same error on every switch attempt — exercises dedup interplay.
        def boom(_intent):
            raise ClaudeSwitchError("slot 2 token expired and refresh failed")

        with (
            patch.object(switcher, "get_active_usage_pct", return_value=99.0),
            patch.object(switcher, "switch", side_effect=boom),
            patch("claude_swap.monitor.time.time", side_effect=fake_wall_time),
            patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep),
        ):
            monitor.run_cli_monitor(switcher, poll_seconds=60)

        msgs = [r.getMessage() for r in caplog.records if r.name == "claude-swap"]
        assert any("wake-gap" in m and "resetting baselines" in m for m in msgs), msgs

        # Load-bearing assertion: post-wake, the identical failure must
        # re-surface at WARNING, not DEBUG.  Two WARNINGs (pre + post wake)
        # would prove last_switch_error actually reset.
        warning_failures = [
            r
            for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.WARNING
            and "monitor switch failed" in r.getMessage()
            and "(repeat)" not in r.getMessage()
        ]
        assert len(warning_failures) == 2, [
            (r.levelname, r.getMessage())
            for r in caplog.records
            if "switch failed" in r.getMessage()
        ]

    def test_idle_heartbeat_fires_after_long_idle_with_enabled_auto_switch(
        self,
        temp_home: Path,
        caplog,
    ):
        """When session detection returns zero PIDs for longer than the
        heartbeat threshold while auto-switch is enabled, emit a WARNING.
        Covers the schema-break failure mode (parser silently bails)
        without spamming on every idle poll.
        """
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True, threshold=95)
        caplog.set_level(logging.INFO, logger="claude-swap")

        sleeps: list[int] = []

        def fake_sleep(_s):
            sleeps.append(_s)
            if len(sleeps) >= 2:
                raise monitor._MonitorStopped

        def fake_wall_time():
            # Before the first sleep, all time.time() calls return the same
            # wall.  After the first sleep, jump past the heartbeat
            # threshold so the elif fires on iter 2.
            if not sleeps:
                return 1_000_000.0
            return 1_000_000.0 + monitor.MONITOR_IDLE_HEARTBEAT_SECONDS + 1

        with (
            patch.object(
                ClaudeAccountSwitcher,
                "_live_default_mode_claude_pids",
                return_value=[],
            ),
            patch("claude_swap.monitor.time.time", side_effect=fake_wall_time),
            patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep),
        ):
            monitor.run_cli_monitor(switcher, poll_seconds=60)

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any("monitor idle for" in m for m in warnings), warnings


class TestFailureBackoffSeconds:
    def test_zero_failures_returns_min(self):
        assert monitor._failure_backoff_seconds(0) == monitor.MONITOR_POLL_SECONDS_MIN

    def test_first_failure_returns_base(self):
        assert (
            monitor._failure_backoff_seconds(1) == monitor.MONITOR_FAILURE_BACKOFF_BASE
        )

    def test_doubles_each_failure(self):
        # BASE=5 → 5, 10, 20, 40
        assert monitor._failure_backoff_seconds(2) == 10
        assert monitor._failure_backoff_seconds(3) == 20
        assert monitor._failure_backoff_seconds(4) == 40

    def test_clamps_at_t_max(self):
        # n=5 → 80 raw, clamped to MAX=60
        assert monitor._failure_backoff_seconds(5) == monitor.MONITOR_POLL_SECONDS
        # Pathological n=20 stays at MAX, no integer overflow concerns.
        assert monitor._failure_backoff_seconds(20) == monitor.MONITOR_POLL_SECONDS

    def test_returns_zero_when_t_max_zero(self):
        """Test contract: poll_seconds=0 disables sleeps everywhere."""
        assert monitor._failure_backoff_seconds(3, t_max=0) == 0

    def test_respects_custom_t_max(self):
        """A caller-supplied t_max overrides the module default ceiling."""
        assert monitor._failure_backoff_seconds(10, t_max=20) == 20


class TestRetryAfterBackoff:
    """The monitor honours a server Retry-After on rate-limited usage fetches."""

    def _unavailable(self, retry_after, failures=0, poll_seconds=60):
        state = monitor.MonitorRuntimeState()
        state.consecutive_failures = failures
        return monitor._step_usage_unavailable(
            state,
            poll_seconds,
            0.0,
            95,
            "unavailable",
            logging.getLogger("claude-swap"),
            retry_after=retry_after,
        )

    def test_none_falls_back_to_failure_backoff(self):
        # No server hint → pure exponential backoff (first failure → BASE).
        result = self._unavailable(None)
        assert result.next_interval == monitor.MONITOR_FAILURE_BACKOFF_BASE

    def test_retry_after_overrides_shorter_backoff(self):
        # Server says wait 120s; that exceeds the failure backoff → honoured.
        result = self._unavailable(120)
        assert result.next_interval == 120

    def test_retry_after_capped(self):
        # A pathologically long Retry-After is clamped to the ceiling.
        result = self._unavailable(99999)
        assert result.next_interval == monitor.MONITOR_RETRY_AFTER_CAP

    def test_failure_backoff_wins_when_larger(self):
        # After many failures the backoff can exceed a tiny Retry-After.
        result = self._unavailable(1, failures=20)
        assert result.next_interval == monitor.MONITOR_POLL_SECONDS

    def test_switcher_reads_retry_after_from_rate_limited_entry(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=(
                    oauth.UsageFetchError(reason="rate_limited", retry_after="90"),
                    "rl",
                ),
            ),
        ):
            assert switcher.get_active_usage_retry_after() == 90

    def test_switcher_retry_after_none_when_not_rate_limited(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=({"five_hour": {"utilization": 10}}, "ok"),
            ),
        ):
            assert switcher.get_active_usage_retry_after() is None

    def test_switcher_reads_retry_after_from_masked_rate_limit_side_field(
        self,
        temp_home: Path,
    ):
        # A trusted prior usage row masks the active 429, but the codec stamped
        # the server Retry-After as a side field — the monitor must still see it,
        # decayed for the 10s that have elapsed since it was stamped.
        switcher = ClaudeAccountSwitcher()
        masked = {
            "five_hour": {"utilization": 80},
            "_cached_at": 1_000.0,
            "_last_rate_limit": {"retry_after": "90", "at": 1_000.0},
        }
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=(masked, "rl"),
            ),
            patch("claude_swap.switcher.time.time", return_value=1_010.0),
        ):
            assert switcher.get_active_usage_retry_after() == 80

    def test_switcher_retry_after_none_when_masked_window_elapsed(
        self,
        temp_home: Path,
    ):
        # Once the server window has fully elapsed, no backoff is reported even
        # though the side field is still present on the cached row.
        switcher = ClaudeAccountSwitcher()
        masked = {
            "five_hour": {"utilization": 80},
            "_cached_at": 1_000.0,
            "_last_rate_limit": {"retry_after": "90", "at": 1_000.0},
        }
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=(masked, "rl"),
            ),
            patch("claude_swap.switcher.time.time", return_value=1_200.0),
        ):
            assert switcher.get_active_usage_retry_after() is None
