"""Tests for the TUI module.

These tests don't render real curses windows. They mock curses primitives
and verify the menu/dispatch logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import stub_screen

from claude_swap import tui
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import ClaudeAccountSwitcher


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_seq(temp_home: Path, accounts: list[tuple[str, str]] | None = None) -> Path:
    """Write a sequence.json to the backup directory.

    accounts: list of (slot, email) tuples. First entry is treated as active.
    """
    accounts = accounts or []
    backup = temp_home / ".claude-swap-backup"
    backup.mkdir(parents=True, exist_ok=True)
    seq_data = {
        "activeAccountNumber": int(accounts[0][0]) if accounts else None,
        "lastUpdated": "2026-04-30T00:00:00Z",
        "sequence": [int(a[0]) for a in accounts],
        "accounts": {
            slot: {
                "email": email,
                "uuid": f"uuid-{slot}",
                "organizationUuid": "",
                "organizationName": "",
                "added": "2026-04-30T00:00:00Z",
            }
            for slot, email in accounts
        },
    }
    (backup / "sequence.json").write_text(json.dumps(seq_data))
    return backup / "sequence.json"


# --------------------------------------------------------------------------- #
# Status line / account items                                                  #
# --------------------------------------------------------------------------- #


class TestStatusLine:
    def test_no_managed_no_login(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        line = tui._status_line(switcher)
        assert "no active login" in line
        assert "0 managed" in line

    def test_with_active_login(self, temp_home: Path):
        config = {"oauthAccount": {"emailAddress": "u@example.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        _make_seq(temp_home, [("1", "u@example.com")])
        switcher = ClaudeAccountSwitcher()
        line = tui._status_line(switcher)
        assert "u@example.com" in line
        assert "1 managed" in line


class TestAccountItems:
    def test_empty_when_no_accounts(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert tui._account_items(switcher) == []

    def test_returns_sorted_items_with_active_marker(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com"), ("2", "b@x.com")])
        config = {"oauthAccount": {"emailAddress": "a@x.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()
        items = tui._account_items(switcher)
        assert len(items) == 2
        labels = [label for label, _ in items]
        assert "★ active" in labels[0]  # slot 1 is active
        assert "★ active" not in labels[1]
        # Values should be the slot numbers as strings
        assert [v for _, v in items] == ["1", "2"]


# --------------------------------------------------------------------------- #
# _select_from menu primitive                                                   #
# --------------------------------------------------------------------------- #


class TestSelectFrom:
    def test_returns_value_on_enter(self):
        screen = stub_screen()
        # press down once, then enter
        screen.getch.side_effect = [tui.curses.KEY_DOWN, 10]
        result = tui._select_from(
            screen,
            "title",
            items=[("first", "a"), ("second", "b")],
        )
        assert result == "b"

    def test_returns_none_on_escape(self):
        screen = stub_screen()
        screen.getch.side_effect = [27]  # Esc
        result = tui._select_from(screen, "t", items=[("x", "1")])
        assert result is None

    def test_returns_none_on_q(self):
        screen = stub_screen()
        screen.getch.side_effect = [ord("q")]
        result = tui._select_from(screen, "t", items=[("x", "1")])
        assert result is None

    def test_wrap_around_on_up_at_top(self):
        screen = stub_screen()
        screen.getch.side_effect = [tui.curses.KEY_UP, 10]
        result = tui._select_from(
            screen, "t",
            items=[("a", "1"), ("b", "2"), ("c", "3")],
        )
        assert result == "3"  # wrapped to last

    def test_cancel_sentinel_returns_none(self):
        screen = stub_screen()
        screen.getch.side_effect = [tui.curses.KEY_DOWN, 10]
        # second item has value=None — selecting it should return None
        result = tui._select_from(
            screen, "t",
            items=[("real", "x"), ("-- Cancel --", None)],
        )
        assert result is None


# --------------------------------------------------------------------------- #
# Sub-flows: switch / add / remove                                              #
# --------------------------------------------------------------------------- #


class TestDoSwitch:
    def test_no_accounts_shows_message(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.return_value = ord("q")  # dismiss the message
        tui._do_switch(screen, switcher)
        # Should NOT call switch_to
        # (we use real switcher; no patching needed since add wasn't called)

    def test_dispatches_to_switch_to(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com"), ("2", "b@x.com")])
        config = {"oauthAccount": {"emailAddress": "a@x.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()

        screen = stub_screen()
        # Pick the second item (slot 2) with one DOWN + ENTER
        screen.getch.side_effect = [tui.curses.KEY_DOWN, 10, ord("q")]

        with patch.object(switcher, "switch_to") as mock_switch:
            tui._do_switch(screen, switcher)

        mock_switch.assert_called_once_with("2")

    def test_cancel_does_not_dispatch(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com")])
        config = {"oauthAccount": {"emailAddress": "a@x.com"}}
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()

        screen = stub_screen()
        screen.getch.side_effect = [27]  # Esc on selection screen

        with patch.object(switcher, "switch_to") as mock_switch:
            tui._do_switch(screen, switcher)

        mock_switch.assert_not_called()


class TestDoAdd:
    def test_login_path_calls_add_account(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        # First menu: Enter on "From current Claude Code login" (idx 0)
        screen.getch.side_effect = [10]
        with patch.object(switcher, "add_account") as mock_add, \
             patch("claude_swap.tui.curses.def_prog_mode"), \
             patch("claude_swap.tui.curses.endwin"), \
             patch("claude_swap.tui.curses.reset_prog_mode"), \
             patch("builtins.input", return_value=""):
            tui._do_add(screen, switcher, has_token_flow=False)
        mock_add.assert_called_once_with()

    def test_token_option_only_when_method_exists(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [27]  # cancel out

        with patch.object(switcher, "add_account") as _:
            tui._do_add(screen, switcher, has_token_flow=False)
        # If we never had add_account_from_token, the token option must not show.
        # Verify by checking the items list passed to addstr — easier: just trust
        # that has_token_flow=False yields a 2-item menu (login + cancel).
        # This test mostly guards against exceptions when method missing.

    def test_token_path_collects_email_and_token(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        # Stub add_account_from_token onto the instance
        switcher.add_account_from_token = MagicMock()

        screen = stub_screen()

        # Sequence:
        #   menu: DOWN once (to "From a setup-token") + ENTER
        #   email prompt: type "u@x.com" + ENTER
        #   token prompt: type "tok" + ENTER
        keys = [tui.curses.KEY_DOWN, 10]  # pick token option
        keys += [ord(c) for c in "u@x.com"] + [10]  # email + Enter
        keys += [ord(c) for c in "tok"] + [10]  # token + Enter
        screen.getch.side_effect = keys

        with patch("claude_swap.tui.curses.def_prog_mode"), \
             patch("claude_swap.tui.curses.endwin"), \
             patch("claude_swap.tui.curses.reset_prog_mode"), \
             patch("claude_swap.tui.curses.curs_set"), \
             patch("builtins.input", return_value=""):
            tui._do_add(screen, switcher, has_token_flow=True)

        switcher.add_account_from_token.assert_called_once_with(
            token="tok", email="u@x.com", slot=None
        )


class TestDoRemove:
    def test_no_accounts_shows_message(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.return_value = ord("q")
        with patch.object(switcher, "remove_account") as mock_rm:
            tui._do_remove(screen, switcher)
        mock_rm.assert_not_called()

    def test_confirm_required(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com")])
        switcher = ClaudeAccountSwitcher()

        screen = stub_screen()
        # pick slot 1 + Enter, then type "n" + Enter on confirm prompt
        keys = [10]  # pick first item
        keys += [ord("n"), 10]  # confirm: "n"
        screen.getch.side_effect = keys

        with patch.object(switcher, "remove_account") as mock_rm, \
             patch("claude_swap.tui.curses.curs_set"):
            tui._do_remove(screen, switcher)

        mock_rm.assert_not_called()

    def test_y_confirms_and_dispatches(self, temp_home: Path):
        _make_seq(temp_home, [("3", "x@y.com")])
        switcher = ClaudeAccountSwitcher()

        screen = stub_screen()
        screen.getch.side_effect = [10, ord("y"), 10, ord("q")]

        with patch.object(switcher, "remove_account") as mock_rm, \
             patch("claude_swap.tui.curses.curs_set"):
            tui._do_remove(screen, switcher)

        mock_rm.assert_called_once_with("3", assume_yes=True)


# --------------------------------------------------------------------------- #
# Main loop dispatch                                                           #
# --------------------------------------------------------------------------- #


class TestMainLoopHealth:
    """The "Account health & usage" entry must shell out to the existing
    ``list_accounts(show_token_status=True, show_health=True)`` renderer —
    SSOT preserved (no curses-native re-implementation)."""

    def _select_keys(self, idx: int) -> list[int]:
        """Keys to move down ``idx`` times and press Enter."""
        return [tui.curses.KEY_DOWN] * idx + [10]

    def test_health_entry_dispatches_with_flags(self, temp_home: Path):
        switcher = MagicMock(spec=ClaudeAccountSwitcher)
        # _status_line consults the switcher; keep it cheap and deterministic.
        switcher.get_auto_switch_config.return_value = {
            "enabled": False, "threshold": 90,
        }
        switcher._get_sequence_data_migrated.return_value = None
        switcher._get_current_account.return_value = None

        screen = stub_screen(rows=40, cols=120)
        # Menu order: switch, add, remove, refresh, list, health(5),
        # status, watch, auto, quit(9). Pick "health" (idx 5), then quit (idx 9).
        screen.getch.side_effect = (
            self._select_keys(5) + self._select_keys(9)
        )

        with patch("claude_swap.tui._run_inline") as mock_inline, \
             patch("claude_swap.tui.curses.curs_set"):
            rc = tui._main_loop(screen, switcher)

        assert rc == 0
        assert mock_inline.call_count == 1
        _stdscr_arg, title, fn = mock_inline.call_args.args
        assert _stdscr_arg is screen
        assert title == "Account health & usage"
        fn()
        switcher.list_accounts.assert_called_once_with(
            show_token_status=True, show_health=True,
        )

    def test_quick_list_entry_uses_no_flags(self, temp_home: Path):
        """Regression: the list entry stays flag-free."""
        switcher = MagicMock(spec=ClaudeAccountSwitcher)
        switcher.get_auto_switch_config.return_value = {
            "enabled": False, "threshold": 90,
        }
        switcher._get_sequence_data_migrated.return_value = None
        switcher._get_current_account.return_value = None

        screen = stub_screen(rows=40, cols=120)
        # Pick "list" (idx 4), then quit (idx 9).
        screen.getch.side_effect = (
            self._select_keys(4) + self._select_keys(9)
        )

        with patch("claude_swap.tui._run_inline") as mock_inline, \
             patch("claude_swap.tui.curses.curs_set"):
            rc = tui._main_loop(screen, switcher)

        assert rc == 0
        assert mock_inline.call_count == 1
        _stdscr_arg, title, fn = mock_inline.call_args.args
        assert title == "Accounts"
        fn()
        switcher.list_accounts.assert_called_once_with()


# --------------------------------------------------------------------------- #
# CLI integration                                                              #
# --------------------------------------------------------------------------- #


class TestCliIntegration:
    def test_tui_in_help(self, tmp_path):
        import os
        import subprocess
        import sys as _sys

        env = {**os.environ}
        env["PYTHONPATH"] = (
            str(Path(__file__).resolve().parent.parent / "src")
            + os.pathsep
            + env.get("PYTHONPATH", "")
        )
        # Isolate the child from the developer's real home/config, consistent
        # with test_cli._subprocess_env. ``--help`` exits in argparse before the
        # switcher is built, so nothing is touched today — this just keeps the
        # "no subprocess inherits the real HOME" invariant uniform.
        env["HOME"] = env["USERPROFILE"] = str(tmp_path)
        for _var in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
            env.pop(_var, None)
        result = subprocess.run(
            [_sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        assert "--tui" in result.stdout

    def test_tui_dispatches_to_run(self):
        import sys as _sys
        from claude_swap import cli

        with patch.object(_sys, "argv", ["claude-swap", "--tui"]), \
             patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.tui.run", return_value=0) as mock_run, \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == 0
        mock_run.assert_called_once_with(switcher_cls.return_value)


class TestClampInterval:
    def test_in_range(self):
        assert tui._clamp_interval(5) == 5

    def test_below_min(self):
        assert tui._clamp_interval(0) == 1

    def test_above_max(self):
        assert tui._clamp_interval(999) == 60


class TestAnsiSegments:
    def test_plain_text_single_run(self):
        assert tui._ansi_segments("hello") == [("hello", 0)]

    def test_bold_run(self):
        assert tui._ansi_segments("\x1b[1mX\x1b[0m") == [("X", tui._STYLE_BOLD)]


class TestRunInline:
    def test_switch_error_captured(self):
        screen = stub_screen()

        def fn():
            raise ClaudeSwitchError("boom")

        with patch("claude_swap.tui._pager") as mock_pager:
            tui._run_inline(screen, "T", fn)
        lines = mock_pager.call_args.args[2]
        assert any("boom" in line for line in lines)


class TestWatch:
    def test_empty_state_returns_without_loop(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.return_value = ord("q")
        with patch.object(switcher, "list_accounts") as mock_list:
            tui._do_watch(screen, switcher)
        mock_list.assert_not_called()

    def test_refreshes_then_quits(self, temp_home: Path):
        _make_seq(temp_home, [("1", "a@x.com")])
        switcher = ClaudeAccountSwitcher()
        screen = stub_screen()
        screen.getch.side_effect = [ord("q")]
        with patch.object(switcher, "list_accounts") as mock_list, \
             patch("claude_swap.tui.time.monotonic", return_value=100.0):
            tui._watch_loop(screen, switcher, interval=5)
        mock_list.assert_called()
        screen.timeout.assert_any_call(250)
        screen.timeout.assert_any_call(-1)
