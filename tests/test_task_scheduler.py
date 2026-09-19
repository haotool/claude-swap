"""The manager must distinguish absence, failure, and an unrelated task."""

import base64
import json
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from claude_swap import task_scheduler as ts
from claude_swap.exceptions import ClaudeSwitchError

SID = "S-1-5-21-100-200-300-1001"
DESCRIPTION = ts._OWNER + "a" * 32
NS = {"t": ts._NAMESPACE}


def record(tmp_path, *, description=DESCRIPTION, state="Ready", sid=SID):
    context = {"version": 1, "home": str(tmp_path), "config": None}
    return {
        "found": True,
        "state": state,
        "sid": sid,
        "description": description,
        "xml": ts._build_task_xml(
            sid, description, str(tmp_path / "pythonw.exe"), context
        ),
    }


@pytest.fixture
def manager(monkeypatch, tmp_path):
    state = {"task": None, "calls": []}
    monkeypatch.setattr(ts, "_require_windows", lambda: None)
    monkeypatch.setattr(ts, "_current_user_sid", lambda: SID)
    monkeypatch.setattr(ts, "_resolve_program", lambda: str(tmp_path / "pythonw.exe"))
    monkeypatch.setattr(
        ts,
        "_runtime_context",
        lambda: {
            "version": 1,
            "home": str(tmp_path),
            "config": None,
        },
    )
    monkeypatch.setattr(ts, "_management_lock", lambda name: nullcontext())
    monkeypatch.setattr(ts, "_query_task", lambda name: state["task"])
    monkeypatch.setattr("claude_swap.paths.get_backup_root", lambda: tmp_path)

    def register(name, xml):
        state["calls"].append("register")
        root = ET.fromstring(xml)
        state["task"] = {
            "found": True,
            "state": "Ready",
            "sid": SID,
            "xml": xml,
            "description": root.findtext(
                "t:RegistrationInfo/t:Description", namespaces=NS
            ),
        }

    def start(name, sid, description):
        state["calls"].append("start")
        assert state["task"]["description"] == description
        state["task"]["state"] = "Running"

    def remove(name, sid, description):
        state["calls"].append("remove")
        assert state["task"]["description"] == description
        state["task"] = None

    monkeypatch.setattr(ts, "_register_task", register)
    monkeypatch.setattr(ts, "_start_task", start)
    monkeypatch.setattr(ts, "_remove_task", remove)
    return state


def test_install_starts_now_and_reinstall_replaces_one_task(manager):
    assert ts.install()["state"] == "Running"
    old = manager["task"]["description"]
    assert ts.install()["state"] == "Running"
    assert manager["task"]["description"] != old
    assert manager["calls"] == ["register", "start", "remove", "register", "start"]


def test_start_failure_leaves_no_future_trigger(manager, monkeypatch):
    monkeypatch.setattr(
        ts, "_start_task", Mock(side_effect=ClaudeSwitchError("start denied"))
    )
    with pytest.raises(ClaudeSwitchError, match="start denied"):
        ts.install()
    assert manager["task"] is None
    assert manager["calls"] == ["register", "remove"]


def test_register_timeout_after_commit_cleans_its_own_attempt(manager, monkeypatch):
    original = ts._register_task

    def ambiguous(*args):
        original(*args)
        raise ClaudeSwitchError("timed out after registration")

    monkeypatch.setattr(ts, "_register_task", ambiguous)
    with pytest.raises(ClaudeSwitchError, match="timed out"):
        ts.install()
    assert manager["task"] is None


def test_interrupt_after_register_is_compensated(manager, monkeypatch):
    monkeypatch.setattr(ts, "_start_task", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        ts.install()
    assert manager["task"] is None


def test_failed_reinstall_restores_previous_definition_and_running_state(
    manager, monkeypatch, tmp_path
):
    previous = record(tmp_path, state="Running")
    manager["task"] = previous.copy()
    original = ts._start_task

    def start(name, sid, description):
        if description != DESCRIPTION:
            raise ClaudeSwitchError("new action failed")
        original(name, sid, description)

    monkeypatch.setattr(ts, "_start_task", start)
    with pytest.raises(ClaudeSwitchError, match="new action failed"):
        ts.install()
    assert manager["task"]["xml"] == previous["xml"]
    assert manager["task"]["state"] == "Running"


def test_recovery_error_keeps_both_causes(manager, monkeypatch):
    monkeypatch.setattr(
        ts, "_start_task", Mock(side_effect=ClaudeSwitchError("start failed"))
    )
    monkeypatch.setattr(
        ts, "_remove_task", Mock(side_effect=ClaudeSwitchError("remove denied"))
    )
    with pytest.raises(ClaudeSwitchError, match="start failed.*remove denied"):
        ts.install()
    assert manager["task"] is not None


@pytest.mark.parametrize("operation", [ts.install, ts.uninstall, ts.status])
@pytest.mark.parametrize(
    "changed", [{"sid": "S-1-5-99"}, {"description": "unrelated task"}]
)
def test_foreign_task_is_untouched(operation, changed, manager, tmp_path):
    foreign = record(tmp_path)
    foreign.update(changed)
    manager["task"] = foreign
    with pytest.raises(ClaudeSwitchError, match="not owned"):
        operation()
    assert manager["task"] is foreign
    assert manager["calls"] == []


def test_registration_collision_is_not_removed_as_ours(manager, monkeypatch, tmp_path):
    foreign = record(tmp_path, description="foreign successor")

    def raced(*args):
        manager["task"] = foreign
        raise ClaudeSwitchError("name occupied")

    monkeypatch.setattr(ts, "_register_task", raced)
    with pytest.raises(ClaudeSwitchError):
        ts.install()
    assert manager["task"] is foreign
    assert manager["calls"] == []


def test_uninstall_is_idempotent(manager):
    assert ts.uninstall()["removed"] is False
    ts.install()
    assert ts.uninstall()["removed"] is True
    assert ts.uninstall()["removed"] is False
    assert manager["task"] is None


def test_removal_error_is_not_reported_as_success(manager, monkeypatch, tmp_path):
    previous = record(tmp_path, state="Running")
    manager["task"] = previous
    monkeypatch.setattr(
        ts, "_remove_task", Mock(side_effect=ClaudeSwitchError("stop denied"))
    )
    with pytest.raises(ClaudeSwitchError, match="stop denied"):
        ts.uninstall()
    assert manager["task"] is previous


def test_status_does_not_construct_a_switcher(manager, monkeypatch):
    monkeypatch.setattr(
        "claude_swap.switcher.ClaudeAccountSwitcher", Mock(side_effect=AssertionError)
    )
    assert ts.status()["installed"] is False
    assert manager["calls"] == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"found": 0},
        {"found": False, "error": "denied"},
        {"found": True},
        {"found": True, "state": []},
    ],
)
def test_malformed_query_is_not_absence(response, monkeypatch):
    monkeypatch.setattr(ts, "_powershell", lambda *args: response)
    with pytest.raises(ClaudeSwitchError):
        ts._query_task("probe")


def test_only_confirmed_absence_returns_none(monkeypatch):
    monkeypatch.setattr(ts, "_powershell", lambda *args: {"found": False})
    assert ts._query_task("probe") is None


def test_query_preserves_manager_failure(monkeypatch):
    monkeypatch.setattr(
        ts, "_powershell", Mock(side_effect=ClaudeSwitchError("CIM unavailable"))
    )
    with pytest.raises(ClaudeSwitchError, match="CIM unavailable"):
        ts._query_task("probe")


def test_query_rejects_a_definition_that_changed_mid_read(monkeypatch, tmp_path):
    value = record(tmp_path)
    value["description"] = "changed"
    monkeypatch.setattr(ts, "_powershell", lambda *args: value)
    with pytest.raises(ClaudeSwitchError, match="changed"):
        ts._query_task("probe")


def test_query_reads_valid_state(monkeypatch, tmp_path):
    value = record(tmp_path)
    monkeypatch.setattr(ts, "_powershell", lambda *args: value)
    assert ts._query_task("probe") == value


def test_remove_requires_observed_absence(monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "_powershell", lambda *args: {"removed": True})
    monkeypatch.setattr(ts, "_query_task", lambda name: record(tmp_path))
    with pytest.raises(ClaudeSwitchError, match="removed"):
        ts._remove_task("probe", SID, DESCRIPTION)


def test_removal_disables_triggers_before_stopping_and_unregistering(monkeypatch):
    run = Mock(return_value={"removed": True})
    monkeypatch.setattr(ts, "_powershell", run)
    monkeypatch.setattr(ts, "_query_task", lambda name: None)
    ts._remove_task("probe", SID, DESCRIPTION)
    body, payload = run.call_args.args
    assert (
        body.index("Disable-ScheduledTask")
        < body.index("Stop-ScheduledTask")
        < body.index("Unregister-ScheduledTask")
    )
    assert "Assert-Task $task" in body
    assert "while ($null -ne (Find-Task))" in body
    assert payload["timeout"] > 0
    assert payload["poll"] > 0


def test_powershell_passes_data_separately_and_surfaces_nonzero(monkeypatch):
    monkeypatch.setattr(ts, "_powershell_path", lambda: "powershell.exe")
    monkeypatch.setattr(ts.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    run = Mock(
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="access denied")
    )
    monkeypatch.setattr(ts.subprocess, "run", run)
    payload = {"name": "apostrophe ' & Unicode 空白"}
    with pytest.raises(ClaudeSwitchError, match="access denied"):
        ts._powershell("@{} | ConvertTo-Json", payload)
    assert json.loads(base64.b64decode(run.call_args.kwargs["input"])) == payload
    script = base64.b64decode(run.call_args.args[0][-1]).decode("utf-16-le")
    assert payload["name"] not in script
    assert "-ErrorAction Stop" in script
    assert run.call_args.kwargs["timeout"] > 0


@pytest.mark.parametrize("stdout", ["", "null", "[]", "not json"])
def test_powershell_rejects_invalid_success_output(monkeypatch, stdout):
    monkeypatch.setattr(ts, "_powershell_path", lambda: "powershell.exe")
    monkeypatch.setattr(ts.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )
    with pytest.raises(ClaudeSwitchError, match="invalid response"):
        ts._powershell("probe")


def test_sid_comes_from_os_not_username_environment(monkeypatch):
    monkeypatch.setenv("USERNAME", "not-the-owner")
    monkeypatch.setenv("USERDOMAIN", "not-the-domain")
    monkeypatch.setattr(ts, "_powershell", lambda *a: {"sid": SID})
    assert ts._current_user_sid() == SID
    assert SID in ts._task_name(SID)


def test_default_context_stays_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert ts._runtime_context()["config"] is None
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    assert ts._runtime_context()["config"] == str(tmp_path / ".claude")


def test_relative_context_is_frozen_at_install(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "profile")
    assert ts._runtime_context()["config"] == str(tmp_path / "profile")


def test_xml_encodes_context_and_resident_task_policy(tmp_path):
    context = {
        "version": 1,
        "home": str(tmp_path),
        "config": str(tmp_path / "空白 o'brien & profile"),
    }
    program = str(tmp_path / "with spaces" / "pythonw.exe")
    root = ET.fromstring(ts._build_task_xml(SID, DESCRIPTION, program, context))

    def text(path):
        return root.findtext(path, namespaces=NS)

    assert text("t:Principals/t:Principal/t:UserId") == SID
    assert text("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert text("t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert text("t:Triggers/t:LogonTrigger/t:UserId") == SID
    assert text("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT5M"
    for key, value in {
        "ExecutionTimeLimit": "PT0S",
        "MultipleInstancesPolicy": "IgnoreNew",
        "StartWhenAvailable": "true",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
    }.items():
        assert text("t:Settings/t:" + key) == value
    assert text("t:Settings/t:RestartOnFailure/t:Interval") == "PT1M"
    assert text("t:Settings/t:RestartOnFailure/t:Count") == "3"
    assert text("t:Actions/t:Exec/t:Command") == program
    argv = text("t:Actions/t:Exec/t:Arguments").split()
    assert argv[:4] == ["-E", "-P", "-m", "claude_swap._auto_service"]
    assert json.loads(base64.b64decode(argv[4])) == context


def test_off_windows_refuses_before_any_manager_call(monkeypatch):
    monkeypatch.setattr(ts.sys, "platform", "linux")
    run = Mock(side_effect=AssertionError)
    monkeypatch.setattr(ts, "_powershell", run)
    with pytest.raises(ClaudeSwitchError, match="only available on Windows"):
        ts.install()
    run.assert_not_called()


def test_invalid_runtime_context_is_refused_before_mutation(monkeypatch):
    monkeypatch.setattr(ts.os, "environ", {"CLAUDE_CONFIG_DIR": "bad\x00path"})
    with pytest.raises(ClaudeSwitchError, match="NUL"):
        ts._runtime_context()


def test_registration_is_disabled_until_verified_start(tmp_path, monkeypatch):
    task = record(tmp_path)
    root = ET.fromstring(task["xml"])
    assert root.findtext("t:Settings/t:Enabled", namespaces=NS) == "false"
    run = Mock(return_value={"running": True})
    monkeypatch.setattr(ts, "_powershell", run)
    ts._start_task("probe", SID, DESCRIPTION)
    body = run.call_args.args[0]
    assert (
        body.index("Assert-Task")
        < body.index("Enable-ScheduledTask")
        < body.index("Start-ScheduledTask")
    )


def test_status_reports_the_installed_home_not_the_callers_home(
    manager, monkeypatch, tmp_path
):
    task = record(tmp_path / "installed")
    manager["task"] = task
    monkeypatch.setattr(
        "claude_swap.paths.get_backup_root", lambda: tmp_path / "other-home"
    )
    result = ts.status()
    assert result["output_log"] == str(
        tmp_path / "installed" / ".claude-swap-backup" / "auto-service.log"
    )
    assert result["log"] == str(
        tmp_path / "installed" / ".claude-swap-backup" / "claude-swap.log"
    )


def test_status_refuses_an_unreadable_installed_context(manager, tmp_path):
    task = record(tmp_path)
    root = ET.fromstring(task["xml"])
    root.find("t:Actions/t:Exec/t:Arguments", NS).text = "unexpected action"
    task["xml"] = ET.tostring(root, encoding="unicode")
    manager["task"] = task
    with pytest.raises(ClaudeSwitchError, match="runtime context"):
        ts.status()
