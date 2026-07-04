"""Real Windows Task Scheduler registration acceptance (CI windows job only).

Unit tests fake every PowerShell call, so nothing locally proves the task XML
is schema-valid for ``Register-ScheduledTask`` (namespace, version stamp,
trigger shape, argv quoting). This round-trip registers the production XML
against the real Task Scheduler, asserts it queries back as loaded, and
unregisters it.

``Start-ScheduledTask`` is deliberately skipped — registration acceptance is
the thing under test, not the monitor. The XML's own TimeTrigger may still
briefly auto-start the task on the runner; uninstall's Stop+Unregister ends
it, and the workflow's ``if: always()`` sweep covers a crashed run.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from claude_swap import service_spec
from claude_swap.service_backends import task_scheduler

pytestmark = [
    pytest.mark.windows_service_integration,
    pytest.mark.skipif(
        sys.platform != "win32", reason="real Task Scheduler requires Windows"
    ),
    pytest.mark.skipif(
        not os.environ.get("CSWAP_TASK_SCHEDULER_INTEGRATION"),
        reason=(
            "set CSWAP_TASK_SCHEDULER_INTEGRATION=1 to register a real "
            "scheduled task (CI windows-task-scheduler job)"
        ),
    ),
]


def test_register_query_unregister_roundtrip(tmp_path, monkeypatch):
    # Unique per-run task name so a leftover from a parallel or crashed run
    # can never collide with (or mask) this run's registration.
    run_id = os.environ.get("GITHUB_RUN_ID", str(os.getpid()))
    monkeypatch.setattr(service_spec, "SERVICE_ID", f"cswap-monitor-ci-{run_id}")
    monkeypatch.setattr(task_scheduler, "_start_task", lambda: None)

    host = SimpleNamespace(backup_dir=tmp_path)
    backend = task_scheduler.TaskSchedulerBackend()
    try:
        assert backend.install(host) == 0

        exists, state = task_scheduler._query_task_state()
        assert exists, "registered task must query back via Get-ScheduledTask"
        assert state.lower() != "disabled", state
        assert backend.state() == "loaded"

        assert backend.uninstall(host) == 0
        exists_after, _ = task_scheduler._query_task_state()
        assert not exists_after, "uninstall must remove the task"
    finally:
        # Failure-path cleanup: a broken assertion must not leave a task behind.
        task_scheduler._unregister_task(check=False)
