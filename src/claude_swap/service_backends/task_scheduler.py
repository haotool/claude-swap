"""Windows Task Scheduler backend for the auto-switch engine.

The odd one out among the backends: Task Scheduler has no real supervisor
semantics, so the task XML approximates them — a crashed engine is revived
by repeating logon and time triggers plus ``MultipleInstancesPolicy=IgnoreNew``,
and the version stamp lives in ``RegistrationInfo/Version`` because the
schema has no per-task environment variables. ``pythonw.exe`` keeps the
hidden task from flashing a console window; the persisted XML under the
log dir doubles as the version-drift record.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from xml.dom import minidom

from claude_swap import __version__, service_spec
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import bolded, dimmed, muted, warning
from claude_swap.protocols import ServiceState
from claude_swap.protocols import ServiceHost

_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_VERSION_RE = re.compile(r"<Version>([^<]+)</Version>")
# Legacy version stamp: older fork versions recorded the version as an Exec
# environment variable in the persisted XML. Keep parsing it so the
# version-drift reinstall prompt still fires for those installs.
_ENV_VAR_RE = re.compile(
    r'<Variable Name="([^"]+)" Value="([^"]*)"\s*/>',
)


def _task_xml_path(switcher: ServiceHost) -> Path:
    return service_spec.log_dir(switcher) / f"{service_spec.SERVICE_ID}.xml"


def _resolve_python_executable() -> str:
    """Return absolute ``pythonw.exe`` when present, else ``python.exe``.

    Deliberately not ``.resolve()``d: dereferencing a venv symlink would run
    the base interpreter without the venv's ``pyvenv.cfg`` search path, and
    ``-m claude_swap`` then fails. launchd/systemd supervise the unresolved
    ``sys.executable`` for the same reason.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def _program_arguments() -> list[str]:
    # Same argv as service_spec.program_arguments, but through pythonw.exe so
    # the hidden task never flashes a console window.
    return [_resolve_python_executable(), *service_spec.program_arguments()[1:]]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ClaudeSwitchError(
            "cswap service (Task Scheduler) requires Windows. "
            "Use `cswap auto` in the foreground on this platform."
        )


def _powershell(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return service_spec.run_service_command(
        [
            service_spec.powershell_exe(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=check,
    )


def _task_name_literal() -> str:
    return service_spec.SERVICE_ID.replace("'", "''")


def _build_task_xml(switcher: ServiceHost) -> str:
    argv = _program_arguments()
    command = argv[0]
    if " " in command:
        # Task Scheduler does not reliably treat an unquoted spaced path
        # ("C:\Program Files\...") as one command; quote it, matching the
        # argv escaping launchd/systemd already apply. Windows paths cannot
        # contain '"', so wrapping is enough.
        command = f'"{command}"'
    arguments = " ".join(argv[1:])

    ET.register_namespace("", _TASK_NS)
    root = ET.Element(f"{{{_TASK_NS}}}Task", {"version": "1.4"})

    reg_info = ET.SubElement(root, f"{{{_TASK_NS}}}RegistrationInfo")
    ET.SubElement(reg_info, f"{{{_TASK_NS}}}Description").text = (
        "Claude Swap auto-switch engine"
    )
    # Version drift is stamped here because the Exec action cannot carry it:
    # the schema allows only Command / Arguments / WorkingDirectory under
    # Exec, and Register-ScheduledTask rejects anything else with
    # SCHED_E_UNEXPECTEDNODE. RegistrationInfo/Version is a schema-valid slot.
    ET.SubElement(reg_info, f"{{{_TASK_NS}}}Version").text = __version__

    # DOMAIN\user rather than the bare username: bare names may not resolve
    # for AzureAD/domain accounts, and the same identity scopes the logon
    # trigger below. USERDOMAIN is the machine name on workgroup setups, so
    # the composed form is valid everywhere it exists.
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    if domain:
        user = f"{domain}\\{user}"

    triggers = ET.SubElement(root, f"{{{_TASK_NS}}}Triggers")
    logon = ET.SubElement(triggers, f"{{{_TASK_NS}}}LogonTrigger")
    # The repeating trigger is the explicit watchdog. IgnoreNew only prevents
    # another instance of this same scheduled task while its action is still
    # running; it is not a process-wide singleton for every `cswap auto` entry.
    repetition = ET.SubElement(logon, f"{{{_TASK_NS}}}Repetition")
    ET.SubElement(repetition, f"{{{_TASK_NS}}}Interval").text = "PT5M"
    ET.SubElement(repetition, f"{{{_TASK_NS}}}StopAtDurationEnd").text = "false"
    ET.SubElement(logon, f"{{{_TASK_NS}}}Enabled").text = "true"
    # Scope the trigger to the installing user: on a multi-user machine any
    # logon would otherwise fire the task, which then fails under the other
    # user's InteractiveToken and litters the task history with noise.
    ET.SubElement(logon, f"{{{_TASK_NS}}}UserId").text = user
    # The logon trigger's repetition only arms on an actual logon;
    # Start-ScheduledTask (the install-time kick) arms no trigger at all. This
    # TimeTrigger anchors the same periodic retry in the install session.
    time_trigger = ET.SubElement(triggers, f"{{{_TASK_NS}}}TimeTrigger")
    time_repetition = ET.SubElement(time_trigger, f"{{{_TASK_NS}}}Repetition")
    ET.SubElement(time_repetition, f"{{{_TASK_NS}}}Interval").text = "PT5M"
    ET.SubElement(time_repetition, f"{{{_TASK_NS}}}StopAtDurationEnd").text = "false"
    ET.SubElement(time_trigger, f"{{{_TASK_NS}}}StartBoundary").text = (
        datetime.now().replace(microsecond=0).isoformat()
    )
    ET.SubElement(time_trigger, f"{{{_TASK_NS}}}Enabled").text = "true"

    principals = ET.SubElement(root, f"{{{_TASK_NS}}}Principals")
    principal = ET.SubElement(
        principals,
        f"{{{_TASK_NS}}}Principal",
        {"id": "Author"},
    )
    ET.SubElement(principal, f"{{{_TASK_NS}}}UserId").text = user
    ET.SubElement(principal, f"{{{_TASK_NS}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{_TASK_NS}}}RunLevel").text = "LeastPrivilege"

    settings = ET.SubElement(root, f"{{{_TASK_NS}}}Settings")
    ET.SubElement(settings, f"{{{_TASK_NS}}}MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, f"{{{_TASK_NS}}}StartWhenAvailable").text = "true"
    ET.SubElement(settings, f"{{{_TASK_NS}}}Hidden").text = "true"
    ET.SubElement(settings, f"{{{_TASK_NS}}}Enabled").text = "true"
    # The engine is a resident process, so the schema defaults are hostile:
    # ExecutionTimeLimit defaults to PT72H (task hard-killed after 72 hours)
    # and the battery settings default to true (never starts on battery,
    # killed when unplugging). PT0S means "no time limit".
    ET.SubElement(settings, f"{{{_TASK_NS}}}ExecutionTimeLimit").text = "PT0S"
    ET.SubElement(settings, f"{{{_TASK_NS}}}DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{_TASK_NS}}}StopIfGoingOnBatteries").text = "false"
    # RestartOnFailure is supplemental; the periodic triggers above are the
    # behavior this backend relies on for recurring supervision.
    restart = ET.SubElement(settings, f"{{{_TASK_NS}}}RestartOnFailure")
    ET.SubElement(restart, f"{{{_TASK_NS}}}Interval").text = "PT1M"
    ET.SubElement(restart, f"{{{_TASK_NS}}}Count").text = "3"

    actions = ET.SubElement(root, f"{{{_TASK_NS}}}Actions", {"Context": "Author"})
    exec_action = ET.SubElement(actions, f"{{{_TASK_NS}}}Exec")
    ET.SubElement(exec_action, f"{{{_TASK_NS}}}Command").text = command
    ET.SubElement(exec_action, f"{{{_TASK_NS}}}Arguments").text = arguments

    rough = ET.tostring(root, encoding="unicode")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ")


def _installed_version_from_xml(text: str) -> str | None:
    version_match = _VERSION_RE.search(text)
    if version_match:
        return version_match.group(1)
    env_vars: dict[str, str] = {}
    for match in _ENV_VAR_RE.finditer(text):
        env_vars[match.group(1)] = match.group(2)
    return service_spec.installed_version_from_env(env_vars)


def _installed_version(switcher: ServiceHost) -> str | None:
    xml_path = _task_xml_path(switcher)
    try:
        text = xml_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _installed_version_from_xml(text)


def _task_error(action: str, proc: subprocess.CompletedProcess[str]) -> ClaudeSwitchError:
    detail = (proc.stderr or proc.stdout or "").strip()
    return ClaudeSwitchError(
        f"Task Scheduler {action} failed (rc={proc.returncode})"
        + (f": {detail}" if detail else "")
    )


def _query_task_state() -> tuple[bool, str]:
    """Return ``(exists, state)``; raise when Task Scheduler cannot be queried.

    The root task collection must be read successfully before absence can be
    concluded. This avoids treating a suppressed CIM/Task Scheduler failure as
    the same thing as a successful lookup with no matching task.
    """
    name = _task_name_literal()
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$tasks = @(Get-ScheduledTask -TaskPath '\\*' -ErrorAction Stop | "
        f"Where-Object {{ $_.TaskName -eq '{name}' }}); "
        "if ($tasks.Count -eq 0) { exit 2 }; "
        "if ($tasks.Count -ne 1) { Write-Error 'multiple root tasks matched'; exit 3 }; "
        "$tasks[0].State"
    )
    proc = _powershell(script, check=False)
    if proc.returncode == 2:
        return False, ""
    if proc.returncode != 0:
        raise _task_error("query", proc)
    return True, proc.stdout.strip()


def _remove_task() -> bool:
    """Stop and unregister the task, returning whether one was present.

    A missing task is an idempotent success. Running instances are stopped;
    ready/disabled tasks have nothing to stop and go straight to unregister.
    Task Scheduler can take a moment to drop a registration, so the same
    manager operation polls that postcondition for up to five seconds.
    """
    name = _task_name_literal()
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$tasks = @(Get-ScheduledTask -TaskPath '\\*' -ErrorAction Stop | "
        f"Where-Object {{ $_.TaskName -eq '{name}' }}); "
        "if ($tasks.Count -eq 0) { exit 2 }; "
        "if ($tasks.Count -ne 1) { Write-Error 'multiple root tasks matched'; exit 3 }; "
        "$task = $tasks[0]; "
        "if ($task.State -eq 'Running') { Stop-ScheduledTask -InputObject $task -ErrorAction Stop }; "
        "Unregister-ScheduledTask -InputObject $task -Confirm:$false -ErrorAction Stop; "
        "$removed = $false; "
        "for ($i = 0; $i -lt 50; $i++) { "
        "  $remaining = @(Get-ScheduledTask -TaskPath '\\*' -ErrorAction Stop | "
        f"Where-Object {{ $_.TaskName -eq '{name}' }}); "
        "  if ($remaining.Count -eq 0) { $removed = $true; break }; "
        "  Start-Sleep -Milliseconds 100 "
        "}; "
        "if (-not $removed) { Write-Error 'task is still registered after 5 seconds'; exit 3 }"
    )
    proc = _powershell(script, check=False)
    if proc.returncode == 2:
        return False
    if proc.returncode != 0:
        raise _task_error("removal", proc)
    return True


def _register_task(xml_path: Path) -> None:
    name = _task_name_literal()
    path_literal = str(xml_path).replace("'", "''")
    script = (
        f"$xml = Get-Content -LiteralPath '{path_literal}' -Raw -Encoding UTF8; "
        f"Register-ScheduledTask -TaskName '{name}' -Xml $xml -Force"
    )
    _powershell(script)


def _start_task() -> None:
    """Start the registered task immediately or surface the failure."""
    name = _task_name_literal()
    _powershell(f"Start-ScheduledTask -TaskName '{name}'")


class TaskSchedulerBackend:
    """Windows Task Scheduler supervisor implementing ``ServiceBackend``."""

    def install(self, switcher: ServiceHost) -> int:
        _require_windows()
        log_dir = service_spec.log_dir(switcher)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(switcher.backup_dir, 0o700)
        os.chmod(log_dir, 0o700)
        xml_path = _task_xml_path(switcher)
        xml_path.write_text(_build_task_xml(switcher), encoding="utf-8")

        # Reinstall is idempotent, but replacing the previous registration is a
        # real lifecycle transition; manager failures must stop the install.
        _remove_task()
        _register_task(xml_path)
        try:
            _start_task()
        except ClaudeSwitchError as start_error:
            # Registration succeeded but the promised immediate start did not.
            # Remove the new future-triggering task before surfacing failure.
            try:
                _remove_task()
            except ClaudeSwitchError as cleanup_error:
                raise ClaudeSwitchError(
                    f"{start_error}; cleanup also failed: {cleanup_error}"
                ) from start_error
            raise

        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            # launchd/systemd forward CLAUDE_CONFIG_DIR to the engine, but
            # the task XML schema cannot carry environment variables — the
            # engine only sees what the user account's baseline environment
            # holds, and with the default config dir it would manage the
            # wrong (empty) account set.
            warning(
                "CLAUDE_CONFIG_DIR is set in this shell, but Task Scheduler "
                "cannot forward it to the background engine. Make it a "
                "user-level environment variable first:\n"
                f'  setx CLAUDE_CONFIG_DIR "{config_dir}"\n'
                "then run `cswap service install` again from a new shell."
            )
        command = service_spec.RUNNER_COMMAND_LABEL
        service_spec.print_install_success(
            switcher,
            artifact_path=xml_path,
            run_hint=f"runs `{command}` at logon via Task Scheduler (hidden, per-user)",
        )
        return 0

    def uninstall(self, switcher: ServiceHost) -> int:
        xml_path = _task_xml_path(switcher)
        registered, _ = _query_task_state()
        existed = registered or xml_path.exists()
        if registered:
            _remove_task()
        # Only discard the local definition after the manager has confirmed
        # the task absent. On failure it remains useful for diagnosis/retry.
        xml_path.unlink(missing_ok=True)
        service_spec.print_uninstall_result(
            switcher,
            existed=existed,
            retained_hint="task XML backup removed",
        )
        return 0

    def state(self) -> ServiceState:
        exists, task_state = _query_task_state()
        if not exists:
            return "not installed"
        if task_state.lower() == "disabled":
            return "installed but not loaded"
        return "loaded"

    def status(self, switcher: ServiceHost) -> int:
        current = self.state()
        if current == "not installed":
            service_spec.print_status_not_installed()
            return 0

        installed_ver = _installed_version(switcher)
        service_spec.warn_version_drift(installed_ver)

        if current == "installed but not loaded":
            service_spec.print_status_installed_but_not_loaded()
            return 0

        exists, task_state = _query_task_state()
        stdout = f"state = {task_state}" if exists else ""
        service_spec.print_status_loaded(supervisor_stdout=stdout)
        service_spec.print_status_decision_log(switcher)
        return 0

    def logs(self, switcher: ServiceHost, lines: int = 40) -> int:
        structured = switcher.backup_dir / "claude-swap.log"
        print(bolded("== claude-swap.log (structured) =="))
        print(f"  {dimmed(str(structured))}")
        if not structured.exists():
            print(f"  {dimmed('(none yet)')}")
        else:
            tail = structured.read_text(encoding="utf-8", errors="replace").splitlines()[
                -lines:
            ]
            for line in tail:
                print(f"  {muted(line)}")

        print(bolded(f"== Task Scheduler ({service_spec.SERVICE_ID}) =="))
        exists, task_state = _query_task_state()
        if not exists:
            print(f"  {dimmed('(task not registered)')}")
            return 0
        print(f"  {muted(f'State: {task_state}')}")
        return 0