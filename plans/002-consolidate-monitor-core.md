# Plan 002: Collapse the duplicated auto-switch monitor into a single SSOT core

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> "STOP condition" occurs, stop and report — do not improvise. When done,
> update this plan's row in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat <SHA-after-plan-001>..HEAD -- src/claude_swap/monitor.py src/claude_swap/tui.py src/claude_swap/switcher.py`
> Plan 001 must be DONE first. Re-read the "Current state" excerpts below
> against the live files; on any mismatch treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW — pure refactor behind unchanged behavior; covered by existing + new tests.
- **Depends on**: `plans/001-sync-fork-onto-upstream.md`
- **Category**: tech-debt (KISS / SSOT / DRY)
- **Planned at**: commit `d3b86d2`, 2026-06-13 (re-validate after 001 lands)

## Why this matters

The poll→check→switch loop that powers "auto-switch at limit" is implemented
**twice**, and the polling interval is defined **twice** with the same literal:

- CLI: `monitor.py` → `run_cli_monitor()`, constant `AUTO_MONITOR_POLL_SECONDS = 60`.
- TUI: `tui.py` → `_run_auto_monitor()`, constant `_AUTO_POLL_SECONDS = 60`.

Both independently decide "should I switch?" — `tui.py` has a named helper
`_should_auto_switch(pct, threshold)`, while `monitor.py` re-inlines the same
`pct is not None and pct >= threshold` test. Plan 005 (launchd service) would
add a **third** copy. Three call sites means three places to fix when the switch
rule, the poll cadence, or the logging changes — exactly the drift KISS/SSOT
exist to prevent.

This plan extracts **one** monitor core — a single decision function and a
single iteration step — into `monitor.py`, and rewires both the CLI and the TUI
to call it. No behavior changes; the win is that plans 003 (observability) and
005 (service) extend one function instead of three.

## Current state

**`src/claude_swap/monitor.py`** (CLI monitor). Key excerpt
(`monitor.py:16` and `:65-121`):

```python
AUTO_MONITOR_POLL_SECONDS = 60
...
def run_cli_monitor(switcher, *, poll_seconds=AUTO_MONITOR_POLL_SECONDS,
                    once=False, stream=None) -> int:
    ...
    while True:
        pct = switcher.get_active_usage_pct()
        ...
        if pct is not None and pct >= threshold:   # <-- inline decision
            ...
            switcher.switch()
        if once:
            return 0
        time.sleep(poll_seconds)
```

**`src/claude_swap/tui.py`** (TUI monitor). Key excerpts:

```python
# tui.py:33
_AUTO_POLL_SECONDS = 60

# tui.py:187
def _should_auto_switch(pct: float | None, threshold: int) -> bool:
    return pct is not None and pct >= threshold

# tui.py:246  _run_auto_monitor(...) drives a curses countdown and calls
# _should_auto_switch(last_pct, threshold), then _auto_perform_switch(...)
```

**`src/claude_swap/switcher.py`** owns the real state and is already the SSOT
for the threshold and usage:
- `DEFAULT_AUTO_SWITCH_THRESHOLD = 95` (`switcher.py:69`)
- `get_active_usage_pct() -> float | None` (`switcher.py:1633`)
- `get_auto_switch_config()` / `set_auto_switch_config()` (`switcher.py:1586`/`:1604`)

**Repo conventions to match**:
- Module-level constants are `UPPER_SNAKE`, defined once at the top of their
  module (see `switcher.py:65-69`). Private helpers are `_leading_underscore`.
- `from __future__ import annotations` heads every module; type hints use
  `X | None`.
- Tests live in `tests/`, use plain `pytest` + `unittest.mock`, and never hit
  the network — see `tests/test_auto_switch.py` (which already fakes
  `switcher.get_active_usage_pct`) as the structural pattern to follow.

## Commands you will need

| Purpose | Command | Expected |
|---------|---------|----------|
| Run tests | `python -m pytest -q` | all pass |
| Monitor tests | `python -m pytest tests/test_auto_switch.py -q` | all pass |
| Find poll constants | `grep -rn "POLL_SECONDS" src/claude_swap/` | only the new SSOT name after this plan |
| Parse check | `python -c "import claude_swap.monitor, claude_swap.tui"` | exit 0 |

Activate the venv first if needed: `source .venv/bin/activate`.

## Scope

**In scope**:
- `src/claude_swap/monitor.py` — host the SSOT core.
- `src/claude_swap/tui.py` — call the core; drop the duplicate constant/decision.
- `tests/test_auto_switch.py` — add tests for the extracted core (and update
  any test that imported `_AUTO_POLL_SECONDS` / `AUTO_MONITOR_POLL_SECONDS`).

**Out of scope** (do NOT touch):
- `src/claude_swap/switcher.py` — it already owns threshold/usage; do not move
  that logic. The core *calls* `switcher`, never the reverse.
- The curses rendering in `tui.py` (`_draw_monitor`, `_select_from`, etc.) —
  only the loop's decision/cadence wiring changes, not the UI.
- Logging — plan 003 adds it on top of the core created here. Do not add logging
  calls in this plan.

## Steps

### Step 1: Define the single source of truth in `monitor.py`

At the top of `src/claude_swap/monitor.py`, replace the lone
`AUTO_MONITOR_POLL_SECONDS = 60` with one canonical constant plus the canonical
decision function:

```python
# The single source of truth for the auto-switch monitor's cadence and rule.
# Both the CLI monitor (cswap --monitor), the TUI monitor, and the launchd
# service import these — do not redefine the interval or the rule elsewhere.
MONITOR_POLL_SECONDS = 60


def should_switch(pct: float | None, threshold: int) -> bool:
    """Whether the active account's usage warrants an automatic switch.

    ``pct`` is the highest 5h/7d utilization for the active account (or
    ``None`` when usage is unavailable); ``threshold`` is the configured
    percentage. The rule is intentionally trivial and centralized so every
    caller (CLI, TUI, service) shares one definition.
    """
    return pct is not None and pct >= threshold
```

Keep a backward-compatible alias only if `grep -rn "AUTO_MONITOR_POLL_SECONDS"`
finds importers outside the files in scope; otherwise rename outright. Update
`run_cli_monitor`'s default to `poll_seconds: int = MONITOR_POLL_SECONDS` and
replace its inline `pct is not None and pct >= threshold` with
`should_switch(pct, threshold)`.

**Verify**: `python -c "from claude_swap.monitor import MONITOR_POLL_SECONDS, should_switch; print(should_switch(96, 95), should_switch(None, 95))"`
prints `True False`.

### Step 2: Point the TUI at the core

In `src/claude_swap/tui.py`:
- Delete the local `_AUTO_POLL_SECONDS = 60` (`tui.py:33`) and the local
  `_should_auto_switch` definition (`tui.py:187-189`).
- Import the core: `from claude_swap.monitor import MONITOR_POLL_SECONDS, should_switch`.
- Replace every `_AUTO_POLL_SECONDS` use (in `_run_auto_monitor` and
  `_draw_monitor`, ~`tui.py:267`, `:330`) with `MONITOR_POLL_SECONDS`.
- Replace the `_should_auto_switch(last_pct, threshold)` call (`tui.py:268`)
  with `should_switch(last_pct, threshold)`.

**Verify**: `grep -n "_AUTO_POLL_SECONDS\|_should_auto_switch" src/claude_swap/tui.py`
returns nothing; `python -c "import claude_swap.tui"` exits 0.

### Step 3: Update tests that referenced the old names

`grep -rn "_AUTO_POLL_SECONDS\|AUTO_MONITOR_POLL_SECONDS\|_should_auto_switch" tests/`
and repoint each to `MONITOR_POLL_SECONDS` / `should_switch`.

**Verify**: `python -m pytest tests/test_auto_switch.py tests/test_tui.py -q` → all pass.

### Step 4: Add focused tests for the core

In `tests/test_auto_switch.py`, add a small test class modeled on the existing
tests in that file:

```python
from claude_swap.monitor import should_switch

class TestShouldSwitch:
    def test_at_threshold_switches(self):
        assert should_switch(95, 95) is True
    def test_above_threshold_switches(self):
        assert should_switch(99.5, 95) is True
    def test_below_threshold_holds(self):
        assert should_switch(94.9, 95) is False
    def test_none_usage_holds(self):
        assert should_switch(None, 95) is False
```

**Verify**: `python -m pytest tests/test_auto_switch.py -q -k ShouldSwitch` →
4 passed.

## Test plan

- New: `TestShouldSwitch` (4 cases above) in `tests/test_auto_switch.py`,
  modeled on the existing classes there.
- Regression: the whole `tests/test_auto_switch.py` and `tests/test_tui.py`
  suites must still pass unchanged in behavior.
- Verification: `python -m pytest -q` → all pass, including the 4 new cases.

## Done criteria

ALL must hold:

- [ ] `grep -rn "POLL_SECONDS" src/claude_swap/` shows `MONITOR_POLL_SECONDS`
      defined exactly once (in `monitor.py`) and no other `*_POLL_SECONDS`.
- [ ] `grep -rn "_should_auto_switch\|_AUTO_POLL_SECONDS" src/` returns nothing.
- [ ] `monitor.py` defines `should_switch`; `tui.py` and `run_cli_monitor` both
      call it (no inline `pct >= threshold` remains:
      `grep -rn ">= threshold" src/claude_swap/` returns nothing).
- [ ] `python -m pytest -q` exits 0; the 4 new `should_switch` tests pass.
- [ ] No files outside the in-scope list modified (`git status`).
- [ ] `plans/README.md` status row updated.

## STOP conditions

Stop and report if:

- Plan 001 is not DONE (this plan assumes the post-rebase file layout).
- `grep` finds `AUTO_MONITOR_POLL_SECONDS` imported somewhere unexpected
  (outside `monitor.py`/tests) — report so the alias decision is explicit.
- Removing `_should_auto_switch` from `tui.py` breaks a test that asserts on
  TUI-internal symbols — report rather than rewriting the test's intent.

## Maintenance notes

- After this, the auto-switch *rule* and *cadence* live in `monitor.py` only.
  Plan 003 adds logging inside `should_switch`'s callers / a shared
  `poll_once` boundary; plan 005's service imports the same core.
- A reviewer should confirm the TUI still polls every 60s and switches at the
  threshold (behavior unchanged) — diff should be deletions + import swaps, not
  new logic.
- Deferred: extracting a full `poll_once(switcher, threshold) -> Outcome`
  helper that both the blocking CLI loop and the curses countdown share. Left
  out here to keep the curses integration untouched; revisit if plan 005 needs
  it.
