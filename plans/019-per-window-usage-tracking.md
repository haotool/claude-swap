# Plan 019: Per-window usage tracking for the auto-switch monitor

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If a "STOP condition" occurs, stop and report — do not improvise.
> When done, update the status row in `plans/README.md` unless a reviewer told
> you they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 72a13eb..HEAD -- src/claude_swap/monitor.py src/claude_swap/switcher.py`
> If these changed since this plan was written, re-read `get_active_usage_pct`,
> `_max_usage_pct`, `should_switch`, and `_next_poll_interval`, and re-confirm
> the excerpts below before proceeding; on a material mismatch, STOP.

## Status

- **Priority**: P1 (correctness of the core feature)
- **Effort**: M
- **Risk**: MED — changes the monitor's trigger/poll signal; guard with the suite
- **Depends on**: none (independent of plan 018 Track C)
- **Category**: bug / production-grade
- **Planned at**: commit `72a13eb`, 2026-06-25

## Problem (evidence)

On 2026-06-24 the monitor reported `active_usage_pct=87.0` for **56
consecutive polls** (~22:00–22:59), then jumped straight to `100.0` at
23:00:36 and switched 2→1 — but Account 1 was already saturated, so it parked
and held until the 23:35 reset. No rate-limit errors occurred that evening
(0 in the log), so this was **not** a fetch failure.

Root cause: the monitor collapses usage to a single number.
`get_active_usage_pct()` → `_max_usage_pct(usage)` returns
`max(five_hour.pct, seven_day.pct)`. The slow-moving 7-day window (~87%,
nearly flat hour-to-hour) **masked** the fast-moving 5-hour window climbing
underneath it. Because the reported series was flat at 87%:

- `_next_poll_interval` saw velocity ≈ 0 → stayed at `t_max` (60s) instead of
  accelerating toward the floor (5s).
- `MONITOR_POLL_NEAR_TRIGGER_RATIO` (0.95 → 93.1% band) never engaged because
  the masked value never entered 93–98 until it was already 100.
- The real 5h crossing happened between two 60s polls, unseen.

The collapse to `max()` is the defect: **per-window velocity is invisible**,
so the adaptive poller and near-trigger guard cannot do their job.

## Current state (excerpts)

- `src/claude_swap/switcher.py` `_max_usage_pct` — collapses to one float over
  `("five_hour", "seven_day")`.
- `src/claude_swap/switcher.py` `get_active_usage_pct() -> float | None` —
  returns that single float from the short-lived (15s TTL) usage cache or a
  live fetch.
- `src/claude_swap/monitor.py` `should_switch(pct, threshold)` and
  `_next_poll_interval(current_pct, last_pct, …)` — both consume the single
  collapsed pct; `MonitorRuntimeState.last_pct` is one float.
- Note: the **target picker** `_slot_switch_score` already evaluates 5h/7d
  separately (per-window saturation + `resets_at`). Only the **active-account
  monitoring path** is blind. Scope this plan to that path; do not rework the
  picker.

## Goal

Make the monitor reason per-window so a fast 5h climb under a high flat 7d is
detected and accelerates polling. Behavior on the collapsed metric must be a
strict superset: anything that triggered before still triggers.

## Approach

Track both windows end to end instead of one `max`.

1. **Expose per-window pct.** Add `get_active_usage_breakdown() ->
   dict[str, float] | None` returning e.g. `{"five_hour": 72.0,
   "seven_day": 87.0}` (omit windows without a usable pct). Keep
   `get_active_usage_pct` as a thin `max(...)` wrapper over it for any
   non-monitor caller / back-compat.

2. **Per-window state.** Replace `MonitorRuntimeState.last_pct: float` with
   `last_pcts: dict[str, float]` (keep a derived `last_pct` only if a test
   needs it; prefer migrating tests). Compute velocity per window.

3. **Trigger on ANY window.** `should_switch` fires when **any** window
   `>= threshold`. Equivalent to today on the `max` (max≥thr ⇔ some window
   ≥thr), so no false negatives — but now the *which-window* is known for
   logging and target selection.

4. **Poll on the SOONEST-to-cross window.** Compute `_next_poll_interval` for
   each window and take the **minimum** interval (most urgent wins). This is
   what fixes the masking: the 5h's positive velocity drives a short interval
   even while 7d is flat and higher.

5. **Log the binding window.** Every poll/switch line names the window and its
   pct (e.g. `5h=72% 7d=87% next_poll=8s trigger_window=5h`). Directly
   addresses the "which window triggered" observability gap from the review.

## STOP conditions

- If exposing the breakdown would require a second network fetch per poll —
  STOP. Reuse the one cached/fetched usage dict already read in
  `get_active_usage_pct`; this must stay one fetch per poll.
- If any existing auto-switch test asserts a *different* trigger outcome (not
  just a changed log string) — STOP and report; a behavior change beyond the
  superset is out of scope.

## Test plan

- Unit: `_next_poll_interval` per window — flat 7d=87% + 5h rising
  60→75→90 must yield a **shrinking** interval (the bug case); pin it.
- Unit: `should_switch` fires when 5h≥thr while 7d<thr, and vice versa, and
  stays false when both <thr.
- Characterization: feed the 2026-06-24 sequence (7d flat 87, 5h ramping into
  100) and assert polling accelerates before the crossing instead of sitting
  at 60s.
- Full suite green; `ruff check src/ --select F` clean.

## Out of scope (separate decisions)

- Lowering the default/`configured` threshold (review item B) — a config
  change, not a code fix; track separately.
- Reworking `_slot_switch_score` / target selection — already per-window.
- Usage-API granularity/lag (review item C) — external; per-window tracking
  mitigates it but cannot remove server-side reporting lag.

## Verification baseline

Pre-work (commit `72a13eb`): `python -m pytest -q` → **661 passed, 3
skipped**; `ruff check src/ --select F` clean.
