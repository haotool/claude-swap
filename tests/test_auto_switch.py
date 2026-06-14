"""Tests for the auto-switch (Beta) feature.

Covers the switcher-side config/usage helpers and the TUI monitor logic.
Curses primitives are mocked exactly as in ``test_tui.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import monitor, tui
from claude_swap.exceptions import ClaudeSwitchError, ValidationError
from claude_swap.switcher import (
    DEFAULT_AUTO_SWITCH_THRESHOLD,
    ClaudeAccountSwitcher,
    _max_usage_pct,
)


def _stub_screen(rows: int = 30, cols: int = 100) -> MagicMock:
    screen = MagicMock()
    screen.getmaxyx.return_value = (rows, cols)
    return screen


def _login(temp_home: Path, email: str = "u@example.com") -> None:
    config = {"oauthAccount": {"emailAddress": email}}
    (temp_home / ".claude.json").write_text(json.dumps(config))


# --------------------------------------------------------------------------- #
# _max_usage_pct                                                               #
# --------------------------------------------------------------------------- #


class TestMaxUsagePct:
    def test_none_when_no_usage(self):
        assert _max_usage_pct(None) is None
        assert _max_usage_pct({}) is None
        assert _max_usage_pct("no credentials") is None

    def test_returns_highest_of_5h_7d(self):
        usage = {"five_hour": {"pct": 40}, "seven_day": {"pct": 95}}
        assert _max_usage_pct(usage) == 95.0

    def test_ignores_spend_entry(self):
        usage = {"five_hour": {"pct": 10}, "spend": {"pct": 99}}
        assert _max_usage_pct(usage) == 10.0

    def test_handles_missing_pct(self):
        assert _max_usage_pct({"five_hour": {}}) is None


# --------------------------------------------------------------------------- #
# Config persistence                                                          #
# --------------------------------------------------------------------------- #


class TestAutoSwitchConfig:
    def test_default_is_disabled(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        cfg = switcher.get_auto_switch_config()
        assert cfg == {
            "enabled": False,
            "threshold": DEFAULT_AUTO_SWITCH_THRESHOLD,
        }

    def test_enable_and_persist(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True)
        # A fresh instance reads the persisted value.
        assert ClaudeAccountSwitcher().get_auto_switch_config()["enabled"] is True

    def test_set_threshold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        cfg = switcher.set_auto_switch_config(threshold=80)
        assert cfg["threshold"] == 80
        assert switcher.get_auto_switch_config()["threshold"] == 80

    def test_partial_update_keeps_other_field(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True, threshold=70)
        switcher.set_auto_switch_config(threshold=60)
        cfg = switcher.get_auto_switch_config()
        assert cfg == {"enabled": True, "threshold": 60}

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
        with patch.object(switcher, "_read_credentials", return_value=creds), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=usage,
             ):
            assert switcher.get_active_usage_pct() == 96.0

    def test_none_when_api_unavailable(self, temp_home: Path):
        _login(temp_home)
        switcher = ClaudeAccountSwitcher()
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch.object(switcher, "_read_credentials", return_value=creds), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=None,
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
        screen = _stub_screen()
        # Enter on "Enable" (idx 0), then Esc to leave the settings screen.
        screen.getch.side_effect = [10, 27]
        tui._do_auto_switch(screen, switcher)
        assert switcher.get_auto_switch_config()["enabled"] is True

    def test_back_does_nothing(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [27]  # Esc immediately
        tui._do_auto_switch(screen, switcher)
        assert switcher.get_auto_switch_config()["enabled"] is False

    def test_set_threshold_via_prompt(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # Down to "Set threshold" (idx 1) + Enter, type "80" + Enter, then Esc.
        keys = [tui.curses.KEY_DOWN, 10]
        keys += [ord("8"), ord("0"), 10]
        keys += [27]
        screen.getch.side_effect = keys
        with patch("claude_swap.tui.curses.curs_set"):
            tui._do_auto_switch(screen, switcher)
        assert switcher.get_auto_switch_config()["threshold"] == 80

    def test_service_toggle_installs_on_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # Down to "Background service: Install" (idx 2) + Enter, then Esc.
        screen.getch.side_effect = [tui.curses.KEY_DOWN, tui.curses.KEY_DOWN, 10, 27]

        with patch("claude_swap.tui.sys.platform", "darwin"), \
             patch("claude_swap.tui._service_state", return_value="not installed"), \
             patch("claude_swap.tui._shell_out") as mock_shell:
            tui._do_auto_switch(screen, switcher)

        _stdscr_arg, fn = mock_shell.call_args.args
        assert _stdscr_arg is screen
        with patch("claude_swap.tui.service.install", return_value=0) as mock_install:
            fn()
        mock_install.assert_called_once_with(switcher)

    def test_service_toggle_uninstalls_on_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [tui.curses.KEY_DOWN, tui.curses.KEY_DOWN, 10, 27]

        with patch("claude_swap.tui.sys.platform", "darwin"), \
             patch("claude_swap.tui._service_state", return_value="loaded"), \
             patch("claude_swap.tui._shell_out") as mock_shell:
            tui._do_auto_switch(screen, switcher)

        _stdscr_arg, fn = mock_shell.call_args.args
        assert _stdscr_arg is screen
        with patch("claude_swap.tui.service.uninstall", return_value=0) as mock_uninstall:
            fn()
        mock_uninstall.assert_called_once_with(switcher)

    def test_service_status_shells_out(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # Down to "Background service: Show status" (idx 3) + Enter, then Esc.
        screen.getch.side_effect = [
            tui.curses.KEY_DOWN,
            tui.curses.KEY_DOWN,
            tui.curses.KEY_DOWN,
            10,
            27,
        ]

        with patch("claude_swap.tui._shell_out") as mock_shell:
            tui._do_auto_switch(screen, switcher)

        _stdscr_arg, fn = mock_shell.call_args.args
        assert _stdscr_arg is screen
        with patch("claude_swap.tui.service.status", return_value=0) as mock_status:
            fn()
        mock_status.assert_called_once_with(switcher)

    def test_service_status_shows_error_off_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [
            tui.curses.KEY_DOWN,
            tui.curses.KEY_DOWN,
            tui.curses.KEY_DOWN,
            10,
            27,
        ]

        with patch("claude_swap.tui.sys.platform", "linux"), \
             patch("claude_swap.tui._service_state", return_value="unsupported"), \
             patch("claude_swap.tui._show_message") as mock_message, \
             patch("claude_swap.tui._shell_out") as mock_shell:
            tui._do_auto_switch(screen, switcher)

        mock_message.assert_called_once()
        mock_shell.assert_not_called()

    def test_service_toggle_shows_error_off_macos(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [tui.curses.KEY_DOWN, tui.curses.KEY_DOWN, 10, 27]

        with patch("claude_swap.tui.sys.platform", "linux"), \
             patch("claude_swap.tui._service_state", return_value="unsupported"), \
             patch("claude_swap.tui._show_message") as mock_message:
            tui._do_auto_switch(screen, switcher)

        mock_message.assert_called_once()

    def test_service_menu_label_for_installed_but_not_loaded(self):
        assert tui._service_menu_label("installed but not loaded") == (
            "Background service: Uninstall"
        )


# --------------------------------------------------------------------------- #
# TUI monitor loop                                                            #
# --------------------------------------------------------------------------- #


class TestRunAutoMonitor:
    @pytest.fixture(autouse=True)
    def _stub_live_claude(self):
        with patch.object(
            ClaudeAccountSwitcher,
            "_live_default_mode_claude_pids",
            return_value=[99999],
        ):
            yield

    def test_quits_without_switching_below_threshold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [ord("q")]
        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=10.0), \
             patch("claude_swap.tui._auto_perform_switch") as mock_switch, \
             patch("claude_swap.tui._acquire_monitor_pid", return_value=None), \
             patch("claude_swap.tui._release_monitor_pid"), \
             patch("claude_swap.tui.curses.curs_set"):
            tui._run_auto_monitor(screen, switcher, threshold=95)
        mock_switch.assert_not_called()

    def test_switches_when_threshold_reached(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [ord("q")]
        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=96.0), \
             patch(
                 "claude_swap.tui._auto_perform_switch", return_value=True
             ) as mock_switch, \
             patch("claude_swap.tui._acquire_monitor_pid", return_value=None), \
             patch("claude_swap.tui._release_monitor_pid"), \
             patch("claude_swap.tui.curses.curs_set"):
            tui._run_auto_monitor(screen, switcher, threshold=95)
        mock_switch.assert_called_once()

    def test_tui_adapter_surfaces_switch_failed_on_error(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        def boom(_decision):
            raise ClaudeSwitchError("planning failed")

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=96.0):
            result = monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=boom,
            )

        assert result.kind == "switch_failed"
        assert "planning failed" in (result.switch_error or "")

    def test_tui_uses_engine_adaptive_interval(self, temp_home: Path):
        """TUI adapter must sleep using engine-provided next_interval, not a
        fixed 60s cadence."""
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [ord("q")]
        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=91.0), \
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
             ) as mock_step, \
             patch("claude_swap.tui._acquire_monitor_pid", return_value=None), \
             patch("claude_swap.tui._release_monitor_pid"), \
             patch("claude_swap.tui.curses.curs_set"):
            tui._run_auto_monitor(screen, switcher, threshold=95)
        mock_step.assert_called_once()
        drawn = screen.addstr.call_args_list
        assert any("Next check in" in str(c) for c in drawn)

    def test_blocks_when_cli_monitor_already_running(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        screen = _stub_screen()
        screen.getch.return_value = ord("q")

        with patch("claude_swap.tui._acquire_monitor_pid", return_value=12345), \
             patch("claude_swap.tui._show_message") as mock_message, \
             patch("claude_swap.tui.monitor_step") as mock_step, \
             patch("claude_swap.tui.curses.curs_set"):
            tui._run_auto_monitor(screen, switcher, threshold=95)

        mock_step.assert_not_called()
        mock_message.assert_called_once()
        assert "12345" in mock_message.call_args.args[1]

    def test_draws_threshold_from_engine_result(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [ord("q")]
        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=91.0), \
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
             ), \
             patch("claude_swap.tui._acquire_monitor_pid", return_value=None), \
             patch("claude_swap.tui._release_monitor_pid"), \
             patch("claude_swap.tui.curses.curs_set"):
            tui._run_auto_monitor(screen, switcher, threshold=95)

        header_calls = [
            str(c) for c in screen.addstr.call_args_list
            if "threshold" in str(c).lower()
        ]
        assert any("threshold 80%" in c for c in header_calls)

    def test_ctrl_c_during_switch_is_not_already_optimal(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        def cancel(_decision):
            raise monitor.SwitchCancelled

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=96.0):
            result = monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=cancel,
            )

        assert result.kind == "switch_cancelled"
        assert "cancelled" in result.user_message.lower()
        assert result.kind != "already_optimal"


class TestMonitorEngine:
    @pytest.fixture(autouse=True)
    def _stub_live_claude(self):
        with patch.object(
            ClaudeAccountSwitcher,
            "_live_default_mode_claude_pids",
            return_value=[99999],
        ):
            yield

    def test_step_already_optimal_does_not_call_switch(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=96.0):
            result = monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=perform,
            )

        assert result.kind == "already_optimal"
        perform.assert_called_once()
        decision = perform.call_args[0][0]
        assert decision.threshold == 95
        assert decision.active_usage_pct == 96.0

    def test_step_threshold_without_handler(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=99.0):
            result = monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=None,
            )

        assert result.kind == "threshold_no_handler"
        assert "no switch handler" in result.user_message

    def test_step_switch_failed_dedups_log(self, temp_home: Path, caplog):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        caplog.set_level(logging.DEBUG, logger="claude-swap")

        def boom(_decision) -> bool:
            raise ClaudeSwitchError("same error")

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=99.0), \
             patch("claude_swap.monitor.time.time", return_value=1_000_000.0):
            monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=boom,
            )
            monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=boom,
            )

        warnings = [
            r for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.WARNING
            and "switch failed" in r.getMessage()
        ]
        debugs = [
            r for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.DEBUG
            and "switch failed (repeat)" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert len(debugs) == 1

    def test_step_decision_error_returns_switch_failed(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=96.0), \
             patch.object(
                 switcher,
                 "build_auto_switch_decision",
                 side_effect=OSError("planning lock failed"),
             ):
            result = monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=MagicMock(),
            )

        assert result.kind == "switch_failed"
        assert "planning lock failed" in (result.switch_error or "")

    def test_saturated_hold_skips_repeated_switch(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(switcher, "get_active_usage_pct", return_value=96.0), \
             patch("claude_swap.monitor.time.time", return_value=1_000_000.0):
            result1 = monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=perform,
            )
            result2 = monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=perform,
            )

        assert result1.kind == "already_optimal"
        assert result2.kind == "already_optimal"
        perform.assert_called_once()

    def test_saturated_hold_clears_when_below_threshold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        state = monitor.MonitorRuntimeState()
        perform = MagicMock(return_value=False)

        with patch.object(
            switcher, "get_auto_switch_config",
            return_value={"enabled": True, "threshold": 95},
        ), patch.object(
            switcher,
            "get_active_usage_pct",
            side_effect=[96.0, 90.0, 96.0],
        ), patch("claude_swap.monitor.time.time", return_value=1_000_000.0):
            monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=perform,
            )
            monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=perform,
            )
            monitor.monitor_step(
                switcher, state, poll_seconds=0, perform_switch=perform,
            )

        assert perform.call_count == 2


# --------------------------------------------------------------------------- #
# CLI monitor                                                                #
# --------------------------------------------------------------------------- #


class TestCliAutoMonitor:
    @pytest.fixture(autouse=True)
    def _stub_live_claude(self):
        """The adaptive monitor short-circuits to idle when no default-mode
        Claude Code processes are running.  These tests pretend one process
        is always present so the usage-API path is exercised; the dedicated
        idle test below overrides this with an empty list.
        """
        with patch.object(
            ClaudeAccountSwitcher,
            "_live_default_mode_claude_pids",
            return_value=[99999],
        ):
            yield

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

    def test_once_switches_when_threshold_reached(self, temp_home: Path, capsys):
        switcher = ClaudeAccountSwitcher()

        with patch.object(switcher, "get_active_usage_pct", return_value=96.0), \
             patch.object(switcher, "switch") as mock_switch:
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

    def test_restores_sigterm_handler_after_once_run(self, temp_home: Path):
        import signal

        switcher = ClaudeAccountSwitcher()
        original = signal.getsignal(signal.SIGTERM)

        with patch.object(switcher, "get_active_usage_pct", return_value=10.0):
            monitor.run_cli_monitor(switcher, poll_seconds=1, once=True)

        assert signal.getsignal(signal.SIGTERM) == original

    def test_logs_poll_and_switch_at_threshold(
        self, temp_home: Path, caplog
    ):
        switcher = ClaudeAccountSwitcher()
        switched = {"n": 0, "intent": None}

        def _do_switch(intent) -> bool:
            switched["n"] += 1
            switched["intent"] = intent
            return True

        caplog.set_level(logging.INFO, logger="claude-swap")
        with patch.object(switcher, "get_active_usage_pct", return_value=96.0), \
             patch.object(switcher, "switch", side_effect=_do_switch):
            monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        records = [
            r for r in caplog.records if r.name == "claude-swap"
        ]
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
        with patch.object(switcher, "get_active_usage_pct", return_value=99.0), \
             patch.object(switcher, "switch", side_effect=_raise):
            monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        warnings = [
            r
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert warnings, [
            (r.name, r.levelname, r.getMessage()) for r in caplog.records
        ]
        assert any("monitor switch failed" in r.getMessage() for r in warnings)
        assert any("boom" in r.getMessage() for r in warnings)

    def test_cli_stdout_omits_switching_before_switch_failed(
        self, temp_home: Path, capsys,
    ):
        switcher = ClaudeAccountSwitcher()

        def _raise(_intent) -> bool:
            raise ClaudeSwitchError("boom")

        with patch.object(switcher, "get_active_usage_pct", return_value=99.0), \
             patch.object(switcher, "switch", side_effect=_raise):
            monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        out = capsys.readouterr().out
        assert "switch failed: boom" in out
        assert "switching account" not in out
        """Config is re-read each cycle: disabling via TUI stops switching."""
        switcher = ClaudeAccountSwitcher()
        # First call (startup): enabled → monitor starts.
        # Second call (in-loop): disabled → bail without switching.
        with patch.object(
            switcher,
            "get_auto_switch_config",
            side_effect=[
                {"enabled": True, "threshold": 98},
                {"enabled": False, "threshold": 98},
            ],
        ), patch.object(switcher, "get_active_usage_pct", return_value=99.0), \
           patch.object(switcher, "switch") as mock_switch:
            code = monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        assert code == 0
        mock_switch.assert_not_called()

    def test_monitor_picks_up_threshold_change_at_poll_time(self, temp_home: Path):
        """Config is re-read each cycle: lowering threshold takes effect immediately."""
        switcher = ClaudeAccountSwitcher()
        # Startup: threshold=98; in-loop: threshold lowered to 50.
        # Usage=60% → below 98 (no switch), but above 50 (switch).
        with patch.object(
            switcher,
            "get_auto_switch_config",
            side_effect=[
                {"enabled": True, "threshold": 98},
                {"enabled": True, "threshold": 50},
            ],
        ), patch.object(switcher, "get_active_usage_pct", return_value=60.0), \
           patch.object(switcher, "switch") as mock_switch:
            code = monitor.run_cli_monitor(switcher, poll_seconds=0, once=True)

        assert code == 0
        mock_switch.assert_called_once()

    def test_monitor_idles_when_no_live_claude_sessions(
        self, temp_home: Path, caplog,
    ):
        """Phase 4 short-circuit: with zero default-mode Claude Code processes
        running there is nothing burning tokens, so the monitor skips the
        usage API call entirely and idles at the polling ceiling.  Override
        the class-level fixture's return value to simulate the idle state.
        """
        switcher = ClaudeAccountSwitcher()
        caplog.set_level(logging.INFO, logger="claude-swap")

        with patch.object(
            ClaudeAccountSwitcher,
            "_live_default_mode_claude_pids",
            return_value=[],
        ), patch.object(switcher, "get_active_usage_pct") as mock_usage, \
           patch.object(switcher, "switch") as mock_switch:
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

        with patch.object(switcher, "get_active_usage_pct", return_value=99.0), \
             patch.object(switcher, "switch", side_effect=boom), \
             patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep):
            # Explicit poll_seconds=60 so the test does not silently depend on
            # the module-level default; the value itself doesn't matter here
            # because time.sleep is mocked.
            monitor.run_cli_monitor(switcher, poll_seconds=60)

        warnings = [
            r for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.WARNING
            and "switch failed" in r.getMessage()
        ]
        debugs = [
            r for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.DEBUG
            and "switch failed (repeat)" in r.getMessage()
        ]
        # First identical failure surfaces; subsequent ones drop to debug.
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert len(debugs) >= 1, [r.getMessage() for r in debugs]

    def test_monitor_backs_off_on_consecutive_usage_failures(
        self, temp_home: Path, caplog,
    ):
        """Phase 3 backoff: when the usage API returns None, the failure
        counter increments and the next poll interval grows exponentially.
        We exercise the in-process counter by running the loop multiple times
        through the (mocked) sleep boundary, verifying logged backoff values.
        """
        switcher = ClaudeAccountSwitcher()
        caplog.set_level(logging.WARNING, logger="claude-swap")

        sleeps: list[int] = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            # Bail out after 3 backoffs so the test always terminates.
            if len(sleeps) >= 3:
                raise monitor._MonitorStopped

        with patch.object(switcher, "get_active_usage_pct", return_value=None), \
             patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep):
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
        assert monitor._next_poll_interval(50.0, None, 0.0, 95) == monitor.MONITOR_POLL_SECONDS

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


class TestSleepWakeAndHeartbeat:
    """Round-2 review additions: sleep/wake baseline reset and idle heartbeat
    for the schema-break safety net.  Both are operational guardrails for
    monitor.err observability.
    """

    @pytest.fixture(autouse=True)
    def _stub_live_claude(self):
        with patch.object(
            ClaudeAccountSwitcher,
            "_live_default_mode_claude_pids",
            return_value=[99999],
        ):
            yield

    def test_wake_gap_resets_baseline_and_last_switch_error(
        self, temp_home: Path, caplog,
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

        with patch.object(switcher, "get_active_usage_pct", return_value=99.0), \
             patch.object(switcher, "switch", side_effect=boom), \
             patch("claude_swap.monitor.time.time", side_effect=fake_wall_time), \
             patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep):
            monitor.run_cli_monitor(switcher, poll_seconds=60)

        msgs = [r.getMessage() for r in caplog.records if r.name == "claude-swap"]
        assert any("wake-gap" in m and "resetting baselines" in m for m in msgs), msgs

        # Load-bearing assertion: post-wake, the identical failure must
        # re-surface at WARNING, not DEBUG.  Two WARNINGs (pre + post wake)
        # would prove last_switch_error actually reset.
        warning_failures = [
            r for r in caplog.records
            if r.name == "claude-swap"
            and r.levelno == logging.WARNING
            and "monitor switch failed" in r.getMessage()
            and "(repeat)" not in r.getMessage()
        ]
        assert len(warning_failures) == 2, [
            (r.levelname, r.getMessage()) for r in caplog.records
            if "switch failed" in r.getMessage()
        ]

    def test_idle_heartbeat_fires_after_long_idle_with_enabled_auto_switch(
        self, temp_home: Path, caplog,
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

        with patch.object(
            ClaudeAccountSwitcher,
            "_live_default_mode_claude_pids",
            return_value=[],
        ), patch("claude_swap.monitor.time.time", side_effect=fake_wall_time), \
           patch("claude_swap.monitor.time.sleep", side_effect=fake_sleep):
            monitor.run_cli_monitor(switcher, poll_seconds=60)

        warnings = [
            r.getMessage() for r in caplog.records
            if r.name == "claude-swap" and r.levelno == logging.WARNING
        ]
        assert any("monitor idle for" in m for m in warnings), warnings


class TestFailureBackoffSeconds:
    def test_zero_failures_returns_min(self):
        assert monitor._failure_backoff_seconds(0) == monitor.MONITOR_POLL_SECONDS_MIN

    def test_first_failure_returns_base(self):
        assert (
            monitor._failure_backoff_seconds(1)
            == monitor.MONITOR_FAILURE_BACKOFF_BASE
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
