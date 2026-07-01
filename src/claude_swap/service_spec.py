"""Platform-independent supervisor concerns shared across service backends.

Holds SERVICE_ID/label constants, program argv resolution, env passthrough,
log dir paths, installed-version stamping/drift helpers, and user-facing
messaging helpers. Platform-specific plist/unit/task wiring lives in backends.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from claude_swap import __version__
from claude_swap.models import is_wsl as is_wsl  # SSOT in models; re-export for backends
from claude_swap.printer import accent, bolded, dimmed, muted, warning
from claude_swap.protocols import ServiceHost

VERSION_ENV_KEY = "CSWAP_INSTALLED_VERSION"
# Bound every service-manager call so a hung launchctl/systemctl/schtasks can't
# wedge the CLI or monitor; these are short-lived management commands.
SUBPROCESS_TIMEOUT = 10

SERVICE_LABEL = "com.claude-swap.monitor"
SERVICE_ID = "cswap-monitor"

# State paths the supervised monitor must see (same as the user's shell).
# PATH is intentionally NOT forwarded: ProgramArguments runs an absolute
# ``sys.executable`` and launchd supplies a safe default PATH, so snapshotting
# the install-time shell PATH only risks persisting a poisoned entry.
_FORWARDED_ENV_KEYS = ("HOME", "USER", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME")


def log_dir(switcher: ServiceHost) -> Path:
    """Absolute directory for the supervised monitor's stdout/stderr logs."""
    return switcher.backup_dir / "logs"


def program_arguments() -> list[str]:
    """Return the absolute ``python -m claude_swap --monitor`` argv to supervise."""
    return [sys.executable, "-m", "claude_swap", "--monitor"]


def passthrough_env() -> dict[str, str]:
    """Environment forwarded to the supervised monitor, stamped with the version."""
    env = {k: os.environ[k] for k in _FORWARDED_ENV_KEYS if k in os.environ}
    env[VERSION_ENV_KEY] = __version__
    return env


def installed_version_from_env(env_vars: object) -> str | None:
    """Return the cswap version from a backend env-var map, or ``None``."""
    if not isinstance(env_vars, dict):
        return None
    version = env_vars.get(VERSION_ENV_KEY)
    return version if isinstance(version, str) else None


def warn_version_drift(installed_ver: str | None) -> None:
    """Warn when the installed service version differs from the running cswap."""
    if installed_ver is not None and installed_ver != __version__:
        warning(
            f"Service was installed with cswap {installed_ver}; "
            f"current version is {__version__}. "
            "Run `cswap service install` to restart on the new version."
        )


def print_install_success(
    switcher: ServiceHost,
    *,
    artifact_path: Path,
    run_hint: str,
) -> None:
    """Shared install summary. ``run_hint`` is the backend-specific 'how it
    runs / where output goes' line (launchd file logs vs journal vs Task
    Scheduler).
    """
    print(f"{bolded('Service installed:')} {muted(SERVICE_LABEL)}")
    print(f"  {dimmed(str(artifact_path))}")
    print(f"  {dimmed(run_hint)}")
    print(
        f"  {dimmed('structured log → ' + str(switcher.backup_dir / 'claude-swap.log'))}"
    )


def print_uninstall_result(
    switcher: ServiceHost,
    *,
    existed: bool,
    retained_hint: str,
) -> None:
    """Shared uninstall summary. ``retained_hint`` names the backend-specific
    artifact left behind (launchd logs / journal / task XML).
    """
    msg = "removed" if existed else "was not installed"
    print(f"{bolded('Service ' + msg + ':')} {muted(SERVICE_LABEL)}")
    if existed:
        structured_log = switcher.backup_dir / "claude-swap.log"
        print(f"  {dimmed(retained_hint)}")
        print(f"  {dimmed('structured log retained → ' + str(structured_log))}")


def print_status_not_installed() -> None:
    """Print the status line for a service that is not installed."""
    print(f"{bolded('Service:')} {dimmed('not installed')}")


def print_status_installed_but_not_loaded() -> None:
    """Print the status line for an installed-but-unloaded service."""
    print(f"{bolded('Service:')} {accent('installed but not loaded')}")
    print(f"  {dimmed('run `cswap service install` to (re)load it')}")


def print_status_loaded(*, supervisor_stdout: str) -> None:
    """Print the loaded-service status with the supervisor's key state lines."""
    print(f"{bolded('Service:')} {accent('loaded')} {muted(SERVICE_LABEL)}")
    for line in supervisor_stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state =", "pid =", "last exit code =")):
            print(f"  {muted(stripped)}")


def print_status_decision_log(switcher: ServiceHost) -> None:
    """Print the pointer to the structured auto-switch decision log."""
    print(
        f"  {dimmed('decision log → ' + str(switcher.backup_dir / 'claude-swap.log'))}"
    )
