"""Structural Protocol satisfaction for monitor and credential refresh hosts."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from claude_swap.credential_refresh import CredentialRefresher
from claude_swap.models import (
    AutoSwitchDecisionContext,
    SwitchPreconditionKind,
    SwitchPreconditions,
)
from claude_swap.monitor import MonitorRuntimeState, monitor_step
from claude_swap.switch_cli import run_switch_cli


def _monitor_host(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        backup_dir=tmp_path,
        _logger=logging.getLogger("test.monitor_host"),
        get_auto_switch_config=lambda: {"enabled": False, "threshold": 95},
        ensure_auto_switch_enabled=lambda: {"enabled": True, "threshold": 95},
        get_active_usage_pct=lambda: None,
        get_active_usage_breakdown=lambda: None,
        active_account_is_api_key=lambda: False,
        build_auto_switch_decision=lambda threshold, pct: AutoSwitchDecisionContext(
            threshold=threshold,
            active_usage_pct=pct,
            live_active_slot=None,
            sequence_active_slot=None,
            usage_by_slot={},
        ),
        switch=lambda *args, **kwargs: False,
        _live_default_mode_claude_pids=lambda: [],
        _get_sequence_data=lambda: {"sequence": []},
        _account_is_switchable=lambda num: True,
        _trusted_usage_snapshots=lambda: {},
        _refresh_switchable_usage_cache=lambda: None,
    )


def test_monitor_step_accepts_minimal_monitor_host(tmp_path: Path):
    host = _monitor_host(tmp_path)
    result = monitor_step(host, MonitorRuntimeState(), poll_seconds=60)
    assert result.kind == "disabled"


def test_credential_refresher_accepts_minimal_refresh_host(tmp_path: Path):
    lock_file = tmp_path / ".lock"
    store: dict[tuple[str, str], str] = {}
    live = {
        "creds": '{"claudeAiOauth":{"accessToken":"t","refreshToken":"r","expiresAt":9}}'
    }
    host = SimpleNamespace(
        lock_file=lock_file,
        _logger=logging.getLogger("test.refresh_host"),
        _read_credentials=lambda: live["creds"],
        _read_account_credentials=lambda num, email: store.get((num, email), ""),
        _write_account_credentials=lambda num, email, creds: store.__setitem__(
            (num, email),
            creds,
        ),
        _live_session_pids=lambda num, email: [],
    )
    refresher = CredentialRefresher(host)
    result = refresher.write_verified_live("1", "a@example.com", live["creds"])
    assert result == live["creds"]


def test_run_switch_cli_accepts_minimal_switch_cli_host():
    host = SimpleNamespace(
        _classify_switch_preconditions=lambda: SwitchPreconditions(
            kind=SwitchPreconditionKind.SINGLE_ACCOUNT,
            identity=("a@example.com", "org-uuid"),
            current_slot="1",
        ),
        _switch_noop=lambda **kwargs: {
            "switched": False,
            "strategy": kwargs["strategy"],
            "reason": kwargs["reason"],
        },
    )
    result = run_switch_cli(host, json_output=True)
    assert result == {
        "switched": False,
        "strategy": "rotation",
        "reason": "only-one-account",
    }
