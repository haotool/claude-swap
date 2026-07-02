"""Tests for repository-level CI quality gates."""

from __future__ import annotations

from pathlib import Path


def test_ci_mypy_gate_runs_strict() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv run mypy --strict src/claude_swap" in workflow
