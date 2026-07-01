"""Auto-switch monitor for claude-swap.

Owns the adaptive polling loop that watches the active account's usage and
triggers background switches when a configured threshold is crossed. Split out
so ``monitor_step`` serves the foreground ``--monitor`` command, the TUI, and
the launchd service from one decision engine.

Depends on ``MonitorHost`` for reads and planning but never owns credential
storage or switch orchestration — callers supply ``perform_switch`` to wire the
actual ``switch(BackgroundAutoSwitchIntent(...))`` call.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TextIO, cast

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AutoSwitchDecisionContext, BackgroundAutoSwitchIntent
from claude_swap.printer import accent, bolded, dimmed, muted
from claude_swap.protocols import MonitorHost

PerformSwitch = Callable[[AutoSwitchDecisionContext], bool]

MonitorStepKind = Literal[
    "disabled",
    "idle",
    "usage_unavailable",
    "threshold_no_handler",
    "switch_failed",
    "switch_cancelled",
    "switched",
    "already_optimal",
    "no_trusted_signal",
    "polled",
]

_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}

# Adaptive polling — ceiling exported for the TUI; min/factor/ratio tune the schedule.
MONITOR_POLL_SECONDS = 60
MONITOR_POLL_SECONDS_MIN = 5
MONITOR_POLL_SAFETY_FACTOR = 3
MONITOR_POLL_NEAR_TRIGGER_RATIO = 0.95

# Exponential backoff on consecutive usage-API failures.
MONITOR_FAILURE_BACKOFF_BASE = MONITOR_POLL_SECONDS_MIN
# Honour a server ``Retry-After`` up to this ceiling so a 429 cannot wedge the
# monitor into a pathologically long sleep.
MONITOR_RETRY_AFTER_CAP = 300

# Idle heartbeat and sleep/wake baseline reset.
MONITOR_IDLE_HEARTBEAT_SECONDS = 3600
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

    Pure function — no I/O, no module state. When ``t_max <= 0`` (test path)
    always returns 0 so ``once=True`` fixtures finish without sleeping. With no
    velocity baseline yet, or when velocity is non-positive, returns ``t_max``.
    Near the threshold (usage at or above ``NEAR_TRIGGER_RATIO * threshold``)
    forces ``t_min``; otherwise positive velocity yields
    ``ETA / SAFETY_FACTOR`` clamped to ``[t_min, t_max]``.
    """
    if t_max <= 0:
        return 0

    if (
        current_pct is not None
        and current_pct >= threshold * MONITOR_POLL_NEAR_TRIGGER_RATIO
    ):
        return max(min(t_min, t_max), 0)

    if current_pct is None or last_pct is None or elapsed <= 0:
        return t_max

    delta = current_pct - last_pct
    velocity = delta / elapsed
    if velocity <= 0.0:
        return t_max

    eta_to_threshold = (threshold - current_pct) / velocity
    target = eta_to_threshold / MONITOR_POLL_SAFETY_FACTOR
    return int(max(t_min, min(round(target), t_max)))


def _next_poll_interval_multi(
    current: dict[str, float],
    last: dict[str, float],
    elapsed: float,
    threshold: int,
    *,
    t_min: int = MONITOR_POLL_SECONDS_MIN,
    t_max: int = MONITOR_POLL_SECONDS,
) -> int:
    """Adaptive poll interval across all usage windows — most urgent wins.

    Computes ``_next_poll_interval`` per window (each against its own previous
    reading) and returns the minimum, so a fast-climbing 5h window drives a
    short interval even while a higher but flat 7d window would, alone, sit at
    ``t_max``. Falls back to ``t_max`` when no window has a usable reading.
    """
    if t_max <= 0:
        return 0
    intervals = [
        _next_poll_interval(
            pct, last.get(window), elapsed, threshold, t_min=t_min, t_max=t_max,
        )
        for window, pct in current.items()
    ]
    return min(intervals) if intervals else t_max


def _failure_backoff_seconds(
    consecutive_failures: int,
    *,
    t_max: int = MONITOR_POLL_SECONDS,
) -> int:
    """Exponential backoff for consecutive usage-API failures.

    Returns ``BASE * 2^(n-1)`` clamped to ``t_max``. ``n=0`` collapses to
    ``MIN`` so the first successful recovery does not pay any extra delay.
    """
    if t_max <= 0:
        return 0
    if consecutive_failures <= 0:
        return MONITOR_POLL_SECONDS_MIN
    raw = MONITOR_FAILURE_BACKOFF_BASE * (2 ** (consecutive_failures - 1))
    return int(min(raw, t_max))


def _logger(switcher: MonitorHost) -> logging.Logger:
    """The shared 'claude-swap' file logger the switcher already configured."""
    return switcher._logger


def _stay_put_kind(
    switcher: MonitorHost,
    decision: AutoSwitchDecisionContext,
) -> Literal["already_optimal", "no_trusted_signal"]:
    """Map a no-op automated switch to an honest monitor step kind."""
    plan_fn = getattr(switcher, "plan_automated_switch", None)
    if plan_fn is None:
        return "already_optimal"
    if plan_fn(decision).outcome == "no_trusted_signal":
        return "no_trusted_signal"
    return "already_optimal"


@dataclass
class MonitorRuntimeState:
    """Mutable monitor runtime — owned exclusively by ``monitor_step``."""

    last_pct: float | None = None
    last_pcts: dict[str, float] = field(default_factory=dict)
    last_poll_time: float | None = None
    last_wall_time: float | None = None
    consecutive_failures: int = 0
    last_switch_error: str | None = None
    saturated_hold: bool = False
    usage_cache_warmed: bool = False
    idle_started_wall: float | None = None
    idle_heartbeat_at: float = 0.0

    def record_pcts(self, pct: float, windows: dict[str, float] | None) -> None:
        """Record this poll's readings as the velocity baseline."""
        self.last_pct = pct
        self.last_pcts = dict(windows) if windows else {}

    def reset_pcts(self) -> None:
        """Clear the velocity baseline (after a switch, failure, or wake-gap)."""
        self.last_pct = None
        self.last_pcts = {}


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


def _step_disabled(
    state: MonitorRuntimeState,
    threshold: int,
    poll_seconds: int,
    log: logging.Logger,
) -> MonitorStepResult:
    log.info("monitor poll: auto-switch disabled — sleeping")
    return MonitorStepResult(
        kind="disabled",
        threshold=threshold,
        pct=state.last_pct,
        next_interval=poll_seconds,
        user_message="Auto-switch disabled.",
    )


def _step_idle(
    state: MonitorRuntimeState,
    poll_seconds: int,
    wall: float,
    threshold: int,
    log: logging.Logger,
) -> MonitorStepResult:
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
    state.reset_pcts()
    state.last_poll_time = None
    state.last_wall_time = None
    state.consecutive_failures = 0
    state.saturated_hold = False
    return MonitorStepResult(
        kind="idle",
        threshold=threshold,
        pct=None,
        next_interval=poll_seconds,
        pct_text="idle",
        user_message="No live Claude Code sessions — idle.",
    )


def _apply_wake_gap_reset(
    state: MonitorRuntimeState,
    wall: float,
    poll_seconds: int,
    log: logging.Logger,
) -> None:
    if (
        state.last_wall_time is not None
        and wall - state.last_wall_time
        > MONITOR_WAKE_GAP_MULTIPLIER * poll_seconds
    ):
        log.info(
            "monitor: wake-gap %ds detected — resetting baselines",
            int(wall - state.last_wall_time),
        )
        state.reset_pcts()
        state.last_poll_time = None
        state.last_switch_error = None
        state.saturated_hold = False
        state.consecutive_failures = 0


def _step_idle_api_key(
    state: MonitorRuntimeState,
    poll_seconds: int,
    wall: float,
    threshold: int,
    log: logging.Logger,
) -> MonitorStepResult:
    state.consecutive_failures = 0
    state.reset_pcts()
    state.last_poll_time = None
    state.last_wall_time = wall
    state.saturated_hold = False
    log.info("monitor poll: active account is API-key (no quota) — idle")
    return MonitorStepResult(
        kind="idle",
        threshold=threshold,
        pct=None,
        next_interval=poll_seconds,
        pct_text="api-key",
        user_message="Active account is an API-key account (no quota to monitor).",
    )


def _step_usage_unavailable(
    state: MonitorRuntimeState,
    poll_seconds: int,
    wall: float,
    threshold: int,
    pct_text: str,
    log: logging.Logger,
    retry_after: int | None = None,
) -> MonitorStepResult:
    state.consecutive_failures += 1
    interval = _failure_backoff_seconds(
        state.consecutive_failures, t_max=poll_seconds,
    )
    if retry_after is not None:
        # A rate-limited fetch carries the server's own backoff window; honour
        # it (capped) rather than hammering the API on our shorter schedule.
        interval = max(interval, min(retry_after, MONITOR_RETRY_AFTER_CAP))
    log.warning(
        "monitor poll: active_usage_pct=None failures=%d backoff=%ds",
        state.consecutive_failures,
        interval,
    )
    state.reset_pcts()
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


def _step_threshold_no_handler(
    state: MonitorRuntimeState,
    *,
    threshold: int,
    pct: float,
    pct_text: str,
    interval: int,
    wall: float,
    log: logging.Logger,
) -> MonitorStepResult:
    log.warning(
        "monitor threshold reached but no switch handler: pct=%s threshold=%s",
        pct,
        threshold,
    )
    state.reset_pcts()
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


def _attempt_threshold_switch(
    switcher: MonitorHost,
    state: MonitorRuntimeState,
    perform_switch: PerformSwitch,
    *,
    threshold: int,
    pct: float,
    pct_text: str,
    windows: dict[str, float],
    interval: int,
    wall: float,
    now: float,
    log: logging.Logger,
) -> tuple[bool, str | None, AutoSwitchDecisionContext | None] | MonitorStepResult:
    switched = False
    switch_error: str | None = None
    decision: AutoSwitchDecisionContext | None = None
    try:
        decision = switcher.build_auto_switch_decision(threshold, pct)
        switched = perform_switch(decision)
    except SwitchCancelled:
        log.info("monitor switch cancelled at pct=%s", pct)
        state.saturated_hold = False
        state.record_pcts(pct, windows)
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
        if switch_error == state.last_switch_error:
            log.debug(
                "monitor switch failed (repeat): pct=%s error=%s",
                pct, exc,
            )
        else:
            log.warning(
                "monitor switch failed: pct=%s error=%s", pct, exc
            )
            state.last_switch_error = switch_error
    else:
        if switched:
            log.info("monitor switched account at pct=%s", pct)
            state.last_switch_error = None
        else:
            stay_put = _stay_put_kind(switcher, decision)
            if stay_put == "no_trusted_signal":
                log.info(
                    "monitor: no trusted usage signal at pct=%s — holding",
                    pct,
                )
            else:
                log.info(
                    "monitor: already on optimal account at pct=%s — holding",
                    pct,
                )
    return switched, switch_error, decision


def _finalize_threshold_step(
    switcher: MonitorHost,
    state: MonitorRuntimeState,
    decision: AutoSwitchDecisionContext | None,
    *,
    threshold: int,
    pct: float,
    pct_text: str,
    windows: dict[str, float],
    interval: int,
    poll_seconds: int,
    wall: float,
    now: float,
    switched: bool,
    switch_error: str | None,
) -> MonitorStepResult:
    state.last_wall_time = wall
    if switch_error is not None:
        state.reset_pcts()
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
        state.reset_pcts()
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
    assert decision is not None
    stay_put = _stay_put_kind(switcher, decision)
    state.saturated_hold = True
    state.record_pcts(pct, windows)
    state.last_poll_time = now
    # When staying put at threshold, respect a server Retry-After already folded
    # into `interval` (masked-429 case) instead of only the hold cadence.
    hold_interval = max(poll_seconds, interval)
    if stay_put == "no_trusted_signal":
        return MonitorStepResult(
            kind="no_trusted_signal",
            threshold=threshold,
            pct=pct,
            next_interval=hold_interval,
            pct_text=pct_text,
            user_message=(
                f"Reached {pct:.0f}% — no trusted usage signal; staying put."
            ),
        )
    return MonitorStepResult(
        kind="already_optimal",
        threshold=threshold,
        pct=pct,
        next_interval=hold_interval,
        pct_text=pct_text,
        user_message=(
            f"Reached {pct:.0f}% — already on soonest-to-free account."
        ),
    )


def _step_threshold(
    switcher: MonitorHost,
    state: MonitorRuntimeState,
    *,
    threshold: int,
    pct: float,
    pct_text: str,
    windows: dict[str, float],
    interval: int,
    poll_seconds: int,
    wall: float,
    now: float,
    perform_switch: PerformSwitch | None,
    log: logging.Logger,
) -> MonitorStepResult:
    if perform_switch is None:
        return _step_threshold_no_handler(
            state,
            threshold=threshold,
            pct=pct,
            pct_text=pct_text,
            interval=interval,
            wall=wall,
            log=log,
        )
    if state.saturated_hold:
        log.info(
            "monitor: saturated hold at pct=%s — replanning at %ds",
            pct,
            poll_seconds,
        )
    else:
        log.info(
            "monitor threshold reached: pct=%s threshold=%s — switching",
            pct,
            threshold,
        )
    outcome = _attempt_threshold_switch(
        switcher,
        state,
        perform_switch,
        threshold=threshold,
        pct=pct,
        pct_text=pct_text,
        windows=windows,
        interval=interval,
        wall=wall,
        now=now,
        log=log,
    )
    if isinstance(outcome, MonitorStepResult):
        return outcome
    switched, switch_error, decision = outcome
    return _finalize_threshold_step(
        switcher,
        state,
        decision,
        threshold=threshold,
        pct=pct,
        pct_text=pct_text,
        windows=windows,
        interval=interval,
        poll_seconds=poll_seconds,
        wall=wall,
        now=now,
        switched=switched,
        switch_error=switch_error,
    )


def _step_polled(
    state: MonitorRuntimeState,
    *,
    threshold: int,
    pct: float,
    pct_text: str,
    windows: dict[str, float],
    interval: int,
    wall: float,
    now: float,
) -> MonitorStepResult:
    state.saturated_hold = False
    state.record_pcts(pct, windows)
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


def _warm_usage_cache_on_first_poll(
    switcher: MonitorHost,
    state: MonitorRuntimeState,
    log: logging.Logger,
) -> None:
    """One-shot cache refresh when monitor starts with incomplete snapshots."""
    if state.usage_cache_warmed:
        return
    state.usage_cache_warmed = True

    data = switcher._get_sequence_data() or {}
    switchable = [
        str(num)
        for num in data.get("sequence", [])
        if switcher._account_is_switchable(str(num))
    ]
    if not switchable:
        return

    snapshots = switcher._trusted_usage_snapshots()
    if len(snapshots) >= len(switchable):
        return

    log.info(
        "monitor: warming usage cache (%d/%d trusted snapshots)",
        len(snapshots),
        len(switchable),
    )
    switcher._refresh_switchable_usage_cache()


def monitor_step(
    switcher: MonitorHost,
    state: MonitorRuntimeState,
    *,
    poll_seconds: int = MONITOR_POLL_SECONDS,
    perform_switch: PerformSwitch | None = None,
) -> MonitorStepResult:
    """Advance the monitor by one decision cycle (no sleep / no rendering).

    CLI, TUI, and launchd adapters call this in a loop and handle I/O
    themselves. ``perform_switch`` receives the poll-cycle decision snapshot
    and must invoke ``switcher.switch(BackgroundAutoSwitchIntent(...))`` (or
    the interactive equivalent for TUI).
    """
    log = _logger(switcher)
    cfg = switcher.get_auto_switch_config()
    if not cfg["enabled"]:
        return _step_disabled(
            state, int(cfg["threshold"]), poll_seconds, log,
        )

    threshold = int(cfg["threshold"])
    wall = time.time()

    live_pids = switcher._live_default_mode_claude_pids()
    if not live_pids:
        return _step_idle(state, poll_seconds, wall, threshold, log)

    state.idle_started_wall = None
    state.idle_heartbeat_at = 0.0

    _warm_usage_cache_on_first_poll(switcher, state, log)
    _apply_wake_gap_reset(state, wall, poll_seconds, log)

    now = time.monotonic()
    pct = switcher.get_active_usage_pct()
    pct_text = "unavailable" if pct is None else f"{pct:.0f}%"

    if pct is None and switcher.active_account_is_api_key():
        return _step_idle_api_key(state, poll_seconds, wall, threshold, log)

    if pct is None:
        return _step_usage_unavailable(
            state, poll_seconds, wall, threshold, pct_text, log,
            retry_after=switcher.get_active_usage_retry_after(),
        )

    state.consecutive_failures = 0
    breakdown = switcher.get_active_usage_breakdown()
    windows = breakdown or {"max": pct}
    elapsed = (now - state.last_poll_time) if state.last_poll_time is not None else 0.0
    interval = _next_poll_interval_multi(
        windows, state.last_pcts, elapsed, threshold,
        t_max=poll_seconds,
    )
    # A trusted prior usage row can mask an active 429: get_active_usage_pct
    # then returns the stale pct (not None), so this path — not the
    # usage-unavailable one — runs. Still honour the server's Retry-After
    # (capped) for any stay/hold interval so we don't poll through the window.
    retry_after = switcher.get_active_usage_retry_after()
    if retry_after is not None:
        interval = max(interval, min(retry_after, MONITOR_RETRY_AFTER_CAP))
    reached = should_switch(pct, threshold)
    holding = reached and perform_switch is not None and state.saturated_hold
    windows_text = " ".join(
        f"{_WINDOW_LABELS.get(k, k)}={v:.0f}%" for k, v in windows.items()
    )
    log.info(
        "monitor poll: active_usage_pct=%s (%s) threshold=%s next_poll=%ds",
        pct, windows_text, threshold, poll_seconds if holding else interval,
    )

    if reached:
        return _step_threshold(
            switcher,
            state,
            threshold=threshold,
            pct=pct,
            pct_text=pct_text,
            windows=windows,
            interval=interval,
            poll_seconds=poll_seconds,
            wall=wall,
            now=now,
            perform_switch=perform_switch,
            log=log,
        )

    return _step_polled(
        state,
        threshold=threshold,
        pct=pct,
        pct_text=pct_text,
        windows=windows,
        interval=interval,
        wall=wall,
        now=now,
    )


class _MonitorStopped(Exception):
    """Control-flow signal: foreground monitor received SIGTERM (not a user error)."""


class SwitchCancelled(Exception):
    """Control-flow signal: interactive switch cancelled (e.g. Ctrl-C), not a user error."""


def _pid_file(switcher: MonitorHost) -> Path:
    return switcher.backup_dir / "auto-switch-monitor.pid"


def get_logger(switcher: MonitorHost) -> logging.Logger:
    """Return the shared 'claude-swap' logger the switcher configured."""
    return _logger(switcher)


def _pid_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _tasklist_image(pid: int) -> tuple[bool, str | None]:
    """Query Windows ``tasklist`` for a PID.

    Returns ``(queried, image)``: ``queried`` is ``False`` when ``tasklist`` is
    unavailable (liveness undeterminable), ``image`` is ``None`` when the query
    ran but no process owns the PID.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return (False, None)
    if result.returncode != 0:
        return (False, None)
    line = result.stdout.strip()
    if not line or line.upper().startswith("INFO:"):
        return (True, None)
    image = line.split(",", 1)[0].strip().strip('"')
    return (True, image or None)


def _pid_is_running_windows(pid: int) -> bool:
    queried, image = _tasklist_image(pid)
    if not queried:
        # tasklist unavailable: mirror POSIX's conservative bias and assume the
        # PID still belongs to the holder rather than allow a second monitor.
        return True
    if image is None:
        return False
    lowered = image.lower()
    # A ``py``/``python`` host running the module can't be told apart from an
    # unrelated interpreter by image name alone, so treat it as the holder.
    return "monitor" in lowered or any(
        token in lowered
        for token in ("claude_swap", "claude-swap", "cswap", "python", "py.exe")
    )


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmd = _pid_command(pid)
    if cmd is None:
        return True
    lowered = cmd.lower()
    # The PID file is only ever written by a monitor-start path
    # (``_acquire_monitor_pid``), so any live process whose command line belongs
    # to claude-swap is the holder. Match every entrypoint: ``cswap --monitor`` /
    # ``claude-swap --monitor`` (console scripts) and the TUI in-process monitor,
    # whose argv is just ``cswap``. The token match still rejects a recycled PID
    # running an unrelated command.
    return "monitor" in lowered or any(
        token in lowered for token in ("claude_swap", "claude-swap", "cswap")
    )


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
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_running_pid(path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    return None


def acquire_pid(switcher: MonitorHost) -> int | None:
    """Claim the single-instance monitor PID file; return the live owner if taken."""
    return _acquire_monitor_pid(_pid_file(switcher))


def _release_monitor_pid(path: Path) -> None:
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def release_pid(switcher: MonitorHost) -> None:
    """Remove this process's monitor PID file on shutdown."""
    _release_monitor_pid(_pid_file(switcher))


def active_usage_display(
    result: MonitorStepResult,
    state: MonitorRuntimeState,
) -> str:
    """Shared active-usage label for CLI/TUI adapters."""
    if result.pct is not None:
        return result.pct_text
    if state.last_pct is not None:
        return f"{state.last_pct:.0f}%"
    return "unavailable"


_THRESHOLD_REACHED_LINES: dict[
    MonitorStepKind,
    Callable[[MonitorStepResult], str],
] = {
    "already_optimal": lambda _r: (
        f"  {accent('threshold reached')} "
        f"{muted('holding on soonest-to-free account')}"
    ),
    "no_trusted_signal": lambda _r: (
        f"  {accent('threshold reached')} "
        f"{muted('no trusted usage signal — staying put')}"
    ),
    "switched": lambda _r: (
        f"  {accent('threshold reached')} {muted('switching account')}"
    ),
    "switch_failed": lambda r: (
        f"  {accent('threshold reached')} "
        f"{dimmed(f'switch failed: {r.switch_error}')}"
    ),
}


def render_step(
    result: MonitorStepResult,
    *,
    stream: TextIO | None = None,
) -> None:
    """Print CLI-facing output for one monitor engine iteration."""
    out = stream or sys.stdout
    if result.kind == "disabled":
        return

    if result.kind == "idle":
        print(
            f"  {muted('active usage:')} {result.pct_text} "
            f"{muted(f'· idle {result.next_interval}s')}",
            file=out,
            flush=True,
        )
    elif result.kind == "usage_unavailable":
        print(
            f"  {muted('active usage:')} {result.pct_text} "
            f"{muted(f'· backoff {result.next_interval}s '
                     f'({result.consecutive_failures} consecutive failures)')}",
            file=out,
            flush=True,
        )
    else:
        print(
            f"  {muted('active usage:')} {result.pct_text} "
            f"{muted(f'· next poll {result.next_interval}s')}",
            file=out,
            flush=True,
        )

    threshold_line = _THRESHOLD_REACHED_LINES.get(result.kind)
    if threshold_line is not None:
        print(threshold_line(result), file=out, flush=True)


def run_cli_monitor(
    switcher: MonitorHost,
    *,
    poll_seconds: int = MONITOR_POLL_SECONDS,
    once: bool = False,
    stream: TextIO | None = None,
) -> int:
    """Run the foreground auto-switch monitor from the CLI.

    ``poll_seconds`` is the *ceiling* on the adaptive interval (not the
    fixed cadence). Tests pass ``poll_seconds=0`` to disable sleeps and the
    adaptive algorithm degrades cleanly to "no sleep" in that case.
    """
    out = stream or sys.stdout
    log = _logger(switcher)
    cfg = switcher.ensure_auto_switch_enabled()
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

    def stop_monitor(_signum: int, _frame: object) -> None:
        raise _MonitorStopped

    signal.signal(signal.SIGTERM, stop_monitor)

    state = MonitorRuntimeState()

    def perform_switch(decision: AutoSwitchDecisionContext) -> bool:
        return cast(
            bool,
            switcher.switch(BackgroundAutoSwitchIntent(decision=decision)),
        )

    try:
        while True:
            result = monitor_step(
                switcher,
                state,
                poll_seconds=poll_seconds,
                perform_switch=perform_switch,
            )
            render_step(result, stream=out)
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
