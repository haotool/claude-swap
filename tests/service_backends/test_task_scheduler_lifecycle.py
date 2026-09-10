"""Lifecycle regressions for the Windows Task Scheduler service backend."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.service_backends import task_scheduler as task_scheduler_backend


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _raise(message: str) -> None:
    raise ClaudeSwitchError(message)


def test_query_distinguishes_confirmed_absence_from_operational_failure(monkeypatch):
    """Absence requires a successful root-task discovery with no exact match."""
    scripts: list[str] = []

    def absent(script: str, **_kwargs):
        scripts.append(script)
        return _completed(2)

    monkeypatch.setattr(task_scheduler_backend, "_powershell", absent)
    assert task_scheduler_backend._query_task_state() == (False, "")
    assert "-TaskPath '\\*'" in scripts[0]
    assert "-ErrorAction Stop" in scripts[0]
    assert "SilentlyContinue" not in scripts[0]

    monkeypatch.setattr(
        task_scheduler_backend,
        "_powershell",
        lambda *a, **k: _completed(5, stderr="access denied"),
    )
    with pytest.raises(ClaudeSwitchError, match="query failed"):
        task_scheduler_backend._query_task_state()


def test_remove_surfaces_a_failed_postcondition(monkeypatch):
    """The manager operation fails if its final re-query still sees the task."""
    seen: list[str] = []

    def powershell(script: str, **_kwargs):
        seen.append(script)
        return _completed(3, stderr="task is still registered")

    monkeypatch.setattr(task_scheduler_backend, "_powershell", powershell)

    with pytest.raises(ClaudeSwitchError, match="removal failed"):
        task_scheduler_backend._remove_task()

    script = seen[0]
    assert "Unregister-ScheduledTask" in script
    assert "$remaining = @(Get-ScheduledTask" in script


def test_start_failure_rolls_back_the_new_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Install failure must not leave a future-triggering half-installed task."""
    monkeypatch.setattr(sys, "platform", "win32")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    switcher = MagicMock(backup_dir=backup_dir)

    removals = 0

    def remove_task() -> bool:
        nonlocal removals
        removals += 1
        return removals > 1

    monkeypatch.setattr(task_scheduler_backend, "_remove_task", remove_task)
    monkeypatch.setattr(task_scheduler_backend, "_register_task", lambda _path: None)
    monkeypatch.setattr(
        task_scheduler_backend,
        "_start_task",
        lambda: _raise("start failed"),
    )
    success = MagicMock()
    monkeypatch.setattr(
        task_scheduler_backend.service_spec,
        "print_install_success",
        success,
    )

    with pytest.raises(ClaudeSwitchError, match="start failed"):
        task_scheduler_backend.TaskSchedulerBackend().install(switcher)

    assert removals == 2  # replace old registration, then rollback new one
    success.assert_not_called()


def test_uninstall_failure_keeps_the_local_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep diagnostic/retry state when the manager cannot confirm removal."""
    backup_dir = tmp_path / "backup"
    log_dir = backup_dir / "logs"
    log_dir.mkdir(parents=True)
    switcher = MagicMock(backup_dir=backup_dir)
    xml_path = log_dir / f"{task_scheduler_backend.service_spec.SERVICE_ID}.xml"
    xml_path.write_text("<Task />", encoding="utf-8")

    monkeypatch.setattr(
        task_scheduler_backend,
        "_query_task_state",
        lambda: (True, "Running"),
    )
    monkeypatch.setattr(
        task_scheduler_backend,
        "_remove_task",
        lambda: _raise("remove failed"),
    )
    success = MagicMock()
    monkeypatch.setattr(
        task_scheduler_backend.service_spec,
        "print_uninstall_result",
        success,
    )

    with pytest.raises(ClaudeSwitchError, match="remove failed"):
        task_scheduler_backend.TaskSchedulerBackend().uninstall(switcher)

    assert xml_path.exists()
    success.assert_not_called()
