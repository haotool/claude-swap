"""macOS launchd background service for the auto-switch monitor.

``cswap service install`` writes a LaunchAgent that runs ``cswap --monitor``
(the same foreground loop consolidated in plans 002/003) under launchd, which
supervises and restarts it on crash and captures its output. macOS only for now;
the launchd specifics live behind ``_launchd_*`` helpers so a systemd backend
can be added later without touching the CLI wiring.

The service is intentionally a thin supervisor: it shells out via
``[sys.executable, "-m", "claude_swap", "--monitor"]`` so future changes to the
monitor loop require no changes here.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, bolded, dimmed, muted
from claude_swap.switcher import ClaudeAccountSwitcher

SERVICE_LABEL = "com.claude-swap.monitor"

# Pin the absolute path to Apple's system binary rather than resolving via PATH,
# matching the supply-chain hygiene used for ``/usr/bin/security`` in
# ``macos_keychain.py``: a PATH-injected ``launchctl`` could bootstrap the agent
# against an attacker-controlled label. ``/usr/bin/launchctl`` is present on
# every macOS.
_LAUNCHCTL = "/usr/bin/launchctl"

# HOME, CLAUDE_CONFIG_DIR, XDG_DATA_HOME determine WHERE the supervised
# ``cswap --monitor`` process reads state, so the launchd agent must see
# the same values as the user's shell. PATH is forwarded so the monitor
# child can resolve any subprocesses it spawns under launchd's otherwise-
# empty default PATH (the agent's own ``launchctl`` is pinned absolute).
_FORWARDED_ENV_KEYS = ("HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "PATH")


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
    # so the agent keeps working regardless of the login shell's PATH and
    # survives venv activations that ``cswap`` would otherwise miss.
    return [sys.executable, "-m", "claude_swap", "--monitor"]


def _passthrough_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _FORWARDED_ENV_KEYS if k in os.environ}


def _build_plist(switcher: ClaudeAccountSwitcher) -> dict:
    log_dir = _log_dir(switcher)
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": _program_arguments(),
        "RunAtLoad": True,
        # Restart on crash, but NOT after a clean exit (e.g. user ``bootout``).
        # Dict form is mandatory: ``KeepAlive=True`` would resurrect the agent
        # immediately after ``launchctl bootout``, defeating uninstall.
        "KeepAlive": {"SuccessfulExit": False},
        # Guard against crash-restart storms; pairs with KeepAlive.
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(log_dir / "monitor.out"),
        "StandardErrorPath": str(log_dir / "monitor.err"),
        "EnvironmentVariables": _passthrough_env(),
    }


def _uid() -> int:
    return os.getuid()


def _launchd_domain() -> str:
    return f"gui/{_uid()}"


def _launchd_service_target() -> str:
    return f"{_launchd_domain()}/{SERVICE_LABEL}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Invoke ``/usr/bin/launchctl`` with the given arguments.

    Mirrors the disciplined ``subprocess.run`` shape in ``macos_keychain.py``:
    capture both streams, check the return code explicitly, and surface the
    failure as a ``ClaudeSwitchError`` so the CLI renders it as a clean stderr
    line. ``check=False`` is reserved for idempotent calls where a non-zero
    exit is part of the contract (e.g. ``bootout`` before ``bootstrap``).
    """
    proc = subprocess.run(
        [_LAUNCHCTL, *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise ClaudeSwitchError(
            f"launchctl {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc


def install(switcher: ClaudeAccountSwitcher) -> int:
    """Write the LaunchAgent plist and bootstrap it into the user's GUI domain."""
    _require_macos()
    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    _log_dir(switcher).mkdir(parents=True, exist_ok=True)
    tmp_path = plist_path.with_suffix(plist_path.suffix + ".tmp")
    with tmp_path.open("wb") as fh:
        plistlib.dump(_build_plist(switcher), fh)
    os.replace(tmp_path, plist_path)
    os.chmod(plist_path, 0o600)
    # Replace any prior instance: ``bootout`` is best-effort (not installed yet
    # on first run is normal), then ``bootstrap`` must succeed.
    _launchctl("bootout", _launchd_service_target(), check=False)
    _launchctl("bootstrap", _launchd_domain(), str(plist_path))
    print(f"{bolded('Service installed:')} {muted(SERVICE_LABEL)}")
    print(f"  {dimmed(str(plist_path))}")
    print(
        f"  {dimmed('runs `cswap --monitor` at login; logs under ' + str(_log_dir(switcher)))}"
    )
    return 0


def uninstall(switcher: ClaudeAccountSwitcher) -> int:
    """Bootout the agent and remove the plist. Idempotent."""
    _require_macos()
    _launchctl("bootout", _launchd_service_target(), check=False)
    plist_path = _plist_path()
    existed = plist_path.exists()
    plist_path.unlink(missing_ok=True)
    msg = "removed" if existed else "was not installed"
    print(f"{bolded('Service ' + msg + ':')} {muted(SERVICE_LABEL)}")
    return 0


def status(switcher: ClaudeAccountSwitcher) -> int:
    """Print a short summary: not installed / installed-but-not-loaded / loaded."""
    _require_macos()
    if not _plist_path().exists():
        print(f"{bolded('Service:')} {dimmed('not installed')}")
        return 0
    proc = _launchctl("print", _launchd_service_target(), check=False)
    if proc.returncode != 0:
        print(f"{bolded('Service:')} {accent('installed but not loaded')}")
        print(f"  {dimmed('run `cswap service install` to (re)load it')}")
        return 0
    # ``launchctl print`` is verbose; surface only the state / pid / last exit
    # lines so the output stays scannable.
    print(f"{bolded('Service:')} {accent('loaded')} {muted(SERVICE_LABEL)}")
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state =", "pid =", "last exit code =")):
            print(f"  {muted(stripped)}")
    return 0


def logs(switcher: ClaudeAccountSwitcher, lines: int = 40) -> int:
    """Tail the launchd-captured stderr/stdout files for the agent."""
    _require_macos()
    for name in ("monitor.err", "monitor.out"):
        path = _log_dir(switcher) / name
        print(bolded(f"== {name} =="))
        if not path.exists():
            print(f"  {dimmed('(none yet)')}")
            continue
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        for line in tail:
            print(f"  {muted(line)}")
    return 0
