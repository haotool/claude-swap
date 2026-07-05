"""Data models for Claude Swap."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


_WSL_PROC_PATHS = (Path("/proc/version"), Path("/proc/sys/kernel/osrelease"))


def is_linux() -> bool:
    """Return True on Linux (including WSL).

    Runtime check so mypy keeps every platform branch reachable instead of
    narrowing on ``sys.platform`` under ``--platform`` analysis.
    """
    return sys.platform.startswith("linux")


def is_wsl() -> bool:
    """Return True when running under WSL (Microsoft kernel/userspace).

    Single source of truth for WSL detection: probes the Microsoft marker in
    /proc, falling back to the WSL_DISTRO_NAME env var. Used by both
    ``Platform.detect`` and the systemd service backend.
    """
    if not is_linux():
        return False
    for path in _WSL_PROC_PATHS:
        try:
            if "microsoft" in path.read_text(encoding="utf-8", errors="replace").lower():
                return True
        except OSError:
            continue
    return bool(os.environ.get("WSL_DISTRO_NAME"))


class Platform(Enum):
    """Supported platforms."""

    MACOS = auto()
    LINUX = auto()
    WSL = auto()
    WINDOWS = auto()
    UNKNOWN = auto()

    @classmethod
    def detect(cls) -> Platform:
        """Detect current platform.

        Uses sys.platform rather than platform.system() because the latter
        calls platform.uname() on Windows, which runs a WMI query that can
        hang indefinitely when the WMI service is slow or unresponsive.
        """
        if sys.platform == "darwin":
            return cls.MACOS
        elif sys.platform == "win32":
            return cls.WINDOWS
        elif sys.platform.startswith("linux"):
            if is_wsl():
                return cls.WSL
            return cls.LINUX
        else:
            return cls.UNKNOWN


@dataclass
class AccountInfo:
    """Information about a managed account."""

    email: str
    uuid: str
    organization_uuid: str
    organization_name: str
    added: str
    number: int

    @property
    def is_organization(self) -> bool:
        """Whether this is an organization account."""
        return bool(self.organization_uuid)

    @property
    def display_label(self) -> str:
        """Display label: 'email [OrgName]' or 'email [personal]'."""
        tag = self.organization_name if self.organization_name else "personal"
        return f"{self.email} [{tag}]"

    @classmethod
    def from_dict(cls, number: int, data: dict[str, Any]) -> AccountInfo:
        """Create AccountInfo from dictionary."""
        return cls(
            email=data.get("email", ""),
            uuid=data.get("uuid", ""),
            organization_uuid=data.get("organizationUuid", "") or "",
            organization_name=data.get("organizationName", "") or "",
            added=data.get("added", ""),
            number=number,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "email": self.email,
            "uuid": self.uuid,
            "organizationUuid": self.organization_uuid,
            "organizationName": self.organization_name,
            "added": self.added,
        }


@dataclass
class SwitchTransaction:
    """Represents a switch operation that can be rolled back."""

    original_credentials: str
    original_config: str
    original_account_num: str
    original_email: str
    config_path: Path
    completed_steps: list[str] = field(default_factory=list)

    def record_step(self, step: str) -> None:
        """Record a completed step."""
        self.completed_steps.append(step)

    def rollback(self, switcher: ClaudeAccountSwitcher) -> bool:
        """Rollback all completed steps in reverse order.

        Returns:
            True if rollback successful, False if any step failed.
        """
        success = True
        for step in reversed(self.completed_steps):
            try:
                if step == "credentials_written":
                    switcher._write_credentials(self.original_credentials)
                elif step == "config_written":
                    self.config_path.write_text(
                        self.original_config, encoding="utf-8"
                    )
                    if sys.platform != "win32":
                        os.chmod(self.config_path, 0o600)
                elif step == "sequence_updated":
                    data = switcher._get_sequence_data()
                    if data:
                        data["activeAccountNumber"] = int(self.original_account_num)
                        data["lastUpdated"] = get_timestamp()
                        switcher._write_json(switcher.sequence_file, data)
                switcher._logger.info(f"Rolled back step: {step}")
            except Exception as e:
                switcher._logger.error(f"Failed to rollback step {step}: {e}")
                success = False
        return success


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ManualSwitchIntent:
    """Interactive manual rotation (round-robin)."""

    quiet: ClassVar[bool] = False
    force_refresh: ClassVar[bool] = False


@dataclass(frozen=True)
class CliSwitchIntent:
    """CLI ``--switch`` path (strategy / JSON)."""

    quiet: bool = False
    force_refresh: bool = False


SwitchIntent = ManualSwitchIntent | CliSwitchIntent


class SwitchPreconditionKind(Enum):
    """Outcome of shared switch() / _switch_cli() preamble classification."""

    FRESH_MACHINE = auto()
    UNMANAGED = auto()
    SINGLE_ACCOUNT = auto()
    READY = auto()


@dataclass(frozen=True)
class SwitchPreconditions:
    """Neutral switch preamble snapshot — classification only, no actions."""

    kind: SwitchPreconditionKind
    identity: tuple[str, str] | None = None
    data: dict[str, Any] | None = None
    sequence: list[Any] | None = None
    current_slot: str | None = None
