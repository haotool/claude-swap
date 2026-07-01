"""Cross-platform background-service facade for the auto-switch monitor.

Thin facade over platform-native ``ServiceBackend`` implementations (launchd on
macOS, systemd --user on Linux/WSL, Task Scheduler on Windows), chosen by
``select_backend()``. The public API (``install`` / ``uninstall`` /
``service_state`` / ``status`` / ``logs``) is stable for CLI and TUI callers;
each manager's specifics live in ``service_backends``.

The service shells out via ``[sys.executable, "-m", "claude_swap", "--monitor"]``
so monitor-loop changes never require edits here.
"""

from __future__ import annotations

import subprocess
import sys

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import Platform
from claude_swap.service_backends import select_backend
from claude_swap.service_backends import launchd as _launchd
from claude_swap.service_spec import SERVICE_LABEL, VERSION_ENV_KEY
from claude_swap.protocols import ServiceHost

# Re-exported for tests and launchd runtime hooks (monkeypatch targets).
_LAUNCHCTL = _launchd._LAUNCHCTL
_build_plist = _launchd._build_plist
_plist_path = _launchd._plist_path

# Backward-compatible aliases for tests and callers.
_VERSION_ENV_KEY = VERSION_ENV_KEY

__all__ = [
    "SERVICE_LABEL",
    "_LAUNCHCTL",
    "_VERSION_ENV_KEY",
    "_build_plist",
    "_plist_path",
    "__version__",
    "install",
    "logs",
    "service_state",
    "status",
    "subprocess",
    "sys",
    "uninstall",
]


def _require_supported_platform() -> None:
    platform = Platform.detect()
    if platform in (Platform.MACOS, Platform.LINUX, Platform.WSL, Platform.WINDOWS):
        return
    raise ClaudeSwitchError(
        "cswap service is not supported on this platform yet. "
        "Use `cswap --monitor` in the foreground."
    )


def install(switcher: ServiceHost) -> int:
    """Register the monitor with the platform's per-user service manager."""
    _require_supported_platform()
    return select_backend().install(switcher)


def uninstall(switcher: ServiceHost) -> int:
    """Stop the service and remove its registration. Idempotent."""
    _require_supported_platform()
    return select_backend().uninstall(switcher)


def service_state() -> str:
    """Return ``not installed``, ``installed but not loaded``, or ``loaded``."""
    _require_supported_platform()
    return select_backend().state()


def status(switcher: ServiceHost) -> int:
    """Print a short summary: not installed / installed-but-not-loaded / loaded."""
    _require_supported_platform()
    return select_backend().status(switcher)


def logs(switcher: ServiceHost, lines: int = 40) -> int:
    """Tail monitor log surfaces: structured log, then the backend's stderr/stdout."""
    _require_supported_platform()
    return select_backend().logs(switcher, lines=lines)
