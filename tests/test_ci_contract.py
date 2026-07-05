"""Tests for repository-level CI quality gates."""

from __future__ import annotations

import re
from pathlib import Path


def test_ci_mypy_gate_runs_strict() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv run mypy --strict src/claude_swap" in workflow


def test_ci_mypy_gate_covers_win32_platform_branches() -> None:
    # The default run analyzes the host platform, leaving every
    # sys.platform == "win32" branch (msvcrt, ctypes, OEM decode) unchecked.
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "mypy --strict --platform win32" in workflow


def test_ci_windows_task_scheduler_job_is_blocking() -> None:
    # The registration round-trip and the start smoke are the only real
    # Task Scheduler coverage; continue-on-error would silently waive both.
    # Scoped to this job so a justified waiver elsewhere stays possible.
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    job = re.search(r"^  windows-task-scheduler:.*?(?=^  \S|\Z)", workflow, re.M | re.S)
    assert job is not None
    assert "continue-on-error" not in job.group(0)
    assert "Start-Sleep" in job.group(0)


def test_ci_task_scheduler_covers_spaced_interpreter_path() -> None:
    # The <Command> quoting branch in _build_task_xml only runs when the
    # interpreter path has a space; the stock runner Python never does, so
    # the spaced-venv smoke is its only behavioral coverage.
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "C:\\spaced dir\\venv" in workflow


def test_ci_has_linux_systemd_round_trip() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "linux-systemd:" in workflow
    assert "systemctl --user is-active cswap-monitor.service" in workflow


def test_ci_has_redirected_list_smoke() -> None:
    # `cswap --list > file` under a CJK console CP (Windows) and the C
    # locale (Linux) is the UnicodeEncodeError regression surface; both
    # variants must stay wired to the real CLI, not just unit tests.
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "chcp 950 & cswap --list > out.txt" in workflow
    assert "LC_ALL=C uv run cswap --list > out.txt" in workflow
