"""Tests for repository-level CI quality gates."""

from __future__ import annotations

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
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "continue-on-error" not in workflow
    assert "Start-Sleep" in workflow


def test_ci_has_linux_systemd_round_trip() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "linux-systemd:" in workflow
    assert "systemctl --user is-active cswap-monitor.service" in workflow
