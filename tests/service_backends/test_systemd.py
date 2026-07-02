"""Tests for the Linux/WSL systemd --user service backend."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_swap import service
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.service_backends import systemd as systemd_backend
from claude_swap.switcher import ClaudeAccountSwitcher


def _force_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "linux")


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

        monkeypatch.setattr(service.sys, "platform", "linux")
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

        monkeypatch.setattr(service.sys, "platform", "linux")
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

        monkeypatch.setattr(service.sys, "platform", "darwin")
        assert service_spec.is_wsl() is False


class TestBuildUnit:
    def test_execstart_uses_program_arguments(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        unit = systemd_backend._build_unit(switcher)
        assert f"ExecStart={sys.executable}" in unit.replace('"', "")
        assert "claude_swap" in unit
        assert "--monitor" in unit
        assert "Restart=on-failure" in unit
        assert "RestartSec=30" in unit
        assert "WantedBy=default.target" in unit
        assert "Description=Claude Swap auto-switch monitor" in unit

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

        monkeypatch.setattr(service.subprocess, "run", fake_run)
        monkeypatch.setattr(systemd_backend.service_spec, "is_wsl", lambda: False)

        switcher = ClaudeAccountSwitcher()
        rc = systemd_backend.SystemdBackend().install(switcher)

        assert rc == 0
        assert unit_path.exists()
        text = unit_path.read_text(encoding="utf-8")
        assert "ExecStart=" in text
        assert "--monitor" in text
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
        monkeypatch.setattr(service.subprocess, "run", _stub_run())

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
        monkeypatch.setattr(service.subprocess, "run", _stub_run())
        monkeypatch.setattr(systemd_backend.service_spec, "is_wsl", lambda: True)

        systemd_backend.SystemdBackend().install(ClaudeAccountSwitcher())

        out = capsys.readouterr().out
        assert "WSL note" in out
        assert "wsl.exe -d Ubuntu -u dev" in out
        assert "idle" in out.lower()
        assert ".claude" in out

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

        monkeypatch.setattr(service.subprocess, "run", fake_run)

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

        monkeypatch.setattr(service.subprocess, "run", fake_run)
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
            service.subprocess,
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
        monkeypatch.setattr(service.subprocess, "run", _stub_run(returncode=1))

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

        monkeypatch.setattr(service.subprocess, "run", fake_run)
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

        monkeypatch.setattr(service.sys, "platform", "linux")
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        backend = select_backend()
        assert isinstance(backend, SystemdBackend)
        assert backend.platform_label == "systemd"


class TestRequireSystemd:
    """The pre-flight guard must also verify the per-user manager, not just PID 1."""

    def test_raises_when_user_manager_offline(self, monkeypatch: pytest.MonkeyPatch):
        _force_linux(monkeypatch)
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        # `systemctl --user is-system-running` -> "offline": manager unreachable.
        monkeypatch.setattr(
            service.subprocess,
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
            service.subprocess,
            "run",
            _stub_run(returncode=1, stdout="degraded"),
        )
        systemd_backend._require_systemd()

    def test_passes_when_bus_reachable_but_empty(self, monkeypatch: pytest.MonkeyPatch):
        _force_linux(monkeypatch)
        monkeypatch.setattr(systemd_backend, "_pid1_is_systemd", lambda: True)
        monkeypatch.setattr(
            service.subprocess, "run", _stub_run(returncode=0, stdout="")
        )
        systemd_backend._require_systemd()
