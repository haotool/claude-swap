"""Tests for the Windows Task Scheduler service backend."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_swap import service
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.service_backends import task_scheduler as ts_backend
from claude_swap.switcher import ClaudeAccountSwitcher


def _force_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "win32")


def _stub_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    return MagicMock(return_value=completed)


def _task_xml_path(switcher: ClaudeAccountSwitcher) -> Path:
    return switcher.backup_dir / "logs" / f"{ts_backend.service_spec.SERVICE_ID}.xml"


class TestResolvePythonExecutable:
    def test_prefers_pythonw_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _force_win32(monkeypatch)
        python_dir = tmp_path / "venv" / "Scripts"
        python_dir.mkdir(parents=True)
        python_exe = python_dir / "python.exe"
        python_exe.write_text("", encoding="utf-8")
        pythonw_exe = python_dir / "pythonw.exe"
        pythonw_exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(service.sys, "executable", str(python_exe))

        assert ts_backend._resolve_python_executable() == str(pythonw_exe.resolve())

    def test_falls_back_to_python_when_no_pythonw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _force_win32(monkeypatch)
        python_dir = tmp_path / "venv" / "Scripts"
        python_dir.mkdir(parents=True)
        python_exe = python_dir / "python.exe"
        python_exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(service.sys, "executable", str(python_exe))

        assert ts_backend._resolve_python_executable() == str(python_exe.resolve())

    def test_non_windows_uses_sys_executable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(service.sys, "platform", "darwin")
        assert ts_backend._resolve_python_executable() == str(
            Path(service.sys.executable).resolve()
        )


class TestBuildTaskXml:
    def test_at_logon_trigger_and_restart_settings(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        assert "<LogonTrigger>" in xml
        assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
        assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
        assert "<Hidden>true</Hidden>" in xml
        assert "<Interval>PT1M</Interval>" in xml
        assert "<Count>3</Count>" in xml

    def test_action_uses_program_arguments(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_win32(monkeypatch)
        monkeypatch.setattr(
            ts_backend,
            "_program_arguments",
            lambda: [r"C:\venv\Scripts\pythonw.exe", "-m", "claude_swap", "--monitor"],
        )
        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        assert "<Command>C:\\venv\\Scripts\\pythonw.exe</Command>" in xml
        assert "<Arguments>-m claude_swap --monitor</Arguments>" in xml

    def test_logon_trigger_repeats_as_watchdog(self, temp_home: Path):
        # Task Scheduler's RestartOnFailure ignores exit codes (it only fires
        # when the action fails to launch), so exit 75 alone would never be
        # retried. The repeating trigger re-launches the task periodically and
        # MultipleInstancesPolicy=IgnoreNew de-duplicates while it is alive.
        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        assert "<Repetition>" in xml
        assert "<Interval>PT5M</Interval>" in xml
        assert "<StopAtDurationEnd>false</StopAtDurationEnd>" in xml
        # The repetition must live inside the trigger, not a Settings block.
        assert xml.index("<Repetition>") < xml.index("</LogonTrigger>")

    def test_long_running_monitor_settings(self, temp_home: Path):
        # Schema defaults would kill the resident monitor: ExecutionTimeLimit
        # defaults to PT72H, and both battery settings default to true.
        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
        assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
        assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml

    def test_arguments_carry_service_monitor_flag(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        assert "--monitor --service-monitor" in xml

    def test_no_environment_variables_element(self, temp_home: Path):
        # The Task Scheduler XML schema only allows Command / Arguments /
        # WorkingDirectory under Exec; an EnvironmentVariables node makes
        # Register-ScheduledTask fail with SCHED_E_UNEXPECTEDNODE.
        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        assert "EnvironmentVariables" not in xml
        assert "<Variable " not in xml

    def test_xml_escapes_special_chars_in_program_path(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_win32(monkeypatch)
        monkeypatch.setattr(
            ts_backend,
            "_program_arguments",
            lambda: [
                r"C:\Program Files\py & co\pythonw.exe",
                "-m",
                "claude_swap",
                "--monitor",
            ],
        )
        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        # ElementTree must escape & (and keep the space verbatim); a raw
        # ampersand would make the task XML invalid.
        assert "<Command>C:\\Program Files\\py &amp; co\\pythonw.exe</Command>" in xml
        assert "py & co" not in xml

    def test_register_task_doubles_single_quotes_in_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _force_win32(monkeypatch)
        captured: dict[str, str] = {}
        monkeypatch.setattr(
            ts_backend,
            "_powershell",
            lambda script, **kw: captured.setdefault("script", script),
        )
        xml_path = tmp_path / "o'brien" / "task.xml"
        ts_backend._register_task(xml_path)
        # A single quote in the path must be doubled inside the PowerShell
        # single-quoted -LiteralPath literal, else the command breaks/injects.
        assert "o''brien" in captured["script"]
        assert "-LiteralPath" in captured["script"]

    def test_stamps_installed_version(self, temp_home: Path):
        from claude_swap import __version__

        switcher = ClaudeAccountSwitcher()
        xml = ts_backend._build_task_xml(switcher)
        assert f"<Version>{__version__}</Version>" in xml


class TestInstall:
    def test_registers_task_with_powershell(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_win32(monkeypatch)
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            completed = MagicMock()
            completed.returncode = 0
            completed.stdout = ""
            completed.stderr = ""
            return completed

        monkeypatch.setattr(service.subprocess, "run", fake_run)

        switcher = ClaudeAccountSwitcher()
        rc = ts_backend.TaskSchedulerBackend().install(switcher)

        assert rc == 0
        xml_path = _task_xml_path(switcher)
        assert xml_path.exists()
        text = xml_path.read_text(encoding="utf-8")
        assert "<LogonTrigger>" in text
        assert "--monitor" in text
        assert (switcher.backup_dir / "logs").is_dir()

        ps_calls = [c for c in calls if c and c[0] == "powershell"]
        assert len(ps_calls) >= 2
        scripts = " ".join(c[-1] for c in ps_calls)
        assert "Unregister-ScheduledTask" in scripts
        assert "Register-ScheduledTask" in scripts
        assert "Start-ScheduledTask" in scripts
        assert ts_backend.service_spec.SERVICE_ID in scripts
        assert "-Force" in scripts

        out = capsys.readouterr().out
        assert "Service installed" in out
        assert "Task Scheduler" in out

    def test_idempotent_reinstall_overwrites_xml(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_win32(monkeypatch)
        switcher = ClaudeAccountSwitcher()
        xml_path = _task_xml_path(switcher)
        xml_path.parent.mkdir(parents=True)
        xml_path.write_text("stale xml\n", encoding="utf-8")
        monkeypatch.setattr(service.subprocess, "run", _stub_run())

        ts_backend.TaskSchedulerBackend().install(switcher)
        assert "<LogonTrigger>" in xml_path.read_text(encoding="utf-8")

    def test_register_failure_raises(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _force_win32(monkeypatch)

        def fake_run(argv, **kwargs):
            completed = MagicMock()
            if "Register-ScheduledTask" in argv[-1]:
                completed.returncode = 1
                completed.stderr = "access denied"
            else:
                completed.returncode = 0
                completed.stderr = ""
            completed.stdout = ""
            return completed

        monkeypatch.setattr(service.subprocess, "run", fake_run)
        with pytest.raises(ClaudeSwitchError, match="Register-ScheduledTask"):
            ts_backend.TaskSchedulerBackend().install(ClaudeAccountSwitcher())

    def test_non_windows_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(service.sys, "platform", "darwin")
        with pytest.raises(ClaudeSwitchError, match="Task Scheduler"):
            ts_backend.TaskSchedulerBackend().install(ClaudeAccountSwitcher())


class TestUninstall:
    def test_unregisters_and_removes_xml(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_win32(monkeypatch)
        switcher = ClaudeAccountSwitcher()
        xml_path = _task_xml_path(switcher)
        xml_path.parent.mkdir(parents=True)
        xml_path.write_text("<Task></Task>", encoding="utf-8")
        calls: list[list[str]] = []
        monkeypatch.setattr(
            service.subprocess,
            "run",
            lambda argv, **kwargs: calls.append(list(argv)) or _stub_run()(),
        )
        monkeypatch.setattr(ts_backend, "_query_task_state", lambda: (True, "Ready"))

        rc = ts_backend.TaskSchedulerBackend().uninstall(switcher)

        assert rc == 0
        assert not xml_path.exists()
        scripts = " ".join(c[-1] for c in calls if c[0] == "powershell")
        assert "Unregister-ScheduledTask" in scripts
        assert ts_backend.service_spec.SERVICE_ID in scripts
        assert "Service removed" in capsys.readouterr().out

    def test_idempotent_when_absent(
        self,
        temp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        _force_win32(monkeypatch)
        monkeypatch.setattr(ts_backend, "_query_task_state", lambda: (False, ""))
        monkeypatch.setattr(service.subprocess, "run", _stub_run())

        rc = ts_backend.TaskSchedulerBackend().uninstall(ClaudeAccountSwitcher())

        assert rc == 0
        assert "was not installed" in capsys.readouterr().out


class TestState:
    @pytest.mark.parametrize(
        ("exists", "task_state", "expected"),
        [
            (False, "", "not installed"),
            (True, "Disabled", "installed but not loaded"),
            (True, "Ready", "loaded"),
            (True, "Running", "loaded"),
        ],
    )
    def test_maps_scheduled_task_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
        exists: bool,
        task_state: str,
        expected: str,
    ):
        monkeypatch.setattr(
            ts_backend,
            "_query_task_state",
            lambda: (exists, task_state),
        )
        assert ts_backend.TaskSchedulerBackend().state() == expected


class TestInstalledVersion:
    def test_reads_version_from_registration_info(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        switcher = ClaudeAccountSwitcher()
        xml_path = _task_xml_path(switcher)
        xml_path.parent.mkdir(parents=True)
        xml_path.write_text(
            "<RegistrationInfo><Version>9.9.9</Version></RegistrationInfo>",
            encoding="utf-8",
        )
        assert ts_backend._installed_version(switcher) == "9.9.9"

    def test_reads_version_from_legacy_env_var_xml(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # XML persisted by older fork versions stamped the version as a
        # (schema-invalid) Exec environment variable; keep the drift warning
        # working for those files so the reinstall prompt still fires.
        switcher = ClaudeAccountSwitcher()
        xml_path = _task_xml_path(switcher)
        xml_path.parent.mkdir(parents=True)
        xml_path.write_text(
            '<Variable Name="CSWAP_INSTALLED_VERSION" Value="9.9.9" />',
            encoding="utf-8",
        )
        assert ts_backend._installed_version(switcher) == "9.9.9"


class TestSelectBackendWindows:
    def test_win32_selects_task_scheduler_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from claude_swap.service_backends import select_backend
        from claude_swap.service_backends.task_scheduler import TaskSchedulerBackend

        monkeypatch.setattr(service.sys, "platform", "win32")
        backend = select_backend()
        assert isinstance(backend, TaskSchedulerBackend)
        assert backend.platform_label == "task_scheduler"
