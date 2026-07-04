"""Monitor PID-file lifecycle: stale-pid reclaim, holder detection, and
supervised-monitor exclusivity."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import monitor
from claude_swap.switcher import ClaudeAccountSwitcher


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


