"""Plain CLI auto-switch monitor with adaptive polling.

The monitor periodically checks the active account's usage percentage and
rotates to the next account when a configured threshold is reached.  The
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
from pathlib import Path
from typing import TextIO

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, bolded, dimmed, muted
from claude_swap.switcher import ClaudeAccountSwitcher

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

    if current_pct is None or last_pct is None or elapsed <= 0:
        return t_max

    # Final-approach override — see NEAR_TRIGGER_RATIO docstring.
    if current_pct >= threshold * MONITOR_POLL_NEAR_TRIGGER_RATIO:
        return max(min(t_min, t_max), 0)

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

    # Adaptive-polling state.  Reset to None after a switch (or whenever the
    # baseline becomes meaningless) so the next iteration falls back to t_max
    # rather than computing velocity from a stale reference point.
    last_pct: float | None = None
    last_poll_time: float | None = None
    last_wall_time: float | None = None
    consecutive_failures = 0
    # Dedup the switch-error log: a permanently broken slot (e.g. expired
    # refresh token with no live session) would otherwise spam a WARNING on
    # every poll.  Log the first occurrence loud, subsequent identical
    # failures at DEBUG so launchd's monitor.err stays scannable.
    last_switch_error: str | None = None
    # Schema-break heartbeat: when the session-detection function returns
    # zero live PIDs for an extended period, emit a single WARNING so an
    # on-call sees we've gone silent — covers the case where Anthropic
    # changes the session file schema and our parser silently bails.
    idle_started_wall: float | None = None
    idle_heartbeat_at: float = 0.0

    try:
        while True:
            # Re-read config each cycle so TUI changes (threshold, disable)
            # take effect within one poll interval without a service restart.
            cfg = switcher.get_auto_switch_config()
            if not cfg["enabled"]:
                log.info("monitor poll: auto-switch disabled — sleeping")
                if once:
                    return 0
                time.sleep(poll_seconds)
                continue
            threshold = int(cfg["threshold"])

            # Phase 4 short-circuit: if no default-mode Claude Code is running
            # the active account cannot be burning tokens, so skip the usage
            # API call entirely and idle at t_max.  Reset the velocity baseline
            # too — once a session reappears, we want a clean delta from the
            # fresh sample, not one across a long inactivity gap.
            live_pids = switcher._live_default_mode_claude_pids()
            if not live_pids:
                # Heartbeat: emit ONE WARNING when we've been idle longer
                # than MONITOR_IDLE_HEARTBEAT_SECONDS while auto-switch is
                # enabled.  Covers the schema-break failure mode where our
                # session parser silently bails and the monitor goes silent.
                wall = time.time()
                if idle_started_wall is None:
                    idle_started_wall = wall
                elif (
                    wall - idle_started_wall >= MONITOR_IDLE_HEARTBEAT_SECONDS
                    and wall - idle_heartbeat_at >= MONITOR_IDLE_HEARTBEAT_SECONDS
                ):
                    log.warning(
                        "monitor idle for %ds with auto-switch enabled — "
                        "no live Claude Code sessions detected. If you have "
                        "Claude Code running, the session-detection signal "
                        "may have changed (claude-code internals).",
                        int(wall - idle_started_wall),
                    )
                    idle_heartbeat_at = wall

                log.info(
                    "monitor poll: no live Claude Code sessions — idle at %ds",
                    poll_seconds,
                )
                last_pct = None
                last_poll_time = None
                last_wall_time = None
                consecutive_failures = 0
                if once:
                    return 0
                time.sleep(poll_seconds)
                continue

            # Active path: reset idle-heartbeat tracking.
            idle_started_wall = None
            idle_heartbeat_at = 0.0

            # Sleep/wake recovery: a long wall-clock gap means the laptop
            # slept (monotonic is paused) or the process was stalled.  The
            # old baseline is garbage and a stale ``last_switch_error`` could
            # mask a real new failure as a "repeat".  Reset both.
            wall = time.time()
            if (
                last_wall_time is not None
                and wall - last_wall_time
                > MONITOR_WAKE_GAP_MULTIPLIER * poll_seconds
            ):
                log.info(
                    "monitor: wake-gap %ds detected — resetting baselines",
                    int(wall - last_wall_time),
                )
                last_pct = None
                last_poll_time = None
                last_switch_error = None
                consecutive_failures = 0

            now = time.monotonic()
            pct = switcher.get_active_usage_pct()
            pct_text = "unavailable" if pct is None else f"{pct:.0f}%"

            if pct is None:
                # Phase 3: usage API failed → exponential backoff.  The previous
                # velocity baseline is now stale; drop it so a recovery doesn't
                # compute a misleading delta.
                consecutive_failures += 1
                interval = _failure_backoff_seconds(
                    consecutive_failures, t_max=poll_seconds,
                )
                print(
                    f"  {muted('active usage:')} {pct_text} "
                    f"{muted(f'· backoff {interval}s ({consecutive_failures} consecutive failures)')}",
                    file=out,
                    flush=True,
                )
                log.warning(
                    "monitor poll: active_usage_pct=None failures=%d backoff=%ds",
                    consecutive_failures,
                    interval,
                )
                last_pct = None
                last_poll_time = None
                # Even on failure, record the wall time so wake-gap detection
                # has a valid reference point.  The next successful poll will
                # disambiguate transient failure from sleep+recovery.
                last_wall_time = wall
                if once:
                    return 0
                time.sleep(interval)
                continue

            consecutive_failures = 0

            # Compute the adaptive interval *before* the switch decision so we
            # log a consistent "next poll" value even on threshold iterations.
            elapsed = (now - last_poll_time) if last_poll_time is not None else 0.0
            interval = _next_poll_interval(
                pct, last_pct, elapsed, threshold,
                t_max=poll_seconds,
            )
            print(
                f"  {muted('active usage:')} {pct_text} "
                f"{muted(f'· next poll {interval}s')}",
                file=out,
                flush=True,
            )
            log.info(
                "monitor poll: active_usage_pct=%s threshold=%s next_poll=%ds",
                pct, threshold, interval,
            )

            if should_switch(pct, threshold):
                print(
                    f"  {accent('threshold reached')} {muted('switching account')}",
                    file=out,
                    flush=True,
                )
                log.info(
                    "monitor threshold reached: pct=%s threshold=%s — switching",
                    pct,
                    threshold,
                )
                try:
                    # quiet=True: no user is watching launchd/foreground output,
                    # so suppress interactive banners. force_refresh=True: mint a
                    # fresh OAuth token so Claude Code's first API call after
                    # picking up the new credentials has maximum remaining
                    # lifetime — production-grade seamless handoff.
                    switcher.switch(quiet=True, force_refresh=True)
                except ClaudeSwitchError as exc:
                    print(f"  {dimmed(f'switch failed: {exc}')}", file=out, flush=True)
                    err_msg = str(exc)
                    if err_msg == last_switch_error:
                        # Same failure as last time — likely a permanently
                        # broken slot.  Drop to DEBUG so logs stay readable;
                        # the user already has the actionable message.
                        log.debug(
                            "monitor switch failed (repeat): pct=%s error=%s",
                            pct, exc,
                        )
                    else:
                        log.warning(
                            "monitor switch failed: pct=%s error=%s", pct, exc
                        )
                        last_switch_error = err_msg
                else:
                    log.info("monitor switched account at pct=%s", pct)
                    last_switch_error = None
                # The active account just changed — the previous pct sample
                # belongs to the old account.  Reset baseline so we don't
                # compute a wildly negative velocity on the next iteration.
                last_pct = None
                last_poll_time = None
            else:
                last_pct = pct
                last_poll_time = now

            # Wall-clock timestamp drives the sleep/wake-gap detection above;
            # always update regardless of switch decision.
            last_wall_time = wall

            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Monitor stopped')}", file=out)
        return 130
    except _MonitorStopped:
        print(f"\n{dimmed('Monitor stopped')}", file=out)
        return 143
    finally:
        log.info("monitor stopped")
        signal.signal(signal.SIGTERM, previous_sigterm)
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
