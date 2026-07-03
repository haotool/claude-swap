"""Platform-native service supervisor backends."""

from __future__ import annotations

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import Platform
from claude_swap.protocols import ServiceBackend, ServiceState
from claude_swap.protocols import ServiceHost

_UNSUPPORTED_MSG = (
    "cswap service is not supported on this platform yet. "
    "Use `cswap --monitor` in the foreground."
)


class UnsupportedBackend:
    """Placeholder backend for platforms without a supervisor implementation."""

    def _require_supported(self) -> None:
        raise ClaudeSwitchError(_UNSUPPORTED_MSG)

    def install(self, switcher: ServiceHost) -> int:
        self._require_supported()
        return 0

    def uninstall(self, switcher: ServiceHost) -> int:
        self._require_supported()
        return 0

    def state(self) -> ServiceState:
        self._require_supported()
        return "not installed"

    def status(self, switcher: ServiceHost) -> int:
        self._require_supported()
        return 0

    def logs(self, switcher: ServiceHost, lines: int = 40) -> int:
        self._require_supported()
        return 0


def select_backend() -> ServiceBackend:
    """Return the supervisor backend for the current platform."""
    platform = Platform.detect()
    if platform == Platform.MACOS:
        from claude_swap.service_backends.launchd import LaunchdBackend

        return LaunchdBackend()
    if platform in (Platform.LINUX, Platform.WSL):
        from claude_swap.service_backends.systemd import SystemdBackend

        return SystemdBackend()
    if platform == Platform.WINDOWS:
        from claude_swap.service_backends.task_scheduler import TaskSchedulerBackend

        return TaskSchedulerBackend()
    return UnsupportedBackend()
