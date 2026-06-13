"""Plain CLI auto-switch monitor."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import TextIO

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, bolded, dimmed, muted
from claude_swap.switcher import ClaudeAccountSwitcher

AUTO_MONITOR_POLL_SECONDS = 60


class _MonitorStopped(Exception):
    """Raised when the foreground monitor receives a stop signal."""


def _pid_file(switcher: ClaudeAccountSwitcher) -> Path:
    return switcher.backup_dir / "auto-switch-monitor.pid"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_running_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_is_running(pid) else None


def _acquire_monitor_pid(path: Path) -> int | None:
    existing = _read_running_pid(path)
    if existing is not None:
        return existing
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    return None


def run_cli_monitor(
    switcher: ClaudeAccountSwitcher,
    *,
    poll_seconds: int = AUTO_MONITOR_POLL_SECONDS,
    once: bool = False,
    stream: TextIO | None = None,
) -> int:
    """Run the foreground auto-switch monitor from the CLI."""
    out = stream or sys.stdout
    cfg = switcher.get_auto_switch_config()
    if not cfg["enabled"]:
        cfg = switcher.set_auto_switch_config(enabled=True)
    threshold = int(cfg["threshold"])

    pid_path = _pid_file(switcher)
    running_pid = _acquire_monitor_pid(pid_path)
    if running_pid is not None:
        print(
            f"{bolded('Status:')} Auto-switch monitor (Beta) "
            f"{muted(f'already running (pid {running_pid})')}",
            file=out,
        )
        return 0

    print(bolded("Auto-switch monitor (Beta)"), file=out)
    print(
        f"  {dimmed(f'threshold {threshold}% · polling every {poll_seconds}s')}",
        file=out,
    )
    print(f"  {dimmed(f'pid {os.getpid()}')}", file=out)

    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_monitor(_signum, _frame) -> None:
        raise _MonitorStopped

    signal.signal(signal.SIGTERM, stop_monitor)
    try:
        while True:
            pct = switcher.get_active_usage_pct()
            pct_text = "unavailable" if pct is None else f"{pct:.0f}%"
            print(f"  {muted('active usage:')} {pct_text}", file=out, flush=True)

            if pct is not None and pct >= threshold:
                print(
                    f"  {accent('threshold reached')} {muted('switching account')}",
                    file=out,
                    flush=True,
                )
                try:
                    switcher.switch()
                except ClaudeSwitchError as exc:
                    print(f"  {dimmed(f'switch failed: {exc}')}", file=out, flush=True)

            if once:
                return 0
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Monitor stopped')}", file=out)
        return 130
    except _MonitorStopped:
        print(f"\n{dimmed('Monitor stopped')}", file=out)
        return 143
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
