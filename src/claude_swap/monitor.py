"""Plain CLI auto-switch monitor with adaptive polling.

The monitor periodically checks the active account's usage percentage and
switches to the cooldown-aware best target when a configured threshold is
reached.  The
cadence is *adaptive*: when usage is far from threshold and stable, the
monitor polls slowly (the historical 60s ceiling); as usage approaches the
threshold or starts climbing, the interval shrinks toward a floor of 5s so
the swap fires before the user hits a rate-limit error.

Every tunable lives behind a named constant with a documented rationale —
no inline magic numbers — so the trade-offs stay auditable.  The pure
function ``_next_poll_interval`` and ``_failure_backoff_seconds`` are easy
to test in isolation; ``run_cli_monitor`` wires them into the existing
PID-file + SIGTERM-handling skeleton.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

MonitorStepKind = Literal[
    "disabled",
    "idle",
    "usage_unavailable",
    "threshold_no_handler",
    "switch_failed",
    "switch_cancelled",
    "switched",
    "already_optimal",
    "polled",
]

from claude_swap.models import AutoSwitchDecisionContext, BackgroundAutoSwitchIntent
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, bolded, dimmed, muted
from claude_swap.switcher import ClaudeAccountSwitcher

PerformSwitch = Callable[[AutoSwitchDecisionContext], bool]

# ----------------------------------------------------------------------------
# Polling cadence — all tunables live here so the trade-offs are auditable.
# ----------------------------------------------------------------------------

# Ceiling on the polling interval.  Historical default; preserved as the
# canonical name so the TUI's ``MONITOR_POLL_SECONDS`` import keeps working.
# Used as ``t_max`` in the adaptive algorithm.
MONITOR_POLL_SECONDS = 60

# Floor on the polling interval.  Below this, we risk overwhelming the usage
# API and producing log noise without meaningful gain in detection latency.
# 5s ≈ one Claude Code API round-trip plus margin: the monitor can react
# within a single user message of the threshold being crossed.
MONITOR_POLL_SECONDS_MIN = 5

# Safety factor for the time-to-threshold prediction.  If velocity says we
# hit the threshold in T seconds, schedule the next poll at T / FACTOR so
# we get at least FACTOR polls before the predicted crossing.  3 is the
# smallest value that tolerates one noisy delta without overshooting.
MONITOR_POLL_SAFETY_FACTOR = 3

# When ``current_pct`` reaches this fraction of ``threshold``, stop trusting
# the velocity model and force the floor.  A single bursty prompt at the end
# of the budget can eat the remaining few percent faster than any predictor
# can react.  0.95 → for threshold=95 the override fires at ≥90.25%.
MONITOR_POLL_NEAR_TRIGGER_RATIO = 0.95

# Base for the usage-API failure backoff sequence: BASE, 2·BASE, 4·BASE, …
# up to the polling ceiling.  Starts at the floor so a transient blip costs
# only one normal interval before retry; doubles on each consecutive failure
# so a persistent outage stops hammering the API.
MONITOR_FAILURE_BACKOFF_BASE = MONITOR_POLL_SECONDS_MIN

# Schema-break safety net.  Phase 4 idles when the session-detection
# function returns zero live PIDs.  If that signal misfires (e.g. Anthropic
# changes ``~/.claude/sessions/*.json`` and our parser silently returns []),
# we'd never poll usage again — auto-switch goes observably-silent.  Emit a
# WARNING when we've been idle this long while the config says auto-switch
# is enabled, so an on-call engineer sees the symptom in monitor.err.
MONITOR_IDLE_HEARTBEAT_SECONDS = 3600

# Sleep/wake recovery.  ``time.monotonic`` is steady across macOS sleep, but
# ``last_pct`` from before sleep is stale.  If the wall-clock gap between
# polls exceeds the polling ceiling by this multiple, treat the previous
# baseline as garbage and reset both the velocity track and the dedup'd
# switch-error key so a real new failure isn't masked as a "repeat".
MONITOR_WAKE_GAP_MULTIPLIER = 4


def should_switch(pct: float | None, threshold: int) -> bool:
    """Whether the active account's usage warrants an automatic switch.

    ``pct`` is the highest 5h/7d utilization for the active account (or
    ``None`` when usage is unavailable); ``threshold`` is the configured
    percentage. The rule is intentionally trivial and centralized so every
    caller (CLI, TUI, service) shares one definition.
    """
    return pct is not None and pct >= threshold


def _next_poll_interval(
    current_pct: float | None,
    last_pct: float | None,
    elapsed: float,
    threshold: int,
    *,
    t_min: int = MONITOR_POLL_SECONDS_MIN,
    t_max: int = MONITOR_POLL_SECONDS,
) -> int:
    """Pick the next poll interval based on velocity-to-threshold.

    Pure function — no I/O, no module state.  Behaviour summary:

    * ``t_max <= 0`` (test path): always return 0 so ``time.sleep(0)`` is a
      no-op and existing ``once=True`` test fixtures still finish in millis.
    * No baseline yet (first iteration, or just after a switch reset):
      return ``t_max`` since we have nothing to predict from.
    * Usage at or above ``NEAR_TRIGGER_RATIO * threshold``: override to
      ``t_min`` regardless of velocity — final-approach guard rail.
    * Velocity ≤ 0 (idle, plateaued, or post-switch drop): return ``t_max``.
    * Positive velocity: compute ETA to threshold, schedule next poll at
      ``ETA / SAFETY_FACTOR``, clamped to ``[t_min, t_max]``.
    """
    if t_max <= 0:
        # Test contract: poll_seconds=0 disables real sleeps everywhere.
        return 0

    # Final-approach override — see NEAR_TRIGGER_RATIO docstring.
    # Checked before the no-baseline path so a post-switch reset at high
    # usage still polls at the floor instead of idling at t_max.
    if (
        current_pct is not None
        and current_pct >= threshold * MONITOR_POLL_NEAR_TRIGGER_RATIO
    ):
        return max(min(t_min, t_max), 0)

    if current_pct is None or last_pct is None or elapsed <= 0:
        return t_max

    # Velocity in pct-points per second.  Negative values (post-switch drop)
    # and exact zero collapse to "idle" — slow polling is correct.
    delta = current_pct - last_pct
    velocity = delta / elapsed
    if velocity <= 0.0:
        return t_max

    eta_to_threshold = (threshold - current_pct) / velocity
    target = eta_to_threshold / MONITOR_POLL_SAFETY_FACTOR
    # ``round`` rather than ``int``: keep the interval closest to the
    # predicted ETA / SAFETY_FACTOR instead of always biasing earlier.
    return int(max(t_min, min(round(target), t_max)))


def _failure_backoff_seconds(
    consecutive_failures: int,
    *,
    t_max: int = MONITOR_POLL_SECONDS,
) -> int:
    """Exponential backoff for consecutive usage-API failures.

    Returns ``BASE * 2^(n-1)`` clamped to ``t_max``.  ``n=0`` collapses to
    ``MIN`` so the first successful recovery does not pay any extra delay.
    """
    if t_max <= 0:
        return 0
    if consecutive_failures <= 0:
        return MONITOR_POLL_SECONDS_MIN
    raw = MONITOR_FAILURE_BACKOFF_BASE * (2 ** (consecutive_failures - 1))
    return int(min(raw, t_max))


def _logger(switcher: ClaudeAccountSwitcher):
    """The shared 'claude-swap' file logger the switcher already configured."""
    return switcher._logger


@dataclass
class MonitorRuntimeState:
    """Mutable monitor runtime — owned exclusively by ``monitor_step``."""

    last_pct: float | None = None
    last_poll_time: float | None = None
    last_wall_time: float | None = None
    consecutive_failures: int = 0
    last_switch_error: str | None = None
    saturated_hold: bool = False
    idle_started_wall: float | None = None
    idle_heartbeat_at: float = 0.0


@dataclass(frozen=True)
class MonitorStepResult:
    """Render-neutral outcome of one monitor engine iteration."""

    kind: MonitorStepKind
    threshold: int
    pct: float | None
    next_interval: int
    pct_text: str = "unavailable"
    switched: bool = False
    switch_error: str | None = None
    user_message: str = ""
    consecutive_failures: int = 0


def monitor_step(
    switcher: ClaudeAccountSwitcher,
    state: MonitorRuntimeState,
    *,
    poll_seconds: int = MONITOR_POLL_SECONDS,
    perform_switch: PerformSwitch | None = None,
) -> MonitorStepResult:
    """Advance the monitor by one decision cycle (no sleep / no rendering).

    CLI, TUI, and launchd adapters call this in a loop and handle I/O
    themselves.  ``perform_switch`` receives the poll-cycle decision snapshot
    and must invoke ``switcher.switch(BackgroundAutoSwitchIntent(...))`` (or
    the interactive equivalent for TUI).
    """
    log = _logger(switcher)
    cfg = switcher.get_auto_switch_config()
    if not cfg["enabled"]:
        log.info("monitor poll: auto-switch disabled — sleeping")
        return MonitorStepResult(
            kind="disabled",
            threshold=int(cfg["threshold"]),
            pct=state.last_pct,
            next_interval=poll_seconds,
            user_message="Auto-switch disabled.",
        )

    threshold = int(cfg["threshold"])
    wall = time.time()

    live_pids = switcher._live_default_mode_claude_pids()
    if not live_pids:
        if state.idle_started_wall is None:
            state.idle_started_wall = wall
        elif (
            wall - state.idle_started_wall >= MONITOR_IDLE_HEARTBEAT_SECONDS
            and wall - state.idle_heartbeat_at >= MONITOR_IDLE_HEARTBEAT_SECONDS
        ):
            log.warning(
                "monitor idle for %ds with auto-switch enabled — "
                "no live Claude Code sessions detected. If you have "
                "Claude Code running, the session-detection signal "
                "may have changed (claude-code internals).",
                int(wall - state.idle_started_wall),
            )
            state.idle_heartbeat_at = wall

        log.info(
            "monitor poll: no live Claude Code sessions — idle at %ds",
            poll_seconds,
        )
        state.last_pct = None
        state.last_poll_time = None
        state.last_wall_time = None
        state.consecutive_failures = 0
        return MonitorStepResult(
            kind="idle",
            threshold=threshold,
            pct=None,
            next_interval=poll_seconds,
            pct_text="idle",
            user_message="No live Claude Code sessions — idle.",
        )

    state.idle_started_wall = None
    state.idle_heartbeat_at = 0.0

    if (
        state.last_wall_time is not None
        and wall - state.last_wall_time
        > MONITOR_WAKE_GAP_MULTIPLIER * poll_seconds
    ):
        log.info(
            "monitor: wake-gap %ds detected — resetting baselines",
            int(wall - state.last_wall_time),
        )
        state.last_pct = None
        state.last_poll_time = None
        state.last_switch_error = None
        state.saturated_hold = False
        state.consecutive_failures = 0

    now = time.monotonic()
    pct = switcher.get_active_usage_pct()
    pct_text = "unavailable" if pct is None else f"{pct:.0f}%"

    if pct is None:
        state.consecutive_failures += 1
        interval = _failure_backoff_seconds(
            state.consecutive_failures, t_max=poll_seconds,
        )
        log.warning(
            "monitor poll: active_usage_pct=None failures=%d backoff=%ds",
            state.consecutive_failures,
            interval,
        )
        state.last_pct = None
        state.last_poll_time = None
        state.last_wall_time = wall
        return MonitorStepResult(
            kind="usage_unavailable",
            threshold=threshold,
            pct=None,
            next_interval=interval,
            pct_text=pct_text,
            consecutive_failures=state.consecutive_failures,
            user_message=(
                f"Usage unavailable — retry in {interval}s "
                f"({state.consecutive_failures} consecutive failures)."
            ),
        )

    state.consecutive_failures = 0
    elapsed = (now - state.last_poll_time) if state.last_poll_time is not None else 0.0
    interval = _next_poll_interval(
        pct, state.last_pct, elapsed, threshold,
        t_max=poll_seconds,
    )
    log.info(
        "monitor poll: active_usage_pct=%s threshold=%s next_poll=%ds",
        pct, threshold, interval,
    )

    if should_switch(pct, threshold):
        log.info(
            "monitor threshold reached: pct=%s threshold=%s — switching",
            pct,
            threshold,
        )
        if perform_switch is None:
            log.warning(
                "monitor threshold reached but no switch handler: pct=%s threshold=%s",
                pct,
                threshold,
            )
            state.last_pct = None
            state.last_poll_time = None
            state.last_wall_time = wall
            return MonitorStepResult(
                kind="threshold_no_handler",
                threshold=threshold,
                pct=pct,
                next_interval=interval,
                pct_text=pct_text,
                user_message=(
                    f"Reached {pct:.0f}% — threshold crossed but no switch handler."
                ),
            )
        if state.saturated_hold:
            log.info(
                "monitor: saturated hold at pct=%s — skipping replan",
                pct,
            )
            state.last_pct = pct
            state.last_poll_time = now
            state.last_wall_time = wall
            return MonitorStepResult(
                kind="already_optimal",
                threshold=threshold,
                pct=pct,
                next_interval=interval,
                pct_text=pct_text,
                user_message=(
                    f"Reached {pct:.0f}% — already on soonest-to-free account."
                ),
            )

        switched = False
        switch_error: str | None = None
        try:
            decision = switcher.build_auto_switch_decision(threshold, pct)
            switched = perform_switch(decision)
        except SwitchCancelled:
            log.info("monitor switch cancelled at pct=%s", pct)
            state.saturated_hold = False
            state.last_pct = pct
            state.last_poll_time = now
            state.last_wall_time = wall
            return MonitorStepResult(
                kind="switch_cancelled",
                threshold=threshold,
                pct=pct,
                next_interval=interval,
                pct_text=pct_text,
                user_message=f"Reached {pct:.0f}% — switch cancelled.",
            )
        except (ClaudeSwitchError, OSError) as exc:
            switch_error = str(exc)
            err_msg = switch_error
            if err_msg == state.last_switch_error:
                log.debug(
                    "monitor switch failed (repeat): pct=%s error=%s",
                    pct, exc,
                )
            else:
                log.warning(
                    "monitor switch failed: pct=%s error=%s", pct, exc
                )
                state.last_switch_error = err_msg
        else:
            if switched:
                log.info("monitor switched account at pct=%s", pct)
                state.last_switch_error = None
            else:
                log.info(
                    "monitor: already on optimal account at pct=%s — holding",
                    pct,
                )
        state.last_wall_time = wall
        if switch_error is not None:
            state.last_pct = None
            state.last_poll_time = None
            return MonitorStepResult(
                kind="switch_failed",
                threshold=threshold,
                pct=pct,
                next_interval=interval,
                pct_text=pct_text,
                switch_error=switch_error,
                user_message=f"Reached {pct:.0f}% — switch failed (see above).",
            )
        if switched:
            state.saturated_hold = False
            state.last_pct = None
            state.last_poll_time = None
            return MonitorStepResult(
                kind="switched",
                threshold=threshold,
                pct=pct,
                next_interval=interval,
                pct_text=pct_text,
                switched=True,
                user_message=f"Reached {pct:.0f}% — switched account.",
            )
        state.saturated_hold = True
        state.last_pct = pct
        state.last_poll_time = now
        return MonitorStepResult(
            kind="already_optimal",
            threshold=threshold,
            pct=pct,
            next_interval=interval,
            pct_text=pct_text,
            user_message=(
                f"Reached {pct:.0f}% — already on soonest-to-free account."
            ),
        )

    state.saturated_hold = False
    state.last_pct = pct
    state.last_poll_time = now
    state.last_wall_time = wall
    return MonitorStepResult(
        kind="polled",
        threshold=threshold,
        pct=pct,
        next_interval=interval,
        pct_text=pct_text,
        user_message="Monitoring active account.",
    )


class _MonitorStopped(Exception):
    """Raised when the foreground monitor receives a stop signal."""


class SwitchCancelled(Exception):
    """Raised when an interactive switch is cancelled (e.g. Ctrl-C)."""


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


def _release_monitor_pid(path: Path) -> None:
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def run_cli_monitor(
    switcher: ClaudeAccountSwitcher,
    *,
    poll_seconds: int = MONITOR_POLL_SECONDS,
    once: bool = False,
    stream: TextIO | None = None,
) -> int:
    """Run the foreground auto-switch monitor from the CLI.

    ``poll_seconds`` is the *ceiling* on the adaptive interval (not the
    fixed cadence).  Tests pass ``poll_seconds=0`` to disable sleeps and the
    adaptive algorithm degrades cleanly to "no sleep" in that case.
    """
    out = stream or sys.stdout
    log = _logger(switcher)
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
        f"  {dimmed(f'threshold {threshold}% · adaptive {MONITOR_POLL_SECONDS_MIN}–{poll_seconds}s')}",
        file=out,
    )
    print(f"  {dimmed(f'pid {os.getpid()}')}", file=out)
    log.info(
        "monitor start: threshold=%s adaptive=%s-%ss pid=%s",
        threshold,
        MONITOR_POLL_SECONDS_MIN,
        poll_seconds,
        os.getpid(),
    )

    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_monitor(_signum, _frame) -> None:
        raise _MonitorStopped

    signal.signal(signal.SIGTERM, stop_monitor)

    state = MonitorRuntimeState()

    def perform_switch(decision: AutoSwitchDecisionContext) -> bool:
        return switcher.switch(BackgroundAutoSwitchIntent(decision=decision))

    try:
        while True:
            result = monitor_step(
                switcher,
                state,
                poll_seconds=poll_seconds,
                perform_switch=perform_switch,
            )
            threshold = result.threshold

            if result.kind == "disabled":
                if once:
                    return 0
                time.sleep(result.next_interval)
                continue

            if result.kind == "idle":
                print(
                    f"  {muted('active usage:')} {result.pct_text} "
                    f"{muted(f'· idle {result.next_interval}s')}",
                    file=out,
                    flush=True,
                )
                if once:
                    return 0
                time.sleep(result.next_interval)
                continue

            if result.kind == "usage_unavailable":
                print(
                    f"  {muted('active usage:')} {result.pct_text} "
                    f"{muted(f'· backoff {result.next_interval}s '
                             f'({result.consecutive_failures} consecutive failures)')}",
                    file=out,
                    flush=True,
                )
                if once:
                    return 0
                time.sleep(result.next_interval)
                continue

            print(
                f"  {muted('active usage:')} {result.pct_text} "
                f"{muted(f'· next poll {result.next_interval}s')}",
                file=out,
                flush=True,
            )

            if result.kind in ("switched", "switch_failed", "already_optimal"):
                if result.kind == "already_optimal":
                    print(
                        f"  {accent('threshold reached')} "
                        f"{muted('holding on soonest-to-free account')}",
                        file=out,
                        flush=True,
                    )
                else:
                    print(
                        f"  {accent('threshold reached')} {muted('switching account')}",
                        file=out,
                        flush=True,
                    )
                    if result.kind == "switch_failed":
                        print(
                            f"  {dimmed(f'switch failed: {result.switch_error}')}",
                            file=out,
                            flush=True,
                        )

            if once:
                return 0
            time.sleep(result.next_interval)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Monitor stopped')}", file=out)
        return 130
    except _MonitorStopped:
        print(f"\n{dimmed('Monitor stopped')}", file=out)
        return 143
    finally:
        log.info("monitor stopped")
        signal.signal(signal.SIGTERM, previous_sigterm)
        _release_monitor_pid(pid_path)
