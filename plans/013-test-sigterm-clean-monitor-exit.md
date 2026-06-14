# Plan 013: Add regression test for SIGTERM → clean monitor exit (launchd contract)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 331798e..HEAD -- src/claude_swap/monitor.py tests/test_auto_switch.py`
> If either file changed materially around `run_cli_monitor` (signal
> handling) or `TestMonitorPidLifecycle` (the closest existing test class),
> re-read both before adding the new test; on material drift, STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `331798e`, 2026-06-14

## Why this matters

`run_cli_monitor` installs a SIGTERM handler so launchd can stop the
background monitor cleanly. The handler raises a sentinel exception
(`_MonitorStopped`) which is caught and returns exit code 143 (the
conventional 128 + SIGTERM). The `finally` block also restores the
previous SIGTERM handler so the process is left in a clean state.

**No test exercises this path today.** If a refactor accidentally removes
the `signal.signal(signal.SIGTERM, stop_monitor)` call or breaks the
`_MonitorStopped` exception flow, the test suite passes — and launchd's
`launchctl bootout` (which sends SIGTERM) silently degrades to "process
ignores signal" until the agent is force-killed. That's a regression a
single, focused test can prevent.

## Current state

`src/claude_swap/monitor.py:574-709` — relevant excerpts (read the file
to confirm — line numbers may have shifted):

The SIGTERM lifecycle:

```python
previous_sigterm = signal.getsignal(signal.SIGTERM)

def stop_monitor(_signum, _frame) -> None:
    raise _MonitorStopped

signal.signal(signal.SIGTERM, stop_monitor)

state = MonitorRuntimeState()

def perform_switch(decision: AutoSwitchDecisionContext) -> bool:
    return switcher.switch(BackgroundAutoSwitchIntent(decision=decision))

try:
    while True:
        result = monitor_step(...)
        ...
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
```

The pid-lifecycle tests in `tests/test_auto_switch.py` (search for
`TestMonitorPidLifecycle`) are the closest existing scaffolding — read
the class's fixtures and helper patterns and reuse them.

## Commands you will need

| Purpose         | Command                                                                | Expected on success |
|-----------------|------------------------------------------------------------------------|---------------------|
| Drift check     | `git diff --stat 331798e..HEAD -- src/claude_swap/monitor.py tests/test_auto_switch.py` | inspect; no churn near lines 574-709 |
| Read scaffolding| `grep -n "TestMonitorPidLifecycle\|run_cli_monitor" tests/test_auto_switch.py` | locate the existing test class |
| New test        | `python -m pytest -q tests/test_auto_switch.py -k "sigterm"`           | the new test passes |
| Full suite      | `python -m pytest -q`                                                  | 653 passed, 3 skipped |

## Scope

**In scope** (the only files you should modify):

- `tests/test_auto_switch.py` — add one new test, ideally in the
  `TestMonitorPidLifecycle` class (or sibling, if class membership feels
  forced).

**Out of scope** (do NOT touch):

- `src/claude_swap/monitor.py` — production code is correct; this plan
  adds coverage only.
- `tests/conftest.py` — unless a missing fixture genuinely blocks the
  test, which is unlikely (the pid-lifecycle tests already work).
- Any other test file.

## Git workflow

- Branch: stay on `feat/auto-switch-on-limit`.
- Single commit. Suggested message:
  `test(monitor): cover SIGTERM clean-exit contract for launchd`
- Do NOT push.

## Steps

### Step 1: Locate the existing pid-lifecycle test class

```bash
grep -n "class TestMonitorPidLifecycle\|def test_run_cli_monitor" tests/test_auto_switch.py
```

Read the class and its fixtures to understand the mocking pattern
(`switcher`, `temp_home`, any `stub_live_claude` fixture, `once=True` vs
loop tests, how `monitor_step` is patched or invoked).

### Step 2: Add the SIGTERM exit test

Add a new test method to the same class — or as a standalone function if
the class is strict about its scope. Two equivalent patterns work; use
whichever is closer to the existing tests in that class:

**Pattern A — signal-handler invocation (no actual signal)**:

```python
def test_run_cli_monitor_exits_143_on_sigterm(
    self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """SIGTERM (launchd bootout) raises _MonitorStopped → return code 143."""
    switcher = ClaudeAccountSwitcher()
    # Whatever bootstrap the other tests in this class use:
    bootstrap_switchable_accounts(switcher, ...)

    installed: dict[str, object] = {}

    real_signal = signal.signal

    def capture_signal(signum, handler):
        if signum == signal.SIGTERM:
            installed["handler"] = handler
        return real_signal(signum, handler)

    monkeypatch.setattr("claude_swap.monitor.signal.signal", capture_signal)
    # Sleep is a no-op when poll_seconds=0, but ensure we don't actually
    # rely on time passing — fire SIGTERM inline via the captured handler.
    monkeypatch.setattr(
        "claude_swap.monitor.time.sleep",
        lambda _seconds: installed["handler"](signal.SIGTERM, None),
    )

    rc = monitor.run_cli_monitor(switcher, poll_seconds=0, once=False)

    assert rc == 143
    # Handler must be restored after the run.
    assert signal.getsignal(signal.SIGTERM) is not installed["handler"]
```

**Pattern B — direct os.kill on a thread** is heavier and more flaky;
prefer Pattern A unless the existing class consistently uses Pattern B.

Imports to add (top of file or in the new test):

```python
import signal
# `monitor` is likely already imported as `from claude_swap import monitor`;
# verify before adding a duplicate import.
```

### Step 3: Add a sanity sibling test for handler restoration after KeyboardInterrupt

(Optional — only if `Ctrl-C → 130` is also untested. Check:
`grep -n "KeyboardInterrupt\|return 130" tests/test_auto_switch.py`. If
that path *is* covered, skip; if it isn't, mirror the SIGTERM test for
KeyboardInterrupt to lock in the symmetric contract.)

### Step 4: Run the new test then the full suite

```bash
python -m pytest -q tests/test_auto_switch.py -k "sigterm"
python -m pytest -q
```

Expected:

- The new test passes.
- Full suite reports **653 passed, 3 skipped** (one more passing test
  than the baseline 652).

## Test plan

The single new test covers:

- SIGTERM delivery during the monitor loop returns exit code 143.
- The original SIGTERM handler is restored in the `finally` block.

What is *not* covered (acceptable — they're either trivial or already
covered elsewhere):

- The actual OS signal delivery path (mocked).
- The `KeyboardInterrupt → 130` path (covered separately if Step 3
  applies; not strictly required by this plan).

Model after the closest existing test in `TestMonitorPidLifecycle` —
e.g. `test_run_cli_monitor_releases_pid_in_finally` for fixture and
patching shape.

## Done criteria

- [ ] `python -m pytest -q tests/test_auto_switch.py -k "sigterm"` reports
      1 passed, 0 failed.
- [ ] `python -m pytest -q` reports **653 passed, 3 skipped** (one more
      than the 652 baseline).
- [ ] `git diff --stat` shows exactly one file changed:
      `tests/test_auto_switch.py`.
- [ ] No production source file is modified.
- [ ] `plans/README.md` status row for plan 013 updated to DONE.

## STOP conditions

Stop and report if:

- The existing `TestMonitorPidLifecycle` class does not exist (renamed
  or removed) — the test still belongs somewhere, but stop and ask where.
- The monitor's SIGTERM path no longer raises `_MonitorStopped` (e.g. it
  now sets a flag) — the new test must match the new production behavior;
  STOP, report the actual control flow, and re-plan the assertion.
- The test passes without the `signal.signal` patch (meaning the handler
  isn't actually installed) — the test is then asserting nothing useful;
  STOP and dig into why.
- Patching `time.sleep` to fire the handler causes other tests in the
  module to fail due to fixture spillover — scope the patch tightly via
  `monkeypatch` (already shown above) or move the test to its own
  fixture-free function.

## Maintenance notes

- A future contributor changing the monitor's signal handling (e.g.
  adding SIGINT explicitly, or migrating to a flag-based shutdown) must
  update this test. The test is intentionally precise about exit code
  143 and handler restoration; both are stable contracts toward launchd.
- If the codebase later adopts a unified shutdown helper for the
  monitor, fold this test's expectation into that helper's tests rather
  than duplicating.
