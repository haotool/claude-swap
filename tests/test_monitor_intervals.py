"""Monitor cadence pure functions: adaptive poll intervals, failure backoff,
and server Retry-After handling."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import monitor, oauth
from claude_swap.switcher import ClaudeAccountSwitcher


class TestNextPollInterval:
    """Lock the behaviour contract of the velocity-based interval picker.

    These tests cover the design assumptions that callers (the monitor loop)
    can safely rely on: bounds, idle handling, near-trigger override, and
    the test-friendly t_max=0 degradation.
    """

    def test_returns_zero_when_t_max_zero(self):
        """Test contract: poll_seconds=0 propagates as a 0-second sleep so
        existing once=True fixtures finish in milliseconds."""
        assert monitor._next_poll_interval(50.0, 40.0, 1.0, 95, t_max=0) == 0

    def test_returns_t_max_without_baseline(self):
        """First iteration, no previous sample → no velocity → idle at max."""
        assert (
            monitor._next_poll_interval(50.0, None, 0.0, 95)
            == monitor.MONITOR_POLL_SECONDS
        )

    def test_returns_t_max_when_velocity_zero(self):
        """User idle: same pct, no token consumption — slow poll is correct."""
        out = monitor._next_poll_interval(50.0, 50.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_returns_t_max_when_velocity_negative(self):
        """Post-switch drop: clamps to idle rather than computing negative ETA."""
        out = monitor._next_poll_interval(20.0, 50.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_returns_t_min_at_near_trigger_ratio(self):
        """At ≥ NEAR_TRIGGER_RATIO * threshold, ignore velocity and force the
        floor.  For threshold=95 and ratio=0.95 the override fires at 90.25%.
        """
        out = monitor._next_poll_interval(91.0, 50.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_near_trigger_override_fires_even_when_velocity_zero(self):
        """The override doesn't care about velocity — at the final approach a
        single bursty prompt can blow through the remaining budget."""
        out = monitor._next_poll_interval(94.0, 94.0, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_near_trigger_after_baseline_reset(self):
        """Post-switch baseline reset at high usage must not skip the floor."""
        out = monitor._next_poll_interval(96.0, None, 0.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_predicted_interval_within_bounds(self):
        """Positive velocity: schedule the next poll well before predicted
        threshold crossing.  Result must land inside [MIN, MAX]."""
        out = monitor._next_poll_interval(60.0, 50.0, 60.0, 95, t_max=120)
        assert monitor.MONITOR_POLL_SECONDS_MIN <= out <= 120

    def test_high_velocity_shrinks_to_t_min(self):
        """Pathological velocity (10% in 1s) → ETA tiny → clamped to floor."""
        out = monitor._next_poll_interval(60.0, 50.0, 1.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

    def test_low_velocity_caps_at_t_max(self):
        """Very slow burn → ETA huge → clamped to ceiling."""
        out = monitor._next_poll_interval(50.0, 49.9, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_respects_custom_t_max(self):
        """Test fixtures override t_max via the run_cli_monitor poll_seconds
        kwarg; the picker must honour the smaller ceiling."""
        out = monitor._next_poll_interval(50.0, 49.9, 60.0, 95, t_max=15)
        assert out == 15


class TestNextPollIntervalMulti:
    """Per-window interval picker — the most-urgent window wins."""

    def test_fast_window_not_masked_by_flat_higher_window(self):
        """The 2026-06-24 bug: a flat, higher 7d window must not hide a fast 5h
        climb. The aggregate max would sit at 87% (flat → t_max); per-window the
        rising 5h must drive a short interval."""
        current = {"seven_day": 87.0, "five_hour": 90.0}
        last = {"seven_day": 87.0, "five_hour": 75.0}  # 5h +15 over the gap
        out = monitor._next_poll_interval_multi(current, last, 60.0, 98)
        # The old collapsed max(5h,7d)=87 (flat) would have returned t_max(60).
        assert out < monitor.MONITOR_POLL_SECONDS, (
            f"fast 5h climb must shorten the interval, got {out}"
        )

    def test_matches_single_window_when_one_window(self):
        """With a lone aggregate window it equals the scalar picker."""
        multi = monitor._next_poll_interval_multi(
            {"max": 60.0},
            {"max": 50.0},
            60.0,
            95,
        )
        single = monitor._next_poll_interval(60.0, 50.0, 60.0, 95)
        assert multi == single

    def test_empty_falls_back_to_t_max(self):
        out = monitor._next_poll_interval_multi({}, {}, 60.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS

    def test_near_trigger_in_any_window_forces_floor(self):
        """A 5h in the near-trigger band forces the floor even if 7d is calm."""
        current = {"seven_day": 40.0, "five_hour": 94.0}
        out = monitor._next_poll_interval_multi(current, {}, 0.0, 95)
        assert out == monitor.MONITOR_POLL_SECONDS_MIN

class TestFailureBackoffSeconds:
    def test_zero_failures_returns_min(self):
        assert monitor._failure_backoff_seconds(0) == monitor.MONITOR_POLL_SECONDS_MIN

    def test_first_failure_returns_base(self):
        assert (
            monitor._failure_backoff_seconds(1) == monitor.MONITOR_FAILURE_BACKOFF_BASE
        )

    def test_doubles_each_failure(self):
        # BASE=5 → 5, 10, 20, 40
        assert monitor._failure_backoff_seconds(2) == 10
        assert monitor._failure_backoff_seconds(3) == 20
        assert monitor._failure_backoff_seconds(4) == 40

    def test_clamps_at_t_max(self):
        # n=5 → 80 raw, clamped to MAX=60
        assert monitor._failure_backoff_seconds(5) == monitor.MONITOR_POLL_SECONDS
        # Pathological n=20 stays at MAX, no integer overflow concerns.
        assert monitor._failure_backoff_seconds(20) == monitor.MONITOR_POLL_SECONDS

    def test_returns_zero_when_t_max_zero(self):
        """Test contract: poll_seconds=0 disables sleeps everywhere."""
        assert monitor._failure_backoff_seconds(3, t_max=0) == 0

    def test_respects_custom_t_max(self):
        """A caller-supplied t_max overrides the module default ceiling."""
        assert monitor._failure_backoff_seconds(10, t_max=20) == 20


class TestRetryAfterBackoff:
    """The monitor honours a server Retry-After on rate-limited usage fetches."""

    def _unavailable(self, retry_after, failures=0, poll_seconds=60):
        state = monitor.MonitorRuntimeState()
        state.consecutive_failures = failures
        return monitor._step_usage_unavailable(
            state,
            poll_seconds,
            0.0,
            95,
            "unavailable",
            logging.getLogger("claude-swap"),
            retry_after=retry_after,
        )

    @pytest.mark.parametrize(
        ("retry_after", "failures", "expected"),
        [
            # No server hint → pure exponential backoff (first failure → BASE).
            pytest.param(
                None, 0, monitor.MONITOR_FAILURE_BACKOFF_BASE,
                id="none-falls-back-to-failure-backoff",
            ),
            # Server says wait 120s; that exceeds the failure backoff → honoured.
            pytest.param(120, 0, 120, id="retry-after-overrides-shorter-backoff"),
            # A pathologically long Retry-After is clamped to the ceiling.
            pytest.param(
                99999, 0, monitor.MONITOR_RETRY_AFTER_CAP, id="retry-after-capped",
            ),
            # After many failures the backoff can exceed a tiny Retry-After.
            pytest.param(
                1, 20, monitor.MONITOR_POLL_SECONDS,
                id="failure-backoff-wins-when-larger",
            ),
        ],
    )
    def test_backoff_interval(
        self, retry_after: int | None, failures: int, expected: int
    ):
        result = self._unavailable(retry_after, failures=failures)
        assert result.next_interval == expected

    def test_switcher_reads_retry_after_from_rate_limited_entry(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=(
                    oauth.UsageFetchError(reason="rate_limited", retry_after="90"),
                    "rl",
                ),
            ),
        ):
            assert switcher.get_active_usage_retry_after() == 90

    def test_switcher_retry_after_none_when_not_rate_limited(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=({"five_hour": {"utilization": 10}}, "ok"),
            ),
        ):
            assert switcher.get_active_usage_retry_after() is None

    def test_switcher_reads_retry_after_from_masked_rate_limit_side_field(
        self,
        temp_home: Path,
    ):
        # A trusted prior usage row masks the active 429, but the codec stamped
        # the server Retry-After as a side field — the monitor must still see it,
        # decayed for the 10s that have elapsed since it was stamped.
        switcher = ClaudeAccountSwitcher()
        masked = {
            "five_hour": {"utilization": 80},
            "_cached_at": 1_000.0,
            "_last_rate_limit": {"retry_after": "90", "at": 1_000.0},
        }
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=(masked, "rl"),
            ),
            patch("claude_swap.switcher.time.time", return_value=1_010.0),
        ):
            assert switcher.get_active_usage_retry_after() == 80

    def test_switcher_retry_after_none_when_masked_window_elapsed(
        self,
        temp_home: Path,
    ):
        # Once the server window has fully elapsed, no backoff is reported even
        # though the side field is still present on the cached row.
        switcher = ClaudeAccountSwitcher()
        masked = {
            "five_hour": {"utilization": 80},
            "_cached_at": 1_000.0,
            "_last_rate_limit": {"retry_after": "90", "at": 1_000.0},
        }
        with (
            patch.object(
                switcher,
                "_active_account_slot",
                return_value=("1", "a@example.com"),
            ),
            patch.object(
                switcher,
                "_resolve_active_usage_entry",
                return_value=(masked, "rl"),
            ),
            patch("claude_swap.switcher.time.time", return_value=1_200.0),
        ):
            assert switcher.get_active_usage_retry_after() is None
