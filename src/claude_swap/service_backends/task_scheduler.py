"""Windows Task Scheduler backend for the auto-switch monitor."""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from claude_swap import __version__, service_spec
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import bolded, dimmed, muted
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
    """Return absolute ``pythonw.exe`` when present, else ``python.exe``."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw.resolve())
    return str(exe.resolve())


def _program_arguments() -> list[str]:
    return [
        _resolve_python_executable(),
        "-m",
        "claude_swap",
        "--monitor",
        service_spec.SERVICE_MONITOR_FLAG,
    ]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ClaudeSwitchError(
            "cswap service (Task Scheduler) requires Windows. "
            "Use `cswap --monitor` in the foreground on this platform."
        )


def _run(
    argv: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    from claude_swap import service

    try:
        proc: subprocess.CompletedProcess[str] = service.subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=service_spec.SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ClaudeSwitchError(
            f"{' '.join(argv)} timed out after {service_spec.SUBPROCESS_TIMEOUT}s"
        )
    if check and proc.returncode != 0:
        raise ClaudeSwitchError(
            f"{' '.join(argv)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def _powershell(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=check,
    )


def _task_name_literal() -> str:
    return service_spec.SERVICE_ID.replace("'", "''")


def _build_task_xml(switcher: ServiceHost) -> str:
    argv = _program_arguments()
    command = argv[0]
    arguments = " ".join(argv[1:])

    ET.register_namespace("", _TASK_NS)
    root = ET.Element(f"{{{_TASK_NS}}}Task", {"version": "1.4"})

    reg_info = ET.SubElement(root, f"{{{_TASK_NS}}}RegistrationInfo")
    ET.SubElement(reg_info, f"{{{_TASK_NS}}}Description").text = (
        "Claude Swap auto-switch monitor"
    )
    # Version drift is stamped here because the Exec action cannot carry it:
    # the schema allows only Command / Arguments / WorkingDirectory under
    # Exec, and Register-ScheduledTask rejects anything else with
    # SCHED_E_UNEXPECTEDNODE. RegistrationInfo/Version is a schema-valid slot.
    ET.SubElement(reg_info, f"{{{_TASK_NS}}}Version").text = __version__

    triggers = ET.SubElement(root, f"{{{_TASK_NS}}}Triggers")
    logon = ET.SubElement(triggers, f"{{{_TASK_NS}}}LogonTrigger")
    # Task Scheduler has no supervisor semantics for exit codes: once the
    # process launched successfully, any exit status (including the monitor's
    # retryable 75) counts as success and RestartOnFailure never fires — it
    # only covers actions that failed to start at all. The standard watchdog
    # pattern is a repeating trigger: re-fire every five minutes with no end,
    # and let MultipleInstancesPolicy=IgnoreNew drop the re-fire while a
    # monitor instance is still alive. Net effect: a dead monitor is back
    # within five minutes; a healthy one is never disturbed.
    repetition = ET.SubElement(logon, f"{{{_TASK_NS}}}Repetition")
    ET.SubElement(repetition, f"{{{_TASK_NS}}}Interval").text = "PT5M"
    ET.SubElement(repetition, f"{{{_TASK_NS}}}StopAtDurationEnd").text = "false"
    ET.SubElement(logon, f"{{{_TASK_NS}}}Enabled").text = "true"

    principals = ET.SubElement(root, f"{{{_TASK_NS}}}Principals")
    principal = ET.SubElement(
        principals,
        f"{{{_TASK_NS}}}Principal",
        {"id": "Author"},
    )
    user = os.environ.get("USERNAME") or getpass.getuser()
    ET.SubElement(principal, f"{{{_TASK_NS}}}UserId").text = user
    ET.SubElement(principal, f"{{{_TASK_NS}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{_TASK_NS}}}RunLevel").text = "LeastPrivilege"

    settings = ET.SubElement(root, f"{{{_TASK_NS}}}Settings")
    ET.SubElement(settings, f"{{{_TASK_NS}}}MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, f"{{{_TASK_NS}}}StartWhenAvailable").text = "true"
    ET.SubElement(settings, f"{{{_TASK_NS}}}Hidden").text = "true"
    ET.SubElement(settings, f"{{{_TASK_NS}}}Enabled").text = "true"
    # The monitor is a resident process, so the schema defaults are hostile:
    # ExecutionTimeLimit defaults to PT72H (task hard-killed after 72 hours)
    # and the battery settings default to true (never starts on battery,
    # killed when unplugging). PT0S means "no time limit".
    ET.SubElement(settings, f"{{{_TASK_NS}}}ExecutionTimeLimit").text = "PT0S"
    ET.SubElement(settings, f"{{{_TASK_NS}}}DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{_TASK_NS}}}StopIfGoingOnBatteries").text = "false"
    # RestartOnFailure only covers launch failures (bad credentials, ACLs);
    # it does NOT react to exit codes, so it is not the exit-75 retry path —
    # the repeating logon trigger above is. Kept for the launch-failure case.
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


def _unregister_task(*, check: bool = False) -> subprocess.CompletedProcess[str]:
    name = _task_name_literal()
    script = (
        f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false "
        f"-ErrorAction SilentlyContinue"
    )
    return _powershell(script, check=check)


def _register_task(xml_path: Path) -> None:
    name = _task_name_literal()
    path_literal = str(xml_path).replace("'", "''")
    script = (
        f"$xml = Get-Content -LiteralPath '{path_literal}' -Raw -Encoding UTF8; "
        f"Register-ScheduledTask -TaskName '{name}' -Xml $xml -Force"
    )
    _powershell(script)


def _start_task() -> None:
    """Best-effort immediate start so the monitor runs without waiting for the
    next logon (parity with launchd ``bootstrap`` / systemd ``enable --now``).

    Non-fatal: the task is already registered with ``MultipleInstancesPolicy``
    ``IgnoreNew``, and the ``LogonTrigger`` still covers subsequent logons.
    """
    name = _task_name_literal()
    _powershell(f"Start-ScheduledTask -TaskName '{name}'", check=False)


def _query_task_state() -> tuple[bool, str]:
    """Return ``(exists, state)`` where *state* is the Task Scheduler state string."""
    name = _task_name_literal()
    script = (
        f"$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue; "
        f"if ($null -eq $t) {{ exit 2 }}; "
        f"$t.State"
    )
    proc = _powershell(script, check=False)
    if proc.returncode == 2:
        return False, ""
    state = proc.stdout.strip()
    return True, state


class TaskSchedulerBackend:
    """Windows Task Scheduler supervisor implementing ``ServiceBackend``."""

    @property
    def platform_label(self) -> str:
        return "task_scheduler"

    def describe(self) -> str:
        return "Windows Task Scheduler (per-user AtLogOn)"

    def install(self, switcher: ServiceHost) -> int:
        _require_windows()
        log_dir = service_spec.log_dir(switcher)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(switcher.backup_dir, 0o700)
        os.chmod(log_dir, 0o700)
        xml_path = _task_xml_path(switcher)
        xml_path.write_text(_build_task_xml(switcher), encoding="utf-8")
        _unregister_task(check=False)
        _register_task(xml_path)
        _start_task()
        service_spec.print_install_success(
            switcher,
            artifact_path=xml_path,
            run_hint="runs `cswap --monitor` at logon via Task Scheduler (hidden, per-user)",
        )
        return 0

    def uninstall(self, switcher: ServiceHost) -> int:
        xml_path = _task_xml_path(switcher)
        existed = xml_path.exists() or _query_task_state()[0]
        _unregister_task(check=False)
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


