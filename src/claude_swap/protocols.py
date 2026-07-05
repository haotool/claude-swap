"""Structural host Protocols for narrow switcher dependencies.

Consumers depend on these views instead of the full ``ClaudeAccountSwitcher`` so
tests can inject minimal fakes and production code stays decoupled from switcher
internals it does not call. Underscore-prefixed members are part of the host
contract on purpose: the leading underscore marks "not a stable public CLI
surface", not "private to one module" — these views are the seam.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Protocol

ServiceState = Literal["not installed", "installed but not loaded", "loaded"]


class RefreshHost(Protocol):
    """Switch surface ``CredentialRefresher`` uses for OAuth read/write paths."""

    lock_file: Path
    credentials_dir: Path
    _logger: logging.Logger

    def _read_credentials(self) -> str | None: ...
    def _read_account_credentials(self, account_num: str, email: str) -> str: ...
    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str,
    ) -> None: ...
    def _live_session_pids(self, account_num: str, email: str) -> list[int]: ...


class ServiceHost(Protocol):
    """Minimal switcher view the service layer needs: just the backup root.

    The supervisor backends only resolve log/artifact paths under ``backup_dir``,
    so they depend on this one-field view instead of the whole switcher.
    """

    backup_dir: Path


class ServiceBackend(Protocol):
    """Platform-native supervisor for the auto-switch engine.

    Methods take a ``ServiceHost`` (not the concrete switcher); the underscore is
    not relevant here — this is the public supervisor contract.
    """

    def install(self, switcher: ServiceHost) -> int: ...

    def uninstall(self, switcher: ServiceHost) -> int: ...

    def state(self) -> ServiceState: ...

    def status(self, switcher: ServiceHost) -> int: ...

    def logs(self, switcher: ServiceHost, lines: int = 40) -> int: ...
