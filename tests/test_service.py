"""Tests for the macOS launchd background service wrapper.

`subprocess.run` and `sys.platform` are mocked so the suite passes on Linux CI
runners — no real ``launchctl`` is ever invoked. Pattern mirrors
``tests/test_auto_switch.py`` (pytest + monkeypatch + tmp_path) and the argv
routing case in ``tests/test_cli.py``.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import cli, service
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import ClaudeAccountSwitcher


def _force_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we're on macOS regardless of the host platform."""
    monkeypatch.setattr(service.sys, "platform", "darwin")


def _stub_launchctl(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a ``subprocess.run`` replacement that records its calls."""
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    mock = MagicMock(return_value=completed)
    return mock


# --------------------------------------------------------------------------- #
# _build_plist                                                                 #
# --------------------------------------------------------------------------- #


class TestBuildPlist:
    def test_core_fields(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        plist = service._build_plist(switcher)
        assert plist["Label"] == service.SERVICE_LABEL
        assert plist["RunAtLoad"] is True
        # Dict-form KeepAlive is load-bearing: a bare ``True`` would resurrect
        # the agent after ``launchctl bootout``, defeating uninstall.
        assert plist["KeepAlive"] == {"SuccessfulExit": False}
        assert plist["ThrottleInterval"] == 30
        assert plist["ProcessType"] == "Background"
        assert plist["LowPriorityIO"] is True

    def test_program_arguments_invoke_monitor(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        plist = service._build_plist(switcher)
        argv = plist["ProgramArguments"]
        assert argv[0] == sys.executable
        assert argv[-1] == "--monitor"
        assert "claude_swap" in argv

    def test_log_paths_under_backup_dir(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        plist = service._build_plist(switcher)
        log_dir = switcher.backup_dir / "logs"
        assert plist["StandardOutPath"] == str(log_dir / "monitor.out")
        assert plist["StandardErrorPath"] == str(log_dir / "monitor.err")

    def test_environment_variables_forwarded(self, temp_home: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/cswap-cfg")
        switcher = ClaudeAccountSwitcher()
        plist = service._build_plist(switcher)
        env = plist["EnvironmentVariables"]
        # HOME is always set in test runs; CLAUDE_CONFIG_DIR was set above.
        assert env.get("CLAUDE_CONFIG_DIR") == "/tmp/cswap-cfg"
        assert "HOME" in env
        # Variables we never forward should not leak in.
        assert "OPENAI_API_KEY" not in env


# --------------------------------------------------------------------------- #
# install / uninstall                                                          #
# --------------------------------------------------------------------------- #


class TestInstall:
    def test_writes_parseable_plist_and_bootstraps(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        launchctl = _stub_launchctl()
        monkeypatch.setattr(service.subprocess, "run", launchctl)

        switcher = ClaudeAccountSwitcher()
        rc = service.install(switcher)

        assert rc == 0
        assert plist_path.exists()
        # Round-trip through plistlib — proves we wrote valid XML plist bytes.
        with plist_path.open("rb") as fh:
            loaded = plistlib.load(fh)
        assert loaded["Label"] == service.SERVICE_LABEL
        assert loaded["ProgramArguments"][-1] == "--monitor"
        # The launchd log dir was created.
        assert (switcher.backup_dir / "logs").is_dir()

        # First call is best-effort bootout; second is the load-bearing bootstrap.
        calls = launchctl.call_args_list
        assert len(calls) == 2
        bootout_args = calls[0].args[0]
        bootstrap_args = calls[1].args[0]
        assert bootout_args[0] == service._LAUNCHCTL
        assert bootout_args[1] == "bootout"
        assert bootstrap_args[1] == "bootstrap"
        assert str(plist_path) in bootstrap_args

        out = capsys.readouterr().out
        assert "Service installed" in out
        assert service.SERVICE_LABEL in out

    def test_bootstrap_failure_raises_claude_switch_error(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)

        # First call (bootout, check=False) succeeds; second (bootstrap, check=True) fails.
        def fake_run(argv, **kwargs):
            completed = MagicMock()
            if "bootstrap" in argv:
                completed.returncode = 5
                completed.stderr = "Bootstrap failed"
            else:
                completed.returncode = 0
                completed.stderr = ""
            completed.stdout = ""
            return completed

        monkeypatch.setattr(service.subprocess, "run", fake_run)
        with pytest.raises(ClaudeSwitchError, match="bootstrap"):
            service.install(ClaudeAccountSwitcher())


class TestUninstall:
    def test_removes_plist_and_calls_bootout(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(b"<plist/>")
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        launchctl = _stub_launchctl()
        monkeypatch.setattr(service.subprocess, "run", launchctl)

        rc = service.uninstall(ClaudeAccountSwitcher())

        assert rc == 0
        assert not plist_path.exists()
        bootout_args = launchctl.call_args_list[0].args[0]
        assert bootout_args[1] == "bootout"

        out = capsys.readouterr().out
        assert "Service removed" in out

    def test_idempotent_when_not_installed(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        # ``bootout`` returns non-zero when the service is not loaded; uninstall
        # must tolerate that (check=False) so the user-visible call stays clean.
        monkeypatch.setattr(service.subprocess, "run", _stub_launchctl(returncode=3))

        rc = service.uninstall(ClaudeAccountSwitcher())

        assert rc == 0
        out = capsys.readouterr().out
        assert "was not installed" in out


# --------------------------------------------------------------------------- #
# status / logs                                                                #
# --------------------------------------------------------------------------- #


class TestStatus:
    def test_service_state_not_installed(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        assert service.service_state() == "not installed"

    def test_service_state_installed_but_not_loaded(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(b"<plist/>")
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        monkeypatch.setattr(service.subprocess, "run", _stub_launchctl(returncode=113))
        assert service.service_state() == "installed but not loaded"

    def test_service_state_loaded(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(b"<plist/>")
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        monkeypatch.setattr(service.subprocess, "run", _stub_launchctl(stdout="state = running"))
        assert service.service_state() == "loaded"

    def test_not_installed(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        # ``subprocess.run`` must not be invoked when the plist is missing.
        sentinel = MagicMock(side_effect=AssertionError("launchctl should not be called"))
        monkeypatch.setattr(service.subprocess, "run", sentinel)

        rc = service.status(ClaudeAccountSwitcher())

        assert rc == 0
        out = capsys.readouterr().out
        assert "not installed" in out

    def test_installed_but_not_loaded(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(b"<plist/>")
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        monkeypatch.setattr(service.subprocess, "run", _stub_launchctl(returncode=113))

        rc = service.status(ClaudeAccountSwitcher())

        assert rc == 0
        out = capsys.readouterr().out
        assert "installed but not loaded" in out

    def test_loaded_surfaces_state_lines(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(b"<plist/>")
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        stdout = (
            "com.claude-swap.monitor = {\n"
            "    state = running\n"
            "    pid = 4242\n"
            "    last exit code = 0\n"
            "    program = /usr/bin/python3\n"
            "}\n"
        )
        monkeypatch.setattr(service.subprocess, "run", _stub_launchctl(stdout=stdout))

        rc = service.status(ClaudeAccountSwitcher())

        assert rc == 0
        out = capsys.readouterr().out
        assert "loaded" in out
        assert "state = running" in out
        assert "pid = 4242" in out
        assert "last exit code = 0" in out
        # ``program = ...`` is filtered out so the output stays scannable.
        assert "program = /usr/bin/python3" not in out

    def test_status_warns_on_version_mismatch(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """status() warns when the plist records an older cswap version."""
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        plist_path.parent.mkdir(parents=True)
        # Write a plist whose installed version differs from the current one.
        plist_data = {"EnvironmentVariables": {service._VERSION_ENV_KEY: "0.0.1"}}
        with plist_path.open("wb") as fh:
            plistlib.dump(plist_data, fh)
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        monkeypatch.setattr(service.subprocess, "run", _stub_launchctl(returncode=0, stdout="state = running\n"))

        rc = service.status(ClaudeAccountSwitcher())

        assert rc == 0
        out = capsys.readouterr().out
        assert "0.0.1" in out
        assert "cswap service install" in out

    def test_status_no_warning_when_version_matches(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """No warning when installed version == current version."""
        _force_darwin(monkeypatch)
        plist_path = temp_home / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        plist_path.parent.mkdir(parents=True)
        plist_data = {"EnvironmentVariables": {service._VERSION_ENV_KEY: service.__version__}}
        with plist_path.open("wb") as fh:
            plistlib.dump(plist_data, fh)
        monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
        monkeypatch.setattr(service.subprocess, "run", _stub_launchctl(returncode=0, stdout="state = running\n"))

        service.status(ClaudeAccountSwitcher())

        out = capsys.readouterr().out
        assert "cswap service install" not in out


class TestLogs:
    def test_missing_files_reported_cleanly(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        rc = service.logs(ClaudeAccountSwitcher())
        assert rc == 0
        out = capsys.readouterr().out
        # All three log surfaces are listed even when missing — on-call needs
        # to see they exist as concepts even before the monitor has written
        # anything.
        assert "claude-swap.log (structured)" in out
        assert "monitor.err (launchd stderr)" in out
        assert "monitor.out (launchd stdout)" in out
        assert "(none yet)" in out

    def test_tails_existing_files(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        _force_darwin(monkeypatch)
        switcher = ClaudeAccountSwitcher()
        log_dir = switcher.backup_dir / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "monitor.out").write_text("first\nsecond\nthird\n")
        (log_dir / "monitor.err").write_text("only-error-line\n")
        # Structured log: the decision trail that an on-call really wants.
        switcher.backup_dir.mkdir(parents=True, exist_ok=True)
        (switcher.backup_dir / "claude-swap.log").write_text(
            "structured-line-1\nstructured-line-2\n"
        )

        rc = service.logs(switcher, lines=2)
        assert rc == 0
        out = capsys.readouterr().out
        assert "structured-line-2" in out
        assert "only-error-line" in out
        # ``lines=2`` keeps only the tail; first line must be dropped.
        assert "second" in out
        assert "third" in out
        assert "first" not in out


# --------------------------------------------------------------------------- #
# Platform guard                                                               #
# --------------------------------------------------------------------------- #


class TestPlatformGuard:
    @pytest.mark.parametrize("action", ["install", "uninstall", "status", "logs"])
    def test_non_macos_raises_claude_switch_error(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, action: str
    ):
        monkeypatch.setattr(service.sys, "platform", "linux")
        fn = getattr(service, action)
        with pytest.raises(ClaudeSwitchError, match="macOS-only"):
            fn(ClaudeAccountSwitcher())


# --------------------------------------------------------------------------- #
# CLI routing                                                                  #
# --------------------------------------------------------------------------- #


class TestCliRouting:
    def test_argv_service_dispatches_to_service_command(self, monkeypatch: pytest.MonkeyPatch):
        called: list[list[str]] = []
        monkeypatch.setattr(cli, "_service_command", lambda argv: called.append(argv))
        monkeypatch.setattr(sys, "argv", ["cswap", "service", "status"])
        cli.main()
        assert called == [["status"]]

    def test_service_unknown_action_errors(self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "argv", ["cswap", "service", "bogus"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        assert "bogus" in capsys.readouterr().err

    def test_service_missing_action_errors(self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "argv", ["cswap", "service"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "action" in err or "required" in err.lower()

    def test_service_help_exits_zero(self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "argv", ["cswap", "service", "--help"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "install" in out
        assert "launchd" in out

    def test_service_status_on_non_macos_clean_error_exit_one(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, temp_home: Path
    ):
        # End-to-end through ``main()`` → ``_service_command`` → ``service.status``.
        # On a non-darwin host the guard fires and the top-level handler renders
        # a clean stderr line + exit 1, with no traceback.
        monkeypatch.setattr(service.sys, "platform", "linux")
        monkeypatch.setattr(sys, "argv", ["cswap", "service", "status"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "macOS-only" in err


# --------------------------------------------------------------------------- #
# Smoke: service.py does not import monitor internals                          #
# --------------------------------------------------------------------------- #


class TestNoMonitorImport:
    def test_module_does_not_import_monitor(self):
        """Plan invariant: the service is a thin supervisor — it shells out via
        ``cswap --monitor`` rather than calling monitor internals. Future changes
        to the monitor loop must require no changes here.
        """
        source = Path(service.__file__).read_text(encoding="utf-8")
        assert "from claude_swap.monitor" not in source
        assert "run_cli_monitor" not in source
