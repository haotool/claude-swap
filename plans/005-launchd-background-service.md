# Plan 005: Ship a best-practice `cswap service` launchd background monitor (macOS)

> **Executor instructions**: Follow each step, run its verification, and stop on
> any "STOP condition". This adds a new module + a CLI subcommand; keep the
> blast radius to the in-scope files. Update this plan's row in
> `plans/README.md` when done.
>
> **Drift check (run first)**:
> `git diff --stat <SHA-after-plan-003>..HEAD -- src/claude_swap/monitor.py src/claude_swap/cli.py`
> Plans 001, 002, 003 must be DONE — this service wraps the consolidated,
> logged monitor core they produce. If `monitor.py` lacks `run_cli_monitor`
> with structured logging, STOP.

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: MED — installs a persistent LaunchAgent that switches accounts
  unattended; a bad plist could hot-restart or run with the wrong environment.
- **Depends on**: `plans/002-consolidate-monitor-core.md`,
  `plans/003-monitor-observability.md`
- **Category**: feature
- **Planned at**: commit `d3b86d2`, 2026-06-13

## Why this matters

Today the auto-switch monitor is **foreground-only**: `cswap --monitor` (and the
TUI) hold a terminal open to poll usage and rotate accounts. Close the terminal
and protection stops. Users who want "switch me before I hit the limit" need it
to run unattended.

This plan productizes the existing foreground monitor into a managed macOS
background service: `cswap service install|uninstall|status|logs`. `install`
writes a best-practice LaunchAgent that runs the **same** `cswap --monitor` loop
(consolidated in plan 002, logged in plan 003) under `launchd`, which supervises
it (restart on crash, throttled), captures its output to log files, and starts
it at login. The foreground `--monitor` remains as the dev/debug mode. Scope is
macOS launchd only (the user's platform); the module is structured so a Linux
`systemd --user` backend can be added later without touching the CLI wiring.

## Current state

**The loop to wrap already exists.** `cswap --monitor` dispatches to
`monitor.run_cli_monitor(switcher)` (`cli.py:257`), which polls every
`MONITOR_POLL_SECONDS`, switches at the threshold, records a PID file at
`switcher.backup_dir / "auto-switch-monitor.pid"`, handles SIGTERM cleanly
(`monitor.py` `run_cli_monitor` → `_MonitorStopped` on SIGTERM), and (after plan
003) logs every event. launchd sends SIGTERM on `bootout`, which the loop
already handles — so it shuts down cleanly.

**The CLI extends via a pre-dispatch for positional subcommands.** A positional
subcommand can't coexist with `main()`'s required mutually-exclusive flag group,
so upstream handles `cswap run` by intercepting it **before** building the
parser. Match this exact pattern (it is the repo's established style). From
upstream `cli.py`:

```python
def _run_command(argv: list[str]) -> None:
    """Handle `cswap run NUM|EMAIL ...`. Pre-dispatched before the main parser."""
    ...

def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        _run_command(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(...)
```

**Path conventions** (`paths.py`): the backup root is `switcher.backup_dir`;
logs and PID files already live under it. The service's process logs go to
`switcher.backup_dir / "logs" / "monitor.out"` and `monitor.err`; the structured
app log stays at `switcher.backup_dir / "claude-swap.log"` (plan 003).

**Conventions to match**:
- `from __future__ import annotations`; `X | None` hints; `_private` helpers.
- Raise `ClaudeSwitchError` (from `claude_swap.exceptions`) for user-facing
  failures — `cli.py`'s top-level handler turns it into a clean stderr line +
  exit 1 (`cli.py:261`).
- Use the printer helpers (`bolded`, `dimmed`, `muted`, `accent`, `error`) from
  `claude_swap.printer` for output, as `monitor.py` does.
- Shell out with `subprocess.run(..., capture_output=True, text=True)` and check
  return codes explicitly (see `macos_keychain.py` for the disciplined pattern).

## Commands you will need

| Purpose | Command | Expected |
|---------|---------|----------|
| Run tests | `python -m pytest -q` | all pass |
| Service tests | `python -m pytest tests/test_service.py -q` | all pass |
| Import check | `python -c "import claude_swap.service"` | exit 0 |
| Manual smoke (macOS only, optional) | `python -m claude_swap service status` | prints "not installed" cleanly |

launchd reference (the commands the module will run, `<uid>` = `os.getuid()`):
- Modern load: `launchctl bootstrap gui/<uid> <plist_path>`
- Modern unload: `launchctl bootout gui/<uid>/<label>`
- Status: `launchctl print gui/<uid>/<label>` (fall back to `launchctl list <label>`)
- Kick once: `launchctl kickstart -k gui/<uid>/<label>`

## Scope

**In scope** (all new except `cli.py`/`README.md`):
- `src/claude_swap/service.py` (create) — plist generation + launchctl wrapper +
  install/uninstall/status/logs.
- `src/claude_swap/cli.py` — pre-dispatch `cswap service <action>` mirroring the
  `run` pattern; add the subcommand to `--help` epilog.
- `tests/test_service.py` (create) — unit tests with `launchctl` and the
  filesystem mocked.
- `README.md` — document the `service` subcommand.

**Out of scope**:
- `src/claude_swap/monitor.py` — reuse `run_cli_monitor` unchanged; the service
  invokes it via `cswap --monitor`, it does not import internals.
- Linux/systemd or Windows service support — design `service.py` so a future
  backend slots in (keep launchd specifics behind functions named
  `_launchd_*`), but implement only launchd now.
- The mutually-exclusive flag group in `main()` — do NOT add `service` there;
  use the pre-dispatch, exactly like `run`.

## Steps

### Step 1: Create `service.py` — constants, paths, plist builder

Create `src/claude_swap/service.py`:

```python
"""macOS launchd background service for the auto-switch monitor.

`cswap service install` writes a LaunchAgent that runs `cswap --monitor`
(the same foreground loop) under launchd, which supervises and restarts it and
captures its output. macOS only for now; the launchd specifics live behind
`_launchd_*` helpers so a systemd backend can be added later.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, bolded, dimmed, error, muted
from claude_swap.switcher import ClaudeAccountSwitcher

SERVICE_LABEL = "com.claude-swap.monitor"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ClaudeSwitchError(
            "cswap service is currently macOS-only (launchd). "
            "Use `cswap --monitor` in the foreground on this platform."
        )


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _log_dir(switcher: ClaudeAccountSwitcher) -> Path:
    return switcher.backup_dir / "logs"


def _program_arguments() -> list[str]:
    # Use the current interpreter + module entry, not a PATH lookup of `cswap`,
    # so the agent keeps working regardless of the login shell's PATH.
    return [sys.executable, "-m", "claude_swap", "--monitor"]


def _passthrough_env() -> dict[str, str]:
    # Only forward the variables that change where cswap reads state, so the
    # background agent resolves the SAME config/backup paths as the user's shell.
    keys = ("HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "PATH")
    return {k: os.environ[k] for k in keys if k in os.environ}


def _build_plist(switcher: ClaudeAccountSwitcher) -> dict:
    log_dir = _log_dir(switcher)
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": _program_arguments(),
        "RunAtLoad": True,
        # Restart if it crashes, but NOT after a clean exit (e.g. user `bootout`).
        "KeepAlive": {"SuccessfulExit": False},
        # Guard against crash-restart storms.
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(log_dir / "monitor.out"),
        "StandardErrorPath": str(log_dir / "monitor.err"),
        "EnvironmentVariables": _passthrough_env(),
    }
```

**Verify**: `python -c "import claude_swap.service as s; print(s.SERVICE_LABEL)"`
prints `com.claude-swap.monitor`.

### Step 2: Add the launchctl wrapper + install/uninstall

Append to `service.py`:

```python
def _uid() -> int:
    return os.getuid()


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["launchctl", *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise ClaudeSwitchError(
            f"launchctl {' '.join(args)} failed: {proc.stderr.strip()}"
        )
    return proc


def install(switcher: ClaudeAccountSwitcher) -> int:
    _require_macos()
    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    _log_dir(switcher).mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as fh:
        plistlib.dump(_build_plist(switcher), fh)
    # Replace any prior instance: bootout (ignore failure), then bootstrap.
    _launchctl("bootout", f"gui/{_uid()}/{SERVICE_LABEL}", check=False)
    _launchctl("bootstrap", f"gui/{_uid()}", str(plist_path))
    print(f"{bolded('Service installed:')} {muted(SERVICE_LABEL)}")
    print(f"  {dimmed(str(plist_path))}")
    print(f"  {dimmed('runs `cswap --monitor` at login; logs under ' + str(_log_dir(switcher)))}")
    return 0


def uninstall(switcher: ClaudeAccountSwitcher) -> int:
    _require_macos()
    _launchctl("bootout", f"gui/{_uid()}/{SERVICE_LABEL}", check=False)
    plist_path = _plist_path()
    existed = plist_path.exists()
    plist_path.unlink(missing_ok=True)
    msg = "removed" if existed else "was not installed"
    print(f"{bolded('Service ' + msg + ':')} {muted(SERVICE_LABEL)}")
    return 0
```

**Verify**: `python -m pytest tests/test_service.py -q -k install` (after Step 5)
passes. Until then: `python -c "import claude_swap.service"` exits 0.

### Step 3: Add status + logs

Append to `service.py`:

```python
def status(switcher: ClaudeAccountSwitcher) -> int:
    _require_macos()
    if not _plist_path().exists():
        print(f"{bolded('Service:')} {dimmed('not installed')}")
        return 0
    proc = _launchctl("print", f"gui/{_uid()}/{SERVICE_LABEL}", check=False)
    if proc.returncode != 0:
        print(f"{bolded('Service:')} {accent('installed but not loaded')}")
        print(f"  {dimmed('run `cswap service install` to (re)load it')}")
        return 0
    # `launchctl print` is verbose; surface the state + last exit lines.
    print(f"{bolded('Service:')} {accent('loaded')} {muted(SERVICE_LABEL)}")
    for line in proc.stdout.splitlines():
        s = line.strip()
        if s.startswith(("state =", "pid =", "last exit code =")):
            print(f"  {muted(s)}")
    return 0


def logs(switcher: ClaudeAccountSwitcher, lines: int = 40) -> int:
    _require_macos()
    for name in ("monitor.err", "monitor.out"):
        p = _log_dir(switcher) / name
        print(bolded(f"== {name} =="))
        if not p.exists():
            print(f"  {dimmed('(none yet)')}")
            continue
        tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        for line in tail:
            print(f"  {muted(line)}")
    return 0
```

**Verify**: `python -c "import claude_swap.service"` exits 0.

### Step 4: Wire the `cswap service` pre-dispatch in `cli.py`

Mirror the `run` pattern. Add a `_service_command(argv)` function and intercept
it at the top of `main()` (before the parser is built):

```python
def _service_command(argv: list[str]) -> None:
    """Handle `cswap service install|uninstall|status|logs`.

    Pre-dispatched before the main parser (a positional subcommand can't live
    in main()'s required mutually-exclusive group), mirroring `cswap run`.
    """
    parser = argparse.ArgumentParser(
        prog="cswap service",
        description="Manage the macOS launchd background auto-switch monitor.",
    )
    parser.add_argument(
        "action",
        choices=("install", "uninstall", "status", "logs"),
        help="install | uninstall | status | logs",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    from claude_swap.switcher import ClaudeAccountSwitcher
    from claude_swap import service

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        fn = {
            "install": service.install,
            "uninstall": service.uninstall,
            "status": service.status,
            "logs": service.logs,
        }[args.action]
        sys.exit(fn(switcher))
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
```

And at the very top of `main()`, alongside the existing `run` intercept:

```python
def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        _run_command(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "service":
        _service_command(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(...)
```

Add a line to the `main()` epilog examples and the `--help` text:
`%(prog)s service install            # background auto-switch monitor (macOS)`.

**Verify**: `python -m claude_swap service status` prints
`Service: not installed` (on macOS) or a clean macOS-only error (on Linux);
exit code 0 (macOS) / 1 (non-macOS). `python -m pytest tests/test_cli.py -q`
still passes.

### Step 5: Tests

Create `tests/test_service.py`, modeled on `tests/test_auto_switch.py` (fixtures
+ `monkeypatch`). Mock `subprocess.run`/`launchctl` and use `tmp_path` for the
plist and log dirs. Cover:

- `_build_plist` returns a dict with `Label == SERVICE_LABEL`,
  `RunAtLoad is True`, `KeepAlive == {"SuccessfulExit": False}`,
  `ProgramArguments[-1] == "--monitor"`, and Std{Out,Err}Path under the backup
  `logs/` dir.
- `install` writes a parseable plist (`plistlib.load` round-trips it) and calls
  `launchctl bootstrap` (assert the mocked subprocess saw `bootstrap`).
- `uninstall` removes the plist and calls `bootout`.
- `status` with no plist prints "not installed" and returns 0 (mock
  `_plist_path` to a missing `tmp_path` file).
- `_require_macos` raises `ClaudeSwitchError` when `sys.platform != "darwin"`
  (monkeypatch `service.sys.platform`).
- A `cli` test: `sys.argv = ["cswap", "service", "status"]` routes to
  `_service_command` (mock it and assert called with `["status"]`).

To keep tests OS-independent, monkeypatch `service.sys.platform = "darwin"` and
mock `subprocess.run` so no real `launchctl` is invoked.

**Verify**: `python -m pytest tests/test_service.py -q` → all pass.

### Step 6: Document it in the README

Under the "Auto-switch at usage limit (Beta)" section, add a short subsection:

```
### Run it in the background (macOS)

cswap service install      # start at login, supervised by launchd
cswap service status       # is it loaded? last exit?
cswap service logs         # tail recent monitor output
cswap service uninstall    # stop and remove

The service runs the same `cswap --monitor` loop under launchd
(com.claude-swap.monitor), restarting it if it crashes and logging to
<backup_dir>/logs/. `cswap --monitor` remains available for a foreground run.
```

**Verify**: `grep -n "cswap service" README.md` matches.

## Test plan

- New: `tests/test_service.py` per Step 5 (plist shape, install/uninstall
  launchctl calls, status-not-installed, macOS guard, CLI routing). Pattern:
  `tests/test_auto_switch.py` (fixtures + monkeypatch) and `tests/test_cli.py`
  (argv routing).
- Regression: `tests/test_cli.py` — the existing flag dispatch is unaffected by
  the new pre-dispatch (the `run` intercept already proves the pattern is safe).
- Verification: `python -m pytest -q` → all pass.

## Done criteria

ALL must hold:

- [ ] `python -m claude_swap service status` runs cleanly (macOS: "not
      installed", exit 0; non-macOS: clean error, exit 1) with nothing installed.
- [ ] `src/claude_swap/service.py` exists; `cli.py` pre-dispatches `service`
      exactly like `run` (`grep -n '== "service"' src/claude_swap/cli.py` matches).
- [ ] The generated plist round-trips through `plistlib` and has
      `KeepAlive={"SuccessfulExit": False}`, `ThrottleInterval=30`,
      `RunAtLoad=True`, and `ProgramArguments` ending in `--monitor`.
- [ ] `service.py` does NOT import monitor internals — it shells out via
      `cswap --monitor` (`grep -n "run_cli_monitor\|from claude_swap.monitor" src/claude_swap/service.py` returns nothing).
- [ ] `python -m pytest -q` exits 0; `tests/test_service.py` passes.
- [ ] README documents `cswap service`.
- [ ] No files outside scope modified.
- [ ] `plans/README.md` status row updated.

## STOP conditions

Stop and report if:

- `switcher.backup_dir` does not exist as an attribute after plan 001 (the log
  path resolution depends on it) — report the actual accessor.
- `launchctl bootstrap` is unavailable on the target macOS (very old OS) — do
  NOT silently fall back to the deprecated `launchctl load` without flagging it;
  report so the fallback is a reviewed decision.
- Writing the pre-dispatch breaks any existing `tests/test_cli.py` case — report
  rather than altering those tests' expectations.
- You find the monitor enables auto-switch as a side effect of `--monitor` and
  the user has it disabled — report; the service should not silently flip a
  user's opt-in setting (it may need a dedicated non-interactive entry).

## Maintenance notes

- The service is intentionally a thin supervisor over `cswap --monitor`: all
  switch logic stays in the consolidated core (plans 002/003). Future changes to
  polling/switching need no service changes.
- Linux follow-up: add `_systemd_*` helpers + a `--user` unit behind the same
  `install/uninstall/status/logs` API; the CLI pre-dispatch already abstracts
  the platform. That is a separate plan.
- A reviewer should verify: (1) no secrets in the plist or logs; (2) the plist's
  `EnvironmentVariables` forwards `CLAUDE_CONFIG_DIR`/`XDG_DATA_HOME` so the
  agent reads the same accounts as the user's shell; (3) `KeepAlive` does not
  restart after a clean `bootout` (avoids a zombie that refuses to die).
- Known limitation to document if asked: a GUI LaunchAgent only runs while the
  user is logged in (by design — it switches *their* Claude account).
