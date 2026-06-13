# Plan 003: Give the auto-switch monitor structured, file-based observability

> **Executor instructions**: Follow each step and run its verification before
> proceeding. On any "STOP condition", stop and report. Update this plan's row
> in `plans/README.md` when done.
>
> **Drift check (run first)**:
> `git diff --stat <SHA-after-plan-002>..HEAD -- src/claude_swap/monitor.py src/claude_swap/logging_config.py`
> Plans 001 and 002 must be DONE. If `monitor.py` lacks `should_switch` /
> `MONITOR_POLL_SECONDS` (added by plan 002), STOP — 002 is not in place.

## Status

- **Priority**: P1 (prerequisite for plan 005 — a backgrounded service with no
  logs is undebuggable)
- **Effort**: S–M
- **Risk**: LOW
- **Depends on**: `plans/002-consolidate-monitor-core.md`
- **Category**: observability / dx
- **Planned at**: commit `d3b86d2`, 2026-06-13 (re-validate after 002)

## Why this matters

The monitor decides, on its own, to rotate a user's Claude account. Today it
leaves **no durable trace**: `monitor.py` and `tui.py` only `print()` to the
screen, and never touch the existing logger. When `cswap --monitor` runs in a
terminal that's fine; but plan 005 runs the same loop under launchd with **no
console at all**. Without structured logs, a user whose account silently
switched (or silently failed to switch) at 3am has nothing to inspect.

The repo already has the machinery: `logging_config.setup_logging()` creates a
rotating file logger at `<backup_dir>/claude-swap.log`, and the switcher holds
one at `self._logger`. This plan makes every monitor poll, decision, switch,
and error emit one structured log line through that existing logger — so the
CLI, the TUI, and the service all produce the same auditable trail. It adds
**logging only**; no behavior changes.

## Current state

**The logger already exists and is wired into the switcher.** From
`src/claude_swap/logging_config.py:23`:

```python
def setup_logging(log_dir: Path, debug: bool = False) -> logging.Logger:
    logger = logging.getLogger("claude-swap")
    ...
    file_handler = _LazyDirRotatingFileHandler(log_dir / "claude-swap.log",
        maxBytes=1024*1024, backupCount=3, delay=True)
    ...
    return logger
```

The switcher constructs one and exposes it as `self._logger` (used e.g. at
`switcher.py:1395` for "Usage fetch unavailable", and `switcher.py:1494` for
"Failed to detect running instances"). The log lives under the backup root;
`switcher.backup_dir` is the directory and the monitor already references it
(`monitor.py` `_pid_file` uses `switcher.backup_dir`).

**The monitor currently logs nothing.** `monitor.py` `run_cli_monitor` prints
"active usage", "threshold reached", "switch failed" but never calls a logger.
The TUI's `_run_auto_monitor` / `_auto_perform_switch` likewise only print.

**Convention for log messages** (match `switcher.py`): lazy `%`-formatting,
key=value fields, INFO for normal events, WARNING/ERROR for failures, e.g.:

```python
self._logger.info(
    "Usage fetch unavailable: account=%s email=%s active=%s reason=%s status=%s",
    num, email, is_active, result.reason, result.status_code,
)
```

Reuse `switcher._logger` — do **not** call `setup_logging` again from the
monitor (that would double-register handlers). Access it as
`switcher._logger` (the monitor already reaches into `switcher` for everything
else).

## Commands you will need

| Purpose | Command | Expected |
|---------|---------|----------|
| Run tests | `python -m pytest -q` | all pass |
| Monitor tests | `python -m pytest tests/test_auto_switch.py tests/test_logging_config.py -q` | all pass |
| Inspect a log line in a test | (assert on `caplog.records`, see Test plan) | — |

## Scope

**In scope**:
- `src/claude_swap/monitor.py` — emit structured log lines at the key events.
- `src/claude_swap/tui.py` — emit the same log lines from the TUI monitor path
  (poll result, switch decision, switch outcome).
- `tests/test_auto_switch.py` — assert the log lines are emitted (using
  pytest's `caplog`).

**Out of scope**:
- `src/claude_swap/logging_config.py` — the handler/format is fine as-is; do
  not change rotation or format.
- Adding new log *sinks* (syslog, JSON) — the rotating file is the SSOT sink.
  The launchd plan (005) captures stdout/stderr separately; do not add that here.
- Changing any user-facing `print()` text — keep the screen output identical;
  logs are *in addition to* prints.

## Steps

### Step 1: Add a tiny logger accessor on the monitor

At the top of `monitor.py`, after imports, add a helper so both the CLI loop
and (via import) the TUI use the same logger object without re-initializing it:

```python
def _logger(switcher: ClaudeAccountSwitcher):
    """The shared 'claude-swap' file logger the switcher already configured."""
    return switcher._logger
```

(If `switcher._logger` does not exist after plan 001's rebase, STOP and
report — the attribute name changed upstream.)

**Verify**: `python -c "import claude_swap.monitor"` exits 0.

### Step 2: Log every monitor event in `run_cli_monitor`

In `monitor.py` `run_cli_monitor`, add INFO/WARNING lines alongside the existing
prints (do not remove the prints):

- On start: `log.info("monitor start: threshold=%s poll=%ss pid=%s", threshold, poll_seconds, os.getpid())`
- After each poll: `log.info("monitor poll: active_usage_pct=%s threshold=%s", pct, threshold)` (pct may be `None`)
- On a switch decision (use the plan-002 `should_switch`): before switching,
  `log.info("monitor threshold reached: pct=%s threshold=%s — switching", pct, threshold)`
- On switch success: `log.info("monitor switched account at pct=%s", pct)`
- On switch failure (the `except ClaudeSwitchError`):
  `log.warning("monitor switch failed: pct=%s error=%s", pct, exc)`
- On stop (the `finally`/stop paths): `log.info("monitor stopped")`

Get the logger once at the top of the function: `log = _logger(switcher)`.

**Verify**: `python -m pytest tests/test_auto_switch.py -q` still passes
(behavior unchanged), and Step 4's new assertions (below) pass.

### Step 3: Log the same events from the TUI monitor path

In `tui.py` `_run_auto_monitor` and `_auto_perform_switch`, get
`log = switcher._logger` and emit the matching lines: poll result, "threshold
reached — switching", switch success/failure. Reuse the **same message strings**
as Step 2 so logs are uniform regardless of entry point. Do not alter the curses
drawing.

**Verify**: `python -c "import claude_swap.tui"` exits 0;
`python -m pytest tests/test_tui.py -q` passes.

### Step 4: Test that the events are logged

In `tests/test_auto_switch.py`, model new tests on the existing ones that fake
`switcher.get_active_usage_pct` and `switcher.switch`. Use `caplog` to assert
structured lines are emitted. Example skeleton:

```python
import logging

def test_monitor_logs_poll_and_switch(monkeypatch, caplog, <existing switcher fixture>):
    sw = <fixture>
    monkeypatch.setattr(sw, "get_active_usage_pct", lambda: 96.0)
    switched = {"n": 0}
    monkeypatch.setattr(sw, "switch", lambda: switched.__setitem__("n", switched["n"] + 1))
    with caplog.at_level(logging.INFO, logger="claude-swap"):
        run_cli_monitor(sw, poll_seconds=0, once=True)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("monitor poll" in m and "96" in m for m in msgs)
    assert any("threshold reached" in m for m in msgs)
    assert switched["n"] == 1
```

Add a second test asserting a failed switch logs at WARNING
(`monkeypatch` `sw.switch` to raise `ClaudeSwitchError`).

**Verify**: `python -m pytest tests/test_auto_switch.py -q` → all pass including
the 2 new tests.

## Test plan

- New: 2 tests in `tests/test_auto_switch.py` — (1) poll+switch emits INFO lines
  and performs the switch; (2) a failing switch emits a WARNING line. Pattern:
  the existing auto-switch tests + pytest `caplog`.
- Regression: `tests/test_tui.py`, `tests/test_logging_config.py` unchanged-pass.
- Verification: `python -m pytest -q` → all pass.

## Done criteria

ALL must hold:

- [ ] `grep -n "_logger\|caplog\|\.info(\|\.warning(" src/claude_swap/monitor.py`
      shows the monitor now emits logs.
- [ ] CLI and TUI use identical monitor log message strings
      (`grep -rn "monitor poll\|threshold reached" src/claude_swap/monitor.py src/claude_swap/tui.py` shows both files).
- [ ] User-facing `print()` output is unchanged
      (`git diff` shows additions, no `print(` lines removed/reworded in `monitor.py`/`tui.py`).
- [ ] `python -m pytest -q` exits 0; 2 new logging tests pass.
- [ ] No files outside scope modified.
- [ ] `plans/README.md` status row updated.

## STOP conditions

Stop and report if:

- `switcher._logger` does not exist (upstream renamed it during plan 001) —
  report the actual attribute name.
- Adding `caplog`-based tests reveals the "claude-swap" logger has
  `propagate = False` and `caplog` sees nothing — report; the test may need
  `caplog.set_level(..., logger="claude-swap")` plus attaching to the named
  logger, and you should not silently weaken the assertion.

## Maintenance notes

- The log file is `<switcher.backup_dir>/claude-swap.log`, rotating at 1MB ×3.
  Plan 005 (launchd) additionally redirects the **process** stdout/stderr to
  `<backup_dir>/logs/monitor.{out,err}`; the structured app log here is the
  primary, human-meaningful trail and should stay the place new events are added.
- A reviewer should confirm no secret/credential value is ever logged — only
  account numbers, emails, percentages, and error messages (matching the
  existing `switcher.py` logging discipline).
