"""Per-user Task Scheduler supervision for the existing auto loop.

Task Scheduler owns the definition; no XML backup or credential is written by
these management operations. The private runner uses this installation's
pythonw, not a shell that can leave its child alive when the task is stopped.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_OWNER = "claude-swap:auto:v1:"
_SETTLE_SECONDS = 5.0
_POLL_SECONDS = 0.1
_COMMAND_TIMEOUT = 30
_STATES = {"Unknown", "Disabled", "Queued", "Ready", "Running"}

# A successful enumeration, not a suppressed cmdlet error, proves absence.
# Windows may spell Principal.UserId as an account name even when we supplied
# a SID. Translate that spelling before comparing identities.
_TASK_FUNCTIONS = r"""
function Find-Task {
    $tasks = @(Get-ScheduledTask -TaskPath '\*' -ErrorAction Stop |
        Where-Object { $_.TaskPath -eq '\' -and $_.TaskName -eq $p.name })
    if ($tasks.Count -gt 1) { throw 'Ambiguous Task Scheduler response' }
    if ($tasks.Count -eq 1) { return $tasks[0] }
    return $null
}
function Get-OwnerSid($task) {
    $identity = [string]$task.Principal.UserId
    if ($identity -notmatch '^S-1-') {
        return [Security.Principal.NTAccount]::new($identity).Translate(
            [Security.Principal.SecurityIdentifier]).Value
    }
    return $identity
}
function Assert-Task($task) {
    if ($null -eq $task -or $task.Description -ne $p.description -or
        (Get-OwnerSid $task) -ne $p.sid) {
        throw 'The task changed or belongs to another installation; refusing to modify it'
    }
}
"""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ClaudeSwitchError("The auto service is only available on Windows.")


def _powershell_path() -> str:
    # Ask Windows rather than searching PATH or trusting a shell-local SystemRoot.
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_directory = kernel.GetSystemDirectoryW
    get_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_directory(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ClaudeSwitchError("Could not locate the Windows system directory.")
    executable = Path(buffer.value) / "WindowsPowerShell/v1.0/powershell.exe"
    if not executable.is_file():
        raise ClaudeSwitchError("Windows PowerShell is unavailable.")
    return str(executable)


def _encoded(value: dict) -> str:
    return base64.b64encode(json.dumps(value, ensure_ascii=True).encode()).decode()


def _powershell(body: str, payload: dict | None = None) -> dict:
    """Run fixed management code with data on stdin and a bounded lifetime."""
    script = (
        "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "try { $p = [Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String([Console]::In.ReadToEnd())) | ConvertFrom-Json; "
        + _TASK_FUNCTIONS
        + body
        + " } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
    )
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        result = subprocess.run(
            [
                _powershell_path(),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            input=_encoded(payload or {}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMAND_TIMEOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeSwitchError(
            "Task Scheduler timed out; its current state could not be confirmed."
        ) from exc
    except OSError as exc:
        raise ClaudeSwitchError(f"Task Scheduler command failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ClaudeSwitchError(
            f"Task Scheduler command failed (exit {result.returncode}): {detail}"
        )
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ClaudeSwitchError("Task Scheduler returned an invalid response.") from exc
    if not isinstance(value, dict):
        raise ClaudeSwitchError("Task Scheduler returned an invalid response.")
    return value


def _current_user_sid() -> str:
    result = _powershell(
        "@{sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value} "
        "| ConvertTo-Json -Compress"
    )
    sid = result.get("sid")
    if not isinstance(sid, str) or not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise ClaudeSwitchError("Could not resolve the current Windows user SID.")
    return sid


def _task_name(sid: str) -> str:
    return f"cswap-auto-{sid}"


@contextmanager
def _management_lock(name: str):
    """Serialize management calls, not auto engines; leave no lock file."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    release = kernel.ReleaseMutex
    release.argtypes = [wintypes.HANDLE]
    release.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(None, False, f"Global\\{name}-management")
    if not handle:
        raise ClaudeSwitchError("Could not acquire the auto-service management lock.")
    acquired = False
    try:
        verdict = wait(handle, 5000)
        # WAIT_ABANDONED grants ownership too; reread OS state under the lock.
        if verdict == 0x102:  # WAIT_TIMEOUT is contention, not an OS failure.
            raise ClaudeSwitchError(
                "Another auto-service management operation is busy."
            )
        if verdict not in (0, 0x80):
            raise ClaudeSwitchError(
                "Windows could not acquire the auto-service management lock."
            )
        acquired = True
        yield
    finally:
        if acquired:
            release(handle)
        close(handle)


def _query_task(name: str) -> dict | None:
    value = _powershell(
        "$task = Find-Task; if ($null -eq $task) { "
        "@{found=$false} | ConvertTo-Json -Compress; return }; "
        "@{found=$true; state=[string]$task.State; sid=(Get-OwnerSid $task); "
        "description=[string]$task.Description; "
        "xml=(Export-ScheduledTask -InputObject $task -ErrorAction Stop)} "
        "| ConvertTo-Json -Compress",
        {"name": name},
    )
    if set(value) == {"found"} and value["found"] is False:
        return None
    if (
        value.get("found") is not True
        or not isinstance(value.get("state"), str)
        or value["state"] not in _STATES
        or any(
            not isinstance(value.get(key), str) for key in ("sid", "description", "xml")
        )
    ):
        raise ClaudeSwitchError("Task Scheduler returned an incomplete task record.")
    try:
        root = ET.fromstring(value["xml"])
        description = root.findtext(
            f"{{{_NAMESPACE}}}RegistrationInfo/{{{_NAMESPACE}}}Description"
        )
    except ET.ParseError as exc:
        raise ClaudeSwitchError("Task Scheduler returned invalid task XML.") from exc
    if description != value["description"]:
        raise ClaudeSwitchError("The task changed while its definition was being read.")
    return value


def _assert_owned(task: dict, sid: str) -> None:
    if task["sid"] != sid or not re.fullmatch(
        re.escape(_OWNER) + r"[0-9a-f]{32}", task["description"]
    ):
        raise ClaudeSwitchError(
            "An existing task is not owned by this auto service; refusing to modify it."
        )


def _remove_task(name: str, sid: str, description: str) -> None:
    # Disable triggers first: a watchdog must not re-fire between Stop and
    # Unregister. Queued/unknown states are observed, not guessed to be stopped.
    value = _powershell(
        r"""
$task = Find-Task
if ($null -eq $task) { @{removed=$true} | ConvertTo-Json -Compress; return }
Assert-Task $task
Disable-ScheduledTask -InputObject $task -ErrorAction Stop | Out-Null
$clock = [Diagnostics.Stopwatch]::StartNew()
do {
    $task = Find-Task
    if ($null -eq $task) { @{removed=$true} | ConvertTo-Json -Compress; return }
    Assert-Task $task
    if ($task.State -in @('Ready', 'Disabled')) { break }
    if ($task.State -eq 'Running') { Stop-ScheduledTask -InputObject $task -ErrorAction Stop }
    if ($clock.Elapsed.TotalSeconds -ge $p.timeout) { throw 'The task did not stop' }
    Start-Sleep -Milliseconds $p.poll
} while ($true)
Unregister-ScheduledTask -InputObject $task -Confirm:$false -ErrorAction Stop
$clock.Restart()
while ($null -ne (Find-Task)) {
    if ($clock.Elapsed.TotalSeconds -ge $p.timeout) { throw 'The task is still registered' }
    Start-Sleep -Milliseconds $p.poll
}
@{removed=$true} | ConvertTo-Json -Compress
""",
        {
            "name": name,
            "sid": sid,
            "description": description,
            "timeout": _SETTLE_SECONDS,
            "poll": int(_POLL_SECONDS * 1000),
        },
    )
    if value.get("removed") is not True or _query_task(name) is not None:
        raise ClaudeSwitchError("Could not verify that the auto service was removed.")


def _register_task(name: str, xml: str) -> None:
    # No -Force: a task created after our absence check must not be overwritten.
    value = _powershell(
        "if ($null -ne (Find-Task)) { throw 'The task name is already in use' }; "
        "Register-ScheduledTask -TaskName $p.name -TaskPath '\\' -Xml $p.xml "
        "-ErrorAction Stop | Out-Null; @{registered=$true} | ConvertTo-Json -Compress",
        {"name": name, "xml": xml},
    )
    if value.get("registered") is not True:
        raise ClaudeSwitchError("Could not verify Task Scheduler registration.")


def _start_task(name: str, sid: str, description: str) -> None:
    value = _powershell(
        r"""
$task = Find-Task
Assert-Task $task
Enable-ScheduledTask -InputObject $task -ErrorAction Stop | Out-Null
Start-ScheduledTask -InputObject $task -ErrorAction Stop
$clock = [Diagnostics.Stopwatch]::StartNew()
do {
    $task = Find-Task
    Assert-Task $task
    if ($task.State -eq 'Running') { @{running=$true} | ConvertTo-Json -Compress; return }
    if ($clock.Elapsed.TotalSeconds -ge $p.timeout) { throw 'The auto-service action did not start' }
    Start-Sleep -Milliseconds $p.poll
} while ($true)
""",
        {
            "name": name,
            "sid": sid,
            "description": description,
            "timeout": _SETTLE_SECONDS,
            "poll": int(_POLL_SECONDS * 1000),
        },
    )
    if value.get("running") is not True:
        raise ClaudeSwitchError(
            "Could not verify that the auto-service action started."
        )


def _runtime_context() -> dict:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config and "\x00" in config:
        raise ClaudeSwitchError("CLAUDE_CONFIG_DIR contains a NUL character.")
    # Unset and an explicit ~/.claude are NOT equivalent: paths.py resolves
    # .claude.json differently. Freeze relative paths without resolving links.
    return {
        "version": 1,
        "home": os.path.abspath(Path.home()),
        "config": os.path.abspath(config) if config else None,
    }


def _resolve_program() -> str:
    program = Path(os.path.abspath(sys.executable)).with_name("pythonw.exe")
    if not program.is_file():
        raise ClaudeSwitchError(
            "This Python installation has no pythonw.exe; the auto service was not changed."
        )
    return str(program)


def _build_task_xml(sid: str, description: str, program: str, context: dict) -> str:
    root = ET.Element("Task", {"version": "1.3", "xmlns": _NAMESPACE})

    def add(parent, name, text=None):
        child = ET.SubElement(parent, name)
        child.text = text
        return child

    info = add(root, "RegistrationInfo")
    add(info, "Description", description)
    triggers = add(root, "Triggers")
    logon = add(triggers, "LogonTrigger")
    add(logon, "Enabled", "true")
    add(logon, "UserId", sid)
    timed = add(triggers, "TimeTrigger")
    repeat = add(timed, "Repetition")
    add(repeat, "Interval", "PT5M")
    add(repeat, "StopAtDurationEnd", "false")
    add(
        timed,
        "StartBoundary",
        datetime.now().astimezone().replace(microsecond=0).isoformat(),
    )
    add(timed, "Enabled", "true")
    principal = add(add(root, "Principals"), "Principal")
    principal.set("id", "Owner")
    add(principal, "UserId", sid)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(root, "Settings")
    # IgnoreNew is local to THIS task, not a machine-wide auto-engine lock.
    # The defaults otherwise stop resident actions after 72h or on battery.
    for name, value in (
        # Commit disabled: an ambiguous register failure cannot arm a future
        # trigger. Only our verified start path enables this attempt.
        ("Enabled", "false"),
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("StartWhenAvailable", "true"),
        ("ExecutionTimeLimit", "PT0S"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
    ):
        add(settings, name, value)
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M")
    add(restart, "Count", "3")
    actions = add(root, "Actions")
    actions.set("Context", "Owner")
    execute = add(actions, "Exec")
    add(execute, "Command", program)
    add(
        execute,
        "Arguments",
        subprocess.list2cmdline(
            ["-E", "-P", "-m", "claude_swap._auto_service", _encoded(context)]
        ),
    )
    add(execute, "WorkingDirectory", context["home"])
    return ET.tostring(root, encoding="unicode")


def _result(name: str, task: dict | None) -> dict:
    from claude_swap import paths

    return {
        "task_name": name,
        "installed": task is not None,
        "state": task["state"] if task else None,
        "log": str(paths.get_backup_root() / "claude-swap.log"),
        "output_log": str(paths.get_backup_root() / "auto-service.log"),
    }


def install() -> dict:
    """Replace our task and start its action; compensate on a failed install."""
    _require_windows()
    sid = _current_user_sid()
    name = _task_name(sid)
    program, context = _resolve_program(), _runtime_context()
    description = _OWNER + uuid.uuid4().hex
    xml = _build_task_xml(sid, description, program, context)
    with _management_lock(name):
        previous = _query_task(name)
        if previous is not None:
            _assert_owned(previous, sid)
            _remove_task(name, sid, previous["description"])
        try:
            _register_task(name, xml)
            _start_task(name, sid, description)
            current = _query_task(name)
            if (
                current is None
                or current["description"] != description
                or current["state"] != "Running"
            ):
                raise ClaudeSwitchError("The newly installed action is not running.")
            _assert_owned(current, sid)
        except BaseException as original:
            # A timeout can arrive AFTER registration. Claim by this attempt's
            # nonce, not a boolean set after the call; never remove a successor.
            try:
                current = _query_task(name)
                if current is not None:
                    if current["description"] != description:
                        raise ClaudeSwitchError("The task changed during installation.")
                    _assert_owned(current, sid)
                    _remove_task(name, sid, description)
                if previous is not None:
                    _register_task(name, previous["xml"])
                    if previous["state"] == "Running":
                        _start_task(name, sid, previous["description"])
            except Exception as cleanup:  # noqa: BLE001 -- preserve both failure causes
                raise ClaudeSwitchError(
                    f"Auto-service installation failed ({original}); recovery also failed ({cleanup}). "
                    "Inspect the task in Task Scheduler before retrying."
                ) from original
            raise
    return _result(name, current)


def uninstall() -> dict:
    """Remove only our registration; retain diagnostic logs and account data."""
    _require_windows()
    sid = _current_user_sid()
    name = _task_name(sid)
    with _management_lock(name):
        task = _query_task(name)
        if task is not None:
            _assert_owned(task, sid)
            _remove_task(name, sid, task["description"])
    return {"task_name": name, "removed": task is not None}


def status() -> dict:
    """Report scheduler state, not credential health or successful switching."""
    _require_windows()
    sid = _current_user_sid()
    name = _task_name(sid)
    task = _query_task(name)
    if task is not None:
        _assert_owned(task, sid)
    return _result(name, task)
