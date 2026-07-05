"""Tests for the Linux/WSL systemd --user service backend."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.service_backends import systemd as systemd_backend
from claude_swap.switcher import ClaudeAccountSwitcher


def _force_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")


def _stub_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    return MagicMock(return_value=completed)


def _unit_path(config_home: Path) -> Path:
    return config_home / "systemd" / "user" / systemd_backend.UNIT_NAME


class TestIsWsl:
    def test_detects_microsoft_in_proc_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        from claude_swap import service_spec

        monkeypatch.setattr(sys, "platform", "linux")
        version = tmp_path / "version"
        version.write_text("#1 SMP Microsoft\n")
        osrelease = tmp_path / "osrelease"
        osrelease.write_text("5.15.0\n")
        from claude_swap import models

        monkeypatch.setattr(
            models,
            "_WSL_PROC_PATHS",
            (version, osrelease),
        )
        assert service_spec.is_wsl() is True

    def test_false_on_plain_linux(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        from claude_swap import service_spec

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        version = tmp_path / "version"
        version.write_text("#1 SMP Debian\n")
        osrelease = tmp_path / "osrelease"
        osrelease.write_text("6.1.0\n")
        from claude_swap import models

        monkeypatch.setattr(
            models,
            "_WSL_PROC_PATHS",
            (version, osrelease),
        )
        assert service_spec.is_wsl() is False

    def test_false_on_darwin(self, monkeypatch: pytest.MonkeyPatch):
        from claude_swap import service_spec

        monkeypatch.setattr(sys, "platform", "darwin")
        assert service_spec.is_wsl() is False


class TestBuildUnit:
    def test_execstart_uses_program_arguments(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        unit = systemd_backend._build_unit(switcher)
        assert f"ExecStart={sys.executable}" in unit.replace('"', "")
        assert "claude_swap auto" in unit.replace('"', "")
        assert "Restart=on-failure" in unit
        assert "RestartSec=30" in unit
        assert "WantedBy=default.target" in unit
        assert "Description=Claude Swap auto-switch engine" in unit

    def test_crash_stays_in_the_restart_set(self, temp_home: Path):
        # Regression guard: SuccessExitStatus or RestartPreventExitStatus
        # entries would stop Restart=on-failure from restarting after an
        # engine crash, silently disabling the service retry path.
        switcher = ClaudeAccountSwitcher()
        unit = systemd_backend._build_unit(switcher)
        assert "Restart=on-failure" in unit
        assert "SuccessExitStatus" not in unit
        assert "RestartPreventExitStatus" not in unit

    def test_stamps_installed_version(self, temp_home: Path):
        from claude_swap import __version__

        switcher = ClaudeAccountSwitcher()
        unit = systemd_backend._build_unit(switcher)
        assert f"CSWAP_INSTALLED_VERSION={__version__}" in unit

    def test_env_value_with_space_is_quoted(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A forwarded path containing a space must produce a valid, quoted
        # systemd assignment (bare `Environment=KEY=/My Drive` is malformed).
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/My Drive/.claude")
        switcher = ClaudeAccountSwitcher()
        unit = systemd_backend._build_unit(switcher)
        assert 'Environment="CLAUDE_CONFIG_DIR=/My Drive/.claude"' in unit
        # The whole assignment is wrapped, so no bare unquoted form leaks.
        assert "Environment=CLAUDE_CONFIG_DIR=/My Drive" not in unit

    def test_env_value_with_quote_is_escaped_and_round_trips(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", '/weird "dir"/.claude')
        switcher = ClaudeAccountSwitcher()
        unit = systemd_backend._build_unit(switcher)
        env_vars: dict[str, str] = {}
        for line in unit.splitlines():
            match = systemd_backend._ENV_LINE.match(line)
            if not match:
                continue
            pair = match.group(1).strip()
            if pair.startswith('"') and pair.endswith('"'):
                key, _, value = systemd_backend._unescape_env_value(
                    pair[1:-1]
                ).partition("=")
                env_vars[key] = value
        assert env_vars["CLAUDE_CONFIG_DIR"] == '/weird "dir"/.claude'


class TestInstall:
    def test_writes_unit_enables_and_lingers(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        unit_path = _unit_path(config_home)
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)

        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            completed = MagicMock()
            completed.returncode = 0
            completed.stdout = ""
            completed.stderr = ""
            return completed

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(systemd_backend.service_spec, "is_wsl", lambda: False)

        switcher = ClaudeAccountSwitcher()
        rc = systemd_backend.SystemdBackend().install(switcher)

        assert rc == 0
        assert unit_path.exists()
        text = unit_path.read_text(encoding="utf-8")
        assert "ExecStart=" in text
        assert "claude_swap auto" in text.replace('"', "")
        assert (switcher.backup_dir / "logs").is_dir()

        assert ["systemctl", "--user", "daemon-reload"] in calls
        assert [
            "systemctl",
            "--user",
            "enable",
            "--now",
            systemd_backend.UNIT_NAME,
        ] in calls
        assert any(argv[:2] == ["loginctl", "enable-linger"] for argv in calls)

        out = capsys.readouterr().out
        assert "Service installed" in out
        assert "systemd journal" in out

    def test_idempotent_reinstall_overwrites_unit(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        unit_path = _unit_path(config_home)
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text("stale unit\n")
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        monkeypatch.setattr(subprocess, "run", _stub_run())

        systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())
        assert "Restart=on-failure" in unit_path.read_text(encoding="utf-8")

    def test_wsl_install_prints_guidance(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
        monkeypatch.setenv("USER", "dev")
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        monkeypatch.setattr(
            systemd_backend, "_unit_path", lambda: _unit_path(config_home)
        )
        monkeypatch.setattr(subprocess, "run", _stub_run())
        monkeypatch.setattr(systemd_backend.service_spec, "is_wsl", lambda: True)

        systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())

        out = capsys.readouterr().out
        assert "WSL note" in out
        assert "wsl.exe -d Ubuntu -u dev" in out
        # The suggested command must leave a long-lived process behind:
        # `--exec /usr/bin/true` exits immediately, so the distro idles out
        # seconds later and takes the monitor down with it. It must also be
        # preinstalled: `dbus-launch` (dbus-x11) is absent from the default
        # Ubuntu WSL image, so copying that guidance failed outright.
        assert systemd_backend._WSL_KEEPALIVE_EXEC in out
        assert "/usr/bin/true" not in out
        assert "dbus-launch" not in out
        assert "idle" in out.lower()
        # The Windows path must render with a single backslash; the doubled
        # form was an escaping bug in the printed guidance.
        assert "%USERPROFILE%\\.claude" in out
        assert "%USERPROFILE%\\\\.claude" not in out

    def test_wsl_install_warns_on_windows_side_config_dir(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        # CLAUDE_CONFIG_DIR on /mnt/<drive> means the session PID files hold
        # Windows PIDs, invisible to WSL's PID namespace — the engine would
        # judge every Windows-side session dead. Say so once at install.
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/mnt/c/Users/dev/.claude")
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        monkeypatch.setattr(
            systemd_backend, "_unit_path", lambda: _unit_path(config_home)
        )
        monkeypatch.setattr(subprocess, "run", _stub_run())
        monkeypatch.setattr(systemd_backend.service_spec, "is_wsl", lambda: True)

        systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())

        out = capsys.readouterr().out
        assert "points at the Windows side" in out
        assert "/mnt/c/Users/dev/.claude" in out

    def test_wsl_install_stays_quiet_for_wsl_side_config_dir(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        monkeypatch.setattr(
            systemd_backend, "_unit_path", lambda: _unit_path(config_home)
        )
        monkeypatch.setattr(subprocess, "run", _stub_run())
        monkeypatch.setattr(systemd_backend.service_spec, "is_wsl", lambda: True)

        systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())

        assert "points at the Windows side" not in capsys.readouterr().out

    def test_wsl_keepalive_command_matches_readme(self):
        # The README documents the same Task Scheduler command; keep the two
        # surfaces in lockstep so users never see conflicting guidance.
        readme = (Path(systemd_backend.__file__).parents[3] / "README.md").read_text(
            encoding="utf-8"
        )
        assert systemd_backend._WSL_KEEPALIVE_EXEC in readme
        assert "--exec /usr/bin/true" not in readme
        assert "dbus-launch" not in readme

    def test_linger_failure_tolerated_with_warning(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        monkeypatch.setattr(
            systemd_backend, "_unit_path", lambda: _unit_path(config_home)
        )
        monkeypatch.setattr(systemd_backend.service_spec, "is_wsl", lambda: False)

        def fake_run(argv, **kwargs):
            completed = MagicMock()
            if argv[0] == "loginctl":
                completed.returncode = 1
                completed.stdout = ""
                completed.stderr = "permission denied"
            else:
                completed.returncode = 0
                completed.stdout = ""
                completed.stderr = ""
            return completed

        monkeypatch.setattr(subprocess, "run", fake_run)

        rc = systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())
        assert rc == 0
        out = capsys.readouterr().out
        assert "enable-linger" in out

    def test_no_systemd_raises_actionable_error(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_linux(monkeypatch)
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: False)
        with pytest.raises(ClaudeSwitchError, match="systemd=true"):
            systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())

    def test_enable_failure_raises(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        monkeypatch.setattr(
            systemd_backend, "_unit_path", lambda: _unit_path(config_home)
        )

        def fake_run(argv, **kwargs):
            completed = MagicMock()
            if "enable" in argv:
                completed.returncode = 1
                completed.stderr = "failed"
            else:
                completed.returncode = 0
                completed.stderr = ""
            completed.stdout = ""
            return completed

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(ClaudeSwitchError, match="enable"):
            systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())


class TestUninstall:
    def test_disables_removes_unit_and_reloads(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        unit_path = _unit_path(config_home)
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text("[Unit]\n")
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kwargs: calls.append(list(argv)) or _stub_run()(),
        )

        rc = systemd_backend.SystemdBackend().uninstall(ClaudeAccountSwitcher())

        assert rc == 0
        assert not unit_path.exists()
        assert [
            "systemctl",
            "--user",
            "disable",
            "--now",
            systemd_backend.UNIT_NAME,
        ] in calls
        assert ["systemctl", "--user", "daemon-reload"] in calls
        assert "Service removed" in capsys.readouterr().out

    def test_idempotent_when_absent(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_linux(monkeypatch)
        config_home = temp_home / ".config"
        unit_path = _unit_path(config_home)
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        monkeypatch.setattr(subprocess, "run", _stub_run(returncode=1))

        rc = systemd_backend.SystemdBackend().uninstall(ClaudeAccountSwitcher())

        assert rc == 0
        assert "was not installed" in capsys.readouterr().out


class TestState:
    @pytest.mark.parametrize(
        ("active_rc", "active_out", "expected"),
        [
            (0, "active\n", "loaded"),
            (3, "inactive\n", "installed but not loaded"),
            (1, "failed\n", "installed but not loaded"),
        ],
    )
    def test_maps_systemctl_is_active(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        active_rc: int,
        active_out: str,
        expected: str,
    ):
        config_home = temp_home / ".config"
        unit_path = _unit_path(config_home)
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text("[Unit]\n")
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)

        def fake_run(argv, **kwargs):
            completed = MagicMock()
            if argv[-2:] == ["is-active", systemd_backend.UNIT_NAME]:
                completed.returncode = active_rc
                completed.stdout = active_out
            else:
                completed.returncode = 0
                completed.stdout = ""
            completed.stderr = ""
            return completed

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert systemd_backend.SystemdBackend().state() == expected

    def test_not_installed_without_unit_file(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        config_home = temp_home / ".config"
        monkeypatch.setattr(
            systemd_backend,
            "_unit_path",
            lambda: _unit_path(config_home),
        )
        assert systemd_backend.SystemdBackend().state() == "not installed"


class TestInstalledVersion:
    def test_reads_version_from_unit_environment(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        config_home = temp_home / ".config"
        unit_path = _unit_path(config_home)
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text('Environment=CSWAP_INSTALLED_VERSION="9.9.9"\n')
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        assert systemd_backend._installed_version() == "9.9.9"

    def test_reads_version_from_whole_assignment_quoted_form(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The current emit form wraps the whole KEY=value assignment in quotes.
        config_home = temp_home / ".config"
        unit_path = _unit_path(config_home)
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text(
            'Environment="CLAUDE_CONFIG_DIR=/My Drive/.claude"\n'
            'Environment="CSWAP_INSTALLED_VERSION=9.9.9"\n'
        )
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        assert systemd_backend._installed_version() == "9.9.9"


class TestSelectBackendLinux:
    def test_linux_selects_systemd_backend(self, monkeypatch: pytest.MonkeyPatch):
        from claude_swap.service_backends import select_backend
        from claude_swap.service_backends.systemd import SystemdBackend

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        backend = select_backend()
        assert isinstance(backend, SystemdBackend)


class TestStatus:
    def _fake_run(self, responses: dict[str, tuple[int, str, str]]):
        """Dispatch on the systemctl verb; default to rc=0 empty output."""

        def run(argv, **kwargs):
            completed = MagicMock()
            rc, stdout, stderr = (0, "", "")
            for verb, response in responses.items():
                if verb in argv:
                    rc, stdout, stderr = response
                    break
            completed.returncode = rc
            completed.stdout = stdout
            completed.stderr = stderr
            return completed

        return run

    def test_not_installed_prints_and_skips_systemctl(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        unit_path = _unit_path(temp_home / ".config")
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        sentinel = MagicMock(side_effect=AssertionError("must not run systemctl"))
        monkeypatch.setattr(subprocess, "run", sentinel)

        rc = systemd_backend.SystemdBackend().status(ClaudeAccountSwitcher())

        assert rc == 0
        assert "not installed" in capsys.readouterr().out

    def test_installed_but_not_loaded_prints_reload_hint(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        unit_path = _unit_path(temp_home / ".config")
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text("[Unit]\n")
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        monkeypatch.setattr(
            subprocess,
            "run",
            self._fake_run({"is-active": (3, "inactive\n", "")}),
        )

        rc = systemd_backend.SystemdBackend().status(ClaudeAccountSwitcher())

        assert rc == 0
        out = capsys.readouterr().out
        assert "installed but not loaded" in out
        assert "cswap service install" in out

    def test_loaded_surfaces_state_lines_and_decision_log(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        unit_path = _unit_path(temp_home / ".config")
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text("[Unit]\n")
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        monkeypatch.setattr(
            subprocess,
            "run",
            self._fake_run(
                {
                    "is-active": (0, "active\n", ""),
                    # Real `systemctl show -p ActiveState -p MainPID` output.
                    "show": (0, "ActiveState=active\nMainPID=4242\n", ""),
                }
            ),
        )

        switcher = ClaudeAccountSwitcher()
        rc = systemd_backend.SystemdBackend().status(switcher)

        assert rc == 0
        out = capsys.readouterr().out
        assert "loaded" in out
        assert "ActiveState=active" in out
        assert "MainPID=4242" in out
        assert "decision log" in out

    def test_loaded_warns_on_version_drift(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        unit_path = _unit_path(temp_home / ".config")
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text(
            '[Service]\nEnvironment="CSWAP_INSTALLED_VERSION=0.0.1"\n'
        )
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        monkeypatch.setattr(
            subprocess, "run", self._fake_run({"is-active": (0, "active\n", "")})
        )

        systemd_backend.SystemdBackend().status(ClaudeAccountSwitcher())

        out = capsys.readouterr().out
        assert "0.0.1" in out
        assert "cswap service install" in out


class TestLogs:
    def test_missing_structured_log_and_unloaded_unit(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        def run(argv, **kwargs):
            completed = MagicMock()
            completed.returncode = 4
            completed.stdout = ""
            completed.stderr = "Unit cswap-monitor.service could not be found."
            return completed

        monkeypatch.setattr(subprocess, "run", run)

        rc = systemd_backend.SystemdBackend().logs(ClaudeAccountSwitcher())

        assert rc == 0
        out = capsys.readouterr().out
        assert "claude-swap.log (structured)" in out
        assert "(none yet)" in out
        assert "(unit not loaded yet)" in out

    def test_tails_structured_log_and_journal(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        switcher = ClaudeAccountSwitcher()
        switcher.backup_dir.mkdir(parents=True, exist_ok=True)
        (switcher.backup_dir / "claude-swap.log").write_text(
            "old-line\nrecent-line-1\nrecent-line-2\n"
        )

        def run(argv, **kwargs):
            completed = MagicMock()
            completed.returncode = 0
            completed.stderr = ""
            completed.stdout = (
                "journal-line-1\njournal-line-2\n" if argv[0] == "journalctl" else ""
            )
            return completed

        monkeypatch.setattr(subprocess, "run", run)

        rc = systemd_backend.SystemdBackend().logs(switcher, lines=2)

        assert rc == 0
        out = capsys.readouterr().out
        assert "recent-line-1" in out
        assert "recent-line-2" in out
        assert "old-line" not in out
        assert "journal-line-1" in out

    def test_journal_failure_reported_cleanly(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        def run(argv, **kwargs):
            completed = MagicMock()
            completed.stdout = ""
            if argv[0] == "journalctl":
                completed.returncode = 1
                completed.stderr = ""
            else:
                completed.returncode = 0
                completed.stderr = ""
            return completed

        monkeypatch.setattr(subprocess, "run", run)

        rc = systemd_backend.SystemdBackend().logs(ClaudeAccountSwitcher())

        assert rc == 0
        assert "(no journal entries yet)" in capsys.readouterr().out


class TestHelpers:
    def test_run_timeout_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(
                side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=10)
            ),
        )
        with pytest.raises(ClaudeSwitchError, match="timed out"):
            systemd_backend._systemctl("is-active", "x")

    def test_unit_path_honors_xdg_config_home(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(temp_home / "xdg"))
        assert systemd_backend._unit_path() == (
            temp_home / "xdg" / "systemd" / "user" / systemd_backend.UNIT_NAME
        )
        monkeypatch.delenv("XDG_CONFIG_HOME")
        assert systemd_backend._unit_path() == (
            temp_home / ".config" / "systemd" / "user" / systemd_backend.UNIT_NAME
        )

    def test_pid1_detection_reads_proc_comm(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "read_text", lambda self, **kw: "systemd\n")
        assert systemd_backend._pid1_is_systemd() is True
        monkeypatch.setattr(
            Path,
            "read_text",
            lambda self, **kw: (_ for _ in ()).throw(OSError("no /proc")),
        )
        assert systemd_backend._pid1_is_systemd() is False

    def test_escape_quotes_empty_and_spaced_arguments(self):
        assert systemd_backend._systemd_escape("") == '""'
        assert systemd_backend._systemd_escape("/plain/path") == "/plain/path"
        assert systemd_backend._systemd_escape("has space") == '"has space"'

    def test_escape_doubles_percent_specifiers(self):
        # systemd expands % specifiers even inside quotes; an unescaped %h
        # in a path would expand (or fail daemon-reload as an invalid
        # specifier) instead of staying literal.
        assert systemd_backend._systemd_escape("%h") == "%%h"
        assert (
            systemd_backend._systemd_escape("/tmp/pct%40home/bin")
            == "/tmp/pct%%40home/bin"
        )
        assert systemd_backend._systemd_escape("has space %i") == '"has space %%i"'
        assert systemd_backend._systemd_escape_value("50%") == "50%%"

    def test_env_value_escape_round_trips_percent(self):
        # Writing doubles % after the backslash pass; reading must fold %%
        # back or a re-read value drifts from what was installed.
        for value in ("50%", "%", "100%%", '/pct%40/"dir"\\x', "%h %i"):
            escaped = systemd_backend._systemd_escape_value(value)
            assert systemd_backend._unescape_env_value(escaped) == value

    def test_unit_with_percent_in_env_value_is_escaped(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/data/pct%40home/.claude")
        switcher = ClaudeAccountSwitcher()
        unit = systemd_backend._build_unit(switcher)
        assert "pct%%40home" in unit
        assert "pct%40home" not in unit.replace("%%", "")

    def test_installed_version_none_when_unit_missing(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            systemd_backend, "_unit_path", lambda: temp_home / "absent.service"
        )
        assert systemd_backend._installed_version() is None

    def test_installed_version_skips_env_lines_without_assignment(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        unit_path = temp_home / "unit.service"
        unit_path.write_text(
            "[Service]\n"
            "Environment=NOASSIGNMENT\n"
            'Environment="CSWAP_INSTALLED_VERSION=1.2.3"\n'
        )
        monkeypatch.setattr(systemd_backend, "_unit_path", lambda: unit_path)
        assert systemd_backend._installed_version() == "1.2.3"


class TestRequireSystemd:
    """The pre-flight guard must also verify the per-user manager, not just PID 1."""

    def test_raises_when_user_manager_offline(self, monkeypatch: pytest.MonkeyPatch):
        _force_linux(monkeypatch)
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        # `systemctl --user is-system-running` -> "offline": manager unreachable.
        monkeypatch.setattr(
            subprocess,
            "run",
            _stub_run(returncode=1, stdout="offline"),
        )
        with pytest.raises(ClaudeSwitchError, match="per-user manager"):
            systemd_backend._require_systemd()

    def test_passes_when_user_manager_degraded(self, monkeypatch: pytest.MonkeyPatch):
        _force_linux(monkeypatch)
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        # "degraded" still means the user manager is up — must not raise.
        monkeypatch.setattr(
            subprocess,
            "run",
            _stub_run(returncode=1, stdout="degraded"),
        )
        systemd_backend._require_systemd()

    def test_passes_when_bus_reachable_but_empty(self, monkeypatch: pytest.MonkeyPatch):
        _force_linux(monkeypatch)
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        monkeypatch.setattr(
            subprocess, "run", _stub_run(returncode=0, stdout="")
        )
        systemd_backend._require_systemd()

    def test_raises_when_user_bus_connection_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _force_linux(monkeypatch)
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        # No status output at all plus a bus error: no session D-Bus (headless
        # SSH / WSL without linger) — enable --now would fail confusingly.
        monkeypatch.setattr(
            subprocess,
            "run",
            _stub_run(
                returncode=1,
                stdout="",
                stderr="Failed to connect to bus: No medium found",
            ),
        )
        with pytest.raises(ClaudeSwitchError, match="enable-linger"):
            systemd_backend._require_systemd()

    def test_raises_on_non_linux_platform(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        with pytest.raises(ClaudeSwitchError, match="requires Linux or WSL"):
            systemd_backend._require_systemd()
