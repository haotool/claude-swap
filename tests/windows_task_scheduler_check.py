"""Opt-in native Task Scheduler acceptance on a disposable Windows runner.

Run directly, not through pytest's home/keychain fixtures or xdist. The CLI
adapter changes only the task name and records each proposed registration
before it can be created. The scheduled action and auto engine are production
code, installed in a venv whose path contains spaces. No accounts are seeded.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import task_scheduler as ts

# Exercise cli.main (the console entry point), without registering the normal
# user's service name. Recording the nonce before registration also makes a
# failed install safely cleanable by the workflow's always() step.
_CLI = r"""
import json, pathlib, sys
from claude_swap import cli, task_scheduler as ts
name, receipt, flag, broken = sys.argv[1:]
ts._task_name = lambda sid: name
build = ts._build_task_xml
def record(sid, description, program, context):
    path = pathlib.Path(receipt)
    data = json.loads(path.read_text(encoding='utf-8'))
    data['descriptions'].append(description)
    path.write_text(json.dumps(data), encoding='utf-8')
    return build(sid, description, program, context)
ts._build_task_xml = record
if broken == 'start':
    ts._resolve_program = lambda: str(pathlib.Path(receipt).parent / 'missing-pythonw.exe')
if broken == 'remove':
    remove = ts._remove_task
    def remove_then_fail(*args):
        ts._remove_task = remove
        remove(*args)
        raise ts.ClaudeSwitchError('injected old-removal failure')
    ts._remove_task = remove_then_fail
sys.argv = ['cswap', 'auto', flag]
cli.main()
"""


def process(pid: int) -> dict | None:
    result = ts._powershell(
        "$items = @(Get-CimInstance Win32_Process -Filter "
        "('ProcessId = ' + [int]$p.pid) -ErrorAction Stop); "
        "if ($items.Count -eq 0) { @{found=$false} | ConvertTo-Json -Compress; return }; "
        "$item = $items[0]; @{found=$true; pid=[int]$item.ProcessId; "
        "command=[string]$item.CommandLine; created=$item.CreationDate.ToUniversalTime().ToString('o')} "
        "| ConvertTo-Json -Compress",
        {"pid": pid},
    )
    return result if result["found"] else None


def wait_gone(identity: dict) -> None:
    deadline = time.monotonic() + 10
    while True:
        current = process(identity["pid"])
        if current is None or current["created"] != identity["created"]:
            return
        if time.monotonic() >= deadline:
            raise AssertionError("The action process survived task removal")
        time.sleep(0.2)


def cleanup(folder: Path) -> None:
    receipt = folder / "receipt.json"
    if not receipt.exists():
        return
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if data["sid"] != ts._current_user_sid() or not data["name"].startswith(
        "cswap-auto-ci-"
    ):
        raise AssertionError("Unexpected integration receipt")
    task = ts._query_task(data["name"])
    if task is not None:
        if (
            task["description"] not in data["descriptions"]
            or task["sid"] != data["sid"]
        ):
            raise AssertionError("Refusing to clean an unrelated task")
        ts._remove_task(data["name"], data["sid"], task["description"])
    for identity in data.get("processes", []):
        wait_gone(identity)


def exercise(folder: Path, python: str, recover: bool) -> None:
    sid = ts._current_user_sid()
    name = (
        "cswap-auto-ci-"
        + os.environ.get("GITHUB_RUN_ID", "manual")
        + "-"
        + uuid.uuid4().hex
    )
    receipt = folder / "receipt.json"
    receipt.write_text(
        json.dumps({"name": name, "sid": sid, "descriptions": [], "processes": []}),
        encoding="utf-8",
    )
    home = folder / "empty home"
    home.mkdir()
    custom = home / "profile 空白 o'brien & test"
    custom.mkdir()
    env = os.environ.copy()
    env.update(HOME=str(home), USERPROFILE=str(home), NO_COLOR="1")
    env.pop("PYTHONPATH", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    output_log = home / ".claude-swap-backup" / "auto-service.log"
    checks = []

    def cli(flag: str, *, failure="", expected=0):
        result = subprocess.run(
            [
                python,
                "-E",
                "-P",
                "-c",
                _CLI,
                name,
                str(receipt),
                flag,
                failure,
            ],
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"{flag} exited {result.returncode}: {result.stdout}\n{result.stderr}"
            )
        return result

    def observe(previous_pid=None, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = output_log.read_text(encoding="utf-8") if output_log.exists() else ""
            starts = list(
                re.finditer(r"Auto-service process started \(pid (\d+)\)", text)
            )
            if starts:
                start = starts[-1]
                pid = int(start.group(1))
                if (
                    pid != previous_pid
                    and "Auto-switch running:" in text[start.end() :]
                ):
                    task = ts._query_task(name)
                    identity = process(pid)
                    if task and task["state"] == "Running" and identity:
                        assert "claude_swap._auto_service" in identity["command"]
                        data = json.loads(receipt.read_text(encoding="utf-8"))
                        data["processes"].append(identity)
                        receipt.write_text(json.dumps(data), encoding="utf-8")
                        return identity
            time.sleep(0.25)
        raise AssertionError(
            "No fresh running auto process and CLI banner were observed"
        )

    def task_context():
        task = ts._query_task(name)
        assert task is not None
        root = ET.fromstring(task["xml"])
        ns = {"t": ts._NAMESPACE}
        args = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
        value = json.loads(base64.b64decode(args.split()[-1]))
        command = root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns)
        assert " " in command and command.lower().endswith("pythonw.exe")
        assert value["home"] == str(home)
        return value

    try:
        assert "not installed" in cli("--service-status").stdout
        cli("--install-service")
        first = observe()
        assert task_context()["config"] is None
        checks.append("default install: fresh CLI banner, process and task Running")

        env["CLAUDE_CONFIG_DIR"] = str(custom)
        cli("--install-service")
        second = observe(first["pid"])
        wait_gone(first)
        assert task_context()["config"] == str(custom)
        assert "Running" in cli("--service-status").stdout
        checks.append(
            "running reinstall: new PID, previous PID gone, custom context and spaced interpreter"
        )

        task = ts._query_task(name)
        ts._start_task(name, sid, task["description"])
        assert process(second["pid"])["created"] == second["created"]
        starts_before = output_log.read_text(encoding="utf-8").count(
            "Auto-service process started"
        )
        time.sleep(2)
        assert (
            output_log.read_text(encoding="utf-8").count("Auto-service process started")
            == starts_before
        )
        checks.append("re-fire: existing task instance retained")

        previous = ts._query_task(name)
        env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(home / "redirected store")
        refused = cli("--install-service", expected=1)
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" in refused.stdout + refused.stderr
        assert ts._query_task(name)["xml"] == previous["xml"]
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        checks.append(
            "secure-storage override: rejected before changing the installed task"
        )

        third = second
        for phase in ("start", "remove"):
            previous = ts._query_task(name)
            failure = cli("--install-service", failure=phase, expected=1)
            assert "Auto service installed:" not in failure.stdout
            restored = ts._query_task(name)
            assert restored["description"] == previous["description"]
            current = observe(third["pid"])
            wait_gone(third)
            third = current
            checks.append(f"native {phase} failure: previous running service restored")

        if recover:
            # Recheck creation time and the exact service command immediately
            # before stopping this one test-owned PID. Never kill by image name.
            ts._powershell(
                "$item = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$p.pid) -ErrorAction Stop; "
                "if ($null -eq $item -or $item.CreationDate.ToUniversalTime().ToString('o') -ne $p.created "
                "-or [string]$item.CommandLine -ne $p.command) { throw 'Process identity changed' }; "
                "Stop-Process -Id $p.pid -ErrorAction Stop; @{stopped=$true} | ConvertTo-Json -Compress",
                third,
            )
            started = time.monotonic()
            fourth = observe(third["pid"], timeout=370)
            checks.append(
                f"forced exit: new action observed after {time.monotonic() - started:.1f}s"
            )
            wait_gone(third)
        else:
            fourth = third

        cli("--uninstall-service")
        wait_gone(fourth)
        assert ts._query_task(name) is None
        cli("--uninstall-service")
        checks.append("uninstall twice: registration absent and last process gone")
        # A same-name task is not ours merely because it belongs to the same
        # Windows user. Keep a disabled fixture intact through all public verbs.
        foreign = "cswap-auto-ci-foreign-" + uuid.uuid4().hex
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["descriptions"].append(foreign)
        receipt.write_text(json.dumps(data), encoding="utf-8")
        fixture = ts._build_task_xml(
            sid,
            foreign,
            ts._resolve_program(),
            {"version": 1, "home": str(home), "config": None},
        )
        ts._register_task(name, fixture)
        before = ts._query_task(name)
        for flag in ("--install-service", "--service-status", "--uninstall-service"):
            rejected = cli(flag, expected=1)
            assert "not owned" in rejected.stdout + rejected.stderr
            assert ts._query_task(name)["xml"] == before["xml"]

        # Pin the provider's create-only behaviour too, not just our precheck.
        # Pretend the precheck raced with another creator, leaving the real
        # native Register-ScheduledTask call to encounter the occupied name.
        powershell = ts._powershell

        def stale_absence(body, payload=None):
            return powershell("function Find-Task { return $null }\n" + body, payload)

        replacement = fixture.replace(foreign, ts._OWNER + uuid.uuid4().hex)
        with patch.object(ts, "_powershell", stale_absence):
            with pytest.raises(ts.ClaudeSwitchError):
                ts._register_task(name, replacement)
        assert ts._query_task(name)["xml"] == before["xml"]
        ts._remove_task(name, sid, foreign)
        checks.append(
            "foreign task: all CLI verbs refuse; native name collision preserves definition"
        )
        assert not (home / ".claude-swap-backup" / "sequence.json").exists()
        result = {
            "checks": checks,
            "windows": list(sys.getwindowsversion()),
            "manual_standard_user": "NOT_PERFORMED",
            "visual_flash_check": "NOT_PERFORMED",
        }
        (folder / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, indent=2), flush=True)
    finally:
        cleanup(folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--recovery", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Run only on a disposable Windows GitHub Actions runner")
    args.folder.mkdir(parents=True, exist_ok=True)
    if args.cleanup:
        cleanup(args.folder)
    else:
        if (args.folder / "receipt.json").exists():
            parser.error("Use a fresh folder for each experiment")
        exercise(args.folder, args.python, args.recovery)


if __name__ == "__main__":
    main()
