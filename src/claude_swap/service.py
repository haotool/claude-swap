"""macOS launchd supervisor for the auto-switch monitor.

Owns install/uninstall/status of the LaunchAgent that runs ``cswap --monitor``
under launchd. macOS-only for now; launchd specifics live behind ``_launchd_*``
helpers so a systemd backend can be added later without touching CLI wiring.

The service shells out via ``[sys.executable, "-m", "claude_swap", "--monitor"]``
so monitor-loop changes never require edits here.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, bolded, dimmed, muted, warning
from claude_swap.switcher import ClaudeAccountSwitcher

_VERSION_ENV_KEY = "CSWAP_INSTALLED_VERSION"

SERVICE_LABEL = "com.claude-swap.monitor"

# Resolve launchctl at import time rather than trusting PATH. macOS 26 moved
# the binary from /usr/bin to /bin — probe both known Apple system locations.
_LAUNCHCTL = (
    "/bin/launchctl"
    if os.path.exists("/bin/launchctl")
    else "/usr/bin/launchctl"
)

# State paths the supervised monitor must see (same as the user's shell).
_FORWARDED_ENV_KEYS = ("HOME", "USER", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "PATH")


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
    return [sys.executable, "-m", "claude_swap", "--monitor"]


def _passthrough_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _FORWARDED_ENV_KEYS if k in os.environ}
    env[_VERSION_ENV_KEY] = __version__
    return env


def _build_plist(switcher: ClaudeAccountSwitcher) -> dict:
    log_dir = _log_dir(switcher)
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": _program_arguments(),
        "RunAtLoad": True,
        # Dict form is mandatory: ``KeepAlive=True`` would resurrect the agent
        # immediately after ``launchctl bootout``, defeating uninstall.
        "KeepAlive": {"SuccessfulExit": False},
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
    """Invoke launchctl; surface failures as ``ClaudeSwitchError``."""
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
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass
    fd = os.open(
        str(tmp_path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            plistlib.dump(_build_plist(switcher), fh)
        os.replace(tmp_path, plist_path)
        os.chmod(plist_path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _launchctl("bootout", _launchd_service_target(), check=False)
    _launchctl("bootstrap", _launchd_domain(), str(plist_path))
    log_dir = _log_dir(switcher)
    print(f"{bolded('Service installed:')} {muted(SERVICE_LABEL)}")
    print(f"  {dimmed(str(plist_path))}")
    print(
        f"  {dimmed('runs `cswap --monitor` at login; launchd output → ' + str(log_dir))}"
    )
    print(
        f"  {dimmed('structured log → ' + str(switcher.backup_dir / 'claude-swap.log'))}"
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
    if existed:
        log_dir = _log_dir(switcher)
        structured_log = switcher.backup_dir / "claude-swap.log"
        print(f"  {dimmed('launchd output retained → ' + str(log_dir))}")
        print(f"  {dimmed('structured log retained → ' + str(structured_log))}")
    return 0


def _installed_version() -> str | None:
    """Return the cswap version recorded in the installed plist, or ``None``."""
    try:
        with _plist_path().open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("EnvironmentVariables", {}).get(_VERSION_ENV_KEY)


def service_state() -> str:
    """Return ``not installed``, ``installed but not loaded``, or ``loaded``."""
    _require_macos()
    if not _plist_path().exists():
        return "not installed"
    proc = _launchctl("print", _launchd_service_target(), check=False)
    if proc.returncode != 0:
        return "installed but not loaded"
    return "loaded"


def status(switcher: ClaudeAccountSwitcher) -> int:
    """Print a short summary: not installed / installed-but-not-loaded / loaded."""
    _require_macos()
    state = service_state()
    if state == "not installed":
        print(f"{bolded('Service:')} {dimmed('not installed')}")
        return 0

    installed_ver = _installed_version()
    if installed_ver is not None and installed_ver != __version__:
        warning(
            f"Service was installed with cswap {installed_ver}; "
            f"current version is {__version__}. "
            "Run `cswap service install` to restart on the new version."
        )

    if state == "installed but not loaded":
        print(f"{bolded('Service:')} {accent('installed but not loaded')}")
        print(f"  {dimmed('run `cswap service install` to (re)load it')}")
        return 0

    proc = _launchctl("print", _launchd_service_target(), check=False)
    print(f"{bolded('Service:')} {accent('loaded')} {muted(SERVICE_LABEL)}")
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state =", "pid =", "last exit code =")):
            print(f"  {muted(stripped)}")
    print(
        f"  {dimmed('decision log → ' + str(switcher.backup_dir / 'claude-swap.log'))}"
    )
    return 0


def logs(switcher: ClaudeAccountSwitcher, lines: int = 40) -> int:
    """Tail monitor log surfaces: structured log, then launchd stderr/stdout."""
    _require_macos()
    structured = switcher.backup_dir / "claude-swap.log"
    for path, label in [
        (structured, "claude-swap.log (structured)"),
        (_log_dir(switcher) / "monitor.err", "monitor.err (launchd stderr)"),
        (_log_dir(switcher) / "monitor.out", "monitor.out (launchd stdout)"),
    ]:
        print(bolded(f"== {label} =="))
        print(f"  {dimmed(str(path))}")
        if not path.exists():
            print(f"  {dimmed('(none yet)')}")
            continue
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        for line in tail:
            print(f"  {muted(line)}")
    return 0
