"""Service management is an exclusive, pre-engine branch of ``cswap auto``."""

import sys
from unittest.mock import Mock

import pytest

from claude_swap import cli, task_scheduler
from claude_swap.exceptions import ClaudeSwitchError


@pytest.fixture
def surface(monkeypatch):
    switcher = Mock(
        side_effect=AssertionError("service management constructed a switcher")
    )
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", switcher)
    return switcher


@pytest.mark.parametrize(
    "flag,operation",
    [
        ("--install-service", "install"),
        ("--service-status", "status"),
        ("--uninstall-service", "uninstall"),
    ],
)
def test_service_flag_routes_only_to_manager(
    flag, operation, surface, monkeypatch, capsys
):
    calls = {}
    result = {
        "installed": True,
        "state": "Running",
        "task_name": "probe",
        "removed": True,
        "log": "log",
        "output_log": "output",
    }
    for name in ("install", "status", "uninstall"):
        calls[name] = Mock(return_value=result)
        monkeypatch.setattr(task_scheduler, name, calls[name])
    with pytest.raises(SystemExit) as exc:
        cli._auto_command([flag])
    assert exc.value.code == 0
    for name, call in calls.items():
        assert call.call_count == (name == operation)
    surface.assert_not_called()
    assert "Auto service" in capsys.readouterr().out


@pytest.mark.parametrize(
    "runtime",
    [
        ["--once"],
        ["--dry-run"],
        ["--json"],
        ["--debug"],
        ["--threshold", "90"],
        ["--interval", "60"],
        ["--cooldown", "0"],
        ["--model", "Fable"],
        ["--strategy", "best"],
        ["--include-api-key-accounts"],
        ["--no-include-api-key-accounts"],
    ],
)
@pytest.mark.parametrize(
    "service", ["--install-service", "--service-status", "--uninstall-service"]
)
def test_service_rejects_runtime_options_even_default_values(
    runtime, service, surface, capsys
):
    with pytest.raises(SystemExit) as exc:
        cli._auto_command([service, *runtime])
    assert exc.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err
    surface.assert_not_called()


@pytest.mark.parametrize(
    "pair",
    [
        ["--install-service", "--service-status"],
        ["--install-service", "--uninstall-service"],
        ["--service-status", "--uninstall-service"],
    ],
)
def test_service_operations_are_mutually_exclusive(pair, surface):
    with pytest.raises(SystemExit) as exc:
        cli._auto_command(pair)
    assert exc.value.code == 2
    surface.assert_not_called()


@pytest.mark.parametrize(
    "flag,operation",
    [
        ("--install-service", "install"),
        ("--service-status", "status"),
        ("--uninstall-service", "uninstall"),
    ],
)
def test_manager_failure_never_prints_success(
    flag, operation, surface, monkeypatch, capsys
):
    monkeypatch.setattr(
        task_scheduler,
        operation,
        Mock(side_effect=ClaudeSwitchError("manager refused")),
    )
    with pytest.raises(SystemExit) as exc:
        cli._auto_command([flag])
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "manager refused" in output.out + output.err
    assert "Auto service installed:" not in output.out
    assert "Auto service removed." not in output.out
    assert "not installed" not in output.out
    surface.assert_not_called()


def test_service_does_not_fall_through_when_exit_is_mocked(surface, monkeypatch):
    monkeypatch.setattr(task_scheduler, "status", lambda: {"installed": False})
    monkeypatch.setattr(sys, "exit", Mock())
    cli._auto_command(["--service-status"])
    surface.assert_not_called()


def test_service_flag_on_other_command_is_not_silently_ignored(surface, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cswap", "list", "--install-service"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    surface.assert_not_called()


def test_non_windows_service_has_clean_cli_error(surface, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as exc:
        cli._auto_command(["--service-status"])
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "only available on Windows" in output.out + output.err
    surface.assert_not_called()
